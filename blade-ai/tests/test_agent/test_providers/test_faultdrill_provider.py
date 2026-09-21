"""FaultDrillProvider conformance — cluster-native recovery (M1/M2).

The generic conformance suite derives its domain from the registry
snapshot at import time — with the dark-launch flag OFF (the default)
FaultDrillProvider is deliberately absent, so THIS file is the
carrier's explicit conformance home, pinning:

1. Protocol surface — the structural contract plus the carrier's own
   design pins (UID-less deterministic-recover carrier; two attribution
   faces — the assembler tool call and the migration-window stdin
   manifest DOCUMENT KIND with ZERO verb/tool vocabulary — no recency
   competition with k8s_native, no binary/tool-face expansion).
2. Registration gating — flag off = structurally absent (short-circuit);
   flag on = tail of the builtin order, idempotent; flag back off =
   reconciled down (no stale registration behind the gate).
3. Attribution family — stdin manifest DOCUMENT KIND is the only legacy
   attribution signal (command-line form never matches), the shared
   ATTEMPT rule, the ns/name handle resolution chain.
4. Manifest whitelist & K1 anchoring — the ``faultdrill`` kind rides the
   stdin-manifest channel with per-document anchoring, legislated
   SINGLE-document for the CR kind (P7: one apply = one CR = one recovery
   handle — a second document form-issues); the CRD itself
   stays off the LLM face (D2 — guards are not migrated, ND3).
5. Assembler face (cluster-native recovery M1/R2) — the
   ``faultdrill_assemble_carrier`` tool's attribution, latest-face
   naming, recipe-bearing handle hydration, the four-ring issue-time
   chain, and the deterministic recover gate (kind shared with the CR
   face, method distinguishes).
6. Deterministic recover convergence (M2 task 2.3, design D4) — the
   four-state pins (pending deletion / injected restore convergence /
   failed terminal cleanup / recovered zero-writes / not-found residue
   with zero actions + "unverified"), the two-stage handle hydration,
   the P12 cross-namespace redirect, and the D1 seam-compat gates (a
   UID-less deterministic handle is never misrouted "llm_driven").
7. Artifact ledger (M2 task 2.6, review P11) — the FULL landed set
   (every landed apply under its own ns/name, not just the latest-wins
   handle), the keep-while-Injected sweep state machine (Injected keeps
   / Recovered settles / NotFound zero-write / everything else stays
   with recover-convergence), and the registry's aggregate-collect /
   first-claim-wins seams.
8. Unit-test group residual pins — the execute-time apply-error family
   anchor (the Forbidden-vs-schema-rejection split at the attribution
   layer), and the non-migration-domain equivalence (chaosblade / host
   attribution unchanged by carrier registration). The master-switch
   side of the dark launch is the whole suite itself: the default
   (``faultdrill_enabled=False``) is what every test above runs under,
   and the per-surface off-pins live in their own sections
   (registration short-circuit here §2, prompt byte identity / probe
   scheduling in test_prompts & preplan_probe, artifact
   zero-registration in test_execution_artifacts, gate all-reject in
   test_screener).
"""

from __future__ import annotations

import base64
import json
import re
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from chaos_agent.tools.guard import CommandResult

from chaos_agent.agent.nodes.recover._recover_verifier_loop import (
    _deterministic_recover_identity,
    recover_verifier,
)
from chaos_agent.agent.providers import FaultProvider, FaultProviderRegistry
from chaos_agent.agent.providers.faultdrill.crd import CRD_KIND
from chaos_agent.agent.providers.faultdrill.declaration import (
    CARRIER_ID,
    SUPPORTED_ACTIONS,
    SUPPORTED_TARGETS,
)
from chaos_agent.agent.providers.faultdrill.provider import (
    FaultDrillProvider,
    _result_is_error,
)
from chaos_agent.agent.providers.k8s_native.classifier import (
    ALLOWED_MANIFEST_KINDS,
    _classify_kubectl_stdin_manifest,
)
from chaos_agent.agent.providers.message_scanning import KUBECTL_WRITE_SUBCOMMANDS
from chaos_agent.agent.result.verdict import FailureCategory
from chaos_agent.config.settings import settings


@pytest.fixture(autouse=True)
def _restore_dark_launch():
    """Every test here may flip the flag; the PRE-TEST value must
    survive (restore-the-original — the default flipped True post
    dark-launch, so hardcode-off here would leak False into the
    module's implicit-default teeth)."""
    _orig = settings.faultdrill_enabled
    try:
        yield
    finally:
        settings.faultdrill_enabled = _orig
        FaultProviderRegistry.register_builtins()


def _apply_call(
    call_id: str, stdin_data: str, *, v_args: str = "-f -",
    tool: str = "kubectl", subcommand: str = "apply",
) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{
            "id": call_id,
            "name": tool,
            "args": {
                "subcommand": subcommand,
                "v_args": v_args,
                "stdin_data": stdin_data,
            },
        }],
    )


_FAULTDRILL_MANIFEST = """apiVersion: drill.blade-ai.io/v1alpha1
kind: FaultDrill
metadata:
  name: fd-demo1
  namespace: cms-demo
spec:
  action: secretSwap
"""

#: A migration-window manifest carrying the FULL spec recipe — the shape
#: the ledger-model recover (task 2.3, ND7) replays: the handle projects
#: targetRef/restorePatches from the APPLIED MANIFEST, never a CR read.
_RECIPE_MANIFEST = """apiVersion: drill.blade-ai.io/v1alpha1
kind: FaultDrill
metadata:
  name: fd-demo1
  namespace: cms-demo
spec:
  targetRef:
    kind: Deployment
    name: web
    namespace: cms-demo
  patches:
  - op: replace
    path: /spec/x
    value: "y"
  restorePatches:
  - op: replace
    path: /spec/x
    value: "z"
"""


# ---------------------------------------------------------------------------
# 1. Protocol surface + channel design pins
# ---------------------------------------------------------------------------


def test_satisfies_protocol():
    assert isinstance(FaultDrillProvider(), FaultProvider)


def test_carrier_and_method_ids():
    p = FaultDrillProvider()
    assert p.carrier == CARRIER_ID == "faultdrill_cr"
    # R2 review Bug#3: the assembler face joins the method index — one
    # backend, two faces (the CR apply and the programmatic assembler
    # tool), both resolving to THIS provider.
    assert p.injection_methods == ("faultdrill_cr", "faultdrill_carrier")


def test_capability_pins():
    p = FaultDrillProvider()
    # UID-less carrier whose recovery IS deterministic (reconcile
    # convergence) — the property separating it from k8s_native.
    assert p.has_experiment_uid is False
    assert p.has_deterministic_recover is True
    assert p.uid_less_verdict_default is False
    assert p.is_multi_step is False
    # Recovery handle: the CR reference (value = ns/name).
    assert p.handle_kind == "faultdrill_cr"


def test_vocabulary_is_empty_by_design():
    p = FaultDrillProvider()
    # Routing is metadata-driven (skill-case recovery_channel + planning
    # + write-set validation), never scope-bridged: the intent vocabulary
    # contributes zero, so INTENT_TARGETS / INTENT_ACTIONS stay
    # byte-identical while the channel exists.
    assert tuple(p.supported_targets) == tuple(SUPPORTED_TARGETS) == ()
    assert tuple(p.supported_actions) == tuple(SUPPORTED_ACTIONS) == ()
    # Zero tool/binary face: rides the existing kubectl surface bound by
    # K8sNativeProvider — no new guard surface, no Gate-① expansion.
    assert p.inject_tool_names == frozenset()
    assert p.inject_kubectl_subcommands == frozenset()
    assert p.injection_binaries == frozenset()
    assert p.kubeconfig_scoped_tool_names == frozenset()
    assert p.audit_scoped_tool_names == frozenset()
    assert p.log_shipping_tool_names == frozenset()
    assert p.tool_pod_namespaces == frozenset()
    assert p.reconcile_create_tool_names == frozenset()
    assert p.reconcile_read_tool_names == frozenset()


def test_apply_verb_is_not_claimed():
    """The zero-recency-competition pin: ``apply`` is in NEITHER set.

    Attribution keys on the stdin manifest document kind (detect), not on
    the command-line form — an ordinary ``kubectl apply`` (Deployment,
    ConfigMap, …) never competes with k8s_native's write verbs."""
    assert "apply" not in KUBECTL_WRITE_SUBCOMMANDS
    assert "apply" not in FaultDrillProvider().inject_kubectl_subcommands


def test_matches_channel_k8s_only():
    p = FaultDrillProvider()
    assert p.matches_channel("k8s") is True
    assert p.matches_channel("host") is False
    assert p.matches_channel("bogus") is False


