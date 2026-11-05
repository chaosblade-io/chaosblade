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

# Companion fact to PARALLELIZE_PRINCIPLE, single-sourced here and rendered
# only in the nodes that end a phase with a progress ledger (planner /
# executor / verifier). Kept SEPARATE from PARALLELIZE_PRINCIPLE because that
# constant is pinned as the single canonical turn-economy directive
# (test_parallelize_principle_single_sourced_in_every_node); this is a
# distinct concern. Not a tool-schema note: the schema budget is load-bearing
# (test_tool_description_budget) and the fact is cross-cutting, so it lives in
# the budget-free system prompt.
#
# WHY IT IS NEEDED — census over the persisted task corpus (464 task JSONs
# under the memory dirs, 985 phase boundaries). Measured on the pair this fact
# governs: a turn whose ONLY tool call records progress, immediately followed
# by a turn carrying a phase-terminal call. Rate of that wasted independent
# turn, bucketed by when the prompt shipped (git log -S on this file): 92.7%
# with no turn-economy principle at all (n=41); 78.9% with
# PARALLELIZE_PRINCIPLE alone (n=279 — median 14.9s per wasted turn, 83 min of
# wall clock across the corpus); 0.0% once this fact shipped (n=8). So the
# principle alone demonstrably did NOT fix it, and the traces show why: the
# model applies the principle's own escape hatch correctly to a FALSE premise.
# Verbatim — "The guidance explicitly says '...serialize when...when safety
# requires ordering, or when in doubt'. Ledger ordering -> serialize."
# (inject-0eb4befa); "that's a wasted turn but safer for ledger integrity"
# (inject-3fe1d1f2). It knows the turn is wasted and pays it anyway, so a
# stronger exhortation to parallelize cannot help — the premise must go.
#
# That premise is ORDER-IMPLIES-TURNS (92% of the 220 pre-fix wasted turns):
# "A must land before B" is read as "A and B cannot share a turn", because
# nothing states that issue order within a turn is preserved. Fear of a race is
# NOT a separate population and this comment used to imply it was (quoting 22%):
# only 2 of the 220 reason from loss alone, while the other 46 loss mentions
# co-occur with the ordering premise — the model first decides "ordered, so
# separate turns", then cites possible loss to justify it. Hence the
# ORDER/RESULT distinction opening the text — that clause is the load-bearing
# one, and it is tool-agnostic.
#
# What was REMOVED, and a correction to what this comment previously claimed:
# the old text justified the guarantee with the ledger channel's reducer
# folding writes in emission order, and the old comment asserted that
# justification was load-bearing because inject-679f78b1 "quoted that reducer
# reasoning". Re-checked against that very task, the claim is false — across
# its AI messages reducer / emission order / ledger channel / fold appear ZERO
# times while the bare guarantee is quoted EIGHT times ("per Core Principles:
# same-turn update_progress batched BEFORE the terminal signal is safe"); its
# lone "by construction" hit is unrelated (a systemctl-mask argument).
# Systematic confirmation: 0/12 post-fix batched boundaries anywhere in the
# corpus mention a mechanism word.
#
# Mechanism talk was an AMPLIFIER of that premise, never its cause — and this
# comment previously called it the ORIGIN, which the census refutes: mechanism
# words appear in only 58 of the 220 wasted turns (26.4%), while the other 162
# (73.6%) serialize without mentioning any mechanism at all, so deleting the
# leak could not have reached them. ``finish_execution``'s tool description USED
# to say it "submits the terminal ledger delta the channel reducer applies", and
# some pre-fix traces do reason from exactly that sentence to "if
# finish_execution's terminal marker is processed before the progress update...
# I'll do them sequentially to be safe" (inject-f7a6fdc7). Removing it is
# therefore correct but AUXILIARY — it takes away one more reason to be afraid.
# THIS constant is the primary fix, because the fact it states is derivable from
# nowhere else: not from the turn-economy principle (which only licenses
# batching INDEPENDENT calls and never says ordered calls may share a turn), not
# from any tool description or schema. Without it the model's own best guess
# stays at "tools in a block are TYPICALLY executed in order" (inject-969f59d2)
# — a guess too weak to outweigh the principle's deliberate "when in doubt,
# serialize" hatch, which must stay because a wrongly-batched dependent call
# costs far more than one turn. Raising that confidence is the only lever, and
# that is what the first clause below does.
# Neither side may regress: the leak is gone from that description (see the
# "Mechanism vocabulary boundary" note in ``tools/progress.py``, which is why it
# must not be re-added), and no mechanism word survives in any model-visible
# text — verified across all 27 registered tool descriptions and all 5 rendered
# phase prompts.
#
# Ground truth for the surviving claim: inject-7df60e56 and inject-0e96bc79
# each issued [record, finish_execution] in ONE turn, and the persisted ledger
# log holds the model's entries immediately BEFORE the terminal marker (index
# 18 of 26; 15 of 30). Measured by ``len(text.split())``: 68 -> 66 words, 430
# -> 347 chars, zero tool names, zero mechanism names.
# 2026-10-29 scoping (inject-b3d0df6e, host端口占用案 recovery race): the verb
# was "take effect in the order you issue them" — a PHYSICAL-execution claim
# that is true only for state/record writes and false for concurrent external
# side effects. The live model batched [start-svc, wait, show-svc] in one turn,
# the observation completed BEFORE the start did (140ms vs 171ms — the batch
# runs concurrently), read stale "dead" state, and then reasoned verbatim "the
# two calls ran nearly simultaneously? No, they're ordered" — parroting this
# very sentence to REJECT the true concurrency hypothesis. "are recorded" keeps
# the exact reassurance the ledger case needs (record lands before close) while
# dropping the over-claim; the execution-concurrency half now lives in its own
# companion, CONCURRENT_BATCH_FACT below.
LEDGER_BATCH_FACT = (
    "Needing an ORDER is not needing a RESULT: within one turn your calls "
    "are recorded in the order you issue them, so a call that must land first "
    "does not need a turn of its own — only a call whose arguments depend on "
    "another's output does. Record progress and close the phase in the SAME "
    "turn, record first; closing does not discard what you recorded."
)

