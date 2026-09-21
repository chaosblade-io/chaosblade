"""Recovery-recipe hydration across the graph boundary (audit #1).

WHY A SEPARATE FILE: the carrier's own test homes
(``test_faultdrill_provider.py``, ``test_faultdrill_restore.py``) and the
three source modules they pin are all on the M2
(faultdrill-cluster-native-recovery) change surface right now, so these
reproductions live apart rather than colliding with that work.

THE DEFECT UNDER REPRODUCTION
-----------------------------
The restore recipe — which object to patch back, and to what baseline
value — has exactly ONE read path today: the assembler receipt sitting in
the MESSAGE HISTORY (``build_handle_from_messages`` →
``_assembler_handle_from_events`` → ``parse_receipt(event["result"])`` →
``receipt["artifact"]["recovery_handle"]``).

Both of its legs fail on a cross-task recover (``blade-ai recover
--task-id``):

1. ``build_recover_initial_from_checkpoint`` starts from
   ``recover_reset_state()`` and its ``initial.update({...})`` carries NO
   ``messages`` key — inject history is flattened into the
   ``inject_context`` STRING. So the recover graph's ``messages`` cannot
   contain the receipt at all, compacted or not.
2. The ``fault_handle`` it does carry is the values-stage projection:
   ``build_fault_handle`` returns ``{"kind": ..., "method": ...}`` and its
   own docstring calls that "the minimal kind-bearing handle", with the
   authoritative value/recipe left to "the MESSAGES stage".

``_recipe_face_handle`` therefore finds a non-recipe-bearing handle,
tries to hydrate from empty messages, and returns the bare handle;
``_replay_restore_recipe`` then hits its no-recipe guard and reports
``skipped`` → ``unrecovered`` + RECOVERY_FAILED. The verdict is HONEST
(it says "Verify the cluster state manually", never a fabricated
success), but the deterministic early-convergence replay is lost and the
fault rides out the armed carrier's own deadline instead.

A durable recipe copy already travels with the recover state on BOTH
resolution paths — ``execution_artifacts`` is carried by
``build_recover_initial_from_checkpoint`` AND by the TaskSnapshot leg, and
each ``recovery_carrier`` artifact embeds the full ``recovery_handle``
(``build_carrier_artifact``). Nothing on the recover path reads it for
the recipe today; it is consumed only to tear the carrier stack down.

TEST SHAPE
----------
These were written RED-FIRST: the two hydration tests in section 2 failed
on the single-source code (``assert 0 == 1`` — no replay ran) and turned
green when ``recipe_ledger`` added the artifact-sourced rung. They stay as
the permanent guard on that rung.

Section 4 covers the SECOND Layer-1 entry, ``layer1_destroy``, dispatched
by the LLM-driven recover verifier loop. It is mutually exclusive with the
no-LLM ``recover`` entry the other sections exercise, so covering one
would have left the other single-sourced.

``test_recover_state_carries_the_recipe_but_not_the_messages`` pins the
PREMISE rather than the fix: it asserts the graph-boundary facts the defect
stood on, so a future change to what crosses into recover state fails here
with an explanation instead of silently re-stranding the recipe.

The remaining tests guard the rung's edges — message history keeps
precedence when readable (so the ledger is a fallback, not a takeover), a
genuinely recipe-less state still reports the honest verdict (so the rung
cannot manufacture a convergence), and a CR-face dispatch is never
re-routed onto the carrier recipe the ledger holds (so the rung cannot
widen the identity it was asked to recover).
"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.agent.providers.faultdrill.assembler import (
    ASSEMBLER_TOOL_NAME,
    build_carrier_artifact,
)
from chaos_agent.agent.providers.faultdrill.provider import FaultDrillProvider
from chaos_agent.agent.state_mgmt.recovery_state import (
    build_recover_initial_from_checkpoint,
)

# ---------------------------------------------------------------------------
# Shared fixtures — the carrier face's canonical shape
# ---------------------------------------------------------------------------

_TARGET_REF = {
    "kind": "Deployment", "name": "web", "namespace": "cms-demo",
}
_RESTORE_PATCHES = [{"op": "replace", "path": "/spec/x", "value": "z"}]
_INJECT_PATCHES = [{"op": "replace", "path": "/spec/x", "value": "FAULT"}]
_CARRIER_NS = "cms-demo"
_CARRIER_NAME = "fd-carrier-1"
_INJECT_TASK_ID = "inject-aaaa1111"

# The values-stage projection — what ``_project_fault_handle`` actually
# writes into inject state for the carrier face, and therefore what every
# recover entry inherits. Deliberately spelled out rather than derived so a
# future widening of ``build_fault_handle`` cannot silently make these
# reproductions pass for the wrong reason.
_BARE_HANDLE = {"kind": "faultdrill_cr", "method": "faultdrill_carrier"}


def _recovery_handle() -> dict:
    """The recipe as ``_receipt`` builds it — note it carries BOTH the
    inject-side ``patches`` and the recovery-side ``restore_patches``; the
    recovery identity must project only the latter."""
    return {
        "kind": "recovery_carrier",
        "value": f"{_CARRIER_NS}/{_CARRIER_NAME}",
        "target_ref": dict(_TARGET_REF),
        "patches": list(_INJECT_PATCHES),
        "restore_patches": list(_RESTORE_PATCHES),
        "duration_seconds": 600,
        "recovery_deadline_epoch": 1_900_000_000.0,
        "carrier": {
            "name": _CARRIER_NAME, "namespace": _CARRIER_NS,
            "image": "busybox:1.36",
        },
    }


def _carrier_artifact() -> dict:
    """The registered artifact, built by the production builder so the
    tests pin the real ledger shape (``recovery_handle`` nested inside)."""
    return build_carrier_artifact(
        name=_CARRIER_NAME, namespace=_CARRIER_NS, task_id=_INJECT_TASK_ID,
        rules=[{"kind": "deployment", "verbs": ["patch"]}],
        duration_seconds=600, deadline_epoch=1_900_000_000.0,
        recovery_handle=_recovery_handle(),
    )


def _receipt_messages(*, truncate: bool = False) -> list:
    """Inject history holding ONE registerable assembler receipt — the
    ``success`` shape the recipe rides in (``artifact.recovery_handle``; the
    tool strips the top-level copy before returning).

    ``truncate`` reproduces the memory.tool_compactor path: everything but
    the most recent five tool results is cut to 1KB, and a 2-4KB receipt
    comes out the other side as invalid JSON — which is exactly what
    ``parse_receipt`` refuses to guess at."""
    receipt = {
        "status": "success",
        "error": "",
        "carrier": {
            "name": _CARRIER_NAME, "namespace": _CARRIER_NS,
            "image": "busybox:1.36", "armed": True,
            "recovery_timeout_seconds": 600,
            "recovery_deadline_epoch": 1_900_000_000.0,
            "landing_verified": True,
        },
        "artifact": _carrier_artifact(),
        "steps": [{"cmd": "kubectl run ...", "ok": True}],
    }
    body = json.dumps(receipt)
    if truncate:
        # The compactor's cut is a byte truncation, not a JSON edit.
        body = body[: len(body) // 3]
    return [
        AIMessage(
            content="",
            tool_calls=[{
                "id": "call-1", "name": ASSEMBLER_TOOL_NAME,
                "args": {
                    "target_kind": "Deployment", "target_name": "web",
                    "namespace": _CARRIER_NS, "patches": "[]",
                    "restore_patches": "[]", "duration_seconds": 600,
                },
                "type": "tool_call",
            }],
        ),
        ToolMessage(
            content=body, name=ASSEMBLER_TOOL_NAME, tool_call_id="call-1",
        ),
    ]


def _inject_values(*, truncate: bool = False) -> dict:
    """The inject graph's terminal state, as the recover entry reads it."""
    return {
        "task_id": _INJECT_TASK_ID,
        "injection_method": "faultdrill_carrier",
        "messages": _receipt_messages(truncate=truncate),
        "execution_artifacts": [_carrier_artifact()],
        "fault_handle": dict(_BARE_HANDLE),
        "kubeconfig": "/tmp/inject-kubeconfig",
    }