def test_tools_execute_only_assembler():
    # faultdrill-cluster-native-recovery M1: the EXECUTE phase now carries
    # the provider's ONE own tool — the programmatic recovery-carrier
    # assembler (blade_create precedent; the CR apply keeps riding the
    # standard kubectl face for the M2 migration window). Every other
    # phase — and any unknown phase — still contributes nothing.
    from chaos_agent.agent.providers.faultdrill.assembler import (
        ASSEMBLER_TOOL_NAME,
    )

    p = FaultDrillProvider()
    assert [t.name for t in p.tools("execute")] == [ASSEMBLER_TOOL_NAME]
    for phase in ("plan", "verify", "recover_verify"):
        assert p.tools(phase) == []
    assert p.tools("no_such_phase") == []


# ---------------------------------------------------------------------------
# 2. Registration gating (dark launch)
# ---------------------------------------------------------------------------


def test_flag_off_registration_short_circuit():
    # Explicitly OFF — the post-flip default is True; this tooth pins the
    # CHANNEL-OFF short-circuit, not the default.
    settings.faultdrill_enabled = False
    FaultProviderRegistry.register_builtins()
    carriers = [p.carrier for p in FaultProviderRegistry.all_providers()]
    assert "faultdrill_cr" not in carriers
    assert FaultProviderRegistry.resolve_by_method("faultdrill_cr") is None
    # The k8s scope's candidates are the historical pair — the channel
    # is structurally absent, not merely silent.
    assert [p.carrier for p in FaultProviderRegistry.resolve_by_scope("pod")] == [
        "chaosblade", "k8s_native",
    ]


def test_flag_on_tail_order_and_idempotency():
    settings.faultdrill_enabled = True
    FaultProviderRegistry.register_builtins()
    order = list(FaultProviderRegistry._providers.keys())
    assert order == [
        "chaosblade", "k8s_native", "host_shell", "chaosblade_python",
        "faultdrill_cr",
    ]
    # Idempotent re-registration.
    FaultProviderRegistry.register_builtins()
    assert list(FaultProviderRegistry._providers.keys()) == order
    # Method + handle resolution flow through the registered channel.
    resolved = FaultProviderRegistry.resolve_by_method("faultdrill_cr")
    assert isinstance(resolved, FaultDrillProvider)
    # Scope candidates gain the channel at the tail (precedence intact).
    assert [p.carrier for p in FaultProviderRegistry.resolve_by_scope("pod")] == [
        "chaosblade", "k8s_native", "faultdrill_cr",
    ]


def test_flag_flip_back_reconciles_down():
    settings.faultdrill_enabled = True
    FaultProviderRegistry.register_builtins()
    assert "faultdrill_cr" in FaultProviderRegistry._providers
    settings.faultdrill_enabled = False
    FaultProviderRegistry.register_builtins()
    # register() overwrites but never removes — the reconcile-down pop
    # keeps the dark-launch invariant: no stale registration behind the
    # gate (a later flag-off boot must not inherit an earlier flag-on
    # registration from the same process).
    assert "faultdrill_cr" not in FaultProviderRegistry._providers
    assert FaultProviderRegistry.resolve_by_method("faultdrill_cr") is None


# ---------------------------------------------------------------------------
# 3. Attribution family (stdin manifest document kind)
# ---------------------------------------------------------------------------


def test_detect_fires_on_manifest_kind():
    p = FaultDrillProvider()
    msgs = [
        _apply_call("c1", _FAULTDRILL_MANIFEST),
        ToolMessage(content="faultdrill created", tool_call_id="c1"),
    ]
    assert p.detect(msgs, is_host=False) == "faultdrill_cr"
    assert p.injection_recency(msgs, is_host=False) == 0


def test_detect_host_channel_never_claims():
    p = FaultDrillProvider()
    msgs = [
        _apply_call("c1", _FAULTDRILL_MANIFEST),
        ToolMessage(content="faultdrill created", tool_call_id="c1"),
    ]
    assert p.detect(msgs, is_host=True) is None
    assert p.injection_recency(msgs, is_host=True) == -1


def test_plain_manifest_apply_zero_attribution():
    """A Deployment apply (command line identical to ours) never matches —
    the attribution signal is the DOCUMENT KIND, not the command form."""
    p = FaultDrillProvider()
    plain = _FAULTDRILL_MANIFEST.replace("FaultDrill", "Deployment")
    msgs = [
        _apply_call("c1", plain),
        ToolMessage(content="deployment created", tool_call_id="c1"),
    ]
    assert p.detect(msgs, is_host=False) is None
    assert p.issue_time_method(
        "kubectl", {"subcommand": "apply", "stdin_data": plain},
    ) is None


def test_guard_rejection_is_not_an_attempt():
    p = FaultDrillProvider()
    msgs = [
        _apply_call("c1", _FAULTDRILL_MANIFEST),
        ToolMessage(content="[target_guard] rejected: write-set", tool_call_id="c1"),
    ]
    assert p.detect(msgs, is_host=False) is None
    assert p.was_injection_attempted(msgs) is False


def test_issue_time_method_matches_the_call_before_result():
    p = FaultDrillProvider()
    assert p.issue_time_method(
        "kubectl", {"subcommand": "apply", "stdin_data": _FAULTDRILL_MANIFEST},
    ) == "faultdrill_cr"
    # Other tools / other subcommands never match.
    assert p.issue_time_method(
        "kubectl_read", {"subcommand": "get", "stdin_data": _FAULTDRILL_MANIFEST},
    ) is None
    assert p.issue_time_method("blade", {"command": "create k8s ..."}) is None


def test_handle_from_messages_namespace_chain():
    p = FaultDrillProvider()
    # 1) manifest namespace wins.
    msgs = [
        _apply_call("c1", _FAULTDRILL_MANIFEST),
        ToolMessage(content="created", tool_call_id="c1"),
    ]
    assert p.build_handle_from_messages(msgs) == {
        "kind": "faultdrill_cr", "value": "cms-demo/fd-demo1",
        "method": "faultdrill_cr",
    }
    # 2) manifest omits it → the -n flag anchors.
    ns_free = _FAULTDRILL_MANIFEST.replace("  namespace: cms-demo\n", "")
    msgs = [
        _apply_call("c1", ns_free, v_args="-n ops"),
        ToolMessage(content="created", tool_call_id="c1"),
    ]
    assert p.build_handle_from_messages(msgs)["value"] == "ops/fd-demo1"
    # 3) neither declares it → kubectl's "default".
    msgs = [
        _apply_call("c1", ns_free),
        ToolMessage(content="created", tool_call_id="c1"),
    ]
    assert p.build_handle_from_messages(msgs)["value"] == "default/fd-demo1"


def test_handle_from_messages_requires_a_name():
    p = FaultDrillProvider()
    nameless = _FAULTDRILL_MANIFEST.replace("  name: fd-demo1\n", "")
    msgs = [
        _apply_call("c1", nameless),
        ToolMessage(content="created", tool_call_id="c1"),
    ]
    # Attribution still fires (kind matched) but the handle needs the
    # object identity — a nameless manifest yields no handle.
    assert p.detect(msgs, is_host=False) == "faultdrill_cr"
    assert p.build_handle_from_messages(msgs) is None


def test_handle_from_messages_cr_face_carries_the_recipe():
    """ND7 (task 2.3): the CR-face handle projects the manifest's
    restore-relevant spec — targetRef + restorePatches — so the recover
    replays from the LEDGER. Inject-side fields (``patches``) are
    deliberately excluded: the handle is the RECOVERY identity, not the
    fault description."""
    p = FaultDrillProvider()
    msgs = _landed_messages(_RECIPE_MANIFEST)
    assert p.build_handle_from_messages(msgs) == {
        "kind": "faultdrill_cr",
        "value": "cms-demo/fd-demo1",
        "method": "faultdrill_cr",
        "target_ref": {
            "kind": "Deployment", "name": "web", "namespace": "cms-demo",
        },
        "restore_patches": [{"op": "replace", "path": "/spec/x", "value": "z"}],
    }


def test_handle_from_messages_projects_invalid_secret():
    """The secretSwap recipe's invalidSecret rides the handle too — the
    replay then deletes the derived invalid copy alongside the patches
    (D5: only the marker transits the ledger, never credentials)."""
    manifest = _RECIPE_MANIFEST + """  invalidSecret:
    name: bad-pull-secret
    sourceName: real-pull-secret
"""
    handle = FaultDrillProvider().build_handle_from_messages(
        _landed_messages(manifest)
    )
    assert handle is not None
    assert handle["invalid_secret"] == {
        "name": "bad-pull-secret", "sourceName": "real-pull-secret",
    }


