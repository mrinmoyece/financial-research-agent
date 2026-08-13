"""Thread-safe circuit breakers for external providers."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")


class CircuitOpenError(RuntimeError):
    """Raised while a provider circuit is open."""


@dataclass
class _CircuitState:
    failures: int = 0
    opened_at: float | None = None


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int,
        recovery_seconds: int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self._clock = clock
        self._state = _CircuitState()
        self._lock = threading.Lock()

    def call(self, operation: Callable[[], T]) -> T:
        with self._lock:
            if self._state.opened_at is not None:
                if self._clock() - self._state.opened_at < self.recovery_seconds:
                    raise CircuitOpenError("Provider circuit is open")
                self._state = _CircuitState()
        try:
            result = operation()
        except Exception:
            with self._lock:
                self._state.failures += 1
                if self._state.failures >= self.failure_threshold:
                    self._state.opened_at = self._clock()
            raise
        with self._lock:
            self._state = _CircuitState()
        return result

    @property
    def is_open(self) -> bool:
        with self._lock:
            if self._state.opened_at is None:
                return False
            return self._clock() - self._state.opened_at < self.recovery_seconds