@pytest.fixture()
def _replay_stub(monkeypatch):
    """Keep the replay off the wire and journal it.

    Mirrors ``test_faultdrill_provider._recover_stub`` (duplicated rather
    than imported: that file is on the M2 change surface). ``restore`` is
    the canned convergence verdict; the journal records how many replays
    ran and the exact ``task_state`` projection each one got, so a test can
    tell "hydrated the right recipe" from "hydrated something". Any live
    ``_kubectl`` dispatch fails loudly — a monkeypatched ``_do_restore``
    never touches the wire, so one here means the replay bypassed the
    shared core."""
    import chaos_agent.agent.providers.faultdrill.provider as fd_provider
    import chaos_agent.agent.providers.faultdrill.restore as fd_restore

    journal: dict = {
        "restore": True,
        "calls": {"restore": 0, "restore_state": None},
    }

    async def fake_kubectl(sub, v_args, kubeconfig, *, stdin_data="", timeout=30.0):
        raise AssertionError(
            "a monkeypatched _do_restore must keep the replay off the "
            f"wire — unexpected {sub!r} dispatch"
        )

    async def fake_restore(task_state, kubeconfig):
        journal["calls"]["restore"] += 1
        journal["calls"]["restore_state"] = task_state
        return journal["restore"]

    monkeypatch.setattr(fd_provider, "_kubectl", fake_kubectl)
    monkeypatch.setattr(fd_restore, "_do_restore", fake_restore)
    return journal


