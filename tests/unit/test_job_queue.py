"""Unit tests for durable queue and worker primitives."""

from __future__ import annotations

import asyncio
import threading
from collections import defaultdict
from typing import cast

import pytest

from src.api.job_queue import JobEnvelope, JobQueue, JobQueueError, JSONValue
from src.worker import Worker


class FakeRedis:
    """Small, thread-safe redis-py fake implementing the queue's actual commands."""

    def __init__(self) -> None:
        self.hashes: defaultdict[str, dict[str, str]] = defaultdict(dict)
        self.strings: dict[str, str] = {}
        self.lists: defaultdict[str, list[str]] = defaultdict(list)
        self.sorted_sets: defaultdict[str, dict[str, float]] = defaultdict(dict)
        self.available = True
        self.fail_eval = False
        self._lock = threading.RLock()

    def ping(self) -> bool:
        if not self.available:
            raise OSError("redis unavailable")
        return True

    def eval(self, script: str, numkeys: int, *keys_and_args: str) -> int:
        if self.fail_eval:
            raise OSError("transaction failed")
        keys = keys_and_args[:numkeys]
        args = keys_and_args[numkeys:]
        with self._lock:
            if "-- enqueue" in script:
                known, envelopes, ready = keys
                job_id, encoded, _ttl = args
                if known in self.strings:
                    return 0
                self.strings[known] = "1"
                self.hashes[envelopes][job_id] = encoded
                self.lists[ready].insert(0, job_id)
                return 1
            if "-- ack" in script:
                inflight, leases, envelopes = keys
                job_id = args[0]
                if not self.lrem(inflight, 1, job_id):
                    return 0
                self.zrem(leases, job_id)
                self.hdel(envelopes, job_id)
                return 1
            if "-- retry" in script:
                inflight, leases, envelopes, scheduled = keys
                job_id, encoded, retry_at = args
                if not self.lrem(inflight, 1, job_id):
                    return 0
                self.zrem(leases, job_id)
                self.hashes[envelopes][job_id] = encoded
                self.sorted_sets[scheduled][job_id] = float(retry_at)
                return 1
            if "-- dead" in script or "-- recover" in script:
                inflight, leases, envelopes, destination = keys
                job_id, encoded = args
                if not self.lrem(inflight, 1, job_id):
                    return 0
                self.zrem(leases, job_id)
                self.hashes[envelopes][job_id] = encoded
                self.lists[destination].insert(0, job_id)
                return 1
            if "-- promote" in script:
                scheduled, ready = keys
                job_id = args[0]
                if not self.zrem(scheduled, job_id):
                    return 0
                self.lists[ready].insert(0, job_id)
                return 1
            if "-- renew" in script:
                leases = keys[0]
                job_id, lease_until = args
                if job_id not in self.sorted_sets[leases]:
                    return 0
                self.sorted_sets[leases][job_id] = float(lease_until)
                return 1
            if "-- remove dead" in script:
                dead, envelopes = keys
                job_id = args[0]
                if not self.lrem(dead, 1, job_id):
                    return 0
                self.hdel(envelopes, job_id)
                return 1
        raise AssertionError("unknown script")

    def hsetnx(self, name: str, key: str, value: str) -> int:
        with self._lock:
            if key in self.hashes[name]:
                return 0
            self.hashes[name][key] = value
            return 1

    def hset(self, name: str, key: str, value: str) -> int:
        with self._lock:
            is_new = key not in self.hashes[name]
            self.hashes[name][key] = value
            return int(is_new)

    def hget(self, name: str, key: str) -> str | None:
        return self.hashes[name].get(key)

    def hdel(self, name: str, *keys: str) -> int:
        removed = 0
        for key in keys:
            removed += int(self.hashes[name].pop(key, None) is not None)
        return removed

    def lpush(self, name: str, *values: str) -> int:
        for value in values:
            self.lists[name].insert(0, value)
        return len(self.lists[name])

    def brpoplpush(self, source: str, destination: str, timeout: float) -> str | None:
        del timeout
        with self._lock:
            if not self.lists[source]:
                return None
            value = self.lists[source].pop()
            self.lists[destination].insert(0, value)
            return value

    def lrem(self, name: str, count: int, value: str) -> int:
        assert count == 1
        try:
            self.lists[name].remove(value)
        except ValueError:
            return 0
        return 1

    def lrange(self, name: str, start: int, end: int) -> list[str | bytes]:
        values = self.lists[name]
        return cast(list[str | bytes], values[start:] if end == -1 else values[start : end + 1])

    def llen(self, name: str) -> int:
        return len(self.lists[name])

    def zadd(self, name: str, mapping: dict[str, float]) -> int:
        added = sum(key not in self.sorted_sets[name] for key in mapping)
        self.sorted_sets[name].update(mapping)
        return added

    def zrem(self, name: str, *values: str) -> int:
        removed = 0
        for value in values:
            removed += int(self.sorted_sets[name].pop(value, None) is not None)
        return removed

    def zrangebyscore(
        self, name: str, minimum: float | str, maximum: float | str
    ) -> list[str | bytes]:
        low = float("-inf") if minimum == "-inf" else float(minimum)
        high = float("inf") if maximum == "+inf" else float(maximum)
        return [
            key
            for key, score in sorted(self.sorted_sets[name].items(), key=lambda item: item[1])
            if low <= score <= high
        ]

    def zscore(self, name: str, value: str) -> float | None:
        return self.sorted_sets[name].get(value)

    def zcard(self, name: str) -> int:
        return len(self.sorted_sets[name])


class Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.mark.parametrize("use_redis", [False, True])
def test_happy_path_and_duplicate_enqueue(use_redis: bool) -> None:
    fake = FakeRedis() if use_redis else None
    queue = JobQueue(redis_client=fake, lease_seconds=10)

    assert queue.backend == ("redis" if use_redis else "memory")
    assert queue.healthy is True
    assert queue.enqueue(
        "job-1",
        {"ticker": "MSFT", "options": [1, True, None]},
        principal_id="principal-1",
        tenant_id="tenant-1",
    )
    assert not queue.enqueue(
        "job-1", {"ticker": "OTHER"}, principal_id="principal-1", tenant_id="tenant-1"
    )

    envelope = queue.claim(0.01)
    assert envelope is not None
    assert envelope.job_id == "job-1"
    assert envelope.request["ticker"] == "MSFT"
    assert envelope.attempt == 1
    assert queue.renew(envelope)
    assert queue.ack(envelope)
    assert not queue.renew(envelope)
    assert queue.claim(0.01) is None
    # Idempotency survives acknowledgement, rather than only deduplicating queued work.
    assert not queue.enqueue(
        "job-1", {"ticker": "MSFT"}, principal_id="principal-1", tenant_id="tenant-1"
    )


def test_retry_backoff_and_dead_letter() -> None:
    clock = Clock()
    queue = JobQueue(
        redis_client=FakeRedis(),
        max_attempts=2,
        lease_seconds=10,
        retry_backoff_seconds=5,
        clock=clock,
    )
    queue.enqueue("job-2", {}, principal_id="p", tenant_id="t")

    first = cast(JobEnvelope, queue.claim(0.01))
    assert queue.retry(first, "temporary") == "scheduled"
    assert queue.claim(0.01) is None
    clock.advance(5)
    second = cast(JobEnvelope, queue.claim(0.01))
    assert second.attempt == 2
    assert second.retry_at is None
    assert second.last_error == "temporary"
    assert queue.retry(second, "permanent") == "dead_letter"
    assert queue.dead_letter_count == 1
    assert queue.dead_letters()[0].last_error == "permanent"
    assert queue.remove_dead_letter(queue.dead_letters()[0])
    assert queue.dead_letter_count == 0


def test_memory_idempotency_marker_expires_with_retention() -> None:
    clock = Clock()
    queue = JobQueue(clock=clock, idempotency_ttl_seconds=10)
    assert queue.enqueue("reusable", {}, principal_id="p", tenant_id="t")
    envelope = cast(JobEnvelope, queue.claim(0.01))
    assert queue.ack(envelope)
    assert not queue.enqueue("reusable", {}, principal_id="p", tenant_id="t")
    clock.advance(11)
    assert queue.enqueue("reusable", {}, principal_id="p", tenant_id="t")


