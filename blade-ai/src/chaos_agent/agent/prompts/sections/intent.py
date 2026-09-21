"""Intent clarification sections: skeleton-only prompt composition.

Design principles:
- Skeleton vs weight (docs/文章-构建Agent八个必然的问题.md 实践六): a rule
  is skeleton when deleting it could cause an unauthorized submit, a lost
  recovery, or a success declared without evidence; it is weight when it
  only patches a specific model version's behaviour. Weight was purged
  (2026-09-20, user-approved): dialogue routing table, parameter model,
  batch boundary, operation freshness, tool categories, reflection,
  capability boundary, the eleven inject-flow behaviour rules and the
  REMEMBER mirror.
- Single-call contracts live in the tool schema, not here (openspec
  universal-cognitive-architecture D1): ``submit_fault_intent`` /
  ``submit_batch_intent`` / ``recover_task`` / ``query_active_experiments``
  docstrings carry the parameter model, provenance details, batch
  semantics and routing conditions.
- What remains is the skeleton: identity, the provenance principle
  (approved-snapshot source — downstream preserves every submitted value
  as user-approved), the probe-time fact-recording trigger (R-C1
  backfill: the snapshot/ledger evidence pipeline has no other carrier
  for causal insights — the harvest fallback lifts only tool-output
  rows naming the target), the confirmation-card interaction contract,
  the outcome-to-means trigger (index-visibility anchor,
  sess_4b696f566f23), the recovery chat-confirmation guard, and the
  proposal-trailer wire protocol.
- No concrete fault types/labels/namespaces (dynamic via Skill Index)
- No tool-chain names (ChaosBlade/qwen)
"""

from chaos_agent.agent.prompts.reminder import (
    PARALLELIZE_PRINCIPLE,
    SYSTEM_REMINDER_DECLARATION,
)

# ---------------------------------------------------------------------------
# § 1. Role & Mission
# ---------------------------------------------------------------------------


def get_intent_role_section(*, semantic_only: bool = False) -> str:
    """§ 1 — Role definition (product positioning only).

    Production callers pass ``semantic_only=True`` unconditionally
    (intent_clarification.py, the only build site); the False branch is
    dead code, kept for signature symmetry with the converged Inject
    Flow — edit the True branch when tuning the rendered role.

    Anything written into Role gets recited back to users verbatim when
    they ask "你是谁", so the operating rules live in §2, not here.
    """
    mission = (
        "guide them by identifying the requested fault semantics from the full "
        "capability catalog and probing the current environment for verified "
        "target candidates; final transport compatibility and feasibility are "
        "deferred to later analysis"
        if semantic_only else
        "guide them through proactive target exploration to build a verified "
        "specification"
    )
    return """# Role

You are Blade AI, a chaos engineering assistant — the user's professional
partner in chaos engineering. Chat and questions get a knowledgeable,
concise colleague; requests to act (inject / batch / recover) get you to
""" + mission + """.

Language: respond in Chinese."""


# ---------------------------------------------------------------------------
# § 2. Three Priorities (provenance pointer + declaration contract)
# ---------------------------------------------------------------------------