# ---------------------------------------------------------------------------
# 1. The premise — pins current behaviour, NOT the desired one
# ---------------------------------------------------------------------------


def test_recover_state_carries_the_recipe_but_not_the_messages():
    """Evidence for the defect: the recover initial state holds a complete
    recipe copy in ``execution_artifacts`` and no messages at all, while the
    ``fault_handle`` it inherits is the bare values-stage projection.

    Passes on current code by design — it is the premise the two red tests
    below stand on, and it fails loudly if either leg changes shape."""
    initial = build_recover_initial_from_checkpoint(
        _inject_values(), _INJECT_TASK_ID, record_task_id="recover-bbbb2222",
    )

    # Leg 1 — the message history does not cross the graph boundary.
    assert not initial.get("messages"), (
        "recover state now carries inject messages; the recipe's "
        "message-history read path may no longer be the only one"
    )
    # ...but the injection is still described, just as a flattened string.
    assert initial.get("inject_context")

    # Leg 2 — the inherited handle is a bare shell: no recipe, no value.
    assert initial.get("fault_handle") == _BARE_HANDLE
    assert "restore_patches" not in (initial.get("fault_handle") or {})
    assert not (initial.get("fault_handle") or {}).get("value")

    # And yet the durable recipe copy DID cross, nested in the ledger.
    artifacts = initial.get("execution_artifacts") or []
    assert len(artifacts) == 1
    carried = (artifacts[0].get("recovery_handle") or {})
    assert carried.get("target_ref") == _TARGET_REF
    assert carried.get("restore_patches") == _RESTORE_PATCHES


def test_snapshot_leg_also_persists_the_recipe():
    """The other resolution path agrees: ``build_recovery_handle`` files the
    full artifact list under ``{"kind": "artifact", ...}`` at finalize time,
    so a recover resolved from the persisted TaskSnapshot has the same
    durable copy available — and the same unread-recipe problem."""
    from chaos_agent.agent.result.operation_result import build_recovery_handle

    handle = build_recovery_handle(_inject_values())
    assert (handle or {}).get("kind") == "artifact"
    artifacts = (handle or {}).get("artifacts") or []
    assert len(artifacts) == 1
    assert (artifacts[0].get("recovery_handle") or {}).get(
        "restore_patches"
    ) == _RESTORE_PATCHES


# ---------------------------------------------------------------------------
# 2. The hydration itself — the recipe must come off the ledger the state
#    already carries (both were red on the single-source code)
# ---------------------------------------------------------------------------


async def test_cross_task_recover_replays_the_ledger_recipe(_replay_stub):
    """A cross-task recover must converge deterministically.

    Failed on the single-source code (no rung read ``execution_artifacts``,
    so the replay was skipped and the run reported ``unrecovered`` while an
    armed carrier — and a live fault — was left to its own deadline); green
    since ``recipe_ledger`` added the artifact-sourced rung."""
    initial = build_recover_initial_from_checkpoint(
        _inject_values(), _INJECT_TASK_ID, record_task_id="recover-bbbb2222",
    )

    result = await FaultDrillProvider().recover(
        initial,
        initial.get("fault_handle"),
        kubeconfig="/tmp/inject-kubeconfig",
        messages=initial.get("messages", []),
    )

    assert _replay_stub["calls"]["restore"] == 1, (
        "no replay ran: the recipe was not hydrated from the recover "
        "state's execution_artifacts ledger"
    )
    # The recovery identity is target_ref + restore_patches ONLY — the
    # inject-side ``patches`` describe the fault, not its undo, and must not
    # leak into what gets replayed.
    assert _replay_stub["calls"]["restore_state"] == {
        "target_ref": _TARGET_REF,
        "restore_patches": _RESTORE_PATCHES,
        "invalid_secret": {},
    }
    assert result.recovered is True
    assert result.level == "recovered"
    assert result.failure is None