def test_stale_lease_recovery_including_interrupted_lease_setup() -> None:
    clock = Clock()
    fake = FakeRedis()
    queue = JobQueue(redis_client=fake, max_attempts=3, lease_seconds=10, clock=clock)
    queue.enqueue("stale", {"x": 1}, principal_id="p", tenant_id="t")

    first = cast(JobEnvelope, queue.claim(0.01))
    clock.advance(11)
    assert queue.recover_stale() == 1
    second = cast(JobEnvelope, queue.claim(0.01))
    assert second.attempt == first.attempt + 1
    assert second.last_error == "lease expired"

    # A process can die after the atomic list move but before adding its lease.
    fake.zrem("research_jobs:leases", second.job_id)
    assert queue.recover_stale() == 1
    third = cast(JobEnvelope, queue.claim(0.01))
    assert third.attempt == 3
    clock.advance(11)
    assert queue.recover_stale() == 1
    assert queue.dead_letter_count == 1


def test_required_redis_fails_closed_and_operations_propagate() -> None:
    unavailable = FakeRedis()
    unavailable.available = False
    with pytest.raises(JobQueueError, match="Required Redis"):
        JobQueue(redis_client=unavailable, redis_required=True)

    optional = JobQueue(redis_client=unavailable)
    assert optional.backend == "memory"

    failing = FakeRedis()
    queue = JobQueue(redis_client=failing)
    failing.fail_eval = True
    with pytest.raises(JobQueueError, match="enqueue"):
        queue.enqueue("job", {}, principal_id="p", tenant_id="t")
    failing.fail_eval = False
    failing.available = False
    with pytest.raises(JobQueueError, match="health"):
        _ = queue.healthy


@pytest.mark.asyncio
async def test_worker_acks_success_and_retries_then_propagates_failure() -> None:
    queue = JobQueue(retry_backoff_seconds=0)
    queue.enqueue("ok", {}, principal_id="p", tenant_id="t")
    processed: list[str] = []

    async def succeed(envelope: JobEnvelope) -> None:
        processed.append(envelope.job_id)

    assert await Worker(queue, succeed, poll_timeout=0.01).run_once()
    assert processed == ["ok"]

    queue.enqueue("fail", {}, principal_id="p", tenant_id="t")

    async def fail(_envelope: JobEnvelope) -> None:
        raise LookupError("processor failed")

    with pytest.raises(LookupError, match="processor failed"):
        await Worker(queue, fail, poll_timeout=0.01).run_once()
    retried = queue.claim(0.01)
    assert retried is not None
    assert retried.attempt == 2


@pytest.mark.asyncio
async def test_worker_renews_lease_during_long_processing() -> None:
    queue = JobQueue(lease_seconds=0.03)
    queue.enqueue("long", {}, principal_id="p", tenant_id="t")

    async def process(_envelope: JobEnvelope) -> None:
        await asyncio.sleep(0.08)

    worker = Worker(queue, process, poll_timeout=0.01)
    running = asyncio.create_task(worker.run_once())
    await asyncio.sleep(0.05)
    assert queue.recover_stale() == 0
    assert await running


def test_non_json_request_is_rejected() -> None:
    queue = JobQueue()
    with pytest.raises(ValueError, match="JSON-serializable"):
        queue.enqueue(
            "bad",
            cast(dict[str, JSONValue], {"not_json": object()}),
            principal_id="p",
            tenant_id="t",
        )


def test_burst_delivery_has_no_loss_or_duplicates() -> None:
    queue = JobQueue(lease_seconds=10)
    expected = {f"burst-{index}" for index in range(200)}
    for job_id in expected:
        assert queue.enqueue(job_id, {"job_id": job_id}, principal_id="p", tenant_id="t")

    delivered: set[str] = set()
    while envelope := queue.claim(0.001):
        assert envelope.job_id not in delivered
        delivered.add(envelope.job_id)
        assert queue.ack(envelope)
    assert delivered == expected
