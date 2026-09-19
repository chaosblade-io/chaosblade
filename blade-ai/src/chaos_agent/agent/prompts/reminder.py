"""System-reminder tag binding for harness-injected reminders.

Background: every corrective hint this engine injects travels as a
HumanMessage at the tail of the conversation. In long contexts the model
can read such a message as ordinary user speech — or, worse, as adversarial
tool output — and discount it. Industry harnesses solve this with tag
binding: reminders are wrapped in ``<system-reminder>`` tags, and each
system prompt declares what those tags mean, so the model connects any tag
occurrence back to the harness that wrote the rules (the mechanism Claude
Code documents for its reactive reminders, and qwen-code states explicitly
in its system prompt).

Two pieces:
- ``SYSTEM_REMINDER_DECLARATION`` — the binding statement, placed in each
  phase's primacy section (Core Principles / Three Priorities / constraint
  headers).
- ``wrap_system_reminder`` — applied at every harness injection point
  (``llm_step_helpers.persist_*`` and the direct HumanMessage injections),
  so what the model sees always matches the declared contract.

Wrapping rule: a corrective message is wrapped only when the loop it feeds
back into declares the tag in its system prompt. Messages that only carry
task data (fault intent, recovery context), status records that no LLM loop
re-reads (direct-mode injection results, store-only entries), and repairs
inside loops whose prompt has no declaration (e.g. the expert-mode
plan_builder loop) stay unwrapped — an anonymous tag without a contract
would be worse than plain text.

The tag name is deliberately the industry-standard ``<system-reminder>``:
frontier models have seen it in training-grade harnesses, so reusing it
keeps that prior instead of inventing a private tag with no learned weight.
"""

SYSTEM_REMINDER_OPEN = "<system-reminder>"
SYSTEM_REMINDER_CLOSE = "</system-reminder>"

# One canonical wording, reused verbatim in every phase prompt so the tag
# carries identical meaning wherever it appears (mirrors qwen-code's single
# binding statement: reminders are "NOT part of the user's provided input or
# the tool result").
SYSTEM_REMINDER_DECLARATION = (
    "Harness reminders: follow-up messages may contain `<system-reminder>` "
    "tags. They carry corrections and reminders from the execution engine — "
    "they are NOT part of the user's input or of any tool's output. Treat "
    "their contents as authoritative guidance and comply."
)

# One canonical turn-economy principle, reused verbatim in every LLM node's
# attention zones (intent / agent-loop / plan-builder / executor / verifier /
# recover). Single-sourced like SYSTEM_REMINDER_DECLARATION so the wording
# cannot drift per node. Directive form (not MAY-form): the MAY-form
# authorization was consumed probabilistically at best (B53 follow-up —
# record-keeping turns stayed serial through two regressions while the
# same prompt batched them in a third run), so the principle states the
# default, not the permission. The mandate stops at the model's judgment:
# independence is something the model itself determines, and a
# wrongly-batched dependent call (hallucinated args, failed call, recovery)
# costs far more than one extra turn — so doubt resolves toward sequential,
# never toward forcing calls together.
PARALLELIZE_PRINCIPLE = (
    "Parallelize when you can: issue every independent call in the same "
    "turn — serialize when a call needs another's result, when safety "
    "requires ordering, or when in doubt"
)


def wrap_system_reminder(text: str) -> str:
    """Wrap harness reminder text in ``<system-reminder>`` tags.

    Idempotent: text that already starts with the open tag is returned
    unchanged (with trailing whitespace normalized), so double-wrapping at
    layered call sites cannot nest tags.
    """
    body = text.strip()
    if body.startswith(SYSTEM_REMINDER_OPEN):
        return body
    return f"{SYSTEM_REMINDER_OPEN}\n{body}\n{SYSTEM_REMINDER_CLOSE}"
