"""Ledger-sourced restore-recipe hydration for the faultdrill carrier.

WHY THIS MODULE EXISTS
----------------------
The restore recipe (which object to patch back, and to what baseline
value) had exactly one read path: the assembler receipt in the MESSAGE
HISTORY. That path has two independent failure modes, both reproduced by
``tests/test_agent/test_providers/test_faultdrill_recipe_hydration.py``:

* a cross-task recover (``blade-ai recover --task-id``) inherits NO
  messages at all — the recover-state builder in
  ``state_mgmt.recovery_state`` starts from ``recover_reset_state()`` and
  flattens inject history into the ``inject_context`` string, so the
  receipt cannot be reached whether or not it was compacted;
* within one task, ``memory.tool_compactor`` cuts every tool result
  outside the recent window to 1KB and a 2-4KB receipt comes out as
  invalid JSON, which ``parse_receipt`` correctly refuses to guess at.

Meanwhile a durable copy of the very same recipe already travels with the
recover state on BOTH resolution paths: every ``recovery_carrier``
execution artifact embeds the full ``recovery_handle``
(``assembler.build_carrier_artifact``), and ``execution_artifacts`` is
carried by that recover-state builder AND by the
TaskSnapshot leg (``build_recovery_handle`` files it under
``{"kind": "artifact", ...}``). Nothing on the recover path read it for
the recipe; it was consumed only to tear the carrier stack down.

This module is that missing rung. It is the ledger side of a two-source
hydration — the message history stays FIRST (it is the only source that
can express both faces, so reordering it would change existing
semantics), the ledger is the fallback that survives compaction and the
graph boundary.

LAYERING
--------
``execution_artifacts`` is a generic state field, but ``recovery_carrier``
and ``recovery_handle`` are carrier vocabulary, so recognising them lives
here in the provider package rather than in the generic recover flow. The
generic layer only ever passes the neutral artifact list through.

``project_recipe_handle`` is the SINGLE projection from a recipe dict to a
recipe-bearing handle, shared with the message-history rung
(``provider._assembler_handle_from_events``): two sources, one shape
contract, so the completeness rule (a target needs kind+name+namespace and
at least one restore op) cannot drift between them.
"""

from __future__ import annotations

from typing import Any, Optional

# Carrier vocabulary — deliberately NOT exported to the generic layer.
_CARRIER_ARTIFACT_TYPE = "recovery_carrier"
_CARRIER_METHOD = "faultdrill_carrier"
_HANDLE_KIND = "faultdrill_cr"


def project_recipe_handle(
    recipe: Any, *, method: str = _CARRIER_METHOD,
) -> Optional[dict]:
    """Project a ``recovery_handle`` recipe dict into a recipe-bearing
    fault handle, or ``None`` when it is not replayable.

    The completeness rule is the recovery contract, not a convenience: a
    recipe missing any of ``target_ref.kind`` / ``.name`` / ``.namespace``
    or carrying no dict-shaped restore op cannot address a target, and
    guessing at the missing piece would patch the wrong object — or none.
    Returning ``None`` lets the caller fall through to its honest
    no-recipe verdict instead.

    Only the RECOVERY identity is projected. The recipe also carries the
    inject-side ``patches`` (the fault description); those must never ride
    into what gets replayed, so they are dropped here rather than being
    filtered by each caller.
    """
    if not isinstance(recipe, dict):
        return None
    target = dict(recipe.get("target_ref") or {})
    restore = [
        op for op in (recipe.get("restore_patches") or [])
        if isinstance(op, dict)
    ]
    if not (
        target.get("kind") and target.get("name")
        and target.get("namespace") and restore
    ):
        return None
    handle = {
        "kind": _HANDLE_KIND,
        "value": str(recipe.get("value") or ""),
        "method": method,
        "target_ref": target,
        "restore_patches": restore,
    }
    # An invalidSecret face carries no restore ops; its name is the recipe.
    invalid_secret = recipe.get("invalid_secret")
    if isinstance(invalid_secret, dict) and invalid_secret.get("name"):
        handle["invalid_secret"] = dict(invalid_secret)
    deadline = recipe.get("recovery_deadline_epoch")
    if isinstance(deadline, (int, float)):
        handle["recovery_deadline_epoch"] = float(deadline)
    return handle


def recipe_from_artifacts(
    artifacts: Any, *, method: str = "",
) -> Optional[dict]:
    """Hydrate the carrier face's recipe from the ``execution_artifacts``
    ledger — the rung that survives message compaction and the cross-task
    graph boundary.

    Walks back to the most recent complete recipe, mirroring the
    message-history rung's LATEST-REGISTERABLE rule (a retry builds a new
    stack, so the newest armed carrier is the one whose recipe matters;
    ``collect_execution_artifacts`` merges by ``artifact_id``, so a given
    carrier appears once and the list order is the registration order).

    ``method`` pins the face the caller is recovering. The ledger only ever
    holds the CARRIER face, so a dispatch pinned to the CR face
    (``faultdrill_cr``) must NOT be silently re-routed onto a carrier
    recipe — that is the face-drift rule ``_recipe_face_handle`` already
    enforces on the message rung, kept identical here. An empty ``method``
    (the claim-4 method dispatch, no face pinned) accepts the ledger.

    Returns a recipe-bearing handle, or ``None`` when the ledger carries no
    replayable recipe — never a partial one.
    """
    if method and method != _CARRIER_METHOD:
        return None
    if not isinstance(artifacts, (list, tuple)):
        return None
    for artifact in reversed(list(artifacts)):
        if not isinstance(artifact, dict):
            continue
        if artifact.get("type") != _CARRIER_ARTIFACT_TYPE:
            continue
        handle = project_recipe_handle(
            artifact.get("recovery_handle"), method=_CARRIER_METHOD,
        )
        if handle is not None:
            return handle
    return None


__all__ = ["project_recipe_handle", "recipe_from_artifacts"]