def test_uid_less_neutral_contributions():
    p = FaultDrillProvider()
    msgs = [
        _apply_call("c1", _FAULTDRILL_MANIFEST),
        ToolMessage(content="created", tool_call_id="c1"),
    ]
    assert p.extract_experiment_id(msgs) == ""
    assert p.created_experiment_ids(msgs, {}) == set()
    assert p.destroyed_experiment_ids(msgs) == set()
    assert p.destroyed_proven_experiment_ids(msgs) == set()


def test_issue_disproven_rules():
    p = FaultDrillProvider()
    # Most recent apply errored, nothing landed earlier → disproven.
    msgs = [
        _apply_call("c1", _FAULTDRILL_MANIFEST),
        ToolMessage(content="Error: forbidden: crd create denied", tool_call_id="c1"),
    ]
    assert p.issue_disproven(msgs) is True
    # An earlier LANDED apply keeps the attribution — a later failed
    # re-apply never revokes a live CR.
    msgs = [
        _apply_call("c1", _FAULTDRILL_MANIFEST),
        ToolMessage(content="faultdrill created", tool_call_id="c1"),
        _apply_call("c2", _FAULTDRILL_MANIFEST),
        ToolMessage(content="Error: timeout", tool_call_id="c2"),
    ]
    assert p.issue_disproven(msgs) is False
    # No events at all → nothing to disprove.
    assert p.issue_disproven([]) is False


# The R57/R59 caller-budget-expiry shapes (the outcome-UNKNOWN third
# state; identical bytes to test_issue_disproven.py's native-carrier
# pins — both faces now route through the single-source predicate
# ``message_scanning.is_budget_expiry_unknown``).
_R57_UNKNOWN = (
    "Error: kubectl apply: Command timed out after 300s: kubectl apply -f -\n"
    "Outcome UNKNOWN: only the local wait was killed — the command may "
    "STILL be running server-side. A blind retry can double-execute a "
    "side-effecting command. Reconcile first: re-check the target's "
    "actual state with a read command, then retry only what is genuinely "
    "missing."
)
_R59_UNKNOWN = (
    "Error: task timed out after 300s\n"
    "\n"
    "Outcome UNKNOWN: the CLI's own wait expired — the command may STILL "
    "be running server-side. A blind retry can double-execute a "
    "side-effecting command. Reconcile first: re-check the target's "
    "actual state with a read command, then retry only what is genuinely "
    "missing."
)


def test_issue_disproven_budget_expiry_is_unjudgeable():
    """R66 (fourth case of the B39/B40 family): a budget expiry on the
    apply renders outcome-UNKNOWN behind an ``Error:`` head — the CR most
    probably LANDED (a millisecond apiserver write), so it is never
    counter-evidence. Before the fix the bare ``_result_is_error`` prefix
    reading revoked the attribution and orphaned a live recipe entity."""
    p = FaultDrillProvider()
    # R57 exception shape on the latest apply → attribution stands.
    msgs = [
        _apply_call("c1", _FAULTDRILL_MANIFEST),
        ToolMessage(content=_R57_UNKNOWN, tool_call_id="c1"),
    ]
    assert p.issue_disproven(msgs) is False
    # R59 wiz-receipt shape → same ruling.
    msgs = [
        _apply_call("c1", _FAULTDRILL_MANIFEST),
        ToolMessage(content=_R59_UNKNOWN, tool_call_id="c1"),
    ]
    assert p.issue_disproven(msgs) is False
    # UNKNOWN-then-rejected retry pair: the earlier expiry counts as
    # LANDED (the landed shield widens over the third state) — revoking
    # here would orphan the CR created before the local wait died.
    msgs = [
        _apply_call("c1", _FAULTDRILL_MANIFEST),
        ToolMessage(content=_R57_UNKNOWN, tool_call_id="c1"),
        _apply_call("c2", _FAULTDRILL_MANIFEST),
        ToolMessage(content="Error: forbidden: crd create denied", tool_call_id="c2"),
    ]
    assert p.issue_disproven(msgs) is False
    # Control: a genuine rejection (no budget-expiry feature) still
    # revokes — the third state must not swallow the two-state verdict.
    msgs = [
        _apply_call("c1", _FAULTDRILL_MANIFEST),
        ToolMessage(content="Error: forbidden: crd create denied", tool_call_id="c1"),
    ]
    assert p.issue_disproven(msgs) is True


def test_issue_disproven_route_gate_rejection_revokes():
    """Run6 (inject-3d5de7fa) regression: the cr-channel route gate rejects
    the apply BEFORE dispatch, rendering ``[target_guard] REJECT_BANNED`` —
    which carries no kubectl-layer ``Error:`` prefix, so ``_result_is_error``
    alone misses it and the issue-time ``faultdrill_cr`` attribution stays
    committed for a CR never sent (poisoning Layer-1 routing, which skips it
    as "CR bookkeeping", and the recover dispatch, whose handle points at a
    non-existent CR). ``reached_target`` (PRE_EXEC_REJECTION_MARKERS, which
    already lists ``[target_guard]``) covers this face."""
    p = FaultDrillProvider()
    msgs = [
        _apply_call("c1", _FAULTDRILL_MANIFEST),
        ToolMessage(
            content="[target_guard] REJECT_BANNED — cr-channel route: the "
            "FaultDrill CRD is unavailable on this cluster (probe-error: crd "
            "read failed) — the CR channel's declarative-restore machinery "
            "has nothing to land on",
            tool_call_id="c1",
        ),
    ]
    assert p.issue_disproven(msgs) is True
    # A genuinely LANDED CR carries neither face → never revoked.
    msgs_landed = [
        _apply_call("c1", _FAULTDRILL_MANIFEST),
        ToolMessage(
            content="faultdrill.drill.blade-ai.io/fd-x created", tool_call_id="c1"
        ),
    ]
    assert p.issue_disproven(msgs_landed) is False
    # An earlier LANDED CR still shields a later route-gate rejection (the
    # live CR keeps the attribution — a failed re-apply never revokes it).
    msgs_shielded = [
        _apply_call("c1", _FAULTDRILL_MANIFEST),
        ToolMessage(content="faultdrill created", tool_call_id="c1"),
        _apply_call("c2", _FAULTDRILL_MANIFEST),
        ToolMessage(
            content="[target_guard] REJECT_BANNED — cr-channel route",
            tool_call_id="c2",
        ),
    ]
    assert p.issue_disproven(msgs_shielded) is False


def test_values_form_handle_claims_only_its_own_method():
    p = FaultDrillProvider()
    assert p.build_fault_handle({"injection_method": "faultdrill_cr"}) == {
        "kind": "faultdrill_cr", "method": "faultdrill_cr",
    }
    assert p.build_fault_handle({"injection_method": "kubectl_native"}) is None
    assert p.build_fault_handle({}) is None


def test_result_is_error_strict_prefix_no_embedded_false_fire():
    """P2 pin (M1 adversarial review): the error verdict is a STRICT PREFIX
    on the tool layer's failure marker (``Error: kubectl <sub> ...``). An
    embedded ``ToolGuardError:`` mention must NOT flip an apply result to
    error — the old substring match coupled this judgement to an unrelated
    string shape."""
    assert _result_is_error("Error: kubectl apply (exit 1): Error from server (NotFound)")
    assert _result_is_error("\n  Error: leading whitespace tolerated")
    assert not _result_is_error("ToolGuardError: guard rejected the call")
    assert not _result_is_error("applied fine; the word Error: appears mid-text")
    assert not _result_is_error("")


def test_apiserver_notfound_apply_still_attributed():
    """P1 pin (M1 adversarial review): attribution rides the shared ATTEMPT
    high-tolerance rule — a CR apply that FAILED at the apiserver (CRD not
    installed yet, NotFound) still counts as attempted, so detect() claims
    the carrier. The M2 four-state recover (NotFound = zero action + warn)
    exists precisely to consume this residue; this pin keeps that contract
    from drifting while M1 stands alone."""
    p = FaultDrillProvider()
    msgs = [
        _apply_call("c1", _FAULTDRILL_MANIFEST),
        ToolMessage(
            content=(
                "Error: kubectl apply (exit 1): Error from server (NotFound): "
                "the server could not find the requested resource"
            ),
            tool_call_id="c1",
        ),
    ]
    assert p.detect(msgs, is_host=False) == "faultdrill_cr"
    assert p.was_injection_attempted(msgs) is True


# ---------------------------------------------------------------------------
# 3b. Assembler face attribution (faultdrill-cluster-native-recovery M1,
#     R2 review Bug#3: the fault-handle projection chain — issue-time
#     attribution, values-stage handle, messages-stage recipe hydration,
#     revocation, and the recover/destroy replay — must claim the
#     programmatic assembler exactly like the CR apply face it replaces).
# ---------------------------------------------------------------------------

