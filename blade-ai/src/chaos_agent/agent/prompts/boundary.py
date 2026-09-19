"""Phase boundary declarations — the single source for boundary wording.

Contract (openspec/changes/phase-boundary-declaration): wherever control
flow crosses a seam of the LLM context continuum (graph entry, phase
handover, layer handover, replan, cross-graph graft), the model's
cognitive reorientation must not depend on semantic guessing. Three axes
define the problem space — role (which phase am I in, prior phase over),
evidence tense (where the history above came from; when its observations
were taken; their legitimate use as evidence), tool surface (the
currently bound tool schemas are the authority; tool names seen only in
history may belong to a different phase's surface).

Axes are answered by a combination of four carrier layers — context
surgery (strip the inertia source, e.g. planning_handoff), boundary
declaration message (label the history's provenance, e.g. the Phase-2
kickoff), screener/phase duty (constrain current behaviour), and the tool
schemas themselves. The tool-surface axis is CONDITIONAL: it belongs in a
declaration message only when the inertia source survives (history
carries earlier-phase tool_calls), the surface actually differs, and no
other carrier covers it — the inject→recover graft is the only seam
where all three hold (#29 first-run evidence: recover-98caf0cd
msg[7]-[11], three kubectl_read calls rejected by ToolNode).

This module provides construction only. The four pre-existing
implementations (kickoff marker / verifier context kwargs / the bare
"PHASE TRANSITION" string / the context_anchor flag) are deliberately
NOT migrated here: their detection logic is coupled to their wording and
field-proven; unifying them would be a high-risk rewrite for tidiness
alone. New seams, however, must construct their declaration through this
module rather than starting a second wording source.
"""

from __future__ import annotations

# Public: injection sites probe for this marker when they must check
# whether a declaration is already present in a message stream (the
# L2 idempotence guard mirrors the Phase-2 kickoff's marker detection).
_BOUNDARY_MARKER = "**PHASE BOUNDARY — READ THIS BEFORE PROCEEDING**"
BOUNDARY_MARKER = _BOUNDARY_MARKER


def build_boundary_declaration(
    *,
    role: str,
    evidence_tense: str,
    tool_surface: str | None = None,
) -> str:
    """Build a three-axis boundary declaration.

    Args:
        role: The role axis — the phase/layer now active and the prior
            one being over (e.g. "You are now executing the RECOVERY
            task. The inject task is over.").
        evidence_tense: The temporal axis — the provenance of the
            message history above the seam, when its observations were
            taken, and their legitimate use as evidence.
        tool_surface: The tool-surface axis — optional and conditional
            (see module docstring): pass None on seams where the axis
            does not apply (inertia stripped / surface unchanged /
            duty already covers it) and the axis is omitted entirely.

    Wrapping is the injection site's concern (``wrap_system_reminder``
    for corrective-tag loops, per reminder.py's contract rules); this
    function returns the bare declaration text.
    """
    lines = [
        _BOUNDARY_MARKER,
        "The message history above comes from a COMPLETED earlier phase or",
        "task of this conversation. Treat it accordingly:",
        f"- Role: {role}",
        f"- Evidence: {evidence_tense}",
    ]
    if tool_surface is not None:
        lines.append(f"- Tools: {tool_surface}")
    return "\n".join(lines)


# The inject→recover graft declaration — the one seam where all three
# tool-surface conditions hold. Wording reuses field-proven semantic
# skeletons ("captured BEFORE fault injection" baseline labels, the
# verifier's "stale ... is NOT evidence" principle, layer-1's "tools
# bound in the current environment") rather than inventing new temporal
# vocabulary. Channel-neutral by construction: no kubectl / kubeconfig /
# host vocabulary — the injection site supplies channel context.
INJECT_TO_RECOVER_BOUNDARY_DECLARATION = build_boundary_declaration(
    role=(
        "you are now executing the RECOVERY task; the inject task is over"
    ),
    evidence_tense=(
        "every observation in that history (baseline readings, "
        "fault-presence observations, tool outputs) is a snapshot from "
        "BEFORE or DURING injection — its only legitimate use is as a "
        "comparison baseline for recovery, and it is NEVER evidence of "
        "the current state"
    ),
    tool_surface=(
        "the tools currently bound to you are the authority; tool names "
        "seen only in earlier tool calls may belong to a different "
        "phase's surface and may not exist now"
    ),
)
