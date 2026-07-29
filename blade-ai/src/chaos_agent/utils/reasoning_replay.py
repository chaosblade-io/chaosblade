"""Single source of truth for what reasoning_content actually goes on the wire.

Two subsystems must agree on this, and they live on opposite sides of the
codebase:

  * ``agent.factory``'s outbound monkey-patch DECIDES whether a message's
    thinking trace is replayed to the model, and how much of it;
  * ``memory.tokens.count_tokens_messages`` must COUNT exactly that text,
    because every context decision (auto-compact WARNING/ERROR/BLOCKING, the
    strip-vs-compress choice, per-message keep/drop) is derived from its result.

Keeping the rules in one place is not tidiness — a divergence is silent and
expensive in both directions:

  * counting text that is NOT sent (e.g. thinking from a different model, which
    the provenance guard drops) over-reports and triggers needless compaction;
  * sending text that is NOT counted under-reports. That was the state before
    this module existed: a thinking model leaves ``content`` empty and puts
    everything in ``reasoning_content``, so a 10-turn history measured 83 tokens
    while the wire payload was 7,547 — a 91× gap. Compaction never fired, and
    the failure only surfaced as a context-length error from the API.

The strip path in ``PreReasoningHook`` makes the under-report worse rather than
better: it truncates ``content`` via ``model_copy`` and leaves
``additional_kwargs`` untouched, so it cannot reduce this payload at all. Only
the compress path (``RemoveMessage`` + summary) drops it, and the choice between
them is made on the token count computed here.

Scope: the outbound patch only injects ``reasoning_content`` for the Chat
Completions shape (the Responses API represents reasoning differently), which is
the only shape this project uses. If a caller ever switches to the Responses
API, this module and that patch must move together.
"""

from __future__ import annotations

from chaos_agent.config.settings import settings

# The provenance marker lives in the KEY, not the value, because streaming
# merges ``additional_kwargs`` with ``merge_dicts``, which CONCATENATES string
# values: a per-chunk ``{"_reasoning_model": "qwen"}`` would accumulate into
# "qwenqwenqwen...". A constant ``True`` under a model-specific key is
# idempotent under that same merge.
REASONING_MODEL_KEY_PREFIX = "_reasoning_model:"

# ``BaseMessage.type`` for an assistant message. Compared by value instead of
# ``isinstance(msg, AIMessage)`` so this module needs no langchain import and
# stays usable from the token counter's hot path.
_ASSISTANT_TYPE = "ai"


def reasoning_model_key(model_name: str) -> str:
    """Key under which a message records WHICH model produced its thinking."""
    return f"{REASONING_MODEL_KEY_PREFIX}{model_name}"


def replayed_reasoning(message) -> str:
    """The reasoning text this message will actually put on the wire.

    Returns ``""`` when nothing is replayed — which is also the answer the token
    counter needs, so both callers can use this one function unchanged.

    The conditions, in the order the outbound patch applies them:

    1. assistant messages only — the field has no meaning on a user/tool turn;
    2. a non-blank string ``reasoning_content`` — sending ``""`` is unverified
       behaviour, so an empty trace is not replayed;
    3. provenance match — a checkpointer-restored session may carry thinking
       produced by a DIFFERENT model (the operator switched models between
       runs). Replaying it cross-model is undefined. A MISSING key counts as a
       mismatch, which also excludes legacy messages recorded before the marker
       existed;
    4. tail truncation at ``reasoning_replay_max_chars`` — the TAIL, because a
       thinking trace ends with its conclusion. The limit is a guard against one
       pathological message, not a routine budget: normal traces run
       100–2500 chars, and the context budget itself is managed by the
       compaction system (which needs this function to see the payload at all).
    """
    if getattr(message, "type", "") != _ASSISTANT_TYPE:
        return ""
    akw = getattr(message, "additional_kwargs", None)
    if not isinstance(akw, dict):
        return ""
    reasoning = akw.get("reasoning_content")
    if not isinstance(reasoning, str) or not reasoning.strip():
        return ""
    if not akw.get(reasoning_model_key(settings.model_name)):
        return ""
    limit = settings.reasoning_replay_max_chars
    if limit > 0 and len(reasoning) > limit:
        return reasoning[-limit:]
    return reasoning


__all__ = [
    "REASONING_MODEL_KEY_PREFIX",
    "reasoning_model_key",
    "replayed_reasoning",
]
