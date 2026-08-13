"""
Job store abstraction for the research API.

The API deploys behind a Kubernetes HPA that scales 2-8 replicas
(see k8s/deployment.yaml) and docker-compose.yml provisions a Redis
service alongside the app. A job submitted to one pod must be visible
when a later poll request lands on a *different* pod — a bare
in-process dict cannot do that.

JobStore is backed by Redis (via `settings.redis_url`) when Redis is
reachable, so job state is shared across all replicas. If Redis is not
configured or not reachable (e.g. local dev, unit tests, or a sandbox
without a Redis service), it falls back to an in-process dict so the
app keeps working — single-replica only, which matches local/dev usage.

This is intentionally a thin key-value abstraction (get/set/delete/values),
not a generic Redis client wrapper — it only does what the API needs.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from src.config.settings import Settings

logger = logging.getLogger(__name__)

try:
    import redis as _redis_lib
except ImportError:  # pragma: no cover - exercised only if redis isn't installed
    _redis_lib = None  # type: ignore[assignment]


class JobStoreError(RuntimeError):
    """Raised when the configured persistent job store cannot complete an operation."""


class JobStore:
    """
    Key-value store for research job status, backed by Redis when
    available with an automatic in-memory fallback.

    Values are JSON-serialisable dicts (callers are responsible for
    `model_dump(mode="json")` / `model_validate` at the boundary — this
    class does not know about JobStatus/pydantic).
    """

    def __init__(self, settings: Settings | None = None, namespace: str = "status") -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", namespace):
            raise ValueError("namespace must be a lowercase Redis-safe identifier")
        self._namespace = namespace
        self._ttl_seconds: int = settings.cache_ttl_seconds if settings else 3600
        self._memory: dict[str, dict[str, Any]] = {}
        self._redis: Any | None = None

        if settings is not None and settings.redis_required and _redis_lib is None:
            raise JobStoreError("Required Redis client library is unavailable")

        if settings is not None and _redis_lib is not None:
            try:
                client = _redis_lib.from_url(  # type: ignore[no-untyped-call]
                    settings.redis_url,
                    decode_responses=True,
                    socket_connect_timeout=1,
                    socket_timeout=1,
                )
                client.ping()
                self._redis = client
                logger.info("JobStore: connected to Redis at %s", settings.redis_url)
            except Exception as exc:
                if settings.redis_required:
                    raise JobStoreError("Required Redis job store is unavailable") from exc
                logger.warning(
                    "JobStore: Redis unreachable (%s) — falling back to in-memory store. "
                    "Job state will NOT be shared across replicas.",
                    exc,
                )
                self._redis = None
        else:
            logger.info(
                "JobStore: Redis not configured or 'redis' package unavailable — "
                "using in-memory store (single-replica only)."
            )

    @property
    def backend(self) -> str:
        return "redis" if self._redis is not None else "memory"

    def _key(self, job_id: str) -> str:
        return f"research_job:{self._namespace}:{job_id}"

    def _keys(self) -> list[str]:
        if self._redis is None:
            return []
        return list(self._redis.scan_iter(match=f"research_job:{self._namespace}:*"))

    def ping(self) -> bool:
        """Check that the selected backend is currently usable."""
        if self._redis is None:
            return True
        try:
            return bool(self._redis.ping())
        except Exception as exc:
            logger.error("JobStore.ping: Redis error=%s", exc)
            raise JobStoreError("Redis health check failed") from exc

    def get(self, job_id: str) -> dict[str, Any] | None:
        if self._redis is not None:
            try:
                raw = self._redis.get(self._key(job_id))
                return json.loads(raw) if raw is not None else None
            except Exception as exc:
                logger.error("JobStore.get: Redis error job_id=%s error=%s", job_id, exc)
                raise JobStoreError(f"Unable to read job {job_id}") from exc
        return self._memory.get(job_id)

    def set(self, job_id: str, value: dict[str, Any]) -> None:
        if self._redis is not None:
            try:
                self._redis.set(
                    self._key(job_id),
                    json.dumps(value, default=str),
                    ex=self._ttl_seconds,
                )
                return
            except Exception as exc:
                logger.error("JobStore.set: Redis error job_id=%s error=%s", job_id, exc)
                raise JobStoreError(f"Unable to write job {job_id}") from exc
        self._memory[job_id] = value

    def delete(self, job_id: str) -> None:
        if self._redis is not None:
            try:
                self._redis.delete(self._key(job_id))
            except Exception as exc:
                logger.error("JobStore.delete: Redis error job_id=%s error=%s", job_id, exc)
                raise JobStoreError(f"Unable to delete job {job_id}") from exc
        self._memory.pop(job_id, None)

    def values(self) -> list[dict[str, Any]]:
        """Return all known jobs. Used by the list-jobs endpoint."""
        if self._redis is not None:
            try:
                keys = self._keys()
                if not keys:
                    return []
                raw_values = self._redis.mget(keys)
                return [json.loads(v) for v in raw_values if v is not None]
            except Exception as exc:
                logger.error("JobStore.values: Redis error=%s", exc)
                raise JobStoreError("Unable to list jobs") from exc
        return list(self._memory.values())

    def clear(self) -> None:
        """Remove all jobs. Primarily for test fixtures."""
        if self._redis is not None:
            try:
                keys = self._keys()
                if keys:
                    self._redis.delete(*keys)
            except Exception as exc:
                logger.error("JobStore.clear: Redis error=%s", exc)
                raise JobStoreError("Unable to clear jobs") from exc
        self._memory.clear()

    def __len__(self) -> int:
        if self._redis is not None:
            try:
                return len(self._keys())
            except Exception as exc:
                logger.error("JobStore.__len__: Redis error=%s", exc)
                raise JobStoreError("Unable to count jobs") from exc
        return len(self._memory)
