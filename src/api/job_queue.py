"""Durable at-least-once job queue primitives."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from typing import Protocol, TypeAlias, cast

from src.config.settings import Settings

logger = logging.getLogger(__name__)

try:
    import redis as _redis_lib
except ImportError:  # pragma: no cover - depends on the deployment environment
    _redis_lib = None  # type: ignore[assignment]

JSONScalar: TypeAlias = str | int | float | bool | None  # noqa: UP040
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]  # noqa: UP040

_ENQUEUE_SCRIPT = """
-- enqueue
if not redis.call('SET', KEYS[1], '1', 'NX', 'EX', ARGV[3]) then return 0 end
redis.call('HSET', KEYS[2], ARGV[1], ARGV[2])
redis.call('LPUSH', KEYS[3], ARGV[1])
return 1
"""
_ACK_SCRIPT = """
-- ack
if redis.call('LREM', KEYS[1], 1, ARGV[1]) == 0 then return 0 end
redis.call('ZREM', KEYS[2], ARGV[1])
redis.call('HDEL', KEYS[3], ARGV[1])
return 1
"""
_RETRY_SCRIPT = """
-- retry
if redis.call('LREM', KEYS[1], 1, ARGV[1]) == 0 then return 0 end
redis.call('ZREM', KEYS[2], ARGV[1])
redis.call('HSET', KEYS[3], ARGV[1], ARGV[2])
redis.call('ZADD', KEYS[4], ARGV[3], ARGV[1])
return 1
"""
_DEAD_SCRIPT = """
-- dead
if redis.call('LREM', KEYS[1], 1, ARGV[1]) == 0 then return 0 end
redis.call('ZREM', KEYS[2], ARGV[1])
redis.call('HSET', KEYS[3], ARGV[1], ARGV[2])
redis.call('LPUSH', KEYS[4], ARGV[1])
return 1
"""
_RECOVER_SCRIPT = """
-- recover
if redis.call('LREM', KEYS[1], 1, ARGV[1]) == 0 then return 0 end
redis.call('ZREM', KEYS[2], ARGV[1])
redis.call('HSET', KEYS[3], ARGV[1], ARGV[2])
redis.call('LPUSH', KEYS[4], ARGV[1])
return 1
"""
_PROMOTE_SCRIPT = """
-- promote
if redis.call('ZREM', KEYS[1], ARGV[1]) == 0 then return 0 end
redis.call('LPUSH', KEYS[2], ARGV[1])
return 1
"""
_RENEW_SCRIPT = """
-- renew
if not redis.call('ZSCORE', KEYS[1], ARGV[1]) then return 0 end
redis.call('ZADD', KEYS[1], ARGV[2], ARGV[1])
return 1
"""
_REMOVE_DEAD_SCRIPT = """
-- remove dead
if redis.call('LREM', KEYS[1], 1, ARGV[1]) == 0 then return 0 end
redis.call('HDEL', KEYS[2], ARGV[1])
return 1
"""


class RedisClient(Protocol):
    """Subset of redis-py used by :class:`JobQueue`."""

    def ping(self) -> object:
        raise NotImplementedError

    def eval(self, script: str, numkeys: int, *keys_and_args: str) -> object:
        raise NotImplementedError

    def hsetnx(self, name: str, key: str, value: str) -> int:
        raise NotImplementedError

    def hset(self, name: str, key: str, value: str) -> int:
        raise NotImplementedError

    def hget(self, name: str, key: str) -> str | bytes | None:
        raise NotImplementedError

    def hdel(self, name: str, *keys: str) -> int:
        raise NotImplementedError

    def lpush(self, name: str, *values: str) -> int:
        raise NotImplementedError

    def brpoplpush(self, source: str, destination: str, timeout: float) -> str | bytes | None:
        raise NotImplementedError

    def lrem(self, name: str, count: int, value: str) -> int:
        raise NotImplementedError

    def lrange(self, name: str, start: int, end: int) -> list[str | bytes]:
        raise NotImplementedError

    def llen(self, name: str) -> int:
        raise NotImplementedError

    def zadd(self, name: str, mapping: dict[str, float]) -> int:
        raise NotImplementedError

    def zrem(self, name: str, *values: str) -> int:
        raise NotImplementedError

    def zrangebyscore(
        self, name: str, minimum: float | str, maximum: float | str
    ) -> list[str | bytes]:
        raise NotImplementedError

    def zscore(self, name: str, value: str) -> float | None:
        raise NotImplementedError

    def zcard(self, name: str) -> int:
        raise NotImplementedError


class JobQueueError(RuntimeError):
    """Raised when a queue backend operation cannot be completed safely."""


@dataclass(frozen=True, slots=True)
class JobEnvelope:
    """JSON-serializable unit of work and its delivery metadata."""

    job_id: str
    request: dict[str, JSONValue]
    principal_id: str
    tenant_id: str
    attempt: int
    enqueued_at: float
    retry_at: float | None = None
    last_error: str | None = None

    def to_dict(self) -> dict[str, JSONValue]:
        return cast(dict[str, JSONValue], asdict(self))

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_json(cls, value: str | bytes) -> JobEnvelope:
        raw = json.loads(value)
        if not isinstance(raw, dict):
            raise ValueError("job envelope must be a JSON object")
        request = raw.get("request")
        if not isinstance(request, dict):
            raise ValueError("job envelope request must be a JSON object")
        return cls(
            job_id=str(raw["job_id"]),
            request=cast(dict[str, JSONValue], request),
            principal_id=str(raw["principal_id"]),
            tenant_id=str(raw["tenant_id"]),
            attempt=int(raw["attempt"]),
            enqueued_at=float(raw["enqueued_at"]),
            retry_at=float(raw["retry_at"]) if raw.get("retry_at") is not None else None,
            last_error=str(raw["last_error"]) if raw.get("last_error") is not None else None,
        )


class JobQueue:
    """Redis queue with leases, retries, dead letters, and a local-memory fallback."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        redis_url: str | None = None,
        redis_required: bool | None = None,
        redis_client: RedisClient | None = None,
        namespace: str = "research_jobs",
        max_attempts: int | None = None,
        lease_seconds: float | None = None,
        retry_backoff_seconds: float = 1.0,
        idempotency_ttl_seconds: int | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", namespace):
            raise ValueError("namespace must be a lowercase Redis-safe identifier")
        self._max_attempts = (
            max_attempts
            if max_attempts is not None
            else (settings.job_max_attempts if settings else 3)
        )
        self._lease_seconds = (
            lease_seconds
            if lease_seconds is not None
            else (settings.job_lease_seconds if settings else 300)
        )
        if self._max_attempts < 1 or self._lease_seconds <= 0 or retry_backoff_seconds < 0:
            raise ValueError("max_attempts and lease_seconds must be positive")
        self._retry_backoff = retry_backoff_seconds
        self._idempotency_ttl = idempotency_ttl_seconds or (
            settings.job_retention_seconds if settings else 2592000
        )
        if self._idempotency_ttl < 1:
            raise ValueError("idempotency_ttl_seconds must be positive")
        self._clock = clock
        self._redis: RedisClient | None = None
        self._prefix = namespace
        self._ready_key = f"{namespace}:ready"
        self._inflight_key = f"{namespace}:inflight"
        self._envelopes_key = f"{namespace}:envelopes"
        self._known_key = f"{namespace}:known"
        self._leases_key = f"{namespace}:leases"
        self._scheduled_key = f"{namespace}:scheduled"
        self._dead_key = f"{namespace}:dead"

        self._condition = threading.Condition(threading.RLock())
        self._memory_envelopes: dict[str, JobEnvelope] = {}
        self._memory_known: dict[str, float] = {}
        self._memory_ready: deque[str] = deque()
        self._memory_inflight: dict[str, float] = {}
        self._memory_scheduled: dict[str, float] = {}
        self._memory_dead: deque[str] = deque()

        required = (
            redis_required
            if redis_required is not None
            else bool(settings and settings.redis_required)
        )
        configured_url = (
            redis_url if redis_url is not None else (settings.redis_url if settings else None)
        )
        try:
            client = redis_client
            if client is None and configured_url is not None:
                if _redis_lib is None:
                    raise JobQueueError("Redis client library is unavailable")
                client = cast(
                    RedisClient,
                    _redis_lib.from_url(  # type: ignore[no-untyped-call]
                        configured_url,
                        decode_responses=True,
                        socket_connect_timeout=1,
                        socket_timeout=max(self._lease_seconds, 1),
                    ),
                )
            if client is not None:
                client.ping()
                self._redis = client
        except Exception as exc:
            if required:
                raise JobQueueError("Required Redis queue is unavailable") from exc
            logger.warning("Redis queue unavailable; using in-memory development backend: %s", exc)
        if required and self._redis is None:
            raise JobQueueError("Required Redis queue is unavailable")

    @property
    def backend(self) -> str:
        return "redis" if self._redis is not None else "memory"

    @property
    def lease_seconds(self) -> float:
        return self._lease_seconds

    @property
    def healthy(self) -> bool:
        """Return backend health; backend errors are intentionally not hidden."""
        if self._redis is None:
            return True
        try:
            return bool(self._redis.ping())
        except Exception as exc:
            raise JobQueueError("Redis queue health check failed") from exc

    def enqueue(
        self,
        job_id: str,
        request: dict[str, JSONValue],
        *,
        principal_id: str,
        tenant_id: str,
    ) -> bool:
        """Enqueue once. Return ``False`` when ``job_id`` was seen previously."""
        if not job_id or not principal_id or not tenant_id:
            raise ValueError("job_id, principal_id, and tenant_id are required")
        envelope = JobEnvelope(job_id, request, principal_id, tenant_id, 0, self._clock())
        try:
            encoded = envelope.to_json()
        except (TypeError, ValueError) as exc:
            raise ValueError("request must be JSON-serializable") from exc
        if self._redis is None:
            with self._condition:
                known_until = self._memory_known.get(job_id, 0)
                if known_until > self._clock():
                    return False
                self._memory_known[job_id] = self._clock() + self._idempotency_ttl
                self._memory_envelopes[job_id] = envelope
                self._memory_ready.append(job_id)
                self._condition.notify()
                return True
        try:
            result = self._redis.eval(
                _ENQUEUE_SCRIPT,
                3,
                f"{self._known_key}:{job_id}",
                self._envelopes_key,
                self._ready_key,
                job_id,
                encoded,
                str(self._idempotency_ttl),
            )
            return bool(result)
        except Exception as exc:
            raise JobQueueError(f"Unable to enqueue job {job_id}") from exc

    def claim(self, timeout: float = 1.0) -> JobEnvelope | None:
        """Claim a job for at most ``timeout`` seconds and establish its lease."""
        if timeout <= 0:
            raise ValueError("timeout must be positive and bounded")
        self.recover_stale()
        self._promote_due()
        if self._redis is None:
            deadline = time.monotonic() + timeout
            with self._condition:
                while not self._memory_ready:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    self._condition.wait(remaining)
                    self._promote_due_memory()
                job_id = self._memory_ready.popleft()
                previous = self._memory_envelopes[job_id]
                envelope = replace(
                    previous,
                    attempt=previous.attempt + 1,
                )
                self._memory_envelopes[job_id] = envelope
                self._memory_inflight[job_id] = self._clock() + self._lease_seconds
                return envelope
        try:
            raw_id = self._redis.brpoplpush(self._ready_key, self._inflight_key, timeout)
            if raw_id is None:
                return None
            job_id = self._text(raw_id)
            raw = self._redis.hget(self._envelopes_key, job_id)
            if raw is None:
                self._redis.lrem(self._inflight_key, 1, job_id)
                raise JobQueueError(f"Queue contains missing envelope for {job_id}")
            envelope = JobEnvelope.from_json(raw)
            envelope = replace(envelope, attempt=envelope.attempt + 1, retry_at=None)
            self._redis.hset(self._envelopes_key, job_id, envelope.to_json())
            self._redis.zadd(self._leases_key, {job_id: self._clock() + self._lease_seconds})
            return envelope
        except JobQueueError:
            raise
        except Exception as exc:
            raise JobQueueError("Unable to claim a job") from exc

    def ack(self, envelope: JobEnvelope) -> bool:
        """Acknowledge successful processing and remove the live envelope."""
        job_id = envelope.job_id
        if self._redis is None:
            with self._condition:
                if job_id not in self._memory_inflight:
                    return False
                del self._memory_inflight[job_id]
                self._memory_envelopes.pop(job_id, None)
                return True
        try:
            return bool(
                self._redis.eval(
                    _ACK_SCRIPT,
                    3,
                    self._inflight_key,
                    self._leases_key,
                    self._envelopes_key,
                    job_id,
                )
            )
        except Exception as exc:
            raise JobQueueError(f"Unable to acknowledge job {job_id}") from exc

    def renew(self, envelope: JobEnvelope) -> bool:
        """Extend an active lease while its worker is still processing."""
        job_id = envelope.job_id
        lease_until = self._clock() + self._lease_seconds
        if self._redis is None:
            with self._condition:
                if job_id not in self._memory_inflight:
                    return False
                self._memory_inflight[job_id] = lease_until
                return True
        try:
            return bool(
                self._redis.eval(
                    _RENEW_SCRIPT,
                    1,
                    self._leases_key,
                    job_id,
                    str(lease_until),
                )
            )
        except Exception as exc:
            raise JobQueueError(f"Unable to renew lease for job {job_id}") from exc

    def retry(self, envelope: JobEnvelope, error: str) -> str:
        """Schedule retry with exponential backoff, or dead-letter at the cap."""
        job_id = envelope.job_id
        if envelope.attempt >= self._max_attempts:
            self._dead_letter(envelope, error)
            return "dead_letter"
        retry_at = self._clock() + self._retry_backoff * (2 ** max(envelope.attempt - 1, 0))
        updated = replace(envelope, retry_at=retry_at, last_error=error)
        if self._redis is None:
            with self._condition:
                if job_id not in self._memory_inflight:
                    raise JobQueueError(f"Job {job_id} is not inflight")
                del self._memory_inflight[job_id]
                self._memory_envelopes[job_id] = updated
                self._memory_scheduled[job_id] = retry_at
                self._condition.notify()
                return "scheduled"
        try:
            result = self._redis.eval(
                _RETRY_SCRIPT,
                4,
                self._inflight_key,
                self._leases_key,
                self._envelopes_key,
                self._scheduled_key,
                job_id,
                updated.to_json(),
                str(retry_at),
            )
            if not result:
                raise JobQueueError(f"Job {job_id} is not inflight")
            return "scheduled"
        except JobQueueError:
            raise
        except Exception as exc:
            raise JobQueueError(f"Unable to retry job {job_id}") from exc

    def recover_stale(self) -> int:
        """Return expired (and interrupted lease setup) claims to retry/dead-letter."""
        if self._redis is None:
            with self._condition:
                stale = [
                    job_id
                    for job_id, lease_until in self._memory_inflight.items()
                    if lease_until <= self._clock()
                ]
                for job_id in stale:
                    self._recover_one_memory(job_id)
                return len(stale)
        try:
            expired = {
                self._text(value)
                for value in self._redis.zrangebyscore(self._leases_key, "-inf", self._clock())
            }
            for value in self._redis.lrange(self._inflight_key, 0, -1):
                job_id = self._text(value)
                if self._redis.zscore(self._leases_key, job_id) is None:
                    expired.add(job_id)
            recovered = 0
            for job_id in expired:
                raw = self._redis.hget(self._envelopes_key, job_id)
                if raw is not None and self._recover_one_redis(JobEnvelope.from_json(raw)):
                    recovered += 1
            return recovered
        except Exception as exc:
            raise JobQueueError("Unable to recover stale leases") from exc

    def dead_letters(self) -> list[JobEnvelope]:
        if self._redis is None:
            with self._condition:
                return [self._memory_envelopes[job_id] for job_id in self._memory_dead]
        try:
            result: list[JobEnvelope] = []
            for value in self._redis.lrange(self._dead_key, 0, -1):
                raw = self._redis.hget(self._envelopes_key, self._text(value))
                if raw is not None:
                    result.append(JobEnvelope.from_json(raw))
            return result
        except Exception as exc:
            raise JobQueueError("Unable to read dead letters") from exc

    def remove_dead_letter(self, envelope: JobEnvelope) -> bool:
        """Remove a reconciled dead letter while retaining its idempotency marker."""
        job_id = envelope.job_id
        if self._redis is None:
            with self._condition:
                try:
                    self._memory_dead.remove(job_id)
                except ValueError:
                    return False
                self._memory_envelopes.pop(job_id, None)
                return True
        try:
            return bool(
                self._redis.eval(
                    _REMOVE_DEAD_SCRIPT,
                    2,
                    self._dead_key,
                    self._envelopes_key,
                    job_id,
                )
            )
        except Exception as exc:
            raise JobQueueError(f"Unable to remove dead letter {job_id}") from exc

    @property
    def dead_letter_count(self) -> int:
        if self._redis is None:
            with self._condition:
                return len(self._memory_dead)
        try:
            return self._redis.llen(self._dead_key)
        except Exception as exc:
            raise JobQueueError("Unable to count dead letters") from exc

    def _dead_letter(self, envelope: JobEnvelope, error: str) -> None:
        updated = replace(envelope, retry_at=None, last_error=error)
        job_id = envelope.job_id
        if self._redis is None:
            with self._condition:
                if job_id not in self._memory_inflight:
                    raise JobQueueError(f"Job {job_id} is not inflight")
                del self._memory_inflight[job_id]
                self._memory_envelopes[job_id] = updated
                self._memory_dead.append(job_id)
                return
        try:
            result = self._redis.eval(
                _DEAD_SCRIPT,
                4,
                self._inflight_key,
                self._leases_key,
                self._envelopes_key,
                self._dead_key,
                job_id,
                updated.to_json(),
            )
            if not result:
                raise JobQueueError(f"Job {job_id} is not inflight")
        except JobQueueError:
            raise
        except Exception as exc:
            raise JobQueueError(f"Unable to dead-letter job {job_id}") from exc

    def _promote_due(self) -> None:
        if self._redis is None:
            with self._condition:
                self._promote_due_memory()
            return
        try:
            due = self._redis.zrangebyscore(self._scheduled_key, "-inf", self._clock())
            for value in due:
                job_id = self._text(value)
                self._redis.eval(
                    _PROMOTE_SCRIPT,
                    2,
                    self._scheduled_key,
                    self._ready_key,
                    job_id,
                )
        except Exception as exc:
            raise JobQueueError("Unable to promote scheduled retries") from exc

    def _promote_due_memory(self) -> None:
        due = [
            job_id
            for job_id, retry_at in self._memory_scheduled.items()
            if retry_at <= self._clock()
        ]
        for job_id in due:
            del self._memory_scheduled[job_id]
            self._memory_ready.append(job_id)

    def _recover_one_memory(self, job_id: str) -> None:
        envelope = self._memory_envelopes[job_id]
        del self._memory_inflight[job_id]
        updated = replace(envelope, last_error="lease expired")
        self._memory_envelopes[job_id] = updated
        if envelope.attempt >= self._max_attempts:
            self._memory_dead.append(job_id)
        else:
            self._memory_ready.append(job_id)
            self._condition.notify()

    def _recover_one_redis(self, envelope: JobEnvelope) -> bool:
        updated = replace(envelope, last_error="lease expired")
        destination = self._dead_key if envelope.attempt >= self._max_attempts else self._ready_key
        result = self._redis_or_raise().eval(
            _RECOVER_SCRIPT,
            4,
            self._inflight_key,
            self._leases_key,
            self._envelopes_key,
            destination,
            envelope.job_id,
            updated.to_json(),
        )
        return bool(result)

    def _redis_or_raise(self) -> RedisClient:
        if self._redis is None:  # pragma: no cover - internal invariant
            raise JobQueueError("Redis backend is not configured")
        return self._redis

    @staticmethod
    def _text(value: str | bytes) -> str:
        return value.decode("utf-8") if isinstance(value, bytes) else value
