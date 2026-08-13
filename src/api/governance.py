"""Distributed rate, concurrency, and tamper-evident audit controls."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from src.api.job_store import JobStoreError
from src.config.settings import Settings
from src.security.identity import Principal

logger = logging.getLogger(__name__)

try:
    import redis as _redis_lib
except ImportError:  # pragma: no cover
    _redis_lib = None  # type: ignore[assignment]

_RATE_LIMIT_LUA = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return current
"""

_ACQUIRE_SLOT_LUA = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current >= tonumber(ARGV[1]) then return 0 end
redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ARGV[2])
return 1
"""

_RELEASE_SLOT_LUA = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current <= 1 then redis.call('DEL', KEYS[1]); return 0 end
return redis.call('DECR', KEYS[1])
"""

_AUDIT_LUA = """
local previous = redis.call('GET', KEYS[1]) or string.rep('0', 40)
local sequence = redis.call('INCR', KEYS[2])
local digest = redis.sha1hex(previous .. ARGV[1] .. sequence)
redis.call('SET', KEYS[1], digest)
redis.call(
  'XADD', KEYS[3], 'MAXLEN', '~', ARGV[2], '*',
  'sequence', sequence, 'previous_hash', previous, 'event_hash', digest, 'event', ARGV[1]
)
return {sequence, previous, digest}
"""


class GovernanceStore:
    """Cross-replica governance controls, with a deterministic local backend."""

    def __init__(self, settings: Settings) -> None:
        self._redis: Any | None = None
        self._rate_limit = settings.rate_limit_requests_per_minute
        self._max_concurrent = settings.max_concurrent_jobs_per_tenant
        self._ttl = settings.cache_ttl_seconds
        self._memory_windows: dict[tuple[str, int], int] = defaultdict(int)
        self._memory_slots: dict[str, int] = defaultdict(int)
        self._memory_events: list[dict[str, Any]] = []
        self._last_hash = "0" * 64
        self._lock = threading.Lock()

        if _redis_lib is not None:
            try:
                client = _redis_lib.from_url(  # type: ignore[no-untyped-call]
                    settings.redis_url,
                    decode_responses=True,
                    socket_connect_timeout=1,
                    socket_timeout=1,
                )
                client.ping()
                self._redis = client
            except Exception as exc:
                if settings.redis_required:
                    raise JobStoreError("Required Redis governance store is unavailable") from exc
                logger.warning("GovernanceStore using local-only controls: %s", exc)
        elif settings.redis_required:
            raise JobStoreError("Required Redis client library is unavailable")

    @property
    def backend(self) -> str:
        return "redis" if self._redis is not None else "memory"

    def enforce_rate_limit(self, principal: Principal) -> None:
        key = f"governance:rate:{principal.tenant_id}:{principal.principal_id}"
        if self._redis is not None:
            try:
                count = int(self._redis.eval(_RATE_LIMIT_LUA, 1, key, 60))
            except Exception as exc:
                raise JobStoreError("Rate-limit store unavailable") from exc
        else:
            window = int(time.time() // 60)
            with self._lock:
                count = self._memory_windows[(key, window)] + 1
                self._memory_windows[(key, window)] = count
                if len(self._memory_windows) > 10000:
                    self._memory_windows = {
                        item: value
                        for item, value in self._memory_windows.items()
                        if item[1] >= window - 1
                    }
        if count > self._rate_limit:
            raise PermissionError("Rate limit exceeded")

    def acquire_job_slot(self, tenant_id: str) -> bool:
        key = f"governance:slots:{tenant_id}"
        if self._redis is not None:
            try:
                return bool(
                    self._redis.eval(
                        _ACQUIRE_SLOT_LUA,
                        1,
                        key,
                        self._max_concurrent,
                        self._ttl,
                    )
                )
            except Exception as exc:
                raise JobStoreError("Concurrency store unavailable") from exc
        with self._lock:
            if self._memory_slots[tenant_id] >= self._max_concurrent:
                return False
            self._memory_slots[tenant_id] += 1
            return True

    def release_job_slot(self, tenant_id: str) -> None:
        key = f"governance:slots:{tenant_id}"
        if self._redis is not None:
            try:
                self._redis.eval(_RELEASE_SLOT_LUA, 1, key)
                return
            except Exception as exc:
                raise JobStoreError("Concurrency store unavailable") from exc
        with self._lock:
            self._memory_slots[tenant_id] = max(0, self._memory_slots[tenant_id] - 1)

    def append_audit(
        self,
        event_type: str,
        principal: Principal,
        *,
        job_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = {
            "event_type": event_type,
            "principal_id": principal.principal_id,
            "tenant_id": principal.tenant_id,
            "job_id": job_id,
            "details": details or {},
            "timestamp": datetime.now(UTC).isoformat(),
        }
        body = json.dumps(event, sort_keys=True, separators=(",", ":"), default=str)
        if self._redis is not None:
            try:
                sequence, previous, digest = self._redis.eval(
                    _AUDIT_LUA,
                    3,
                    "governance:audit:last_hash",
                    "governance:audit:sequence",
                    "governance:audit:events",
                    body,
                    100000,
                )
            except Exception as exc:
                raise JobStoreError("Audit store unavailable") from exc
            return {
                **event,
                "sequence": int(sequence),
                "previous_hash": str(previous),
                "event_hash": str(digest),
            }

        with self._lock:
            sequence = len(self._memory_events) + 1
            digest = hashlib.sha256(f"{self._last_hash}{body}{sequence}".encode()).hexdigest()
            record = {
                **event,
                "sequence": sequence,
                "previous_hash": self._last_hash,
                "event_hash": digest,
            }
            self._memory_events.append(record)
            self._last_hash = digest
            return record

    def audit_events(self) -> list[dict[str, Any]]:
        """Return local audit events for tests and development inspection."""
        with self._lock:
            return list(self._memory_events)
