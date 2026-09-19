"""Tests for ResilientChatOpenAI: integrity gate + bounded retry + 4xx classification."""

import logging

import httpx
import openai
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI

from chaos_agent.agent.resilient_llm import (
    _RETRIABLE_4XX_STATUSES,
    ResilientChatOpenAI,
    _is_provider_rejection,
)
from chaos_agent.config.settings import settings
from chaos_agent.errors import (
    ErrorSeverity,
    LLMContextOverflowError,
    LLMProviderRejectError,
    is_recoverable,
)

MODULE_LOGGER = "chaos_agent.agent.resilient_llm"


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


# ---------------------------------------------------------------------------
# Layer 3 — deterministic-rejection classification (issue #1344 signature #1)
# ---------------------------------------------------------------------------


def _api_error(cls: type, message: str, status: int):
    """Build a real ``openai`` status error carrying ``status``.

    ``status_code`` is read off the ``httpx.Response``, NOT off the class, so
    the status has to be passed explicitly: a ``RateLimitError`` built with a
    400 response would silently exercise the fail-fast branch instead of the
    retry branch and the test would pass while pinning nothing.
    """
    response = httpx.Response(
        status_code=status,
        request=httpx.Request("POST", "http://localhost:1/v1/chat/completions"),
    )
    return cls(message, response=response, body={"error": {"message": message}})


# Verbatim provider messages from chaosblade-io/chaosblade#1344 (signatures
# #1-#4) plus the context-window class from design.md D5. Kept verbatim so a
# wording drift in the signature table breaks these tests instead of silently
# degrading every hint to the generic fallback.
_MSG_RESPONSES_API = (
    "Error code: 400 - function_call_output requires item_reference ids "
    "matching each call_id on HTTP requests; continuation via "
    "previous_response_id is only supported on Responses WebSocket v2"
)
_MSG_TOOL_PAIRING = (
    "Error code: 400 - Messages with role 'tool' must be a response to a "
    "preceding message with 'tool_calls'"
)
_MSG_REASONING = (
    "Error code: 400 - The reasoning_content in the thinking mode must be "
    "passed back to the API"
)
_MSG_ENABLE_THINKING = (
    "Error code: 400 - Unsupported parameter: 'enable_thinking' is not "
    "supported with this model"
)
_MSG_CONTEXT_LENGTH = (
    "Error code: 400 - This model's maximum context length is 131072 tokens"
)
# NOT verbatim from issue #1344 — this one is inferred. It is what a provider
# says once the pre-send gate has dropped every message, and the wording
# follows how JSON-schema request validation is normally phrased rather than
# being measured against each endpoint (see the signature table's comment).
_MSG_EMPTY_MESSAGES = (
    "Error code: 400 - Invalid 'messages': empty array. "
    "Expected a non-empty array."
)