from chaos_agent.agent.providers.faultdrill.assembler import (  # noqa: E402
    ASSEMBLER_TOOL_NAME,
)


def _asm_call(call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{
            "id": call_id,
            "name": ASSEMBLER_TOOL_NAME,
            "args": {
                "target_kind": "Service",
                "target_name": "svc-x",
                "target_namespace": "cms-demo",
                "patches": "[]",
                "restore_patches": "[]",
                "duration_seconds": 600,
            },
        }],
    )


_RESTORE_OPS = [{"op": "replace", "path": "/spec/type", "value": "ClusterIP"}]


def _asm_receipt_content(
    *, status: str = "success", carrier: str = "drill-rc-a1b2c3d4",
    ns: str = "cms-demo", target_name: str = "svc-x",
    target_kind: str = "Service",
    restore: list | None = None,
    deadline: float = 1_900_000_000.0,
) -> str:
    """One assembler tool RESULT as the message history sees it: the tool
    strips the top-level ``recovery_handle`` before returning, so the
    recipe rides only inside ``artifact.recovery_handle`` (R2 Bug#3's
    hydration constraint)."""
    restore = _RESTORE_OPS if restore is None else restore
    return json.dumps({
        "status": status,
        "error": "",
        "carrier": {
            "name": carrier, "namespace": ns, "armed": status != "failed",
        },
        "artifact": {
            "artifact_id": f"recovery_carrier:{ns}/{carrier}",
            "type": "recovery_carrier",
            "name": carrier,
            "namespace": ns,
            "recovery_handle": {
                "kind": "recovery_carrier",
                "value": f"{ns}/{carrier}",
                "target_ref": {
                    "kind": target_kind, "name": target_name, "namespace": ns,
                },
                "patches": [{"op": "replace", "path": "/spec/type",
                             "value": "NodePort"}],
                "restore_patches": restore,
                "duration_seconds": 600,
                "recovery_deadline_epoch": deadline,
                "carrier": {"name": carrier, "namespace": ns},
            },
        },
        "steps": [],
    })


def test_issue_time_method_assembler_call_attributes_the_face():
    p = FaultDrillProvider()
    assert p.issue_time_method(
        ASSEMBLER_TOOL_NAME,
        {"target_kind": "Service", "target_name": "svc-x",
         "target_namespace": "cms-demo", "patches": "[]",
         "restore_patches": "[]", "duration_seconds": 600},
    ) == "faultdrill_carrier"
    # The face key is the TOOL NAME — an args-identical call under any
    # other name never claims the carrier face.
    assert p.issue_time_method(
        "some_other_tool",
        {"target_kind": "Service", "duration_seconds": 600},
    ) is None


def test_detect_latest_face_names_the_family():
    p = FaultDrillProvider()
    asm_msgs = [
        _asm_call("a1"),
        ToolMessage(content=_asm_receipt_content(), tool_call_id="a1"),
    ]
    # Assembler-only session → the carrier face.
    assert p.detect(asm_msgs, is_host=False) == "faultdrill_carrier"
    # Migration-window mix: whichever family acted LAST names the face.
    asm_last = [
        _apply_call("c1", _FAULTDRILL_MANIFEST),
        ToolMessage(content="faultdrill created", tool_call_id="c1"),
        _asm_call("a1"),
        ToolMessage(content=_asm_receipt_content(), tool_call_id="a1"),
    ]
    assert p.detect(asm_last, is_host=False) == "faultdrill_carrier"
    cr_last = [
        _asm_call("a1"),
        ToolMessage(content=_asm_receipt_content(), tool_call_id="a1"),
        _apply_call("c1", _FAULTDRILL_MANIFEST),
        ToolMessage(content="faultdrill created", tool_call_id="c1"),
    ]
    assert p.detect(cr_last, is_host=False) == "faultdrill_cr"
    # Host channel never claims either face.
    assert p.detect(asm_msgs, is_host=True) is None
    # Recency follows the same latest-face rule.
    assert p.injection_recency(asm_last, is_host=False) == 2


def test_values_form_handle_claims_the_carrier_face():
    p = FaultDrillProvider()
    # The values stage is kind-bearing only — the recipe hydrates at the
    # messages stage; the kind is the PROVIDER's (routing key), the
    # method names the face.
    assert p.build_fault_handle(
        {"injection_method": "faultdrill_carrier"}
    ) == {"kind": "faultdrill_cr", "method": "faultdrill_carrier"}


def test_handle_from_messages_rebuilds_the_recipe_from_artifact():
    """R2 Bug#3 core pin: the tool strips the top-level ``recovery_handle``,
    so the authoritative handle rebuilds from the receipt's
    ``artifact.recovery_handle`` — the only durable recipe copy."""
    p = FaultDrillProvider()
    msgs = [
        _asm_call("a1"),
        ToolMessage(content=_asm_receipt_content(), tool_call_id="a1"),
    ]
    assert p.build_handle_from_messages(msgs) == {
        "kind": "faultdrill_cr",
        "value": "cms-demo/drill-rc-a1b2c3d4",
        "method": "faultdrill_carrier",
        "target_ref": {
            "kind": "Service", "name": "svc-x", "namespace": "cms-demo",
        },
        "restore_patches": _RESTORE_OPS,
        "recovery_deadline_epoch": 1_900_000_000.0,
    }


def test_handle_from_messages_failed_receipt_vs_earlier_registerable():
    p = FaultDrillProvider()
    # A failed receipt cleaned its stack — nothing to hydrate.
    failed_only = [
        _asm_call("a1"),
        ToolMessage(
            content=json.dumps({
                "status": "failed", "error": "boom",
                "carrier": {"armed": False}, "steps": [],
            }),
            tool_call_id="a1",
        ),
    ]
    assert p.build_handle_from_messages(failed_only) is None
    # A FAILED rebuild after a registerable first attempt: the earlier
    # carrier is still armed (retry-built-new-stack semantics) — the
    # scan walks back to the most recent registerable receipt.
    retry = [
        _asm_call("a1"),
        ToolMessage(content=_asm_receipt_content(), tool_call_id="a1"),
        _asm_call("a2"),
        ToolMessage(
            content=json.dumps({
                "status": "failed", "error": "boom",
                "carrier": {"armed": False}, "steps": [],
            }),
            tool_call_id="a2",
        ),
    ]
    handle = p.build_handle_from_messages(retry)
    assert handle is not None
    assert handle["value"] == "cms-demo/drill-rc-a1b2c3d4"
    # A screener rejection renders as free text — never a receipt.
    rejected = [
        _asm_call("a1"),
        ToolMessage(
            content="[target_guard] REJECT — assembler route unavailable",
            tool_call_id="a1",
        ),
    ]
    assert p.build_handle_from_messages(rejected) is None


def test_issue_disproven_assembler_face():
    p = FaultDrillProvider()
    # A failed receipt (stack cleaned, nothing armed) revokes the
    # issue-time attribution — same contract as a failed CR apply.
    failed = [
        _asm_call("a1"),
        ToolMessage(
            content=json.dumps({
                "status": "failed", "error": "boom",
                "carrier": {"armed": False}, "steps": [],
            }),
            tool_call_id="a1",
        ),
    ]
    assert p.issue_disproven(failed) is True
    # A screener rejection (free text) also never armed anything.
    rejected = [
        _asm_call("a1"),
        ToolMessage(
            content="[target_guard] REJECT — assembler route unavailable",
            tool_call_id="a1",
        ),
    ]
    assert p.issue_disproven(rejected) is True
    # success AND partial both leave an armed carrier — never revoked.
    for status in ("success", "partial"):
        msgs = [
            _asm_call("a1"),
            ToolMessage(
                content=_asm_receipt_content(status=status),
                tool_call_id="a1",
            ),
        ]
        assert p.issue_disproven(msgs) is False, status
    # An earlier registerable receipt shields a later failed rebuild
    # (its carrier is still armed).
    retry = [
        _asm_call("a1"),
        ToolMessage(content=_asm_receipt_content(), tool_call_id="a1"),
        _asm_call("a2"),
        ToolMessage(
            content=json.dumps({
                "status": "failed", "error": "boom",
                "carrier": {"armed": False}, "steps": [],
            }),
            tool_call_id="a2",
        ),
    ]
    assert p.issue_disproven(retry) is False
    # No assembler evidence at all → the CR face's judgement stands
    # untouched (pinned above in test_issue_disproven_rules).
    assert p.issue_disproven([]) is False


