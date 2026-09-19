"""Vocabulary domain registry (B76 round-15 D6, forms extended round-16 S6)
— census + registry.

The census (productionized from probe_b76_round15b) scans all non-test
src files for FIVE comparison shapes — ``== "word"``, ``in ("a", "b")``,
status-map dict keys (≥4 word-like keys in one block), ternary/else
branch defaults (``else "failed"``), and dict get-defaults
(``.get(k, "failed")``) — and aggregates per-word fan-out across files.

The two round-16 shapes exist because the round-15 three-shape scanner
was itself blind to them: 52 ternary-default + 71 get-default vocabulary
literals were invisible to the census (probe_b76_round16 S6), including
the round-16 S2 defect itself (``else "recovered"`` in
session_finalizer). The default-bearing shapes are the highest-risk
hand-copy form — a default literal silently decides the outcome when
the honest answer is "no evidence".

A status-flavoured word with fan-out >= 2 must be CLASSIFIED: either
``REGISTERED`` (pointing at its legislation site) or ``EXEMPT`` (carrying
a reason). An unclassified word fails this test with its file list. That
is the mechanism which ends the "each review round discovers a new
vocabulary domain by luck" pattern: a new domain's words cannot spread to
a second file without CI forcing a classification decision.

The ``FLAVOUR`` seed is the census's known-domain vocabulary list —
extending it is part of registering a new domain family.
"""

import re
from pathlib import Path

