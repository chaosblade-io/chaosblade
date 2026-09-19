"""B76 review G — the liability-axis view over the experiment lifecycle.

``live_liability_uids`` is the root-cause companion of the single
``experiment_uid`` slot: the slot answers the ATTRIBUTION question
(last-write-wins by design — the newest create owns recovery attribution)
while this view answers the LIABILITY question (monotonic by birth, reduced
only by PROVEN death). These tests pin the view's contract directly; the
seam-level behaviour (approval destroy / recover sweep) lives in the
plan-change and registry suites.

B76 review J1 dialect pins: the view's death filter is deliberately PROVEN
(output-proven destroys ∪ retired registry), NOT issued (a destroy CALL
counting as death "whether the destroy succeeded or failed" — that dialect
governs only the attribution-side scan, pinned below). The pre-J1 fixture
here used a bare ``"ok"`` destroy output, which proves nothing under the
proven dialect — the fixture could not distinguish the two dialects, which
is exactly how the issued-implementation drift survived three rounds.
"""

import json

from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.agent.state import live_liability_uids


def _create_exchange(uid: str, call_id: str) -> tuple[AIMessage, ToolMessage]:
    call = {
        "name": "blade_create",
        "id": call_id,
        "args": {"command": "blade create k8s node-network loss"},
    }
    return (
        AIMessage(content="", tool_calls=[call]),
        ToolMessage(
            content=json.dumps({"code": 200, "success": True, "result": uid}),
            name="blade_create",
            tool_call_id=call_id,
        ),
    )


def _destroy_exchange(uid: str, call_id: str) -> tuple[AIMessage, ToolMessage]:
    """A PROVEN destroy: the real blade destroy success payload (JSON
    ``success:true``) — the pre-J1 ``"ok"`` fixture proved nothing under the
    proven dialect (no success/destroyed vocabulary), silently pinning the
    issued semantics instead."""
    call = {"name": "blade_destroy", "id": call_id, "args": {"uid": uid}}
    return (
        AIMessage(content="", tool_calls=[call]),
        ToolMessage(
            content=json.dumps(
                {"code": 200, "success": True, "result": {"Uid": uid}},
            ),
            name="blade_destroy",
            tool_call_id=call_id,
        ),
    )


def _failed_destroy_exchange(uid: str, call_id: str) -> tuple[AIMessage, ToolMessage]:
    """An ISSUED-but-FAILED destroy: the call went out, the output proves
    the kill did NOT happen — the J1a drift scenario."""
    call = {"name": "blade_destroy", "id": call_id, "args": {"uid": uid}}
    return (
        AIMessage(content="", tool_calls=[call]),
        ToolMessage(
            content="Error: exit status 1",
            name="blade_destroy",
            tool_call_id=call_id,
        ),
    )


def test_birth_registry_survives_attribution_overwrite():
    """The G orphan chain's core erase: contract-2's create overwrites the
    single slot to E2 — correct for attribution, but the liability view must
    keep BOTH experiments owed (replacing the intent does not dissolve the
    side effects already running in the cluster)."""
    ai, tool = _create_exchange("e1a1b2c3d4e5f607", "c1")
    state = {
        "messages": [ai, tool],
        "experiment_uid": "uid-e2",
        "owned_experiment_uids": ["e1a1b2c3d4e5f607", "uid-e2"],
    }
    assert live_liability_uids(state) == ["e1a1b2c3d4e5f607", "uid-e2"]


def test_hydration_fallback_rebuilds_ownership_from_provenance():
    """Legacy checkpoints predate the birth registry: ownership rebuilds
    from the create-message provenance scan (the same fallback shape as
    ``materialize_fault_handle``); compacted-away creates simply yield an
    empty set — no false positives."""
    ai, tool = _create_exchange("e1a1b2c3d4e5f607", "c1")
    assert live_liability_uids({"messages": [ai, tool]}) == ["e1a1b2c3d4e5f607"]
    # Compacted history (no create evidence, no registry): empty net.
    assert live_liability_uids({"messages": []}) == []
    assert live_liability_uids({}) == []


