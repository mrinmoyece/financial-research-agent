"""Unit tests for job-store isolation and failure behavior."""

from types import SimpleNamespace

import pytest

from src.api.job_store import JobStore, JobStoreError
from src.config.settings import Settings


class _FakeRedis:
    def __init__(self):
        self.data = {}
        self.last_expiry = None

    def ping(self):
        return True

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, ex, nx=False):
        if nx and key in self.data:
            return False
        self.data[key] = value
        self.last_expiry = ex
        return True

    def delete(self, *keys):
        for key in keys:
            self.data.pop(key, None)

    def scan_iter(self, match):
        prefix = match.removesuffix("*")
        return iter(key for key in self.data if key.startswith(prefix))

    def mget(self, keys):
        return [self.data.get(key) for key in keys]


def test_namespaces_produce_distinct_redis_keys():
    statuses = JobStore(namespace="status")
    results = JobStore(namespace="result")

    assert statuses._key("job-1") == "research_job:status:job-1"
    assert results._key("job-1") == "research_job:result:job-1"


@pytest.mark.parametrize("namespace", ["", "UPPER", "../unsafe", "contains space"])
def test_rejects_unsafe_namespace(namespace):
    with pytest.raises(ValueError):
        JobStore(namespace=namespace)


def test_redis_store_crud_and_namespace(monkeypatch):
    redis = _FakeRedis()
    redis_lib = SimpleNamespace(from_url=lambda *_args, **_kwargs: redis)
    monkeypatch.setattr("src.api.job_store._redis_lib", redis_lib)
    store = JobStore(Settings(redis_url="redis://test"), namespace="status")

    store.set("job-1", {"status": "pending"})
    store.set("job-2", {"status": "complete"})
    assert store.backend == "redis"
    assert store.get("job-1") == {"status": "pending"}
    assert len(store.values()) == 2
    assert len(store) == 2

    store.delete("job-1")
    assert store.get("job-1") is None
    store.clear()
    assert store.values() == []


def test_job_records_use_dedicated_long_retention(monkeypatch):
    redis = _FakeRedis()
    monkeypatch.setattr(
        "src.api.job_store._redis_lib",
        SimpleNamespace(from_url=lambda *_args, **_kwargs: redis),
    )
    store = JobStore(Settings(cache_ttl_seconds=60, job_retention_seconds=86400))
    store.set("awaiting", {"status": "awaiting_approval"})
    assert redis.last_expiry == 86400


def test_set_if_absent_is_atomic_for_memory_and_redis(monkeypatch):
    memory = JobStore(namespace="atomic-memory")
    assert memory.set_if_absent("job", {"status": "pending"})
    assert not memory.set_if_absent("job", {"status": "replacement"})
    assert memory.get("job") == {"status": "pending"}

    redis = _FakeRedis()
    monkeypatch.setattr(
        "src.api.job_store._redis_lib",
        SimpleNamespace(from_url=lambda *_args, **_kwargs: redis),
    )
    persistent = JobStore(Settings(), namespace="atomic-redis")
    assert persistent.set_if_absent("job", {"status": "pending"})
    assert not persistent.set_if_absent("job", {"status": "replacement"})
    assert persistent.get("job") == {"status": "pending"}


def test_memory_store_does_not_expose_mutable_internal_records():
    store = JobStore(namespace="copy-safe")
    original = {"status": "pending"}
    store.set("job", original)
    original["status"] = "corrupted"
    assert store.get("job") == {"status": "pending"}

    retrieved = store.get("job")
    retrieved["status"] = "corrupted"
    assert store.get("job") == {"status": "pending"}


def test_required_redis_fails_startup(monkeypatch):
    def fail(*_args, **_kwargs):
        raise OSError("down")

    monkeypatch.setattr("src.api.job_store._redis_lib", SimpleNamespace(from_url=fail))
    with pytest.raises(JobStoreError, match="Required Redis"):
        JobStore(Settings(redis_required=True))


def test_required_redis_fails_when_client_library_is_missing(monkeypatch):
    monkeypatch.setattr("src.api.job_store._redis_lib", None)
    with pytest.raises(JobStoreError, match="client library"):
        JobStore(Settings(redis_required=True))


def test_redis_operation_failure_is_not_silently_masked(monkeypatch):
    redis = _FakeRedis()
    monkeypatch.setattr(
        "src.api.job_store._redis_lib",
        SimpleNamespace(from_url=lambda *_args, **_kwargs: redis),
    )
    store = JobStore(Settings())
    redis.get = lambda _key: (_ for _ in ()).throw(OSError("down"))
    with pytest.raises(JobStoreError, match="Unable to read"):
        store.get("job-1")

    redis.ping = lambda: (_ for _ in ()).throw(OSError("down"))
    with pytest.raises(JobStoreError, match="health check"):
        store.ping()