# Second companion to PARALLELIZE_PRINCIPLE, single-sourced here and rendered in
# the nodes that ISSUE an external action and OBSERVE its effect (executor /
# verifier / recover). Distinct concern from LEDGER_BATCH_FACT: that one is
# about the order state/record writes are COMBINED (safe to batch); this one is
# about the fact that the calls themselves run CONCURRENTLY, so issue order is
# NOT completion order for external effects. Kept out of tool schemas for the
# same reason as LEDGER_BATCH_FACT (cross-cutting; the schema budget is
# load-bearing) and out of the read-only planner/intent nodes (no external
# action whose effect a sibling could race to observe).
#
# WHY IT IS NEEDED — ground truth inject-b3d0df6e (host端口占用案, Step-8
# recovery): the model batched [reset-failed, start-svc, wait(4), show-svc,
# port-check, curl] in ONE turn, expecting the wait to hold the observation
# until the service was up. The batch ran concurrently — `show` completed at
# 140ms, `start` at 171ms, no 4s gap — so `show` sampled a pre-start "dead"
# state and curl saw nothing listening. Nothing in the prompt stated the
# concurrency, and LEDGER_BATCH_FACT's (now-scoped) "in the order you issue
# them" actively implied the opposite, so the model rejected the true race
# explanation and mis-attributed the stale read to the loop guard (see
# CONCURRENT/loop-hint interaction), burning ~94s across two iterations. The
# fact below is derivable from nowhere else in the prompt and closes the same
# ORDER-vs-RESULT gap from the other side: watching an action take effect is a
# RESULT dependency, which PARALLELIZE_PRINCIPLE already says must serialize.
#
# 2026-10-29 disambiguation (inject-0ba742bd, R2 live re-inject of the same
# host端口占用案): the first live A/A confirmed the concurrency half landed —
# the model reasoned verbatim "blade_status issued in the same turn may sample
# before the effect lands... do the wait first, then in the next turn do the
# observation", with zero loop-guard mis-attribution. But it EXPOSED a residual
# lexical collision between the two companions: LEDGER_BATCH_FACT defines a
# RESULT dependency as "a call whose ARGUMENTS depend on another's output", so
# at Step-2 the model classified systemctl-start→systemctl-status as "an order
# dependency, not result dependency: status output depends on start having run"
# (its args never mention start's output) and batched [start, status,
# journalctl] in ONE turn (concurrent: 178/180/191ms). status then sampled a
# "6ms ago" transient active state and journalctl read a stale 06:42:37 entry,
# forcing a 126s investigation turn before the model self-corrected to the true
# Type=simple false-negative. The two definitions of "RESULT dependency"
# disagreed — LEDGER's is data/argument flow, this one's is effect/timing — and
# the model applied LEDGER's test and never reached this rule. The "even when
# the observation's arguments never mention the action's output" clause below
# reconciles them explicitly, so an effect-observation is caught even when the
# ORDER rule above seems to license batching it.
CONCURRENT_BATCH_FACT = (
    "Issue order is not completion order: the calls you batch in one turn run "
    "at the same time, so an observation issued alongside the action whose "
    "effect it should see may sample before that effect has landed — a wait in "
    "the same turn holds back nothing else. Watching an action take effect is a "
    "RESULT dependency, not an ORDER one — even when the observation's arguments "
    "never mention the action's output, so the ORDER rule above seems to license "
    "batching them: the observation reads the action's EFFECT, not its return "
    "value. Give that observation its own later turn."
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
