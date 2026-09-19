"""Mock-layer request-prefix stability regression (design D5, task 1.7).

Why this file exists
--------------------
A provider prompt cache matches the LONGEST BYTE-IDENTICAL PREFIX of an
incoming request against recently-seen turns. Anything volatile that sits in
the request HEAD (the ``SystemMessage``) invalidates the cache from the first
changed byte onward, for every subsequent turn. blade-ai renders a growing
``progress_ledger`` INTO the system prompt head, so each execute/verify round
rewrites the head — the stable prefix after the ledger's first changed byte is
re-billed as fresh input every round. That is the anti-pattern this whole
change (``context-cache-prefix-stability``) exists to remove.

The target invariant (Unit A, tasks 2.1-2.8)
--------------------------------------------
For a given phase, two ADJACENT rounds that differ ONLY by append-only ledger
growth MUST present a BYTE-IDENTICAL ``[system][tools]`` prefix, with the
volatile ledger riding the message TAIL (an append-only ``messages.append``)
instead of the head. Unit A establishes this by removing the embedded
``progress_ledger`` section from each builder and re-injecting it through the
existing tail-append channel with a "supersedes earlier ledger snapshots"
marker (design D1/D2).

Status (task 1.7 skeleton → Unit A landed for execute + verify)
---------------------------------------------------------------
These guards were introduced RED (``xfail(strict=True)``) to record the
pre-migration baseline: while the ledger rode the head, adjacent rounds
differed and the byte-equality assertion failed. The strict xfail was the
"turns green" handshake — the moment Unit A removed the embedded ledger the
test XPASSed, forcing deletion of the marker.

Both phases covered here have now migrated (execute = tasks 2.1-2.3, verify =
task 2.4): the ledger rides the message tail, so the ``[system]`` head is
byte-identical across adjacent rounds and BOTH guards pass outright. The
``xfail`` markers were removed per the handshake. recover_verifier (task 2.5)
and planning (task 2.6) each have their own builder-level head
(``build_recover_verifier_system_prompt`` / ``build_inject_system_prompt``), so
they grow phase guards here too; their node-level tail appends are asserted in
test_recover_verifier.py / test_agent_loop.py.
"""

from __future__ import annotations

from chaos_agent.agent.prompts.builders import (
    build_execute_system_prompt,
    build_inject_system_prompt,
    build_verifier_prompt,
)
from chaos_agent.agent.prompts.sections.recovery import (
    build_recover_verifier_system_prompt,
)

# Two ADJACENT round snapshots of the same phase's ledger. Round N+1 is
# round N plus one append-only log line — the ONLY thing that legitimately
# changes between consecutive rounds of a stable phase. Everything else
# (role, principles, tools, directives, remember) is identical input, so any
# byte difference in the assembled system prompt is attributable to the
# ledger riding in the head.
_LEDGER_ROUND_N = "## Progress Ledger\n- b-anchor: plan X\n- log: step1 done"
_LEDGER_ROUND_N1 = (
    "## Progress Ledger\n- b-anchor: plan X\n- log: step1 done\n- log: step2 done"
)


def _assert_head_stable(prompt_n: str, prompt_n1: str, phase: str) -> None:
    """The [system] head must not move when only the ledger tail grew."""
    assert prompt_n == prompt_n1, (
        f"{phase} system prompt is volatile across adjacent rounds: the "
        f"progress_ledger still rides in the HEAD, so the request prefix "
        f"(and every cached byte after the ledger's first change) is "
        f"re-billed each round. Unit A must move the ledger to the message "
        f"tail. First divergence at byte "
        f"{next((i for i, (a, b) in enumerate(zip(prompt_n, prompt_n1)) if a != b), min(len(prompt_n), len(prompt_n1)))}."
    )


class TestExecutePrefixStability:
    """Execute phase: the highest-round-count phase, so the biggest cache win."""

    # Unit A task 2.1-2.3 LANDED: build_execute_system_prompt no longer embeds
    # the progress_ledger (the ``progress_ledger_section`` kwarg is now
    # accepted-but-ignored), so adjacent rounds that differ only by ledger growth
    # present a byte-identical head. The former ``@pytest.mark.xfail`` was removed
    # per the task-1.7 handshake — this now passes outright.
    def test_execute_head_byte_identical_across_ledger_growth(self):
        prompt_n = build_execute_system_prompt(
            skill_catalog="x",
            skill_name="k8s-fault",
            plan="the approved plan body",
            progress_ledger_section=_LEDGER_ROUND_N,
        )
        prompt_n1 = build_execute_system_prompt(
            skill_catalog="x",
            skill_name="k8s-fault",
            plan="the approved plan body",
            progress_ledger_section=_LEDGER_ROUND_N1,
        )
        _assert_head_stable(prompt_n, prompt_n1, "execute")


class TestVerifierPrefixStability:
    """Verify phase: the other long loop re-injecting the ledger each round."""

    # Unit A task 2.4 LANDED: build_verifier_prompt no longer embeds the
    # progress_ledger (kwarg accepted-but-ignored), so adjacent rounds present a
    # byte-identical head. The former ``@pytest.mark.xfail`` was removed per the
    # task-1.7 handshake — this now passes outright.
    def test_verifier_head_byte_identical_across_ledger_growth(self):
        prompt_n = build_verifier_prompt(progress_ledger_section=_LEDGER_ROUND_N)
        prompt_n1 = build_verifier_prompt(progress_ledger_section=_LEDGER_ROUND_N1)
        _assert_head_stable(prompt_n, prompt_n1, "verifier")


class TestRecoverVerifierPrefixStability:
    """Recover-verify phase: the third loop that used to re-inject the ledger
    into its system head every round."""

    # Unit A task 2.5 LANDED: build_recover_verifier_system_prompt no longer
    # embeds the progress_ledger (the ``ledger_section`` kwarg is now
    # accepted-but-ignored), so adjacent rounds that differ only by ledger growth
    # present a byte-identical head. The ledger rides the message tail instead
    # (appended + persisted in _recover_verifier_loop).
    def test_recover_verifier_head_byte_identical_across_ledger_growth(self):
        prompt_n = build_recover_verifier_system_prompt(
            layer1_label="recovery execution", ledger_section=_LEDGER_ROUND_N,
        )
        prompt_n1 = build_recover_verifier_system_prompt(
            layer1_label="recovery execution", ledger_section=_LEDGER_ROUND_N1,
        )
        _assert_head_stable(prompt_n, prompt_n1, "recover_verifier")


class TestPlanningPrefixStability:
    """Planning phase (agent_loop, PromptMode.FULL): the fourth loop that used to
    re-inject the ledger into its system head every ReAct round."""

    # Unit A task 2.6 LANDED: build_inject_system_prompt no longer embeds the
    # progress_ledger (the ``progress_ledger_section`` kwarg is now
    # accepted-but-ignored), so adjacent planning rounds that differ only by
    # ledger growth present a byte-identical head. The ledger rides the message
    # tail instead (appended + persisted in agent_loop.py).
    def test_planning_head_byte_identical_across_ledger_growth(self):
        prompt_n = build_inject_system_prompt(
            skill_catalog="x", input_is_nl=True,
            progress_ledger_section=_LEDGER_ROUND_N,
        )
        prompt_n1 = build_inject_system_prompt(
            skill_catalog="x", input_is_nl=True,
            progress_ledger_section=_LEDGER_ROUND_N1,
        )
        _assert_head_stable(prompt_n, prompt_n1, "planning")