class TestDeterministicRejectionFailsFast:
    """HTTP 4xx is a VERDICT on the request, not an incident — except for the
    three codes the openai SDK itself retries (408/409/429)."""

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    async def test_non_exempt_4xx_wraps_and_does_not_retry(
        self, fast_retry, monkeypatch, status
    ):
        llm = _make()
        calls = {"n": 0}

        async def fake(self, *a, **k):
            calls["n"] += 1
            raise _api_error(openai.APIStatusError, f"Error code: {status}", status)

        monkeypatch.setattr(ChatOpenAI, "ainvoke", fake)
        with pytest.raises(LLMProviderRejectError) as ei:
            await llm.ainvoke("hi")

        # Replay of the identical request would be rejected identically, so
        # burning the backoff budget only delays the user's error.
        assert calls["n"] == 1
        assert ei.value.error_code == 4006
        assert ei.value.severity is ErrorSeverity.PERMANENT
        assert f"HTTP {status}" in str(ei.value)

    @pytest.mark.parametrize(
        "status,cls", [(408, openai.APIStatusError), (409, openai.ConflictError)]
    )
    async def test_408_409_stay_transient_and_are_retried(
        self, fast_retry, monkeypatch, status, cls
    ):
        """408/409 are 4xx but NOT verdicts — a request timeout and a lock
        timeout are transient by definition. Failing fast on them contradicts
        upstream and, with ``llm_max_retries=1``, downgrades a gateway hiccup
        into a lost drill.
        """
        llm = _make()
        calls = {"n": 0}

        async def fake(self, *a, **k):
            calls["n"] += 1
            raise _api_error(cls, f"Error code: {status} - timed out", status)

        monkeypatch.setattr(ChatOpenAI, "ainvoke", fake)
        with pytest.raises(cls) as ei:
            await llm.ainvoke("hi")

        assert calls["n"] == 4  # 1 initial + retry_max_retries(3)
        # Unwrapped — a 4006 here would tell the CLI "permanent" while the
        # L4 mapping reads the same text as recoverable AGENT_TIMEOUT.
        assert not isinstance(ei.value, LLMProviderRejectError)

    def test_exempt_set_is_exactly_the_sdk_retriable_4xx(self):
        """Full 400-499 sweep against the SDK's own ``_should_retry``.

        This is the regression lock for the whole classification: our
        fail-fast verdict MUST be the exact complement of upstream's retry
        verdict for every 4xx. A single code drifting out of alignment
        silently changes retry behaviour, and no per-status test above would
        notice a code that is not in its parametrize list.
        """
        from openai._base_client import SyncAPIClient

        client = object.__new__(SyncAPIClient)
        drifted = []
        for status in range(400, 500):
            probe = httpx.Response(
                status_code=status,
                request=httpx.Request("POST", "http://localhost:1/v1/chat/completions"),
            )
            sdk_retries = SyncAPIClient._should_retry(client, probe)
            exc = _api_error(openai.APIStatusError, f"Error code: {status}", status)
            if _is_provider_rejection(exc) == sdk_retries:
                drifted.append((status, sdk_retries))

        assert not drifted, (
            "fail-fast verdict disagrees with openai SDK _should_retry for "
            f"(status, sdk_retries)={drifted}; reconcile _RETRIABLE_4XX_STATUSES"
        )
        # And the exempt set itself is pinned, so an accidental edit to the
        # constant cannot pass the sweep by narrowing both sides together.
        assert _RETRIABLE_4XX_STATUSES == frozenset({408, 409, 429})

    async def test_429_stays_transient_and_is_retried(self, fast_retry, monkeypatch):
        """429 is the one 4xx that IS an incident — the same request later
        succeeds, so it must keep the generic retry and surface unwrapped."""
        llm = _make()
        calls = {"n": 0}

        async def fake(self, *a, **k):
            calls["n"] += 1
            raise _api_error(openai.RateLimitError, "Rate limit reached", 429)

        monkeypatch.setattr(ChatOpenAI, "ainvoke", fake)
        with pytest.raises(openai.RateLimitError):
            await llm.ainvoke("hi")

        assert calls["n"] == 4  # 1 initial + retry_max_retries(3)

    async def test_5xx_stays_transient_and_is_retried(self, fast_retry, monkeypatch):
        llm = _make()
        calls = {"n": 0}

        async def fake(self, *a, **k):
            calls["n"] += 1
            raise _api_error(openai.InternalServerError, "The server had an error", 500)

        monkeypatch.setattr(ChatOpenAI, "ainvoke", fake)
        with pytest.raises(openai.InternalServerError):
            await llm.ainvoke("hi")

        assert calls["n"] == 4

    def test_sync_invoke_fails_fast_too(self, fast_retry, monkeypatch):
        """``invoke`` carries the same gate as ``ainvoke`` — the CLI and every
        non-graph call site go through it."""
        llm = _make()
        calls = {"n": 0}

        def fake(self, *a, **k):
            calls["n"] += 1
            raise _api_error(openai.BadRequestError, _MSG_TOOL_PAIRING, 400)

        monkeypatch.setattr(ChatOpenAI, "invoke", fake)
        with pytest.raises(LLMProviderRejectError):
            llm.invoke("hi")

        assert calls["n"] == 1

    async def test_cause_is_the_original_provider_error(self, fast_retry, monkeypatch):
        """``raise ... from exc`` keeps the provider body reachable for
        debugging and for string-based consumers downstream."""
        llm = _make()
        original = _api_error(openai.BadRequestError, _MSG_REASONING, 400)

        async def fake(self, *a, **k):
            raise original

        monkeypatch.setattr(ChatOpenAI, "ainvoke", fake)
        with pytest.raises(LLMProviderRejectError) as ei:
            await llm.ainvoke("hi")

        assert ei.value.__cause__ is original

    async def test_fail_fast_is_observable_in_logs(self, fast_retry, monkeypatch, caplog):
        """The log line is what tells an operator the retry budget was NOT
        spent — without it a fail-fast looks like a crash on first attempt."""
        llm = _make()

        async def fake(self, *a, **k):
            raise _api_error(openai.BadRequestError, _MSG_REASONING, 400)

        monkeypatch.setattr(ChatOpenAI, "ainvoke", fake)
        with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
            with pytest.raises(LLMProviderRejectError):
                await llm.ainvoke("hi")

        assert "not retried (deterministic rejection)" in caplog.text
        assert "HTTP 400" in caplog.text