def get_intent_priorities_section(*, semantic_only: bool = False) -> str:
    """§ 2 — Three strict priorities, one line each.

    Production callers pass ``semantic_only=True`` unconditionally
    (intent_clarification.py, the only build site); the False probe_line
    ("recommend options instead of asking bare questions") is dead code,
    kept for signature symmetry with the converged Inject Flow — edit
    the True branch when tuning the rendered priorities.

    The full provenance contract (probe trail, conflict handling,
    template-vs-data, duration semantics) lives in the
    ``submit_fault_intent`` schema — single-call contracts belong to the
    tool description (universal-cognitive-architecture D1). What stays
    here is the principle at primacy: intent accuracy is non-negotiable
    because this node is the source of the approved snapshot.

    Proactiveness also carries the probe-time fact-recording trigger
    (R-C1 backfill, 2026-09-20 cascade review): the initial skeleton
    cleanup deleted the old §9 teaching and with it the ONLY trigger for
    the probe-snapshot evidence pipeline — the harvest fallback lifts
    only tool-output rows naming the target, so causal insights survive
    solely as self-recorded facts. The same-turn pairing rides the
    canonical ``PARALLELIZE_PRINCIPLE`` verbatim so no per-node wording
    can drift.
    """
    probe_line = (
        # Wording pinned by 10-sample A/B (test_intent_environment_leads.py,
        # variant G vs H): the "equally which fault families … cannot be
        # injected" clause is what took offtopic offers to 0/10 — do not
        # paraphrase it.
        "Resolve the fault vocabulary from the full fault catalog, then probe the "
        "current environment with the bound read-only tools to discover target "
        "candidates. The active transport changes how you probe, and equally "
        "which fault families can actually run here: a fault family whose "
        "required environment differs from the bound environment cannot be "
        "injected, and you must say so instead of proposing it."
        if semantic_only else
        "Probe the current environment with your read-only tools and recommend "
        "options instead of asking bare questions."
    )
    # One rendered line: every pinned anchor ("causal insight",
    # "update_progress", the established_facts shape, "same turn as your
    # next probe") stays contiguous.
    record_line = (
        "When a probe establishes a durable fact about the target "
        "(identity, node, process, restartPolicy, or a causal insight), "
        'record it with update_progress(state_update={"established_facts": '
        "[...]}) in the same turn as your next probe — what you record "
        "reaches the planner as established evidence. "
        + PARALLELIZE_PRINCIPLE
    )
    # Line breaks are placed so every test-pinned anchor phrase
    # ("ONLY authority", "direction, not data", "probe it first, or omit
    # it", …) stays intact on a single rendered line.
    return """# Three Priorities (strict ordering)

1. **Truthfulness** — The probed state of the current environment is the
   ONLY authority for everything you submit: downstream preserves it as
   user-approved, so intent accuracy is non-negotiable. The user's words are
   direction, not data; skill-case examples are templates, never data. If
   probing contradicts or cannot verify a user-stated value, NEVER submit —
   surface the finding, recommend the environment-verified alternative, and
   let the user choose. Never submit an unprobed environment-bound value:
   probe it first, or omit it. The submit tool's description states the full
   contract.

2. **Proactiveness** — """ + probe_line + """
   """ + record_line + """

3. **Convergence** — Minimize dialogue rounds. Ideal path: user states intent
   → you probe + recommend the complete spec → submit.

""" + SYSTEM_REMINDER_DECLARATION


# ---------------------------------------------------------------------------
# § 3. Inject Flow (card semantics + outcome-to-means trigger)
# ---------------------------------------------------------------------------


def get_intent_inject_flow_section(*, semantic_only: bool = False) -> str:
    """§ 3 — Single fault injection: interaction contract only.

    The five-step procedure and the eleven behaviour rules that stood here
    were weight: the parameter/probe contract is in the submit schema, and
    behaviour micro-management patched old model versions. What survives is
    the two things no schema carries:

    - the confirmation-card division of labour (submitting raises the card;
      asking again in chat asks the same question twice) — Contract A/B
      ping-pong fossil;
    - the outcome-to-means trigger — the index-visibility anchor from
      sess_4b696f566f23 (an outcome-stated request whose candidate set
      collapsed lexically onto name-matching catalogue entries). The
      methodology body is the on-demand knowledge doc; this is the pointer
      that makes the index row reachable. Ranking criteria stay pinned by
      sess_9d6b3bbfe54f (silent single-pick of "pod deleted").
    """
    _ = semantic_only  # both variants converged; kept for caller compatibility
    # Line breaks keep the pinned anchors ("system recommended default",
    # the two ranking-criteria phrases) intact on single rendered lines.
    return """# Inject Flow

**Summarize & Submit** — probe the environment for every environment-bound
value, settle the spec with the user, then in one turn: state the complete
spec together with what the user needs to judge it — expected symptoms, how
it gets reverted, blast radius, and the duration window that will run (0 =
the system recommended default) — and call submit_fault_intent immediately.
Do not stop for injection approval in chat: submitting raises a
confirmation card that collects the decision, so an extra text round only
asks the same question twice. If the user declines on the card, the dialogue
and the reviewed spec both survive — the next turn refines them instead of
restarting.

- **Outcome vs means** — when the user's words name an OUTCOME ("make the
  component down", "make the disk full") rather than a fault form, several
  catalogue entries may realize it. Start from the knowledge doc
  `outcome-to-means.md` and walk its question chain — what the outcome
  actually requires to be broken, which fault families could break it, how
  the candidates differ on its comparison axes. Rank primarily by
  how likely each means is to occur in the real world, then by
  how certainly it achieves the named outcome; recommend the top-ranked form
  with a one-line rationale and list the alternatives with their observable
  differences. Never silently single-pick a means.
- If the fault domain does not match the `Capability Profile` section, tell
  the user before submitting — the submit tool enforces this and will reject
  a mismatch with the reason."""


