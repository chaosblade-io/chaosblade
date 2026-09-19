"""Single choke point for LLM calls: integrity gate + bounded retry + error classification.

Every LLM in the agent is built by ``make_llm`` (factory.py) as a
``ResilientChatOpenAI``, so this one class is the single place where an LLM
call's pre-send validation and failure handling live. Three responsibilities:

1. **Pre-send integrity gate** — ``sanitize_tool_pairing``
   (utils/message_integrity.py) runs on the input message list BEFORE the
   retry loop, so no orphan ToolMessage ever reaches a provider. Lenient
   providers (DashScope) tolerate pairing violations; strict ones (DeepSeek)
   reject them with a 400 (chaosblade-io/chaosblade#1344 signature #2). The
   gate makes the invariant mechanical instead of relying on every message
   producer (compaction, synthetic builders, hint injection) being correct.

   CONSTRAINT: the gate covers ``ainvoke``/``invoke`` only. ``astream`` is
   deliberately NOT wrapped (retrying a partially-yielded stream would
   duplicate tokens), and the agent never calls ``llm.astream`` directly —
   token streaming to the UI comes from the graph's ``astream_events``
   tapping the ``on_chat_model_stream`` events emitted *inside* ``ainvoke``.
   Any future direct ``llm.astream`` call site MUST add the same gate first.

2. **Bounded retry** — on ANY other exception from ``(a)invoke`` it retries
   with exponential backoff up to ``settings.retry_max_retries`` times, then
   re-raises the ORIGINAL error so the turn boundary can surface its content
   and end gracefully (no traceback crash). The retry is deliberately GENERIC
   for the non-deterministic classes — provider error taxonomies are large
   and volatile, so we do not enumerate/branch per error type; a couple of
   cheap backed-off retries clear the common transient cases (rate limit,
   mid-stream drop, brief 5xx).

3. **Deterministic-rejection classification** — HTTP 4xx is normally a
   VERDICT on the request, not an incident: replaying the identical request
   will be rejected identically, so retrying only burns the backoff budget
   and delays the user's error. These fail immediately, wrapped as
   ``LLMProviderRejectError`` (PERMANENT, code 4006) with an actionable hint
   mapped from the provider's error signature; ``__cause__`` keeps the
   original ``openai.APIStatusError``.

   Three 4xx codes are exempt and keep the generic retry —
   ``_RETRIABLE_4XX_STATUSES`` = {408, 409, 429}, matching the openai SDK's
   own ``_should_retry`` verdicts below 500. 408/409 are timeouts and lock
   contention: transient by definition, so classifying them as a verdict
   would contradict upstream and lose a drill to a gateway hiccup.

The other deliberate exclusion is a genuine response/read timeout — it means
the model was simply too slow, so retrying just doubles an already-long wait
(``llm_read_timeout`` defaults to 600s). Those re-raise immediately.

Notes
-----
* Because ``ainvoke`` returns a single aggregated message (nothing is handed
  to the caller until it completes), simply re-running it on failure is safe —
  the returned value always comes from the final, successful attempt. The
  sanitize gate runs once per call (not per attempt): the message list does
  not change between retries.
* ``asyncio.CancelledError`` / ``KeyboardInterrupt`` subclass ``BaseException``,
  not ``Exception``, so user cancellation is never swallowed by the retry.
* Tool binding (``bind_tools`` / ``bind``) returns a ``RunnableBinding`` whose
  ``ainvoke`` delegates back to this instance's wrapped method, so both the
  integrity gate and the retry survive binding.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from functools import lru_cache

from langchain_core.messages import ToolMessage
from langchain_openai import ChatOpenAI

from chaos_agent.config.settings import settings
from chaos_agent.errors import LLMProviderRejectError
from chaos_agent.utils.message_integrity import (
    answer_dangling_tool_calls,
    sanitize_tool_pairing,
)

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _no_retry_exc_types() -> tuple[type[BaseException], ...]:
    """Timeouts that mean 'the model was genuinely slow' — retrying just
    doubles an already-long wait, so these re-raise immediately instead of
    going through the generic retry.

    The 600s read timeout surfaces as ``openai.APITimeoutError`` (the SDK wraps
    ``httpx`` timeouts); ``httpx.ReadTimeout`` covers a raw mid-stream read.
    Resolved lazily + cached so the imports happen once.
    """
    excs: list[type[BaseException]] = []
    try:
        import httpx

        excs.append(httpx.ReadTimeout)
    except Exception:  # pragma: no cover - httpx always present via langchain
        pass
    try:
        import openai

        excs.append(openai.APITimeoutError)
    except Exception:  # pragma: no cover - openai always present
        pass
    return tuple(excs)


def _backoff_delay(attempt: int) -> float:
    """Seconds to wait before the ``attempt``-th retry (0-indexed).

    Full-jitter exponential backoff:
    ``uniform(0, min(base * mult**attempt, cap))``. Reuses the shared
    ``retry_*`` settings so operators tune a single knob set.
    """
    ceiling = min(
        settings.retry_base_delay * (settings.retry_exponential_base ** attempt),
        settings.retry_max_delay,
    )
    if settings.retry_jitter:
        return random.uniform(0, ceiling)
    return ceiling


# ---------------------------------------------------------------------------
# Pre-send integrity gate (Layer 1)
# ---------------------------------------------------------------------------


def _not_executed_answer(tool_call_id: str) -> ToolMessage:
    """Honest placeholder answer for a tool_call that reached the send gate
    with no result — what ``answer_dangling_tool_calls`` injects for the
    reverse-pairing backstop.

    Shares the ``"Not executed:"`` opener and the honesty of
    ``execute_loop._answer_replan_tool_calls`` — the model is never shown a
    fabricated result — but stays NEUTRAL about *why*: this gate is generic (it
    does not know the phase), so it states only that the call did not run and
    has no result. It deliberately avoids protocol mechanics ("this placeholder
    keeps the tool-call / answer pairing the provider requires"): that is
    internal plumbing the model does not need and would only read as noise, and
    it is self-referential in a way the domain-worded precedent is not. The
    stable, derived id keeps repeated sends of the same dangling call
    byte-identical.
    """
    return ToolMessage(
        content=(
            "Not executed: this tool call did not run, so no result is "
            "available for it."
        ),
        tool_call_id=tool_call_id,
        name="tool",
        id=f"synthetic:unanswered:{tool_call_id}",
    )


def _sanitize_input(args: tuple, kwargs: dict) -> tuple[tuple, dict]:
    """Run the tool-pairing gate on the call input when it is a message list.

    Two passes, both zero-copy on a clean sequence: ``sanitize_tool_pairing``
    enforces the forward rule (every result has a preceding caller), then
    ``answer_dangling_tool_calls`` enforces the reverse one (every caller has
    an answer), so a short-circuited execute loop — budget / wall-clock / error
    routed to the verifier before ``phase2_tools`` ran — cannot ship a dangling
    tool_call to a strict provider.

    Non-list inputs (plain string prompts, PromptValue objects) pass through
    untouched. Returns possibly-rebuilt ``(args, kwargs)`` — the original
    containers are never mutated in place.
    """
    candidate = args[0] if args else kwargs.get("input")
    if not isinstance(candidate, list):
        return args, kwargs
    sanitized = sanitize_tool_pairing(candidate)
    sanitized = answer_dangling_tool_calls(sanitized, _not_executed_answer)
    if sanitized is candidate:
        return args, kwargs
    if args:
        return (sanitized, *args[1:]), kwargs
    return args, {**kwargs, "input": sanitized}


# ---------------------------------------------------------------------------
# Deterministic-rejection classification (Layer 3)
# ---------------------------------------------------------------------------

#: Provider error signature → actionable hint, checked in order (first match
#: wins). Wording verified against the verbatim provider messages in
#: chaosblade-io/chaosblade#1344. Keep signatures lowercase-matched.
#:
#: Each signature is a DISJUNCTION (OR) of CONJUNCTIONS (AND) of phrases:
#: ``((("a", "b"), ("c",)), hint)`` matches when the lowered error text
#: contains BOTH "a" AND "b", or contains "c". The AND half is what makes
#: a row trustworthy — a single common word ("tool_calls", "thinking",
#: "context length") shows up in many unrelated 400s, and matching on any
#: one phrase alone (the previous semantics) handed the user the wrong
#: remedy: everything from a stray "'thinking' is not supported here" to
#: any message merely MENTIONING tool_calls used to land on the pairing
#: row. Every AND group below is anchored to a measured (or, for the last
#: row, clearly-marked inferred) provider message; wordings with no anchor
#: — "responses api", bare "thinking" — are dropped rather than guessed:
#: missing a match degrades safely to the generic fallback (which still
#: echoes the provider's own text), a false match confidently prescribes
#: the wrong fix.
_REJECT_HINT_SIGNATURES: tuple[tuple[tuple[tuple[str, ...], ...], str], ...] = (
    (
        (("function_call_output", "previous_response_id"),),
        "the gateway routed this request to OpenAI's stateful Responses API, "
        "which is incompatible with chat/completions tool-call continuation — "
        "check the gateway channel config for this model (issue #1344 signature #1)",
    ),
    (
        (("must be a response to a preceding", "tool_calls"),),
        "the provider rejected the tool_call/ToolMessage pairing of the request. "
        "The pre-send integrity gate should have prevented this — if it fires, "
        "report it: the sanitizer missed an orphan source",
    ),
    (
        (("reasoning_content",),),
        "the provider requires the model's prior reasoning_content echoed back "
        "(thinking-mode contract) — the replay patches in agent/factory.py "
        "should cover this; check they are active for this endpoint",
    ),
    (
        (("enable_thinking", "unsupported parameter"),),
        "a thinking-mode parameter was rejected by this endpoint — verify "
        "llm_thinking_format matches the provider dialect (auto-detection "
        "ships nothing on unknown endpoints)",
    ),
    (
        (
            ("context length", "maximum context"),
            ("context_length_exceeded",),
        ),
        "the conversation exceeds the model's context window — trigger "
        "compaction (/compact) or switch to a larger-window model",
    ),
    # LAST on purpose: these groups are specific enough not to steal another
    # signature's match, and this row must not outrank a more precise one.
    #
    # Reached when the pre-send integrity gate dropped EVERY message (see
    # sanitize_tool_pairing's "emptied the ENTIRE message list" ERROR log,
    # which carries the authoritative cause). The groups are enumerated
    # from how JSON-schema request validation is normally phrased rather
    # than measured against each provider. The AND structure IS the
    # narrowness this row always claimed but, under the old any-phrase
    # semantics, never had: every group requires the complaint to name
    # ``messages`` (or the "at least one message" phrasing that only ever
    # appears in that context) alongside the emptiness wording, so an
    # unrelated "empty array" complaint about some other field falls
    # through to the generic fallback instead of being read as corrupted
    # local conversation state.
    (
        (
            ("messages", "empty array"),
            ("messages", "must not be empty"),
            ("messages", "is required"),
            ("at least one message",),
        ),
        "the request left with NO usable messages: the pre-send integrity "
        "gate dropped every one of them as an unpaired tool result. This is "
        "corrupted local conversation state, not a provider fault — start a "
        "fresh session (/clear) instead of retrying, and check the "
        "'emptied the ENTIRE message list' log line for what was dropped",
    ),
)

#: Cap for the raw provider message echoed into the generic fallback hint.
_RAW_MESSAGE_CHARS = 300

#: 4xx codes that are NOT deterministic verdicts — kept aligned with the
#: openai SDK's own ``_should_retry`` (``_base_client.py``), which returns
#: True for exactly these three below 500:
#:
#:   408 "Retry on request timeouts."  — the server gave up waiting; a
#:       re-send is a fresh request, not a replay of a rejected one.
#:   409 "Retry on lock timeouts."     — contention, clears on its own.
#:   429 rate limit                    — the canonical transient.
#:
#: Treating 408/409 as deterministic would contradict upstream AND cost real
#: attempts: ``settings.llm_max_retries`` defaults to 1, so the SDK retries
#: once and then hands the error to this layer. Failing fast here turns a
#: transient gateway timeout into a lost drill, and the wrapped
#: ``LLMProviderRejectError`` message ("Request timed out") is then read by
#: ``l4/error_mapping`` as ``AGENT_TIMEOUT`` / ``recoverable=True`` — the
#: platform would heal-rerun the whole task for an error this layer just
#: declared unrecoverable.
_RETRIABLE_4XX_STATUSES = frozenset({408, 409, 429})


def _is_provider_rejection(exc: Exception) -> bool:
    """True for deterministic HTTP 4xx verdicts.

    Duck-typed on ``status_code`` so the openai import stays lazy (same
    pattern as ``_no_retry_exc_types``). ``openai.APITimeoutError`` has no
    ``status_code`` and is already handled by the no-retry branch.

    ``_RETRIABLE_4XX_STATUSES`` are excluded: the openai SDK's own
    ``_should_retry`` returns True for them, so calling them deterministic
    would contradict upstream. See the constant for why that matters here.
    """
    status = getattr(exc, "status_code", None)
    return (
        isinstance(status, int)
        and 400 <= status < 500
        and status not in _RETRIABLE_4XX_STATUSES
    )


def _provider_reject_error(exc: Exception) -> LLMProviderRejectError:
    """Map the provider's error signature to an actionable 4006 error.

    A row matches when ANY of its AND-groups has EVERY phrase present in
    the lowered error text (see ``_REJECT_HINT_SIGNATURES`` for why the
    conjunction matters).
    """
    raw = str(exc)
    lowered = raw.lower()
    hint = ""
    for signature, text in _REJECT_HINT_SIGNATURES:
        if any(
            all(phrase in lowered for phrase in group)
            for group in signature
        ):
            hint = text
            break
    status = getattr(exc, "status_code", "?")
    if not hint:
        preview = raw if len(raw) <= _RAW_MESSAGE_CHARS else raw[:_RAW_MESSAGE_CHARS] + "..."
        hint = f"provider rejected the request (HTTP {status}): {preview}"
    return LLMProviderRejectError(f"LLM provider rejection (HTTP {status}): {hint}")


class ResilientChatOpenAI(ChatOpenAI):
    """``ChatOpenAI`` with a pre-send integrity gate, bounded retry, and
    deterministic-rejection classification (see module docstring)."""

    async def ainvoke(self, *args, **kwargs):  # type: ignore[override]
        args, kwargs = _sanitize_input(args, kwargs)
        max_retries = max(0, int(settings.retry_max_retries))
        attempt = 0
        while True:
            try:
                return await super().ainvoke(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - generic by design
                # Slow-model response/read timeout: retrying only doubles an
                # already-long wait — surface it immediately.
                if isinstance(exc, _no_retry_exc_types()):
                    logger.warning(
                        "LLM ainvoke timed out (%s); not retried (model too slow).",
                        type(exc).__name__,
                    )
                    raise
                # Deterministic 4xx verdict: the identical request will be
                # rejected identically — fail fast with an actionable hint.
                if _is_provider_rejection(exc):
                    logger.warning(
                        "LLM ainvoke rejected by provider (%s, HTTP %s); "
                        "not retried (deterministic rejection).",
                        type(exc).__name__, getattr(exc, "status_code", "?"),
                    )
                    raise _provider_reject_error(exc) from exc
                if attempt >= max_retries:
                    logger.warning(
                        "LLM ainvoke failed (%s); retries exhausted after "
                        "%d attempt(s), re-raising.",
                        type(exc).__name__, attempt + 1,
                    )
                    raise
                delay = _backoff_delay(attempt)
                logger.warning(
                    "LLM ainvoke failed (%s); retry %d/%d in %.2fs.",
                    type(exc).__name__, attempt + 1, max_retries, delay,
                )
                await asyncio.sleep(delay)
                attempt += 1

    def invoke(self, *args, **kwargs):  # type: ignore[override]
        args, kwargs = _sanitize_input(args, kwargs)
        max_retries = max(0, int(settings.retry_max_retries))
        attempt = 0
        while True:
            try:
                return super().invoke(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - generic by design
                # Slow-model response/read timeout: retrying only doubles an
                # already-long wait — surface it immediately.
                if isinstance(exc, _no_retry_exc_types()):
                    logger.warning(
                        "LLM invoke timed out (%s); not retried (model too slow).",
                        type(exc).__name__,
                    )
                    raise
                # Deterministic 4xx verdict: the identical request will be
                # rejected identically — fail fast with an actionable hint.
                if _is_provider_rejection(exc):
                    logger.warning(
                        "LLM invoke rejected by provider (%s, HTTP %s); "
                        "not retried (deterministic rejection).",
                        type(exc).__name__, getattr(exc, "status_code", "?"),
                    )
                    raise _provider_reject_error(exc) from exc
                if attempt >= max_retries:
                    logger.warning(
                        "LLM invoke failed (%s); retries exhausted after "
                        "%d attempt(s), re-raising.",
                        type(exc).__name__, attempt + 1,
                    )
                    raise
                delay = _backoff_delay(attempt)
                logger.warning(
                    "LLM invoke failed (%s); retry %d/%d in %.2fs.",
                    type(exc).__name__, attempt + 1, max_retries, delay,
                )
                time.sleep(delay)
                attempt += 1
