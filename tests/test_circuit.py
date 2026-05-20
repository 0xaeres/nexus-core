import asyncio

import pytest

from nexus.retrieval.circuit import CircuitBreaker, CircuitOpen, State


async def _ok() -> str:
    return "ok"


class _Boom(RuntimeError):
    pass


async def _fail() -> str:
    raise _Boom("nope")


@pytest.mark.asyncio
async def test_closed_on_success() -> None:
    cb = CircuitBreaker("x", failure_threshold=2)
    assert (await cb.call(_ok)) == "ok"
    assert cb.state is State.CLOSED


@pytest.mark.asyncio
async def test_opens_after_threshold() -> None:
    cb = CircuitBreaker("x", failure_threshold=2)
    with pytest.raises(_Boom):
        await cb.call(_fail)
    assert cb.state is State.CLOSED
    with pytest.raises(_Boom):
        await cb.call(_fail)
    assert cb.state is State.OPEN
    with pytest.raises(CircuitOpen):
        await cb.call(_fail)


@pytest.mark.asyncio
async def test_half_open_probe_then_close() -> None:
    cb = CircuitBreaker("x", failure_threshold=1, recovery_timeout_s=0)
    with pytest.raises(_Boom):
        await cb.call(_fail)
    assert cb.state is State.OPEN
    # 0-second recovery → next call probes (half-open) and on success → closed
    await asyncio.sleep(0.01)
    assert (await cb.call(_ok)) == "ok"
    assert cb.state is State.CLOSED


@pytest.mark.asyncio
async def test_half_open_failure_reopens() -> None:
    cb = CircuitBreaker("x", failure_threshold=1, recovery_timeout_s=0)
    with pytest.raises(_Boom):
        await cb.call(_fail)
    await asyncio.sleep(0.01)
    with pytest.raises(_Boom):
        await cb.call(_fail)
    assert cb.state is State.OPEN


@pytest.mark.asyncio
async def test_success_resets_failure_counter() -> None:
    cb = CircuitBreaker("x", failure_threshold=3)
    with pytest.raises(_Boom):
        await cb.call(_fail)
    await cb.call(_ok)
    # counter reset → two more failures should NOT open the breaker
    with pytest.raises(_Boom):
        await cb.call(_fail)
    with pytest.raises(_Boom):
        await cb.call(_fail)
    assert cb.state is State.CLOSED
