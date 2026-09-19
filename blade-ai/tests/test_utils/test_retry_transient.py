"""Tests for utils/retry_transient.py — B35 bounded transient retry.

Sister suite to tests/test_transports/test_transient_retry.py (P3): the
transport side classifies MESSAGE SIGNATURES on a populated
CommandResult; this side classifies EXCEPTION TYPES raised by the
recover verifier's LLM verdict calls. The classification contract here
must stay CONSISTENT with agent/resilient_llm._no_retry_exc_types —
the slow-model timeout family is never retried at ANY layer (a Layer 1
call has no outer wait_for, so an SDK-wrapped 600s timeout retried
here would mean 3 x 600s of dead waiting on a slow model whose correct
handling is immediate degradation to the deterministic-merge path).
"""

import asyncio

import httpx
import openai
import pytest

from chaos_agent.config.settings import settings
from chaos_agent.errors import LLMProviderRejectError
from chaos_agent.utils.retry_transient import (
    call_with_transient_retry,
    is_transient_exception,
    retry_transient,
)

_REQ = httpx.Request("POST", "http://localhost:1/v1/chat/completions")


def _no_sleep(monkeypatch) -> list[float]:
    """Replace asyncio.sleep with a recorder; returns the call list.

    The module awaits ``asyncio.sleep`` via the global module object, so
    patching the attribute disables real waiting globally for the test.
    """
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return sleeps


# --- classifier -----------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionError("connection reset by peer"),
        httpx.ConnectError("connection refused", request=_REQ),
        # ConnectTimeout STAYS retryable: the connect budget is short
        # (llm_connect_timeout), so this is a fast-fail gateway blip —
        # not a slow-model verdict.
        httpx.ConnectTimeout("connect timed out", request=_REQ),
        httpx.ReadError("read failed", request=_REQ),
        httpx.RemoteProtocolError("server disconnected without response"),
        openai.APIConnectionError(request=_REQ),
    ],
)
def test_fast_fail_connection_family_is_transient(exc):
    assert is_transient_exception(exc)


@pytest.mark.parametrize(
    "exc_factory",
    [
        # Slow-model verdicts, raw + SDK-wrapped — same policy as
        # resilient_llm._no_retry_exc_types, never retried here either.
        lambda: TimeoutError("slow model"),
        lambda: asyncio.TimeoutError(),
        lambda: httpx.ReadTimeout("read timed out", request=_REQ),
        lambda: openai.APITimeoutError(request=_REQ),
        # Deterministic failures — retry cannot change the outcome.
        lambda: ValueError("deterministic bug"),
        lambda: LLMProviderRejectError("400 bad request"),
    ],
)
def test_slow_model_and_deterministic_not_transient(exc_factory):
    assert not is_transient_exception(exc_factory())


# --- call_with_transient_retry --------------------------------------------


async def test_retry_succeeds_after_transient_streak(monkeypatch):
    monkeypatch.setattr(settings, "retry_transient_max_attempts", 3)
    monkeypatch.setattr(settings, "retry_transient_base_delay", 2.0)
    monkeypatch.setattr(settings, "retry_jitter", False)
    sleeps = _no_sleep(monkeypatch)
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("gateway blip")
        return "recovered"

    assert await call_with_transient_retry(fn, log_name="unit verdict") == "recovered"
    assert calls["n"] == 3
    # Full-jitter OFF -> deterministic exponential: 2s, then 4s.
    assert sleeps == [2.0, 4.0]


async def test_exhaustion_reraises_original_exception(monkeypatch):
    monkeypatch.setattr(settings, "retry_transient_max_attempts", 3)
    sleeps = _no_sleep(monkeypatch)
    sentinel = httpx.ConnectError("refused", request=_REQ)
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        raise sentinel

    with pytest.raises(httpx.ConnectError) as ei:
        await call_with_transient_retry(fn, log_name="unit verdict")
    # The ORIGINAL exception object surfaces — the caller's existing
    # failure paths (RECOVERY_FAILED / layer1 error status) see exactly
    # the shape they always did, never a wrapped re-raise.
    assert ei.value is sentinel
    assert calls["n"] == 3
    assert len(sleeps) == 2


@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: TimeoutError("slow model"),
        lambda: openai.APITimeoutError(request=_REQ),
        lambda: LLMProviderRejectError("400 bad request"),
    ],
)
async def test_slow_model_and_permanent_raise_immediately(exc_factory, monkeypatch):
    sleeps = _no_sleep(monkeypatch)
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        raise exc_factory()

    with pytest.raises(Exception):
        await call_with_transient_retry(fn, log_name="unit verdict")
    assert calls["n"] == 1
    assert sleeps == []


async def test_disabled_by_config(monkeypatch):
    monkeypatch.setattr(settings, "retry_transient_max_attempts", 1)
    sleeps = _no_sleep(monkeypatch)
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        raise ConnectionError("blip")

    with pytest.raises(ConnectionError):
        await call_with_transient_retry(fn, log_name="unit verdict")
    assert calls["n"] == 1
    assert sleeps == []


async def test_cancellation_is_never_swallowed(monkeypatch):
    sleeps = _no_sleep(monkeypatch)
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await call_with_transient_retry(fn, log_name="unit verdict")
    # CancelledError subclasses BaseException — user cancellation is
    # never classified as a transient error.
    assert calls["n"] == 1
    assert sleeps == []


# --- decorator form -------------------------------------------------------


async def test_decorator_form(monkeypatch):
    monkeypatch.setattr(settings, "retry_transient_max_attempts", 3)
    sleeps = _no_sleep(monkeypatch)
    calls = {"n": 0}

    async def probe():
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.ConnectError("refused", request=_REQ)
        return 42

    orig = probe
    decorated = retry_transient("decorated probe")(probe)

    assert await decorated() == 42
    assert calls["n"] == 2
    assert len(sleeps) == 1
    assert decorated.__wrapped__ is orig