class TestRejectHintMapping:
    """Each provider signature maps to a hint the user can act on."""

    @pytest.mark.parametrize(
        "provider_message,expected_hint",
        [
            pytest.param(_MSG_RESPONSES_API, "Responses API", id="sig1_gateway_responses_api"),
            pytest.param(_MSG_TOOL_PAIRING, "tool_call/ToolMessage pairing", id="sig2_tool_pairing"),
            pytest.param(_MSG_REASONING, "reasoning_content echoed back", id="sig3_reasoning_replay"),
            pytest.param(_MSG_ENABLE_THINKING, "llm_thinking_format", id="sig4_enable_thinking"),
            pytest.param(_MSG_CONTEXT_LENGTH, "context window", id="sig5_context_overflow"),
            pytest.param(_MSG_EMPTY_MESSAGES, "NO usable messages", id="sig6_emptied_by_gate"),
        ],
    )
    async def test_signature_maps_to_hint(
        self, fast_retry, monkeypatch, provider_message, expected_hint
    ):
        llm = _make()

        async def fake(self, *a, **k):
            raise _api_error(openai.BadRequestError, provider_message, 400)

        monkeypatch.setattr(ChatOpenAI, "ainvoke", fake)
        with pytest.raises(LLMProviderRejectError) as ei:
            await llm.ainvoke("hi")

        assert expected_hint in str(ei.value)

    async def test_reasoning_row_wins_over_thinking_row(self, fast_retry, monkeypatch):
        """The reasoning_content message ALSO contains the word 'thinking', so
        under the old any-phrase semantics it matched two rows of the table
        and first-match order decided. The AND groups have removed the
        overlap itself — the reasoning row now wins because it is the only
        row whose group is satisfied, not because it sits earlier. Pointing
        the user at the dialect setting when the real problem is the replay
        patch sends them to the wrong config knob.
        """
        llm = _make()

        async def fake(self, *a, **k):
            raise _api_error(openai.BadRequestError, _MSG_REASONING, 400)

        monkeypatch.setattr(ChatOpenAI, "ainvoke", fake)
        with pytest.raises(LLMProviderRejectError) as ei:
            await llm.ainvoke("hi")

        assert "llm_thinking_format" not in str(ei.value)

    @pytest.mark.parametrize(
        "single_phrase_message",
        [
            pytest.param(
                "Error code: 400 - invalid tool_calls format in message 3",
                id="tool_calls_alone_is_not_pairing",
            ),
            pytest.param(
                "Error code: 400 - 'thinking' is not a valid field for this model",
                id="thinking_alone_is_not_dialect",
            ),
            pytest.param(
                "Error code: 400 - responses api is not enabled on this account",
                id="responses_api_alone_is_not_routing",
            ),
            pytest.param(
                "Error code: 400 - the 'tools' field is an empty array",
                id="empty_array_about_another_field_is_not_local_state",
            ),
        ],
    )
    async def test_a_single_common_phrase_no_longer_triggers_a_row(
        self, fast_retry, monkeypatch, single_phrase_message
    ):
        """The regression lock for the AND semantics: each message below
        contains one phrase that USED to match a row outright under the old
        any-phrase probe, misprescribing that row's remedy. Under AND each
        falls through to the generic fallback, which echoes the provider's
        own words — the safe degradation.
        """
        llm = _make()

        async def fake(self, *a, **k):
            raise _api_error(openai.BadRequestError, single_phrase_message, 400)

        monkeypatch.setattr(ChatOpenAI, "ainvoke", fake)
        with pytest.raises(LLMProviderRejectError) as ei:
            await llm.ainvoke("hi")

        text = str(ei.value)
        for locked_out in (
            "tool_call/ToolMessage pairing",
            "llm_thinking_format",
            "Responses API",
            "NO usable messages",
            "context window",
        ):
            assert locked_out not in text, (
                f"a single-phrase message stole a row's hint: {locked_out!r}"
            )
        assert single_phrase_message in text  # generic fallback echoes it

    async def test_context_length_exceeded_variant_hits_the_context_row(
        self, fast_retry, monkeypatch
    ):
        """The second AND-group of the context row: the snake_case error-code
        phrasing carries the whole signature in one token, so it stands
        alone — but 'context length' without 'maximum' still must not.
        """
        llm = _make()
        variant = "Error code: 400 - prompt is too long: context_length_exceeded"

        async def fake(self, *a, **k):
            raise _api_error(openai.BadRequestError, variant, 400)

        monkeypatch.setattr(ChatOpenAI, "ainvoke", fake)
        with pytest.raises(LLMProviderRejectError) as ei:
            await llm.ainvoke("hi")

        assert "context window" in str(ei.value)

    async def test_unknown_signature_echoes_raw_preview(self, fast_retry, monkeypatch):
        """No row matched → the provider's own words survive, so a brand-new
        400 stays debuggable instead of collapsing into 'request failed'."""
        llm = _make()
        novel = "Error code: 400 - a failure mode nobody has catalogued yet"

        async def fake(self, *a, **k):
            raise _api_error(openai.BadRequestError, novel, 400)

        monkeypatch.setattr(ChatOpenAI, "ainvoke", fake)
        with pytest.raises(LLMProviderRejectError) as ei:
            await llm.ainvoke("hi")

        assert novel in str(ei.value)

    async def test_raw_preview_is_capped(self, fast_retry, monkeypatch):
        """The fallback must not paste a whole provider body into the user's
        terminal — capped, with an explicit ellipsis marking the cut."""
        llm = _make()

        async def fake(self, *a, **k):
            raise _api_error(openai.BadRequestError, "x" * 1000, 400)

        monkeypatch.setattr(ChatOpenAI, "ainvoke", fake)
        with pytest.raises(LLMProviderRejectError) as ei:
            await llm.ainvoke("hi")

        text = str(ei.value)
        assert "x" * 300 in text
        assert "x" * 301 not in text
        assert text.rstrip().endswith("...")

    async def test_gate_emptied_list_surfaces_as_local_state_fault(
        self, fast_retry, monkeypatch, caplog
    ):
        """End to end across Layer 1 and Layer 3: an all-orphan list is emptied
        by the gate, the provider then 400s on the empty array, and the user
        has to be told the fault is local state — not handed a bare provider
        complaint that reads like an endpoint problem.
        """
        llm = _make()

        async def fake(self, *a, **k):
            # The provider only ever sees what the gate let through.
            assert a[0] == []
            raise _api_error(openai.BadRequestError, _MSG_EMPTY_MESSAGES, 400)

        monkeypatch.setattr(ChatOpenAI, "ainvoke", fake)
        with caplog.at_level(logging.WARNING):
            with pytest.raises(LLMProviderRejectError) as ei:
                await llm.ainvoke([_tool("call_vanished", "orphan")])

        assert "NO usable messages" in str(ei.value)
        assert "/clear" in str(ei.value)
        # The authoritative cause lives in the gate's own ERROR line, and both
        # halves have to be present for the diagnosis to be actionable.
        assert "emptied the ENTIRE message list" in caplog.text

    async def test_empty_array_row_does_not_steal_other_signatures(
        self, fast_retry, monkeypatch
    ):
        """The new row sits last, so it must not outrank a more precise one —
        and none of the issue #1344 messages may accidentally land on it.
        """
        llm = _make()

        async def fake(self, *a, **k):
            raise _api_error(openai.BadRequestError, _MSG_TOOL_PAIRING, 400)

        monkeypatch.setattr(ChatOpenAI, "ainvoke", fake)
        with pytest.raises(LLMProviderRejectError) as ei:
            await llm.ainvoke("hi")

        assert "tool_call/ToolMessage pairing" in str(ei.value)
        assert "NO usable messages" not in str(ei.value)