@pytest.fixture()
def _patch_fire(monkeypatch):
    """Capture the restore-replay dispatches through the provider's
    ``_kubectl`` seam: the guard-2 readback GET, then the PATCH of the
    pending ops only — any other verb fails loudly (the replay must
    never touch the carrier stack; that belongs to the artifact sweep)."""
    import chaos_agent.agent.providers.faultdrill.provider as fd_provider

    state: dict = {"calls": []}

    async def fake_kubectl(sub, v_args, kubeconfig, *, stdin_data="", timeout=30.0):
        assert sub in ("get", "patch"), (
            f"the replay must not dispatch {sub!r} — the carrier stack "
            "belongs to the artifact sweep"
        )
        state["calls"].append((sub, list(v_args)))
        if sub == "get":
            # The fault-state target: /spec/type is still NodePort, so
            # every restore op is pending (replace ops are never
            # guard-filtered).
            return CommandResult(
                exit_code=0,
                stdout=json.dumps({
                    "kind": "Service", "metadata": {"name": "svc-x"},
                    "spec": {"type": "NodePort"},
                }),
                stderr="",
            )
        return CommandResult(exit_code=0, stdout="patched", stderr="")

    monkeypatch.setattr(fd_provider, "_kubectl", fake_kubectl)
    return state


def _asm_success_messages() -> list:
    return [
        _asm_call("a1"),
        ToolMessage(content=_asm_receipt_content(), tool_call_id="a1"),
    ]


async def test_recover_carrier_face_replays_the_recipe(_patch_fire):
    """Design ND: "blade-ai recover 从任务台账重放配方，与载体双执行
    幂等" — the replay reads the target back (guard 2) then patches the
    TARGET with the receipt's restore recipe (the same json-patch the
    carrier's timer fires) and leaves the armed carrier stack to the
    artifact sweep."""
    p = FaultDrillProvider()
    result = await p.recover({}, None, kubeconfig="/kc", messages=_asm_success_messages())
    assert result.recovered is True
    assert result.level == "recovered"
    assert _patch_fire["calls"] == [
        ("get", ["service", "svc-x", "-n", "cms-demo", "-o", "json"]),
        ("patch", ["service", "svc-x", "-n", "cms-demo", "--type=json",
                   "-p", json.dumps(_RESTORE_OPS)]),
    ]
    assert "idempotent" in result.warnings[0]


async def test_replay_after_timer_fire_is_guard2_no_op(monkeypatch):
    """ND7 three-way convergence (timer first): the target readback finds
    the remove-op paths already ABSENT — guard 2 filters the pending set
    to empty and the replay dispatches ZERO patches, yet still reports
    converged (the cluster state itself is the verdict)."""
    import chaos_agent.agent.providers.faultdrill.provider as fd_provider

    restore = [{"op": "remove", "path": "/spec/type"}]
    calls: list = []

    async def fake_kubectl(sub, v_args, kubeconfig, *, stdin_data="", timeout=30.0):
        calls.append(sub)
        assert sub == "get", "an already-restored target must not be patched"
        return CommandResult(
            exit_code=0,
            stdout=json.dumps({
                "kind": "Service", "metadata": {"name": "svc-x"}, "spec": {},
            }),
            stderr="",
        )

    monkeypatch.setattr(fd_provider, "_kubectl", fake_kubectl)
    msgs = [
        _asm_call("a1"),
        ToolMessage(
            content=_asm_receipt_content(restore=restore), tool_call_id="a1",
        ),
    ]
    result = await FaultDrillProvider().recover(
        {}, None, kubeconfig="/kc", messages=msgs,
    )
    assert result.recovered is True
    assert calls == ["get"]


async def test_recover_carrier_face_dispatch_narrow_handle(_patch_fire):
    """The claim-3 dispatch carries the values-stage NARROW handle (no
    recipe); the recipe hydrates from the receipt inside recover."""
    p = FaultDrillProvider()
    narrow = {"kind": "faultdrill_cr", "method": "faultdrill_carrier"}
    result = await p.recover(
        {}, narrow, kubeconfig="/kc", messages=_asm_success_messages(),
    )
    assert result.recovered is True
    assert _patch_fire["calls"], "the narrow dispatch must still replay"


async def test_recover_carrier_face_failure_fails_visible(monkeypatch):
    import chaos_agent.agent.providers.faultdrill.provider as fd_provider

    async def fake_kubectl(sub, v_args, kubeconfig, *, stdin_data="", timeout=30.0):
        return CommandResult(
            exit_code=1, stdout="", stderr="Error from server (Forbidden)",
        )

    monkeypatch.setattr(fd_provider, "_kubectl", fake_kubectl)
    p = FaultDrillProvider()
    result = await p.recover({}, None, kubeconfig="/kc", messages=_asm_success_messages())
    assert result.recovered is False
    assert result.level == "unrecovered"
    assert result.failure is not None
    assert result.failure[0] is FailureCategory.RECOVERY_FAILED


async def test_recover_carrier_face_no_recipe_fails_visible():
    """A dispatch handle that pins the carrier face with NO hydratable
    recipe (face drift / failed rebuild) is an honest failure — never a
    silent re-route to the CR face."""
    p = FaultDrillProvider()
    narrow = {"kind": "faultdrill_cr", "method": "faultdrill_carrier"}
    result = await p.recover({}, narrow, kubeconfig="/kc", messages=[])
    assert result.recovered is False
    assert result.level == "unrecovered"
    assert result.failure[0] is FailureCategory.RECOVERY_FAILED
    assert "no replayable restore recipe" in result.layer1["details"]


async def test_layer1_destroy_carrier_face_replays_the_recipe(_patch_fire):
    """The D1 gate routes the carrier face's deterministic destroy here
    (uid empty — the carrier is UID-less); the destroy IS the recipe
    replay, verdict-mapped like the CR face."""
    p = FaultDrillProvider()
    verdict = await p.layer1_destroy(
        "", "/kc", messages=_asm_success_messages(),
    )
    assert verdict.status == "passed"
    assert _patch_fire["calls"] == [
        ("get", ["service", "svc-x", "-n", "cms-demo", "-o", "json"]),
        ("patch", ["service", "svc-x", "-n", "cms-demo", "--type=json",
                   "-p", json.dumps(_RESTORE_OPS)]),
    ]


def test_carrier_face_handle_hits_the_deterministic_gate():
    """The kind-sharing pin (R2 Bug#3): the carrier-face handle carries
    the provider's ``handle_kind``, so ``_deterministic_recover_identity``
    routes it to the deterministic destroy exactly like a CR handle —
    a bespoke ``faultdrill_carrier`` kind would fall through the gate and
    misroute the recover into the LLM flow."""
    settings.faultdrill_enabled = True
    FaultProviderRegistry.register_builtins()
    state = {
        "injection_method": "faultdrill_carrier",
        "fault_handle": {
            "kind": "faultdrill_cr", "method": "faultdrill_carrier",
        },
    }
    assert _deterministic_recover_identity(state) is True
    # And the registry's dispatch resolves the SAME provider through
    # claim 3 (the handle's method names this provider, its kind matches).
    provider, identity = FaultProviderRegistry.resolve_fault_dispatch(state)
    assert isinstance(provider, FaultDrillProvider)
    assert identity == state["fault_handle"]
    # The method index resolves the face directly (execute-loop issue time).
    resolved = FaultProviderRegistry.resolve_by_method("faultdrill_carrier")
    assert isinstance(resolved, FaultDrillProvider)


def test_derive_handle_from_legacy_carrier_face():
    """The projection chain (execute_loop._project_fault_handle →
    derive_handle_from_legacy): a committed faultdrill_carrier attribution
    projects the narrow kind-bearing handle — before this fix the
    projection returned None for every assembler injection."""
    settings.faultdrill_enabled = True
    FaultProviderRegistry.register_builtins()
    handle = FaultProviderRegistry.derive_handle_from_legacy(
        {"injection_method": "faultdrill_carrier"},
    )
    assert handle == {"kind": "faultdrill_cr", "method": "faultdrill_carrier"}


# ---------------------------------------------------------------------------
# 4. Manifest whitelist & K1 anchoring
# ---------------------------------------------------------------------------


def test_whitelist_entry_tracks_crd_kind():
    """Drift pin for the literal entry (cross-carrier import is banned by
    the boundary guard; the classifier comment names THIS test)."""
    assert CRD_KIND.lower() in ALLOWED_MANIFEST_KINDS


def test_crd_itself_stays_off_the_llm_face():
    # D2: no admissible LLM path ever installs a CRD (the programmatic
    # installer died with the CR channel; the guard entry stays as a
    # permanent ban).
    assert "customresourcedefinition" not in ALLOWED_MANIFEST_KINDS
    assert "crd" not in ALLOWED_MANIFEST_KINDS


def test_stdin_manifest_anchors_the_cr_document():
    t = _classify_kubectl_stdin_manifest(
        _FAULTDRILL_MANIFEST, ["-f", "-"], "kubectl apply -f -", "apply",
    )
    assert t.scope == "faultdrill"
    assert t.namespace == "cms-demo"
    assert t.names == ("fd-demo1",)