async def test_compacted_receipt_falls_back_to_the_ledger_recipe(_replay_stub):
    """The same-task shape of the defect: inject history still present, but
    the receipt has been compacted past parseability while the ledger copy
    survives.

    Failed on the single-source code for the same missing rung. This is the
    leg the original audit described ("five more tool calls and the receipt
    is out of the recent window"); the cross-task test above shows it is in
    fact the milder of the two, since that one cannot even reach the
    receipt."""
    truncated = _receipt_messages(truncate=True)
    state = {
        "task_id": _INJECT_TASK_ID,
        "injection_method": "faultdrill_carrier",
        "execution_artifacts": [_carrier_artifact()],
        "fault_handle": dict(_BARE_HANDLE),
    }

    result = await FaultDrillProvider().recover(
        state,
        state["fault_handle"],
        kubeconfig="/tmp/inject-kubeconfig",
        messages=truncated,
    )

    assert _replay_stub["calls"]["restore"] == 1, (
        "the compacted receipt was refused (correctly) but nothing fell "
        "back to the ledger copy"
    )
    assert _replay_stub["calls"]["restore_state"]["restore_patches"] == (
        _RESTORE_PATCHES
    )
    assert result.recovered is True


# ---------------------------------------------------------------------------
# 3. Guards on the rung's edges — precedence and honesty
# ---------------------------------------------------------------------------


async def test_message_history_still_wins_when_it_is_readable(_replay_stub):
    """Guard on the fix's precedence: a readable receipt in the history is
    the LATEST face evidence and must keep hydrating the recipe, so adding a
    ledger rung may not change what a same-task recover with intact history
    already does.

    Green before the fix and after — it pins the behaviour the rung must
    preserve, and catches a fix that simply reorders the rungs the wrong way
    round."""
    messages = _receipt_messages()
    state = {
        "task_id": _INJECT_TASK_ID,
        "injection_method": "faultdrill_carrier",
        "execution_artifacts": [_carrier_artifact()],
        "fault_handle": dict(_BARE_HANDLE),
    }

    result = await FaultDrillProvider().recover(
        state, state["fault_handle"], kubeconfig="", messages=messages,
    )

    assert _replay_stub["calls"]["restore"] == 1
    assert _replay_stub["calls"]["restore_state"]["restore_patches"] == (
        _RESTORE_PATCHES
    )
    assert result.recovered is True


async def test_no_recipe_anywhere_still_reports_unrecovered(_replay_stub):
    """Guard on the fix's honesty: with neither a readable receipt nor a
    ledger copy there is nothing to replay, and the verdict must stay the
    honest ``unrecovered`` + RECOVERY_FAILED with its manual-verification
    warning — a ledger rung must not manufacture a convergence.

    Green before the fix and after."""
    state = {
        "task_id": _INJECT_TASK_ID,
        "injection_method": "faultdrill_carrier",
        "execution_artifacts": [],
        "fault_handle": dict(_BARE_HANDLE),
    }

    result = await FaultDrillProvider().recover(
        state, state["fault_handle"], kubeconfig="", messages=[],
    )

    assert _replay_stub["calls"]["restore"] == 0
    assert result.recovered is False
    assert result.level == "unrecovered"
    assert result.failure is not None
    assert result.failure[0].name == "RECOVERY_FAILED"
    assert "manually" in result.warnings[0]