from chaos_agent.agent.state import TASK_STATE_VALUES
from chaos_agent.agent.result.verdict import (
    CHECKLIST_STATUS_VALUES,
    INJECT_VERDICT_VALUES,
    LAYER1_STATUS_VALUES,
    LAYER2_STATUS_VALUES,
    RECOVER_VERDICT_VALUES,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC = PROJECT_ROOT / "src"

# ---------------------------------------------------------------------------
# Census scanner (probe_b76_round15b extraction logic, verbatim shapes)
# ---------------------------------------------------------------------------

_WORD = r"[a-z][a-z0-9_]{2,30}"
_TUPLE_IN_RE = re.compile(rf"in \(((?:\"{_WORD}\"\s*,\s*)+\"{_WORD}\")\)")
_EQ_RE = re.compile(rf'==\s*\"({_WORD})\"')
_BLOCK_RE = re.compile(r"\{[^{}]{40,900}\}", re.DOTALL)
_BLOCK_KEY_RE = re.compile(rf'^\s*\"({_WORD})":', re.MULTILINE)
# Round-16 S6: default-bearing shapes — a branch default or a get-default
# silently decides an outcome when the input carries no evidence, so these
# literals are vocabulary consumption even though they sit in "value"
# position rather than comparison position.
_ELSE_DEFAULT_RE = re.compile(rf'else "({_WORD})"')
_GET_DEFAULT_RE = re.compile(rf'\.get\([^)]*,\s*"({_WORD})"\)')

# Status-flavoured vocabulary seed — the known domain families (verdicts,
# task lifecycle, layers, phases, coarse statuses). Extending this list is
# part of registering a new domain.
FLAVOUR = frozenset({
    "recovered", "partial_recovered", "unrecovered", "unverified", "verified",
    "partial", "failed", "passed", "skipped", "unknown", "injected",
    "injecting", "recovering", "rejected", "completed", "cancelled",
    "planning", "executing", "confirming", "degraded", "warning", "error",
    "in_progress", "expected", "not_applicable", "verification_failed",
    "dry_run_planned", "replanning", "safety_check", "terminating",
    "success", "pending",
})


def _scan_file_words(text: str) -> set[str]:
    """Extract comparison vocabulary from one file's source text.

    Five shapes (round-16 S6 extended the default-bearing pair) — a bare
    string literal in a plain assignment is still NOT a vocabulary
    consumption and is deliberately not extracted.
    """
    words: set[str] = set()
    for m in _TUPLE_IN_RE.finditer(text):
        for w in re.findall(rf'"({_WORD})"', m.group(1)):
            words.add(w)
    for m in _EQ_RE.finditer(text):
        words.add(m.group(1))
    for block in _BLOCK_RE.findall(text):
        keys = _BLOCK_KEY_RE.findall(block)
        if len(keys) >= 4:
            words.update(keys)
    for m in _ELSE_DEFAULT_RE.finditer(text):
        words.add(m.group(1))
    for m in _GET_DEFAULT_RE.finditer(text):
        words.add(m.group(1))
    return words


def _census() -> dict[str, set[str]]:
    """Word → set of non-test src files comparing against it."""
    word_files: dict[str, set[str]] = {}
    for py in sorted(SRC.rglob("*.py")):
        if "__pycache__" in py.parts or "test" in py.parts:
            continue
        for w in _scan_file_words(py.read_text("utf-8", errors="replace")):
            word_files.setdefault(w, set()).add(str(py.relative_to(SRC)))
    return word_files


def _triggered(census: dict[str, set[str]], *, min_fanout: int = 2) -> dict[str, set[str]]:
    """Status-flavoured words whose fan-out crosses the registry threshold."""
    return {
        w: files
        for w, files in census.items()
        if w in FLAVOUR and len(files) >= min_fanout
    }


# ---------------------------------------------------------------------------
# The registry — every fan-out>=2 flavour word must appear in ONE of these
# ---------------------------------------------------------------------------

REGISTERED = {
    # task_state domain — state.py TaskState (round-15 legislation)
    "injected": "state.py TaskState.INJECTED",
    "injecting": "state.py TaskState.INJECTING",
    "recovering": "state.py TaskState.RECOVERING",
    "recovered": "state.py TaskState.RECOVERED + verdict.py RecoverVerdict.RECOVERED",
    "partial_recovered": "state.py TaskState.PARTIAL_RECOVERED",
    "unverified": (
        "verdict.py InjectVerdict.UNVERIFIED / RecoverVerdict.UNVERIFIED "
        "+ state.py TaskState.UNVERIFIED"
    ),
    "unrecovered": "verdict.py RecoverVerdict.UNRECOVERED",
    "failed": (
        "multi-domain: verdict.py Layer2Status/ChecklistItemStatus.FAILED "
        "+ state.py TaskState.FAILED (also generic error-prose word)"
    ),
    "rejected": (
        "state.py TaskState.REJECTED + gates safety_status prose domain "
        "(pending/safe/warning/rejected — unlegislated, single family)"
    ),
    "completed": "state.py TaskState.COMPLETED",
    "cancelled": "state.py TaskState.CANCELLED",
    # verdict domains — verdict.py (round-14 legislation)
    "verified": "verdict.py InjectVerdict.VERIFIED",
    "partial": (
        "multi-domain: verdict.py InjectVerdict/RecoverVerdict/Layer2Status/"
        "ChecklistItemStatus .PARTIAL"
    ),
    # layer status domains — verdict.py (round-14 legislation)
    "passed": "verdict.py Layer1Status/Layer2Status/ChecklistItemStatus .PASSED",
    "skipped": "verdict.py Layer1Status/Layer2Status/ChecklistItemStatus .SKIPPED",
    "unknown": (
        "verdict.py Layer1Status/Layer2Status .UNKNOWN "
        "(also generic absent-value default)"
    ),
    "warning": (
        "verdict.py Layer1Status.WARNING + gates safety_status prose domain"
    ),
    "in_progress": (
        "verdict.py Layer1Status.IN_PROGRESS + infer_status coarse domain "
        "(state.py prose)"
    ),
    "error": (
        "verdict.py Layer1Status.ERROR (also generic tool-status prose word)"
    ),
    "expected": "verdict.py ChecklistItemStatus.EXPECTED",
    "not_applicable": "verdict.py ChecklistItemStatus.NOT_APPLICABLE",
}

EXEMPT = {
    # Coarse four-value status domain (success/failed/in_progress/pending):
    # prose-defined in state.py infer_status / infer_inject_status /
    # infer_recover_status; consumers are display projections (publisher,
    # terminal reports, metrics render). Round-15 scoped legislation to the
    # verdict + task_state domains; this domain remains prose until a
    # future round — registered here as conscious, visible debt.
    "success": "coarse infer_status domain (state.py prose), display-only fan-out",
    # Round-16 S6: "pending" surfaced by the default-bearing census shapes —
    # same coarse infer_status domain as "success" (its fourth member),
    # plus the gates safety_status prose family ("pending" default at
    # state.py infer_phase) and router/plan-generator confirmation prose.
    # Round-17 D4: additionally a LEGISLATED persistence overlay word
    # (TaskStateOverlay.PENDING, state.py — the task_state column's
    # newborn anchor, distinct from the coarse-domain homonym).
    "pending": (
        "coarse infer_status domain (state.py prose, 4th member) + gates "
        "safety_status prose + confirmation prose + LEGISLATED overlay "
        "(TaskStateOverlay.PENDING, round-17 D4); display fan-out"
    ),
    # Round-16 S6: L4 result-status prose domain (passed/failed/cancelled/
    # degraded — L4TaskResult.status, l4/agent.py + l4/execution.py).
    # Not a verdict/Layer2 word (LAYER2_DEGRADED_STATUSES is a SUBSET
    # constant over other members, not this word); scoped out of round-15
    # legislation — conscious debt, same family as the coarse status domain.
    "degraded": "L4 result-status prose domain (l4 prose 4-word set), display fan-out",
}


# ---------------------------------------------------------------------------
# Registry assertions (task-15 4.3 four checks)
# ---------------------------------------------------------------------------


def test_unregistered_fanout_words_fail():
    """Check 1: every fan-out>=2 flavour word is classified.

    A new vocabulary domain spreading to a second file lands here with its
    file list — classify it (legislate the enum → REGISTERED, or EXEMPT
    with a reason) instead of silencing the test.
    """
    census = _census()
    triggered = _triggered(census)
    classified = set(REGISTERED) | set(EXEMPT)
    unregistered = {
        w: sorted(files) for w, files in triggered.items() if w not in classified
    }
    assert not unregistered, (
        "Unregistered vocabulary words with fan-out >= 2 (classify in "
        "REGISTERED/EXEMPT or legislate): "
        + "; ".join(f"{w} -> {fs}" for w, fs in sorted(unregistered.items()))
    )


def test_exempt_words_still_have_fanout():
    """Check 2: exemption staleness — an EXEMPT word must still trigger.

    If an exempted word's fan-out drops below the threshold the exemption
    is dead weight (the domain got legislated or vanished) — remove the
    entry so the list stays honest.
    """
    census = _census()
    stale = {
        w: len(census.get(w, ()))
        for w in EXEMPT
        if len(census.get(w, ())) < 2
    }
    assert not stale, f"Stale exemptions (fan-out < 2, remove them): {stale}"


def test_single_file_words_do_not_trigger():
    """Check 3: fan-out == 1 never triggers the registry requirement."""
    census = {
        "verification_failed": {"agent/state.py"},
        "failed": {"agent/state.py", "l4/adapter.py", "x/y.py"},
        "passed": {"agent/state.py"},
    }
    triggered = _triggered(census)
    assert "verification_failed" not in triggered
    assert "passed" not in triggered
    assert triggered == {"failed": census["failed"]}


def test_census_shape_extraction():
    """Check 4a: the three extraction shapes (synthetic ground truth)."""
    sample = '''
    if l1_status == "passed":
        pass
    if level in ("recovered", "partial"):
        pass
    status_map = {
        "failed": 1,
        "rejected": 2,
        "cancelled": 3,
        "completed": 4,
    }
    small_map = {"a": 1, "b": 2, "c": 3}
    plain = "recovered"
    '''
    words = _scan_file_words(sample)
    assert "passed" in words          # == shape
    assert "recovered" in words       # in-tuple shape
    assert "partial" in words
    # status-map shape: >=4 word-like keys in one block
    assert {"failed", "rejected", "cancelled", "completed"} <= words
    # a 3-key dict does NOT register as a status map
    assert not {"a", "b", "c"} & words


def test_census_sees_the_legislated_domain():
    """Check 4b: census ground truth — the scanner finds the real world.

    The task_state words are compared literally in state.py's own subset
    expressions at minimum; the census must see them (this is the probe's
    P6 ground-truth check, post-fix: fan-outs DROPPED because the round-15
    wiring replaced hand copies with enum references — the census sees the
    remaining legislation-file-internal literals and any future drift).
    """
    census = _census()
    task_state_seen = {w for w in TASK_STATE_VALUES if census.get(w)}
    assert len(task_state_seen) >= 6, (
        f"census lost sight of the task_state domain: only {sorted(task_state_seen)} "
        f"of {len(TASK_STATE_VALUES)} words found"
    )


def test_registry_words_have_live_legislation():
    """Registry ↔ legislation consistency: every REGISTERED word must be a
    member of the closed set its entry names — a drifted entry (word
    renamed in the enum, registry not updated) fails here."""
    legislated = (
        TASK_STATE_VALUES
        | INJECT_VERDICT_VALUES
        | RECOVER_VERDICT_VALUES
        | LAYER1_STATUS_VALUES
        | LAYER2_STATUS_VALUES
        | CHECKLIST_STATUS_VALUES
    )
    unlegislated = sorted(set(REGISTERED) - legislated)
    assert not unlegislated, (
        f"REGISTERED words with no live legislation member: {unlegislated}"
    )