def test_stdin_manifest_mixed_kind_banned():
    mixed = _FAULTDRILL_MANIFEST + """---
kind: ConfigMap
apiVersion: v1
metadata:
  name: ride-along
  namespace: cms-demo
"""
    t = _classify_kubectl_stdin_manifest(
        mixed, ["-f", "-"], "kubectl apply -f -", "apply",
    )
    assert t.scope == "__banned__"


def test_stdin_manifest_crd_document_banned():
    crd_doc = """apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: faultdrills.drill.blade-ai.io
spec: {}
"""
    t = _classify_kubectl_stdin_manifest(
        crd_doc, ["-f", "-"], "kubectl apply -f -", "apply",
    )
    assert t.scope == "__banned__"


def test_stdin_manifest_second_faultdrill_document_banned():
    # P7 (second-round adversarial review): one apply = one CR = one
    # handle. The general branch anchors every document's NAME, but
    # ``build_handle_from_messages`` hydrates exactly ONE ns/name — a
    # second CR's fault would leak with no recovery path. Same-kind
    # twin, same namespace: ONLY the single-document gate can be the
    # rejection reason here (mixed-kind / mixed-namespace gates pass).
    twin = _FAULTDRILL_MANIFEST + """---
apiVersion: drill.blade-ai.io/v1alpha1
kind: FaultDrill
metadata:
  name: fd-second
  namespace: cms-demo
spec:
  action: secretSwap
"""
    t = _classify_kubectl_stdin_manifest(
        twin, ["-f", "-"], "kubectl apply -f -", "apply",
    )
    assert t.scope == "__banned__"
    assert "FaultDrill" in (t.reject_detail or "")
    assert t.reject_suggestion  # the fix path is named, not a bare ban


# Shared canned-response helpers (recover-convergence and CR-face
# migration-window tests below consume these).

class _R:
    def __init__(self, code=0, out="", err=""):
        self.exit_code, self.stdout, self.stderr = code, out, err


_GROUP = "drill.blade-ai.io"


def _landed_messages(
    manifest: str = _FAULTDRILL_MANIFEST, *,
    result: str = "faultdrill/fd-demo1 created",
    v_args: str = "-f -",
) -> list:
    call = _apply_call("tc-rb", manifest, v_args=v_args)
    return [
        call,
        ToolMessage(content=result, tool_call_id="tc-rb", name="kubectl"),
    ]


# ---------------------------------------------------------------------------
# 6. Deterministic ledger-model recover (M2 task 2.3, design ND7) + D1 gates
# ---------------------------------------------------------------------------


@pytest.fixture()
def _recover_stub(monkeypatch):
    """Route the ledger-model recover's replay walk
    (:func:`provider._replay_restore_recipe` → ``restore._do_restore``)
    through a canned verdict: ``restore`` is the canned bool, the call
    journal records the ONE replay and its task_state (the recipe
    projection), and any live ``_kubectl`` dispatch FAILS loudly — a
    monkeypatched ``_do_restore`` never touches the wire, so a dispatch
    here means the replay bypassed the shared core."""
    import chaos_agent.agent.providers.faultdrill.provider as fd_provider
    import chaos_agent.agent.providers.faultdrill.restore as fd_restore

    state: dict = {
        "restore": True,
        "calls": {"restore": 0, "restore_state": None},
    }

    async def fake_kubectl(sub, v_args, kubeconfig, *, stdin_data="", timeout=30.0):
        raise AssertionError(
            "a monkeypatched _do_restore must keep the replay off the "
            f"wire — unexpected {sub!r} dispatch"
        )

    async def fake_restore(task_state, kubeconfig):
        state["calls"]["restore"] += 1
        state["calls"]["restore_state"] = task_state
        return state["restore"]

    monkeypatch.setattr(fd_provider, "_kubectl", fake_kubectl)
    monkeypatch.setattr(fd_restore, "_do_restore", fake_restore)
    return state


_HANDLE = {"kind": "faultdrill_cr", "value": "cms-demo/fd-demo1"}


async def test_recover_cr_face_replays_the_manifest_recipe(_recover_stub):
    """ND7: the migration-window CR face recovers from the LEDGER, not
    the cluster — the applied manifest's spec projects the recipe
    (targetRef + restorePatches; the inject-side ``patches`` are
    deliberately absent — the recovery identity is not the fault
    description), and ONE guard-2 replay converges the target. The CR
    object itself is the artifact sweep's business, never the
    recover's."""
    result = await FaultDrillProvider().recover(
        {}, _HANDLE, kubeconfig="", messages=_landed_messages(_RECIPE_MANIFEST),
    )
    assert result.recovered is True
    assert result.level == "recovered"
    assert result.failure is None
    assert _recover_stub["calls"]["restore"] == 1
    # the recipe projection: recovery identity ONLY — no inject-side patches
    assert _recover_stub["calls"]["restore_state"] == {
        "target_ref": {
            "kind": "Deployment", "name": "web", "namespace": "cms-demo",
        },
        "restore_patches": [{"op": "replace", "path": "/spec/x", "value": "z"}],
        "invalid_secret": {},
    }
    assert "idempotent" in result.warnings[0]


async def test_recover_recipe_replay_failure_fails_visible(_recover_stub):
    """A failed replay is unrecovered + RECOVERY_FAILED — fail-visible,
    never a fabricated success, and no phase bookkeeping softens it
    (the phase write went with the retired CR-reading convergence)."""
    _recover_stub["restore"] = False
    result = await FaultDrillProvider().recover(
        {}, _HANDLE, kubeconfig="", messages=_landed_messages(_RECIPE_MANIFEST),
    )
    assert result.recovered is False
    assert result.level == "unrecovered"
    assert result.failure is not None
    assert result.failure[0] == FailureCategory.RECOVERY_FAILED
    assert result.layer1["status"] == "failed"
    assert _recover_stub["calls"]["restore"] == 1


async def test_recover_unaddressable_handle_fails_visible(_recover_stub):
    """No addressable recipe (dispatch handle carries none AND the
    history yields none): the honest verdict is unrecovered with the
    failure category — and ZERO replays."""
    result = await FaultDrillProvider().recover(
        {}, {"kind": "faultdrill_cr"}, kubeconfig="", messages=[],
    )
    assert result.recovered is False
    assert result.level == "unrecovered"
    assert result.failure is not None
    assert result.failure[0] == FailureCategory.RECOVERY_FAILED
    assert result.layer1["status"] == "skipped"
    assert _recover_stub["calls"]["restore"] == 0


async def test_layer1_destroy_hydrates_handle_from_messages(_recover_stub):
    """The generic flow's identity key is the experiment UID, which this
    UID-less carrier has none of — the dispatch passes an EMPTY uid and
    the recipe hydrates from the applied manifest in ``messages`` (the
    same two-stage hydration every identity seam runs)."""
    r = await FaultDrillProvider().layer1_destroy(
        "", "", messages=_landed_messages(_RECIPE_MANIFEST),
    )
    assert r.status == "passed"
    assert _recover_stub["calls"]["restore"] == 1
    assert _recover_stub["calls"]["restore_state"]["restore_patches"] == [
        {"op": "replace", "path": "/spec/x", "value": "z"},
    ]


async def test_layer1_destroy_uid_bearing_dispatch_is_defensive_skip(_recover_stub):
    """Identity-gate defense: a uid-bearing dispatch never belongs to
    this UID-less carrier (the gates route uid dispatches to experiment
    carriers) — skipped before any replay is attempted."""
    r = await FaultDrillProvider().layer1_destroy(
        "exp-123", "", messages=_landed_messages(_RECIPE_MANIFEST),
    )
    assert r.status == "skipped"
    assert _recover_stub["calls"]["restore"] == 0


async def test_layer1_destroy_unaddressable_is_skipped_not_terminal():
    """No handle anywhere: skipped and NOT terminal — Layer 2 then judges
    from actual cluster evidence (the fault may have self-recovered or
    never landed)."""
    r = await FaultDrillProvider().layer1_destroy("", "", messages=[])
    assert r.status == "skipped"
    assert r.is_terminal() is False