async def test_ledger_rung_refuses_to_drift_onto_the_other_face(_replay_stub):
    """The ledger only ever holds the CARRIER face, so a dispatch pinned to
    the CR face must NOT be silently re-routed onto a carrier recipe.

    This is the constraint that keeps the rung a fallback rather than a
    widening. The ledger copy is complete and would replay successfully —
    which is exactly the danger: handing it to a CR-face dispatch looks
    like a hydrated recovery while replaying an identity the dispatch never
    asked for. The expected outcome is the honest no-recipe failure."""
    cr_face_handle = {
        "kind": "faultdrill_cr",
        "value": f"{_CARRIER_NS}/fd-cr-1",
        "method": "faultdrill_cr",
    }
    state = {
        "task_id": _INJECT_TASK_ID,
        "injection_method": "faultdrill_cr",
        "execution_artifacts": [_carrier_artifact()],
        "fault_handle": dict(cr_face_handle),
    }

    result = await FaultDrillProvider().recover(
        state, state["fault_handle"], kubeconfig="", messages=[],
    )

    assert _replay_stub["calls"]["restore"] == 0, (
        "the CR-face dispatch was re-routed onto the carrier ledger recipe"
    )
    assert result.recovered is False
    assert result.level == "unrecovered"


# ---------------------------------------------------------------------------
# 4. The OTHER Layer-1 entry — ``layer1_destroy``, dispatched by the
#    LLM-driven recover verifier loop and mutually exclusive with the
#    no-LLM ``recover`` entry above
# ---------------------------------------------------------------------------


async def test_layer1_destroy_hydrates_the_recipe_from_the_ledger(_replay_stub):
    """The verifier-loop entry must hydrate off the same two rungs.

    It used to call ``build_handle_from_messages`` directly — messages
    only, no dispatch handle, no ledger — so a cross-task recover arriving
    through THIS door skipped the replay and handed a still-armed fault to
    Layer 2. Covering only ``recover`` would have left that path
    single-sourced."""
    result = await FaultDrillProvider().layer1_destroy(
        "", "/tmp/inject-kubeconfig",
        messages=[], artifacts=[_carrier_artifact()],
    )

    assert _replay_stub["calls"]["restore"] == 1, (
        "no replay ran: layer1_destroy is still single-sourced on messages"
    )
    assert _replay_stub["calls"]["restore_state"]["restore_patches"] == (
        _RESTORE_PATCHES
    )
    assert result.status == "passed"


async def test_layer1_destroy_still_reads_the_message_history(_replay_stub):
    """Same precedence guard on this entry: intact inject history keeps
    hydrating the recipe with an empty ledger, so routing it through the
    shared helper did not demote the message rung."""
    result = await FaultDrillProvider().layer1_destroy(
        "", "", messages=_receipt_messages(), artifacts=[],
    )

    assert _replay_stub["calls"]["restore"] == 1
    assert result.status == "passed"


async def test_layer1_destroy_without_any_evidence_skips_honestly(_replay_stub):
    """Honesty guard on this entry: no receipt and no ledger copy means no
    replay, and ``skipped`` — which HERE is not terminal, so Layer 2 still
    judges the fault's disappearance from cluster evidence (a milder
    consequence than the ``recover`` entry's ``unrecovered``)."""
    result = await FaultDrillProvider().layer1_destroy(
        "", "", messages=[], artifacts=[],
    )

    assert _replay_stub["calls"]["restore"] == 0
    assert result.status == "skipped"


async def test_recover_loop_wrapper_hands_the_ledger_to_the_provider(
    monkeypatch,
):
    """The generic flow's half of the contract: the wrapper reads
    ``execution_artifacts`` off the state it already owns and passes it
    down, so the two evidence sources travel together.

    The assertion is deliberately carrier-agnostic — this layer must not
    know what an artifact MEANS, only that the list reaches the provider.
    A future UID-less deterministic carrier inherits the rung from this
    wiring alone, with no change here."""
    from chaos_agent.agent.nodes.recover import _recover_verifier_loop as loop

    seen: dict = {}

    class _RecordingProvider:
        async def layer1_destroy(self, uid, kubeconfig="", **kwargs):
            seen["uid"] = uid
            seen["kubeconfig"] = kubeconfig
            seen.update(kwargs)
            return "verdict"

    monkeypatch.setattr(
        loop, "_provider_for_recover",
        lambda state, handle=None: _RecordingProvider(),
    )

    out = await loop._layer1_destroy_via_provider(
        {"execution_artifacts": [_carrier_artifact()]},
        "", "/tmp/kc",
        messages=["m"], injection_method="faultdrill_carrier",
    )

    assert out == "verdict"
    assert seen["uid"] == ""
    assert seen["kubeconfig"] == "/tmp/kc"
    assert seen["messages"] == ["m"]
    assert seen["artifacts"] == [_carrier_artifact()], (
        "the wrapper did not hand the ledger over — a UID-less carrier's "
        "recipe hydration stays single-sourced on messages"
    )