class TestContextOverflowIsClassifiedPermanentNotRecoverable:
    """The judgment question from the self-review (design.md D5a).

    Context overflow is the ONE 4xx that reads as recoverable — ``errors.py``
    already has ``LLMContextOverflowError`` (RECOVERABLE/4001, "recoverable by
    compaction") waiting for it, and the architecture spec describes an
    overflow → force-compact → retry path. That path was never built. The hint
    tests above only assert the WORDING, so nothing pinned the classification
    itself: someone could route overflow to the optimistic class and every
    existing test would stay green.

    These pin the verdict. The short version: the retry loop re-sends the
    identical oversized payload, because compaction lives in the node-level
    ``pre_reason_hook`` which has already run and returned — so RECOVERABLE
    here would promise a degradation this layer cannot perform, and would
    trade the specific exit code 4006 for 4001, a bucket already shared by
    ``ToolGuardError`` and by ``session_finalize._format_error``'s catch-all.
    """

    async def test_overflow_is_permanent_and_carries_the_specific_code(
        self, fast_retry, monkeypatch
    ):
        llm = _make()

        async def fake(self, *a, **k):
            raise _api_error(openai.BadRequestError, _MSG_CONTEXT_LENGTH, 400)

        monkeypatch.setattr(ChatOpenAI, "ainvoke", fake)
        with pytest.raises(LLMProviderRejectError) as ei:
            await llm.ainvoke("hi")

        err = ei.value
        assert err.error_code == 4006
        assert err.severity == ErrorSeverity.PERMANENT
        assert "context window" in str(err)
        # The operator's way out is in the hint, which is the whole point of
        # classifying it as permanent-instead-of-retryable.
        assert "/compact" in str(err)

    async def test_overflow_is_not_the_optimistic_class(self, fast_retry, monkeypatch):
        """The regression this class exists to catch."""
        llm = _make()

        async def fake(self, *a, **k):
            raise _api_error(openai.BadRequestError, _MSG_CONTEXT_LENGTH, 400)

        monkeypatch.setattr(ChatOpenAI, "ainvoke", fake)
        with pytest.raises(LLMProviderRejectError) as ei:
            await llm.ainvoke("hi")

        assert not isinstance(ei.value, LLMContextOverflowError)
        assert is_recoverable(ei.value) is False, (
            "an overflow was marked RECOVERABLE. Nothing consumes that tier "
            "(is_recoverable has no caller in src/ and retry_if_transient "
            "refuses it), so this buys no recovery — it only downgrades the "
            "CLI exit code from 4006 to 4001. Re-argue design.md D5a first."
        )

    async def test_the_snake_case_variant_is_classified_the_same_way(
        self, fast_retry, monkeypatch
    ):
        """Both AND-groups of the context row must land on the same verdict —
        the hint test covers the wording for one variant only."""
        llm = _make()
        variant = "Error code: 400 - prompt is too long: context_length_exceeded"

        async def fake(self, *a, **k):
            raise _api_error(openai.BadRequestError, variant, 400)

        monkeypatch.setattr(ChatOpenAI, "ainvoke", fake)
        with pytest.raises(LLMProviderRejectError) as ei:
            await llm.ainvoke("hi")

        assert ei.value.error_code == 4006
        assert ei.value.severity == ErrorSeverity.PERMANENT

    async def test_overflow_does_not_spend_the_retry_budget(
        self, fast_retry, monkeypatch
    ):
        """The point of the verdict, measured: a 3-retry budget must not be
        burned re-sending a payload that cannot shrink by itself."""
        llm = _make()
        calls = {"n": 0}

        async def fake(self, *a, **k):
            calls["n"] += 1
            raise _api_error(openai.BadRequestError, _MSG_CONTEXT_LENGTH, 400)

        monkeypatch.setattr(ChatOpenAI, "ainvoke", fake)
        with pytest.raises(LLMProviderRejectError):
            await llm.ainvoke("hi")

        assert calls["n"] == 1

    async def test_original_provider_error_stays_reachable(
        self, fast_retry, monkeypatch
    ):
        """The window size and token counts live in the provider body, which is
        what an operator needs to pick a bigger model — the hint replaces the
        message, so ``__cause__`` is the only route back to it."""
        llm = _make()

        async def fake(self, *a, **k):
            raise _api_error(openai.BadRequestError, _MSG_CONTEXT_LENGTH, 400)

        monkeypatch.setattr(ChatOpenAI, "ainvoke", fake)
        with pytest.raises(LLMProviderRejectError) as ei:
            await llm.ainvoke("hi")

        assert isinstance(ei.value.__cause__, openai.BadRequestError)
        assert "131072" in str(ei.value.__cause__)