def test_deterministic_recover_identity_gate():
    """D1 seam-compat gate, unit level: TRUE only for the uid-less
    deterministic handle (the CR-reference kind); every experiment
    identity and every uid-less non-deterministic carrier stays on the
    uid / LLM gates."""
    settings.faultdrill_enabled = True
    FaultProviderRegistry.register_builtins()
    # the faultdrill task's real recover-graph state: values-stage
    # attribution handle (kind + method, NO value — claim 3 dispatch)
    assert _deterministic_recover_identity({
        "fault_handle": {"kind": "faultdrill_cr", "method": "faultdrill_cr"},
        "injection_method": "faultdrill_cr",
    }) is True
    # k8s_native: uid-less but NOT deterministic-recover
    assert _deterministic_recover_identity({
        "fault_handle": {"kind": "native", "method": "kubectl_native"},
    }) is False
    # valued handle: an experiment identity — the uid gates own it
    assert _deterministic_recover_identity({
        "fault_handle": {
            "kind": "experiment_uid", "value": "exp-1", "method": "chaosblade",
        },
    }) is False
    # empty state: dispatches to the uid-less verdict default (native)
    assert _deterministic_recover_identity({}) is False


async def test_recover_verifier_types_uidless_cr_as_deterministic(_recover_stub):
    """D1 end-to-end pin: the dispatch identity for a faultdrill task is
    the VALUES-stage handle (no ``value`` — the message-history claim is
    uid-only by construction), so ``experiment_uid`` is EMPTY and the
    Layer-1 type materialization survives only through the seam-compat
    gate — a uid-less deterministic carrier must type as
    "deterministic", never "llm_driven"."""
    settings.faultdrill_enabled = True
    FaultProviderRegistry.register_builtins()
    state = {
        "task_id": "",
        "fault_handle": {"kind": "faultdrill_cr", "method": "faultdrill_cr"},
        "injection_method": "faultdrill_cr",
        "messages": _landed_messages(_RECIPE_MANIFEST),
        "kubeconfig": "",
    }
    result_dict = await recover_verifier(state)
    assert result_dict["recover_layer1_type"] == "deterministic"
    assert result_dict["result"]["recovered"] is True
    assert result_dict["recover_verification"]["level"] == "recovered"
    assert _recover_stub["calls"]["restore"] == 1


def test_verify_prompt_note_final_semantics():
    p = FaultDrillProvider()
    note = p.verify_prompt_note("faultdrill_cr")
    assert note and "recipe" in note
    # 效果即真理: the CR landing is NOT the fault landing — Layer-2 stays
    # the verification authority.
    assert "does not mean the fault has manifested" in note
    assert p.verify_prompt_note("kubectl_native") == ""
    assert p.verify_prompt_note("chaosblade") == ""


# ---------------------------------------------------------------------------
# 7. Artifact ledger — collect hook (the FULL landed set, P11)
# ---------------------------------------------------------------------------


def _apply_pair(
    call_id: str, manifest: str, *, result: str = "faultdrill created",
    v_args: str = "-f -",
) -> list:
    return [
        _apply_call(call_id, manifest, v_args=v_args),
        ToolMessage(content=result, tool_call_id=call_id, name="kubectl"),
    ]


def test_collect_records_every_landed_apply():
    """P11 pin: the fault HANDLE is latest-wins, the LEDGER is the full
    set — a rename-retry's early CR is recorded under its OWN ns/name
    (with its own provenance), so the sweep below, not the handle, owns
    its settle."""
    p = FaultDrillProvider()
    early = _FAULTDRILL_MANIFEST.replace("fd-demo1", "fd-early")
    late = _FAULTDRILL_MANIFEST.replace("fd-demo1", "fd-late")
    msgs = [*_apply_pair("c1", early), *_apply_pair("c2", late)]
    arts = p.collect_artifacts_from_messages(
        msgs, task_id="t-1", operation_family="faultdrill_cr",
    )
    assert [a["name"] for a in arts] == ["fd-early", "fd-late"]
    assert [a["artifact_id"] for a in arts] == [
        "faultdrill_cr:cms-demo/fd-early", "faultdrill_cr:cms-demo/fd-late",
    ]
    assert [a["created_tool_call_id"] for a in arts] == ["c1", "c2"]


def test_collect_failed_apply_registers_nothing():
    """A failed or guard-rejected apply created no cluster object —
    neither registers (the shared strict ``Error:`` prefix rule)."""
    p = FaultDrillProvider()
    msgs = [
        _apply_call("c1", _FAULTDRILL_MANIFEST),
        ToolMessage(content="Error: kubectl apply: forbidden", tool_call_id="c1"),
        _apply_call("c2", _FAULTDRILL_MANIFEST),
        ToolMessage(content="[target_guard] rejected: write-set", tool_call_id="c2"),
    ]
    assert p.collect_artifacts_from_messages(msgs) == []


def test_collect_requires_a_name():
    p = FaultDrillProvider()
    nameless = _FAULTDRILL_MANIFEST.replace("  name: fd-demo1\n", "")
    assert p.collect_artifacts_from_messages(_apply_pair("c1", nameless)) == []


def test_collect_namespace_chain():
    """Same resolution order as build_handle_from_messages: manifest ns
    > ``-n`` flag > ``default`` (kubectl's own fallback chain)."""
    p = FaultDrillProvider()
    ns_free = _FAULTDRILL_MANIFEST.replace("  namespace: cms-demo\n", "")
    msgs = _apply_pair("c1", ns_free, v_args="-n ops")
    assert p.collect_artifacts_from_messages(msgs)[0]["namespace"] == "ops"
    msgs = _apply_pair("c1", ns_free)
    assert p.collect_artifacts_from_messages(msgs)[0]["namespace"] == "default"


def test_collect_shape_and_cleanup_recipe(monkeypatch):
    """The cleanup recipe is the idempotent delete: resource-qualified,
    namespaced, ``--ignore-not-found`` — the same command shape the
    delete-only sweep dispatches."""
    monkeypatch.setattr(settings, "faultdrill_crd_group", _GROUP)
    p = FaultDrillProvider()
    art = p.collect_artifacts_from_messages(
        _apply_pair("c1", _FAULTDRILL_MANIFEST),
        task_id="t-9", operation_family="faultdrill_cr",
    )[0]
    assert art == {
        "artifact_id": "faultdrill_cr:cms-demo/fd-demo1",
        "type": "faultdrill_cr",
        "kind": CRD_KIND,
        "status": "active",
        "task_id": "t-9",
        "name": "fd-demo1",
        "namespace": "cms-demo",
        "operation_family": "faultdrill_cr",
        "created_tool_call_id": "c1",
        "cleanup": {
            "tool": "kubectl",
            "subcommand": "delete",
            "v_args": (
                f"faultdrills.{_GROUP} fd-demo1 -n cms-demo --ignore-not-found"
            ),
        },
    }
    # The family defaults to the carrier when the caller passes none.
    bare = p.collect_artifacts_from_messages(_apply_pair("c1", _FAULTDRILL_MANIFEST))[0]
    assert bare["operation_family"] == "faultdrill_cr"


# ---------------------------------------------------------------------------
# 8. Artifact ledger — sweep hook (delete-only, M2 task 2.3) + registry seams
# ---------------------------------------------------------------------------

_FD_ARTIFACT = {
    "artifact_id": "faultdrill_cr:cms-demo/fd-demo1",
    "type": "faultdrill_cr",
    "name": "fd-demo1",
    "namespace": "cms-demo",
    "status": "active",
}


@pytest.fixture()
def _sweep_stub(monkeypatch):
    """Route the sweep's single delete dispatch through a canned result;
    record the v_args. Delete-only semantics (task 2.3): the recipe
    lives in the task LEDGER (the applied manifest in messages), so
    deleting the CR object can never destroy a recovery — no get, no
    phase gate, ONE idempotent ``--ignore-not-found`` delete (the
    keep-while-Injected gate went with the retired reconciler: with no
    phase ever flipping again, a kept-Injected CR would stay kept
    forever)."""
    import chaos_agent.agent.providers.faultdrill.provider as fd_provider

    state: dict = {"delete": _R(0, "deleted"), "deletes": []}

    async def fake_kubectl(sub, v_args, kubeconfig, *, stdin_data="", timeout=30.0):
        assert sub == "delete", (
            f"the sweep is delete-only; unexpected verb {sub!r} — a read "
            "back would rebuild the retired phase gate"
        )
        state["deletes"].append(list(v_args))
        return state["delete"]

    monkeypatch.setattr(fd_provider, "_kubectl", fake_kubectl)
    monkeypatch.setattr(settings, "faultdrill_crd_group", _GROUP)
    return state


async def test_sweep_unclaimed_artifact_returns_none(_sweep_stub):
    """Claim seam: a non-CR artifact (or a non-dict) is NOT this
    carrier's — ``None`` lets the registry scan move on, with zero
    dispatches spent."""
    p = FaultDrillProvider()
    assert await p.sweep_artifact(
        {"type": "debug_pod", "name": "x"}, kubeconfig="/kc",
    ) is None
    assert await p.sweep_artifact("not-a-dict", kubeconfig="/kc") is None
    assert _sweep_stub["deletes"] == []


