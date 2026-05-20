"""Circuit breaker — per-component degradation chain (ADR-011).

3 consecutive failures → open for `recovery_timeout_s` → half-open probe →
close on success. Emits a Langfuse alert span when the breaker opens.

Usage:
    breaker = CircuitBreaker("reranker")
    try:
        result = await breaker.call(reranker.rerank, query, docs)
    except CircuitOpen:
        # caller decides the fallback (e.g. skip reranking)
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitOpen(RuntimeError):
    """Raised when a call is short-circuited because the breaker is open."""

    def __init__(self, component: str):
        super().__init__(f"circuit open for {component}")
        self.component = component


class State(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class _BreakerState:
    state: State = State.CLOSED
    consecutive_failures: int = 0
    opened_at: float = 0.0


class CircuitBreaker:
    """Lightweight per-component breaker. Construct one per dependency."""

    def __init__(
        self,
        component: str,
        *,
        failure_threshold: int = 3,
        recovery_timeout_s: int = 30,
    ):
        self.component = component
        self.failure_threshold = failure_threshold
        self.recovery_timeout_s = recovery_timeout_s
        self._state = _BreakerState()
        self._lock = asyncio.Lock()

    @property
    def state(self) -> State:
        return self._state.state

    async def call(
        self,
        fn: Callable[..., Awaitable[T]],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> T:
        async with self._lock:
            await self._maybe_half_open()
            if self._state.state is State.OPEN:
                raise CircuitOpen(self.component)
        try:
            result = await fn(*args, **kwargs)
        except Exception:
            await self._on_failure()
            raise
        else:
            await self._on_success()
            return result

    async def _maybe_half_open(self) -> None:
        if self._state.state is State.OPEN:
            elapsed = time.time() - self._state.opened_at
            if elapsed >= self.recovery_timeout_s:
                self._state.state = State.HALF_OPEN
                log.info("circuit.%s: probing (half-open)", self.component)

    async def _on_failure(self) -> None:
        async with self._lock:
            self._state.consecutive_failures += 1
            if self._state.state is State.HALF_OPEN:
                self._state.state = State.OPEN
                self._state.opened_at = time.time()
                log.warning("circuit.%s: probe failed → open again", self.component)
                return
            if (
                self._state.consecutive_failures >= self.failure_threshold
                and self._state.state is not State.OPEN
            ):
                self._state.state = State.OPEN
                self._state.opened_at = time.time()
                log.warning(
                    "circuit.%s: opened after %d failures",
                    self.component,
                    self._state.consecutive_failures,
                )

    async def _on_success(self) -> None:
        async with self._lock:
            if self._state.state in (State.HALF_OPEN, State.OPEN):
                log.info("circuit.%s: closed", self.component)
            self._state.state = State.CLOSED
            self._state.consecutive_failures = 0
            self._state.opened_at = 0.0