# ---------------------------------------------------------------------------
# Layer 1 — pre-send integrity gate wiring
# ---------------------------------------------------------------------------


def _ai(tool_call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{
            "name": "kubectl_read",
            "args": {"command": "get pods"},
            "id": tool_call_id,
            "type": "tool_call",
        }],
    )


def _tool(tool_call_id: str, content: str = "result") -> ToolMessage:
    return ToolMessage(content=content, tool_call_id=tool_call_id)


def _capture_input(store: dict):
    """An (a)invoke stub that records what actually reached the provider."""

    async def capture(self, *a, **k):
        store["input"] = a[0] if a else k.get("input")
        return "ok"

    return capture


class TestSanitizeGateWiring:
    """The gate must sit in front of the retry loop on both entry shapes.

    ``sanitize_tool_pairing`` itself is exhaustively tested in
    tests/test_utils/test_message_integrity.py; these tests pin only that the
    choke point really invokes it and really forwards the result.
    """

    async def test_orphan_dropped_before_reaching_provider(self, fast_retry, monkeypatch):
        """An orphan ToolMessage must never reach the provider — a strict one
        answers with a 400 (#1344 signature #2) and the turn dies."""
        llm = _make()
        seen: dict = {}
        monkeypatch.setattr(ChatOpenAI, "ainvoke", _capture_input(seen))

        msgs = [
            _ai("call_1"),
            _tool("call_1", "paired result"),
            _tool("call_vanished", "orphan left behind by a compaction boundary"),
            HumanMessage(content="next turn"),
        ]
        assert await llm.ainvoke(msgs) == "ok"

        sent = seen["input"]
        assert len(sent) == 3
        assert [m.tool_call_id for m in sent if isinstance(m, ToolMessage)] == ["call_1"]
        assert isinstance(sent[-1], HumanMessage)  # ordering preserved

    async def test_caller_list_is_not_mutated_in_place(self, fast_retry, monkeypatch):
        """The caller's list IS ``state.messages``. Mutating it would rewrite
        graph state from inside an LLM call, invisibly and uncheckpointed."""
        llm = _make()
        seen: dict = {}
        monkeypatch.setattr(ChatOpenAI, "ainvoke", _capture_input(seen))

        msgs = [_tool("call_vanished", "orphan")]
        await llm.ainvoke(msgs)

        assert len(msgs) == 1  # caller's list untouched
        assert seen["input"] is not msgs
        assert seen["input"] == []

    async def test_kwargs_input_shape_is_gated_too(self, fast_retry, monkeypatch):
        """``ainvoke(input=[...])`` is the keyword shape; gating only the
        positional one would leave a wide-open bypass."""
        llm = _make()
        seen: dict = {}
        monkeypatch.setattr(ChatOpenAI, "ainvoke", _capture_input(seen))

        await llm.ainvoke(input=[_tool("call_vanished", "orphan"), HumanMessage(content="hi")])

        assert len(seen["input"]) == 1
        assert isinstance(seen["input"][0], HumanMessage)

    async def test_clean_list_passes_through_by_identity(self, fast_retry, monkeypatch):
        """Zero-copy on the happy path: with no orphan the SAME list object is
        forwarded, so the gate costs nothing on every normal call."""
        llm = _make()
        seen: dict = {}
        monkeypatch.setattr(ChatOpenAI, "ainvoke", _capture_input(seen))

        msgs = [_ai("call_1"), _tool("call_1"), HumanMessage(content="hi")]
        await llm.ainvoke(msgs)

        assert seen["input"] is msgs

    async def test_string_input_is_not_treated_as_message_list(self, fast_retry, monkeypatch):
        llm = _make()
        seen: dict = {}
        monkeypatch.setattr(ChatOpenAI, "ainvoke", _capture_input(seen))

        await llm.ainvoke("plain prompt")

        assert seen["input"] == "plain prompt"

    async def test_gate_runs_once_not_per_retry_attempt(self, fast_retry, monkeypatch):
        """The gate is OUTSIDE the retry loop: the message list cannot change
        between attempts, so re-sanitizing would be pure waste — and if it ever
        ran per attempt, a rebuild would hand each attempt a different object."""
        llm = _make()
        calls = {"n": 0}
        seen: list = []

        async def fake(self, *a, **k):
            calls["n"] += 1
            seen.append(a[0] if a else k.get("input"))
            if calls["n"] < 3:
                raise httpx.ReadError("connection reset")
            return "ok"

        monkeypatch.setattr(ChatOpenAI, "ainvoke", fake)
        msgs = [_ai("call_1"), _tool("call_1"), _tool("call_vanished", "orphan")]

        await llm.ainvoke(msgs)

        assert calls["n"] == 3
        assert all(s is seen[0] for s in seen)  # one sanitized list, reused
        assert all(len(s) == 2 for s in seen)

    async def test_retry_still_works_on_a_sanitized_list(self, fast_retry, monkeypatch):
        """The two mechanisms must compose: sanitize first, then retry the
        transient failure — a gate that broke retry would be a worse bug than
        the orphan it removes."""
        llm = _make()
        calls = {"n": 0}

        async def fake(self, *a, **k):
            calls["n"] += 1
            if calls["n"] < 2:
                raise httpx.ReadError("connection reset")
            return "ok"

        monkeypatch.setattr(ChatOpenAI, "ainvoke", fake)
        msgs = [_ai("call_1"), _tool("call_1"), _tool("call_vanished", "orphan")]

        assert await llm.ainvoke(msgs) == "ok"
        assert calls["n"] == 2

    def test_sync_invoke_gate(self, fast_retry, monkeypatch):
        """``invoke`` carries the same two-pass gate as ``ainvoke``. The
        mismatched orphan is dropped by the forward pass, which leaves call_1
        dangling, and the reverse pass then answers it — so the sync path ships
        a fully-paired sequence instead of a dangling caller a strict provider
        would 400 on.
        """
        llm = _make()
        seen: dict = {}

        def capture(self, *a, **k):
            seen["input"] = a[0] if a else k.get("input")
            return "ok"

        monkeypatch.setattr(ChatOpenAI, "invoke", capture)
        msgs = [_ai("call_1"), _tool("call_vanished", "orphan")]

        llm.invoke(msgs)

        sent = seen["input"]
        assert sent[0] == _ai("call_1"), "the caller is untouched"
        # the orphan is gone, and the caller it left dangling is now answered
        assert [m.tool_call_id for m in sent if isinstance(m, ToolMessage)] == ["call_1"]
        assert isinstance(sent[-1], ToolMessage)
        assert "Not executed" in sent[-1].content