async def test_sweep_unnameable_settles_without_a_delete(_sweep_stub):
    """Nothing sweepable exists for a fact-free row — settle it rather
    than retrying an unnameable artifact forever."""
    p = FaultDrillProvider()
    assert await p.sweep_artifact(
        {"type": "faultdrill_cr", "namespace": "cms-demo"},
    ) is True
    assert await p.sweep_artifact(
        {"type": "faultdrill_cr", "name": "fd-demo1"},
    ) is True
    assert _sweep_stub["deletes"] == []


async def test_sweep_is_one_idempotent_delete(_sweep_stub):
    """The sweep is ONE ``--ignore-not-found`` delete —
    resource-qualified, namespaced — settling whether the object is
    present or already gone (external removal): with no reconciler left
    to flip phases, the CR object is pure residue once the recipe lives
    in the ledger."""
    p = FaultDrillProvider()
    assert await p.sweep_artifact(dict(_FD_ARTIFACT), kubeconfig="/kc") is True
    assert _sweep_stub["deletes"] == [[
        f"faultdrills.{_GROUP}", "fd-demo1", "-n", "cms-demo",
        "--ignore-not-found",
    ]]


async def test_sweep_delete_failure_keeps_for_retry(_sweep_stub):
    """A refused delete (RBAC / channel flake) is not absence — keep the
    artifact; the next round retries the same idempotent delete."""
    _sweep_stub["delete"] = _R(1, "", "Error from server (Forbidden)")
    p = FaultDrillProvider()
    assert await p.sweep_artifact(dict(_FD_ARTIFACT), kubeconfig="/kc") is False


# --- registry seams: aggregate collect / first-claim-wins sweep ------------


def test_registry_collect_aggregates_channel_artifacts(monkeypatch):
    monkeypatch.setattr(settings, "faultdrill_crd_group", _GROUP)
    settings.faultdrill_enabled = True
    FaultProviderRegistry._providers = {}
    FaultProviderRegistry.register_builtins()
    out = FaultProviderRegistry.collect_provider_artifacts(
        _landed_messages(), task_id="t-1",
    )
    # Only the CR channel owns an artifact hook today; the builtin
    # carriers contribute nothing (getattr-optional seams).
    assert out == [{
        "artifact_id": "faultdrill_cr:cms-demo/fd-demo1",
        "type": "faultdrill_cr",
        "kind": CRD_KIND,
        "status": "active",
        "task_id": "t-1",
        "name": "fd-demo1",
        "namespace": "cms-demo",
        "operation_family": "faultdrill_cr",
        "created_tool_call_id": "tc-rb",
        "cleanup": {
            "tool": "kubectl",
            "subcommand": "delete",
            "v_args": (
                f"faultdrills.{_GROUP} fd-demo1 -n cms-demo --ignore-not-found"
            ),
        },
    }]


async def test_registry_sweep_unowned_artifact_untouched(_sweep_stub):
    """First-claim-wins: no provider claims a vehicle artifact →
    ``None``, and the registry spent zero dispatches."""
    settings.faultdrill_enabled = True
    FaultProviderRegistry._providers = {}
    FaultProviderRegistry.register_builtins()
    assert await FaultProviderRegistry.sweep_artifact(
        {"type": "debug_pod", "name": "x"}, kubeconfig="/kc",
    ) is None
    assert _sweep_stub["deletes"] == []


async def test_registry_sweep_routes_to_owning_carrier(_sweep_stub):
    settings.faultdrill_enabled = True
    FaultProviderRegistry._providers = {}
    FaultProviderRegistry.register_builtins()
    assert await FaultProviderRegistry.sweep_artifact(
        dict(_FD_ARTIFACT), kubeconfig="/kc",
    ) is True
    assert _sweep_stub["deletes"] == [[
        f"faultdrills.{_GROUP}", "fd-demo1", "-n", "cms-demo",
        "--ignore-not-found",
    ]]


async def test_registry_dark_launch_collects_and_sweeps_nothing(_sweep_stub):
    """Dark launch: the channel is structurally absent — a landed-apply
    history collects nothing, and nothing claims the artifact."""
    settings.faultdrill_enabled = False
    FaultProviderRegistry._providers = {}
    FaultProviderRegistry.register_builtins()
    assert FaultProviderRegistry.collect_provider_artifacts(
        _landed_messages(),
    ) == []
    assert await FaultProviderRegistry.sweep_artifact(
        dict(_FD_ARTIFACT), kubeconfig="/kc",
    ) is None
    assert _sweep_stub["deletes"] == []


# ---------------------------------------------------------------------------
# 9. Task 2.7 residual pins (unit-test-group audit leftovers)
# ---------------------------------------------------------------------------


def test_execute_time_apply_error_family_is_not_landed():
    """D7 二分的归因层锚：执行期 CR apply 失败（Forbidden 环境层 /
    schema 拒收配方层）一律 not-landed 家族 — 归因可撤回（修配方
    重试路径）且工件层零接管；降级（换 SOP 形态）的裁决不在归因层
    （通道安装面已随 CR 通道退役），replan 引导（M3）消费两类
    error 的不同措辞。NotFound 形态的 ATTEMPT 高容忍钉扎见
    test_apiserver_notfound_apply_still_attributed。"""
    p = FaultDrillProvider()
    forbidden = (
        "Error: kubectl apply (exit 1): Error from server (Forbidden): "
        "faultdrills.drill.blade-ai.io is forbidden"
    )
    schema_rejected = (
        "Error: kubectl apply (exit 1): FaultDrill.spec.invalidSecret: "
        "Unsupported value: ... Invalid value"
    )
    for err in (forbidden, schema_rejected):
        msgs = [
            _apply_call("c1", _FAULTDRILL_MANIFEST),
            ToolMessage(content=err, tool_call_id="c1"),
        ]
        # ATTEMPT high-tolerance: still an attempt (a revocable one)
        assert p.was_injection_attempted(msgs) is True
        # not landed: the attribution is disproven (fix-the-recipe path)
        assert p.issue_disproven(msgs) is True
        # the artifact ledger registers nothing for a failed apply
        assert p.collect_artifacts_from_messages(msgs) == []


def test_chaosblade_and_host_attribution_unchanged_by_channel_registration():
    """非迁移域等价（2.7）：CR 通道是纯叠加 — chaosblade 与宿主域的
    归因在通道注册与否两种注册态下逐字相同（与引入前等价），且 CR
    provider 对 blade 流零归因（域不相交：stdin manifest 文档 kind 是
    唯一归因信号，host 通道上 matches_channel 结构性拒绝）。"""
    blade_msgs = [
        AIMessage(content="", tool_calls=[{
            "name": "kubectl",
            "args": {
                "subcommand": "exec",
                "v_args": (
                    "chaosblade-tool-2l2gj -n chaosblade -- blade create "
                    "k8s pod-cpu fullload --cpu-percent 80"
                ),
            },
            "id": "k1",
        }]),
        ToolMessage(
            content='{"code":200,"success":true,"result":"a1b2c3d4e5f60718"}',
            name="kubectl",
            tool_call_id="k1",
        ),
    ]
    # Host-domain streams (mirror test_builtin_providers conventions):
    # a blade_create delivery and a host exec command.
    host_blade_msgs = [ToolMessage(
        content='{"code":200,"success":true,"result":"a1b2c3d4e5f60718"}',
        name="blade_create", tool_call_id="b1",
    )]
    host_native_msgs = [ToolMessage(
        content="filled /tmp/x", name="exec_host_command", tool_call_id="h1",
    )]

    k8s_verdicts = {}
    host_blade_verdicts = {}
    host_native_verdicts = {}
    for flag in (False, True):
        settings.faultdrill_enabled = flag
        FaultProviderRegistry._providers = {}
        FaultProviderRegistry.register_builtins()
        k8s_verdicts[flag] = FaultProviderRegistry.detect_method(
            blade_msgs, is_host=False,
        )
        host_blade_verdicts[flag] = FaultProviderRegistry.detect_method(
            host_blade_msgs, is_host=True,
        )
        host_native_verdicts[flag] = FaultProviderRegistry.detect_method(
            host_native_msgs, is_host=True,
        )
    assert k8s_verdicts[False] == k8s_verdicts[True] == "kubectl_exec"
    assert host_blade_verdicts[False] == host_blade_verdicts[True] == "host_blade"
    assert host_native_verdicts[False] == host_native_verdicts[True] == "host_native"
    # Domain disjointness: the CR provider never claims a blade stream
    # (either channel), and its own apply stream is host-rejected.
    assert FaultDrillProvider().detect(blade_msgs, is_host=False) is None
    assert FaultDrillProvider().detect(blade_msgs, is_host=True) is None
    assert FaultDrillProvider().detect(
        _apply_pair("c1", _FAULTDRILL_MANIFEST), is_host=True,
    ) is None

