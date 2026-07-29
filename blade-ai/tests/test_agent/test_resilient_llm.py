"""Tests for ResilientChatOpenAI bounded generic retry."""

import httpx
import openai
import pytest
from langchain_openai import ChatOpenAI

from chaos_agent.agent.resilient_llm import ResilientChatOpenAI
from chaos_agent.config.settings import settings


@pytest.fixture
def fast_retry(monkeypatch):
    """3 retries, zero backoff, deterministic (no jitter) — fast + repeatable."""
    monkeypatch.setattr(settings, "retry_max_retries", 3)
    monkeypatch.setattr(settings, "retry_base_delay", 0.0)
    monkeypatch.setattr(settings, "retry_max_delay", 0.0)
    monkeypatch.setattr(settings, "retry_exponential_base", 2.0)
    monkeypatch.setattr(settings, "retry_jitter", False)


def _make() -> ResilientChatOpenAI:
    # Construction never connects — a dummy key/base_url is enough offline.
    return ResilientChatOpenAI(
        model="qwen-max-latest",
        api_key="test-key",
        base_url="http://localhost:1/v1",
    )


async def test_ainvoke_retries_transient_then_succeeds(fast_retry, monkeypatch):
    """A mid-stream ReadError on the first tries is retried until success."""
    llm = _make()
    calls = {"n": 0}

    async def fake_ainvoke(self, *a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ReadError("connection reset (simulated sleep)")
        return "ok"

    monkeypatch.setattr(ChatOpenAI, "ainvoke", fake_ainvoke)
    out = await llm.ainvoke("hi")

    assert out == "ok"
    assert calls["n"] == 3  # 2 transient failures + 1 success


async def test_ainvoke_reraises_after_exhausting_retries(fast_retry, monkeypatch):
    """Persistent transport failure surfaces once the retry budget is spent."""
    llm = _make()
    calls = {"n": 0}

    async def always_fail(self, *a, **k):
        calls["n"] += 1
        raise httpx.RemoteProtocolError("server disconnected")

    monkeypatch.setattr(ChatOpenAI, "ainvoke", always_fail)
    with pytest.raises(httpx.RemoteProtocolError):
        await llm.ainvoke("hi")

    # 1 initial attempt + retry_max_retries(3) retries = 4 total tries.
    assert calls["n"] == 4


async def test_ainvoke_retries_any_error_then_reraises(fast_retry, monkeypatch):
    """Retry is GENERIC (no per-error-type branching): even a plain error is
    retried up to the budget, then the ORIGINAL error is re-raised so the turn
    boundary can surface its content and end gracefully."""
    llm = _make()
    calls = {"n": 0}

    async def fake(self, *a, **k):
        calls["n"] += 1
        raise ValueError("some provider error")

    monkeypatch.setattr(ChatOpenAI, "ainvoke", fake)
    with pytest.raises(ValueError):
        await llm.ainvoke("hi")

    # 1 initial attempt + retry_max_retries(3, via fixture) retries = 4 tries.
    assert calls["n"] == 4


@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: httpx.ReadTimeout("read timed out"),
        lambda: openai.APITimeoutError(
            request=httpx.Request("POST", "http://localhost:1/v1/chat/completions")
        ),
    ],
)
async def test_ainvoke_does_not_retry_slow_timeout(fast_retry, monkeypatch, exc_factory):
    """A genuine response/read timeout (model too slow) is NOT retried —
    retrying would only double an already-long wait — it re-raises at once."""
    llm = _make()
    calls = {"n": 0}

    async def fake(self, *a, **k):
        calls["n"] += 1
        raise exc_factory()

    monkeypatch.setattr(ChatOpenAI, "ainvoke", fake)
    with pytest.raises((httpx.ReadTimeout, openai.APITimeoutError)):
        await llm.ainvoke("hi")

    assert calls["n"] == 1  # slow-timeout is surfaced immediately, no retry


async def test_retry_survives_bind(fast_retry, monkeypatch):
    """bind()/bind_tools() delegate to this instance's wrapped ainvoke, so the
    retry still fires after tools/response_format are bound."""
    llm = _make()
    calls = {"n": 0}

    async def fake(self, *a, **k):
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.ReadError("x")
        return "bound-ok"

    monkeypatch.setattr(ChatOpenAI, "ainvoke", fake)
    bound = llm.bind(response_format={"type": "json_object"})
    out = await bound.ainvoke("hi")

    assert out == "bound-ok"
    assert calls["n"] == 2