def test_retired_registry_filters_liability():
    """Framework-side destroys (approval seam / recover sweep) leave no
    ToolMessage — the retired registry is their only death proof, and it
    must reduce the live set (idempotence across finalize passes)."""
    state = {
        "messages": [],
        "owned_experiment_uids": ["e1a1b2c3d4e5f607", "uid-e2"],
        "retired_experiment_uids": ["e1a1b2c3d4e5f607"],
    }
    assert live_liability_uids(state) == ["uid-e2"]


def test_message_provenance_destroy_filters_liability():
    """An LLM-driven blade_destroy leaves message evidence — BOTH death
    proofs (message scan ∪ retired registry) reduce the liability set."""
    create_ai, create_tool = _create_exchange("e1a1b2c3d4e5f607", "c1")
    destroy_ai, destroy_tool = _destroy_exchange("e1a1b2c3d4e5f607", "d1")
    state = {
        "messages": [create_ai, create_tool, destroy_ai, destroy_tool],
        "owned_experiment_uids": ["e1a1b2c3d4e5f607"],
    }
    assert live_liability_uids(state) == []


def test_hydration_applies_the_same_death_filter():
    """Legacy shape (no registry) with create + destroy evidence in history:
    the hydration fallback applies the same death filter — the rebuild never
    resurrects a proven-dead experiment."""
    create_ai, create_tool = _create_exchange("e1a1b2c3d4e5f607", "c1")
    destroy_ai, destroy_tool = _destroy_exchange("e1a1b2c3d4e5f607", "d1")
    state = {"messages": [create_ai, create_tool, destroy_ai, destroy_tool]}
    assert live_liability_uids(state) == []


def test_duplicate_births_dedupe_birth_order_preserved():
    """A retried create returning the same UID must not double-count; the
    first-seen birth order is the stable iteration contract."""
    state = {"messages": [], "owned_experiment_uids": ["uid-a", "uid-b", "uid-a"]}
    assert live_liability_uids(state) == ["uid-a", "uid-b"]


def test_failed_destroy_keeps_liability_alive():
    """J1a — the drift's core scenario: an ISSUED-but-FAILED destroy leaves
    the experiment possibly-alive and still owed. The liability filter must
    NOT treat the destroy call as death (the issued dialect would silently
    orphan exactly the experiment the sweep exists to retry); doubt stays
    live until the sweep retries or the convergence valve proves the death.
    """
    create_ai, create_tool = _create_exchange("e1a1b2c3d4e5f607", "c1")
    destroy_ai, destroy_tool = _failed_destroy_exchange("e1a1b2c3d4e5f607", "d1")
    state = {
        "messages": [create_ai, create_tool, destroy_ai, destroy_tool],
        "owned_experiment_uids": ["e1a1b2c3d4e5f607"],
    }
    assert live_liability_uids(state) == ["e1a1b2c3d4e5f607"]


def test_unpaired_destroy_call_keeps_liability_alive():
    """Evidence not yet returned: a destroy AIMessage without its paired
    ToolMessage output proves nothing (the call may be in flight or have
    failed silently) — the UID stays owed."""
    create_ai, create_tool = _create_exchange("e1a1b2c3d4e5f607", "c1")
    destroy_ai, _ = _destroy_exchange("e1a1b2c3d4e5f607", "d1")
    state = {
        "messages": [create_ai, create_tool, destroy_ai],
        "owned_experiment_uids": ["e1a1b2c3d4e5f607"],
    }
    assert live_liability_uids(state) == ["e1a1b2c3d4e5f607"]


def test_issued_dialect_still_governs_attribution_scan():
    """Dialect-separation pin: the ATTRIBUTION-side scan
    (:func:`scan_destroyed_uids`) must stay ISSUED — a destroy attempt,
    success or failure, stops the UID being re-claimed as the live fault.
    The two dialects are deliberately opposite; "unifying" them in either
    direction regresses one axis or the other."""
    from chaos_agent.agent.providers.chaosblade.verify import scan_destroyed_uids

    destroy_ai, _ = _failed_destroy_exchange("e1a1b2c3d4e5f607", "d1")
    assert scan_destroyed_uids([destroy_ai]) == {"e1a1b2c3d4e5f607"}