# ---------------------------------------------------------------------------
# Layer 1, reverse half — dangling caller answered at the send gate
# ---------------------------------------------------------------------------


class TestDanglingCallerAnsweredAtSendGate:
    """The reverse half of the gate, end to end through ``ResilientChatOpenAI``.

    ``answer_dangling_tool_calls`` is unit-tested in
    tests/test_utils/test_message_integrity.py; these pin that the send path
    really runs it AFTER the forward gate and really forwards the answered
    sequence to the provider — the shape the execute-loop short-circuit
    (budget / wall-clock / error routed to the verifier before phase2_tools
    ran) actually produces.
    """

    async def test_dangling_caller_is_answered_before_reaching_provider(
        self, fast_retry, monkeypatch
    ):
        """The model asked to run a tool, the loop short-circuited to the
        verifier, and no result was ever produced. A strict provider rejects
        that dangling tool_call (#1344 signature #2, reverse direction), so the
        gate answers it with an honest 'not executed' note carrying a stable id.
        """
        llm = _make()
        seen: dict = {}
        monkeypatch.setattr(ChatOpenAI, "ainvoke", _capture_input(seen))

        msgs = [HumanMessage(content="inject cpu"), _ai("call_1")]
        assert await llm.ainvoke(msgs) == "ok"

        sent = seen["input"]
        assert sent[:2] == msgs, "the caller is untouched; the answer follows it"
        assert isinstance(sent[-1], ToolMessage)
        assert sent[-1].tool_call_id == "call_1"
        assert "Not executed" in sent[-1].content
        assert sent[-1].id == "synthetic:unanswered:call_1"

    async def test_answer_lands_beside_its_caller_not_past_the_verifier_trailer(
        self, fast_retry, monkeypatch
    ):
        """The REAL verifier send shape, end to end through the gate. The
        dangling caller is last in ``state``, but ``_build_layer2_messages``
        appends the synthetic baseline pair and a Human instruction AFTER it
        before sending. The answer must be inserted beside its caller — NOT at
        the very end past the Human — or a strict provider still rejects the
        request (caller immediately followed by another assistant; tool response
        stranded after a Human message). This is the placement the send-side net
        exists to get right.
        """
        llm = _make()
        seen: dict = {}
        monkeypatch.setattr(ChatOpenAI, "ainvoke", _capture_input(seen))

        msgs = [
            HumanMessage(content="inject cpu"),
            _ai("call_1"),                        # dangling: loop short-circuited
            _ai("baseline"), _tool("baseline", "real baseline"),
            HumanMessage(content="verify now"),   # verifier trailer (last)
        ]
        assert await llm.ainvoke(msgs) == "ok"

        sent = seen["input"]
        # [Human, AI(call_1), Tool(call_1), AI(baseline), Tool(baseline), Human]
        assert sent[1] is msgs[1], "caller untouched, still at index 1"
        assert isinstance(sent[2], ToolMessage) and sent[2].tool_call_id == "call_1"
        assert sent[2].id == "synthetic:unanswered:call_1"
        assert "Not executed" in sent[2].content
        # B: the model-facing text stays neutral — no protocol-mechanics leak
        # (the earlier draft said "the ... pairing the provider requires", which
        # is internal plumbing the model does not need). Negative constraint, so
        # it locks the intent without breaking on legitimate rewording.
        assert "provider" not in sent[2].content
        assert "placeholder" not in sent[2].content
        assert sent[3] is msgs[2] and sent[4] is msgs[3], "baseline pair intact, in order"
        assert sent[5] is msgs[4] and isinstance(sent[-1], HumanMessage), \
            "the answer was NOT appended past the verifier trailer"

    async def test_wholesale_short_circuited_multi_call_answers_in_original_order(
        self, fast_retry, monkeypatch
    ):
        """The primary reachable shape end to end: a parallel-tool turn asks for
        [c3, c1, c2] in ONE AIMessage, the loop short-circuits wholesale so all
        three dangle, and the verifier trailer follows. Through the REAL gate
        the three answers must land adjacent to the caller IN THE ORDER THE
        MODEL ASKED (c3, c1, c2 — not sorted c1, c2, c3), then the baseline
        pair, then the Human instruction last.
        """
        llm = _make()
        seen: dict = {}
        monkeypatch.setattr(ChatOpenAI, "ainvoke", _capture_input(seen))

        caller = AIMessage(content="", tool_calls=[
            {"name": "kubectl_read", "args": {"command": "get pods"},
             "id": "c3", "type": "tool_call"},
            {"name": "kubectl_read", "args": {"command": "get nodes"},
             "id": "c1", "type": "tool_call"},
            {"name": "kubectl_read", "args": {"command": "df -h"},
             "id": "c2", "type": "tool_call"},
        ])
        msgs = [
            HumanMessage(content="inject cpu"),
            caller,                               # 3 dangling: loop short-circuited
            _ai("baseline"), _tool("baseline", "real baseline"),
            HumanMessage(content="verify now"),   # verifier trailer (last)
        ]
        assert await llm.ainvoke(msgs) == "ok"

        sent = seen["input"]
        # [Human, caller, Tool(c3), Tool(c1), Tool(c2), AI(baseline), Tool(baseline), Human]
        assert [m.tool_call_id for m in sent[2:5]] == ["c3", "c1", "c2"], \
            "all three answered adjacent to the caller, in ORIGINAL order not sorted"
        assert all(isinstance(m, ToolMessage) for m in sent[2:5])
        assert sent[5] is msgs[2] and sent[6] is msgs[3], \
            "baseline pair intact, after the three answers"
        assert sent[7] is msgs[4] and isinstance(sent[-1], HumanMessage), \
            "the verifier Human instruction is still last"

    async def test_fully_answered_list_still_passes_through_by_identity(
        self, fast_retry, monkeypatch
    ):
        """The reverse gate must not perturb the happy path: a complete
        conversation is forwarded as the SAME object, so it costs nothing on
        every normal call."""
        llm = _make()
        seen: dict = {}
        monkeypatch.setattr(ChatOpenAI, "ainvoke", _capture_input(seen))

        msgs = [_ai("call_1"), _tool("call_1", "real result"), HumanMessage(content="next")]
        await llm.ainvoke(msgs)

        assert seen["input"] is msgs

    async def test_answered_list_is_stable_across_retries(
        self, fast_retry, monkeypatch
    ):
        """The gate runs once, outside the retry loop, and the derived id makes
        the placeholder byte-identical — so a retried send never stacks a second
        answer on the same call."""
        llm = _make()
        calls = {"n": 0}
        seen: list = []

        async def fake(self, *a, **k):
            calls["n"] += 1
            seen.append(a[0] if a else k.get("input"))
            if calls["n"] < 3:
                raise httpx.ReadError("connection reset")
            return "ok"

        monkeypatch.setattr(ChatOpenAI, "ainvoke", fake)
        await llm.ainvoke([_ai("call_1")])

        assert calls["n"] == 3
        assert all(s is seen[0] for s in seen), "one answered list, reused per attempt"
        assert all(len(s) == 2 for s in seen)