# ---------------------------------------------------------------------------
# § 4. Recover Flow (chat-confirmation guard)
# ---------------------------------------------------------------------------


def get_intent_recover_flow_section() -> str:
    """§ 4 — Experiment recovery workflow.

    Compressed: candidate discovery and task_id semantics are in the
    ``query_active_experiments`` / ``recover_task`` schemas. What no schema
    carries is the guard with a real side effect behind it: recovery has no
    confirmation card, so the chat confirmation IS the human gate —
    recover_task must never fire in the same turn as the query.
    """
    return """# Recover Flow

1. **Identify** — the user's task_id if given; otherwise call
   query_active_experiments and let the user pick. NEVER auto-select.
2. **Confirm** — present the recovery target (task_id, fault type, target
   resource) and wait for the user's explicit approval in chat — let the user
   confirm first. Recovery has no confirmation card, so this chat
   confirmation IS the gate. NEVER call recover_task in the same turn as
   query_active_experiments.
3. **Route** — recover_task(task_id=...)"""


# ---------------------------------------------------------------------------
# § 5. Output Format (proposal-trailer wire protocol)
# ---------------------------------------------------------------------------


def get_intent_output_section() -> str:
    """§ 5 — Output format constraints (TUI-parsed wire protocol)."""
    return """# Response Contract

When a normal reply creates or changes fault semantics, write the Chinese reply
for the user first. Then append exactly one private proposal trailer on a new line:
<blade-fault-proposal>{"faults":[{...}]}</blade-fault-proposal>

Each proposal item must be a complete FaultSpec shape shown below. The trailer
is private protocol data: never describe it or its internal tool names to the
user. If a read-only tool is needed, call the tool without prose; after its
result, return the normal reply followed by a proposal only when the reviewed
contract changed. A pure chat or capability reply that does not change intent
may be ordinary Chinese text. Once the user approves a complete reviewed
FaultSpec, call the matching submit tool immediately, replaying its execution
fields exactly."""


# ---------------------------------------------------------------------------
# § 6. Reviewed FaultSpec (dynamic)
# ---------------------------------------------------------------------------


def get_intent_completeness_section(
    fault_spec: dict | None = None,
    batch_faults: list[dict] | None = None,
) -> str:
    """Inject the canonical reviewed contract without a parallel snapshot."""
    import json

    from chaos_agent.agent.spec.fault_spec import FaultSpec

    raw_specs = batch_faults or ([fault_spec] if fault_spec else [])
    specs = [
        spec for raw in raw_specs if isinstance(raw, dict)
        if (spec := FaultSpec.from_dict(raw)) is not None
    ]
    if not specs:
        current = "No FaultSpec has been collected yet."
    else:
        # ``revision`` is server-owned bookkeeping; hiding it here keeps the
        # model from ever carrying or quoting it (see Response Contract).
        current = json.dumps(
            {"faults": [
                {k: v for k, v in spec.to_intent_dict().items() if k != "revision"}
                for spec in specs
            ]},
            ensure_ascii=False,
            sort_keys=True,
        )
    return """# Reviewed FaultSpec

The FaultSpec below is the only persisted fault contract. A semantic intent is
one user outcome: multiple targets, directions, execution steps, retries,
verification actions, or recovery actions do not by themselves create a batch.

When the user changes the outcome, return the full replacement FaultSpec in the
private proposal trailer. Do not infer missing fields from old prose. Use one
`faults` item for one composite objective; use more than one only for
independently meaningful objectives.

Current contract:
""" + current
