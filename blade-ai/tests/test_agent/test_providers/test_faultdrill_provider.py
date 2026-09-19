"""faultdrill-cr-channel M1: FaultDrillProvider conformance + channel pins.

Task 1.7 of openspec change ``faultdrill-cr-channel``. The generic
conformance suite derives its domain from the registry snapshot at import
time — with the dark-launch flag OFF (the default) FaultDrillProvider is
deliberately absent, so THIS file is the channel's explicit conformance
home, pinning:

1. Protocol surface — the structural contract plus the channel's own
   design pins (UID-less deterministic-recover carrier; manifest-kind
   attribution with ZERO verb/tool vocabulary — no recency competition
   with k8s_native, no binary/tool-face expansion).
2. Registration gating — flag off = structurally absent (short-circuit);
   flag on = tail of the builtin order, idempotent; flag back off =
   reconciled down (no stale registration behind the gate).
3. Attribution family — stdin manifest DOCUMENT KIND is the only
   attribution signal (command-line form never matches), the shared
   ATTEMPT rule, the ns/name handle resolution chain.
4. Manifest whitelist & K1 anchoring — the ``faultdrill`` kind rides the
   stdin-manifest channel with per-document anchoring, legislated
   SINGLE-document for the CR kind (P7: one apply = one CR = one recovery
   handle — a second document form-issues); the CRD itself
   stays off the LLM face (D2).
5. CRD install decision family — the D7 degradation branches.
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
8. Task 2.7 unit-test group residual pins — zero-inline credentials
   (the invalidSecret reference-only CLOSED schema), the execute-time
   apply-error family anchor (D7's Forbidden-vs-schema-rejection split
   at the attribution layer), and the non-migration-domain equivalence
   (chaosblade / host attribution unchanged by channel registration).
9. Naming discipline (M3 task 3.3) — ``build_cr_name`` invariants,
   pinned as INVARIANTS not fixed spellings: task-derived /
   reproducible (same task → same object, replan & recover converge),
   prefix-follows-configuration (the hash domain is orthogonal to the
   prefix domain), zero drill signature under the default prefix.
   The master-switch side of 3.3 is the whole suite itself: the
   dark-launch default (``faultdrill_enabled=False``) is what every
   test above runs under, and the per-surface off-pins live in their
   own sections (registration short-circuit here §2, prompt byte
   identity / probe scheduling in test_prompts & preplan_probe,
   artifact zero-registration in test_execution_artifacts, gate
   all-reject in test_screener).
"""

from __future__ import annotations

import base64
import json
import re
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from chaos_agent.tools.guard import CommandResult

import chaos_agent.agent.providers.faultdrill.crd_install as crd_install
from chaos_agent.agent.nodes.recover._recover_verifier_loop import (
    _deterministic_recover_identity,
    recover_verifier,
)
from chaos_agent.agent.providers import FaultProvider, FaultProviderRegistry
from chaos_agent.agent.providers.faultdrill.crd import (
    CRD_KIND,
    PHASE_FAILED,
    PHASE_INJECTED,
    PHASE_PENDING,
    PHASE_RECOVERED,
    build_crd_yaml,
    build_cr_name,
    crd_full_name,
    verify_crd_compatibility,
)
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


# ---------------------------------------------------------------------------
# 1. Protocol surface + channel design pins
# ---------------------------------------------------------------------------


def test_satisfies_protocol():
    assert isinstance(FaultDrillProvider(), FaultProvider)


def test_carrier_and_method_ids():
    p = FaultDrillProvider()
    assert p.carrier == CARRIER_ID == "faultdrill_cr"
    assert p.injection_methods == ("faultdrill_cr",)


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


def test_tools_empty_for_every_phase():
    p = FaultDrillProvider()
    for phase in ("plan", "execute", "verify", "recover_verify"):
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
# 4. Manifest whitelist & K1 anchoring
# ---------------------------------------------------------------------------


def test_whitelist_entry_tracks_crd_kind():
    """Drift pin for the literal entry (cross-carrier import is banned by
    the boundary guard; the classifier comment names THIS test)."""
    assert CRD_KIND.lower() in ALLOWED_MANIFEST_KINDS


def test_crd_itself_stays_off_the_llm_face():
    # D2: the CRD installs programmatically (crd_install); the LLM apply
    # face never sees an admissible CRD install.
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


# ---------------------------------------------------------------------------
# 5. CRD install decision family (D7)
# ---------------------------------------------------------------------------


class _R:
    def __init__(self, code=0, out="", err=""):
        self.exit_code, self.stdout, self.stderr = code, out, err


_GROUP = "drill.blade-ai.io"
_CRD_JSON = None


def _template_json() -> dict:
    global _CRD_JSON
    import yaml

    if _CRD_JSON is None:
        doc = yaml.safe_load(build_crd_yaml(_GROUP))
        doc.setdefault("status", {})["conditions"] = [
            {"type": "Established", "status": "True"},
        ]
        _CRD_JSON = doc
    return _CRD_JSON


@pytest.fixture()
def _kubectl_stub(monkeypatch):
    """Route ``crd_install._kubectl`` through a canned response script."""
    state = {"get_seq": [], "apply": None, "calls": {"get": 0, "apply": 0}}

    async def fake_kubectl(sub, v_args, kubeconfig, *, stdin_data="", timeout=30.0):
        if sub == "get":
            seq = state["get_seq"]
            i = min(state["calls"]["get"], len(seq) - 1)
            state["calls"]["get"] += 1
            return seq[i]
        state["calls"]["apply"] += 1
        # The install apply must deliver the CRD YAML via stdin (the
        # programmatic declarative seam — D2).
        assert stdin_data.startswith("apiVersion: apiextensions.k8s.io/v1")
        return state["apply"]

    monkeypatch.setattr(crd_install, "_kubectl", fake_kubectl)
    monkeypatch.setattr(crd_install, "_established_budget", lambda: 1.0)
    monkeypatch.setattr(crd_install, "_group", lambda: _GROUP)
    return state


async def test_crd_ready_when_compatible(_kubectl_stub):
    _kubectl_stub["get_seq"] = [_R(0, json.dumps(_template_json()))]
    av = await crd_install.probe_crd()
    assert av.status == "ready" and av.usable


async def test_crd_incompatible_legacy_treated_as_unusable(_kubectl_stub):
    import copy


    legacy = copy.deepcopy(_template_json())
    spec_props = legacy["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]
    spec_props["patches"] = {"type": "array"}  # no items-level preserve
    _kubectl_stub["get_seq"] = [_R(0, json.dumps(legacy))]
    av = await crd_install.ensure_crd()
    assert (av.status, av.reason) == ("unavailable", "crd-incompatible")
    # "Exists" ≠ "usable": no install attempt is made against an
    # ownerless old CRD.
    assert _kubectl_stub["calls"]["apply"] == 0


async def test_crd_probe_forbidden_degrades_without_install(_kubectl_stub):
    _kubectl_stub["get_seq"] = [_R(1, "", "Error: forbidden: cannot get")]
    av = await crd_install.ensure_crd()
    assert (av.status, av.reason) == ("unavailable", "probe-forbidden")
    assert _kubectl_stub["calls"]["apply"] == 0


async def test_crd_fresh_install_reaches_established(_kubectl_stub):
    _kubectl_stub["get_seq"] = [
        _R(1, "", "Error from server (NotFound): not found"),
        _R(0, json.dumps(_template_json())),
    ]
    _kubectl_stub["apply"] = _R(0, "created")
    av = await crd_install.ensure_crd()
    assert (av.status, av.usable) == ("installed", True)
    assert _kubectl_stub["calls"]["apply"] == 1


async def test_crd_apply_forbidden_degrades(_kubectl_stub):
    _kubectl_stub["get_seq"] = [_R(1, "", "Error from server (NotFound): not found")]
    _kubectl_stub["apply"] = _R(1, "", "Error: forbidden: crd create denied")
    av = await crd_install.ensure_crd()
    assert (av.status, av.reason) == ("unavailable", "apply-forbidden")


async def test_crd_established_timeout_degrades(_kubectl_stub):
    import copy


    pending = copy.deepcopy(_template_json())
    pending["status"]["conditions"] = []
    _kubectl_stub["get_seq"] = [
        _R(1, "", "Error from server (NotFound): not found"),
        _R(0, json.dumps(pending)),
    ]
    _kubectl_stub["apply"] = _R(0, "created")
    import chaos_agent.agent.providers.faultdrill.crd_install as ci

    ci._established_budget = lambda: 0.0  # never Established within budget
    av = await ci.ensure_crd()
    assert (av.status, av.reason) == ("unavailable", "established-timeout")


async def test_crd_probe_error_degrades(_kubectl_stub):
    _kubectl_stub["get_seq"] = [_R(-1, "", "timed out after 30s")]
    av = await crd_install.probe_crd()
    assert (av.status, av.reason) == ("unavailable", "probe-error")


def test_crd_template_self_compatible():
    ok, why = verify_crd_compatibility(_template_json())
    assert ok, why
    assert crd_full_name(_GROUP) == f"faultdrills.{_GROUP}"


# ---------------------------------------------------------------------------
# 5b. Live-fire execution chain (P5, M1 adversarial review)
#
#     The decision-family tests above mock ``crd_install._kubectl``, so the
#     REAL chain below it — ``exec_kubectl_raw`` → ``build_kubectl_cmd`` →
#     ``execute_via_transport`` (ToolGuard, profile gate, channel preflight /
#     wrap / adapt) — was never exercised. Here ONLY ``run_command`` (the
#     process spawn) is faked; the apiserver is simulated at its return
#     value. This pins the review probes as regressions:
#
#       * the real guard ALLOWs both ``get crd`` and ``apply -f -`` (the
#         command face; the manifest-kind allowlist is LLM-face only, D2),
#       * the CRD YAML reaches the subprocess stdin verbatim (kubeconfig
#         channel) or folds into the wrapped command base64-intact
#         (kubewiz channel) and pipes NOTHING.
# ---------------------------------------------------------------------------


@pytest.fixture()
def _live_fire(monkeypatch):
    """Fake ONLY ``run_command`` (the process spawn); record every dispatch.

    Responses are queued on ``.responses`` (consumed in dispatch order,
    last one repeats); assertions read ``.calls``.
    """
    calls: list[dict] = []
    responses: list[CommandResult] = []

    async def fake_run(cmd, *args, **kwargs):
        calls.append({
            "cmd": list(cmd),
            "stdin_data": str(kwargs.get("stdin_data", "") or ""),
            "channel": str(kwargs.get("channel", "") or ""),
        })
        i = min(len(calls) - 1, len(responses) - 1) if responses else None
        return responses[i] if i is not None else CommandResult(
            exit_code=0, stdout="", stderr="", duration_ms=1.0,
        )

    monkeypatch.setattr("chaos_agent.tools.shell.run_command", fake_run)

    class _Harness:
        pass

    h = _Harness()
    h.calls = calls
    h.responses = responses
    return h


async def test_live_fire_kubeconfig_channel_full_real_chain(
    _live_fire, monkeypatch, tmp_path,
):
    kc = tmp_path / "kubeconfig"
    kc.write_text("# live-fire stub kubeconfig\n")
    monkeypatch.setattr(settings, "kube_connection_mode", "kubeconfig")
    monkeypatch.setattr(settings, "kubeconfig_path", str(kc))
    monkeypatch.setattr(settings, "kubectl_path", "kubectl")
    monkeypatch.setattr(settings, "kubewiz_cluster_uuid", "")
    monkeypatch.setattr(settings, "kubewiz_profile", "")
    monkeypatch.setattr(settings, "faultdrill_crd_group", _GROUP)

    crd_yaml = build_crd_yaml(_GROUP)
    # Dispatch order is ensure_crd's own: probe get → apply → poll get.
    _live_fire.responses.extend([
        CommandResult(
            exit_code=1, stdout="",
            stderr=(
                "Error from server (NotFound): "
                f"customresourcedefinitions.apiextensions.k8s.io \"faultdrills.{_GROUP}\" not found"
            ),
        ),
        CommandResult(
            exit_code=0,
            stdout=f"customresourcedefinitions.apiextensions.k8s.io/faultdrills.{_GROUP} created",
            stderr="",
        ),
        CommandResult(exit_code=0, stdout=json.dumps(_template_json()), stderr=""),
    ])

    av = await crd_install.ensure_crd()
    assert (av.status, av.usable) == ("installed", True)

    calls = _live_fire.calls
    assert len(calls) == 3
    kubectl = "kubectl"
    get_cmd = calls[0]["cmd"]
    assert get_cmd == [
        kubectl, "--kubeconfig", str(kc),
        "get", "crd", f"faultdrills.{_GROUP}", "-o", "json",
    ]
    apply_call = calls[1]
    # The guard passed (no ToolGuardError surfaced) and the install apply
    # is exactly ``kubectl --kubeconfig <kc> apply -f -``.
    assert apply_call["cmd"] == [kubectl, "--kubeconfig", str(kc), "apply", "-f", "-"]
    # The CRD YAML reaches the subprocess stdin VERBATIM (declarative seam).
    assert apply_call["stdin_data"] == crd_yaml
    # Reads pipe nothing.
    assert calls[0]["stdin_data"] == ""
    assert calls[2]["cmd"][3] == "get"


async def test_live_fire_kubewiz_channel_folds_stdin_into_command(
    _live_fire, monkeypatch,
):
    monkeypatch.setattr(settings, "kube_connection_mode", "kubewiz_k8s")
    monkeypatch.setattr(settings, "kubewiz_cluster_uuid", "uuid-fire")
    monkeypatch.setattr(settings, "kubewiz_profile", "prof-fire")
    monkeypatch.setattr(settings, "wiz_path", "wiz")
    monkeypatch.setattr(settings, "faultdrill_crd_group", _GROUP)

    # wiz protocol: stdout "exit_code: <n>\n<inner stdout>"; the inner
    # stderr rides the wrapper stderr (parse_wiz_output keeps it verbatim).
    _live_fire.responses.extend([
        CommandResult(
            exit_code=0, stdout="exit_code: 1\n",
            stderr=(
                "Error from server (NotFound): "
                f"customresourcedefinitions.apiextensions.k8s.io \"faultdrills.{_GROUP}\" not found"
            ),
        ),
        CommandResult(exit_code=0, stdout="exit_code: 0\nfaultdrill created", stderr=""),
        CommandResult(
            exit_code=0, stdout="exit_code: 0\n" + json.dumps(_template_json()), stderr="",
        ),
    ])

    av = await crd_install.ensure_crd()
    assert (av.status, av.usable) == ("installed", True)

    # Dispatch shape: probe get → install apply → ONE Established read
    # (the poll loop exits on the first Established response).
    assert len(_live_fire.calls) == 3

    apply_call = _live_fire.calls[1]
    # Nothing is piped — the payload rides the wrapped command itself.
    assert apply_call["stdin_data"] == ""
    wrapped = apply_call["cmd"]
    assert wrapped[:3] == ["wiz", "task", "exec"]
    script = wrapped[wrapped.index("--command") + 1]
    # The CRD YAML folds in as a single base64 word and round-trips intact.
    m = re.search(r"echo ([A-Za-z0-9+/=]+) \| base64 -d", script)
    assert m, script[:200]
    assert base64.b64decode(m.group(1)).decode("utf-8") == build_crd_yaml(_GROUP)


async def test_live_fire_established_timeout_polls_the_real_chain(
    _live_fire, monkeypatch, tmp_path,
):
    """P14 pin: the D7 established-timeout branch runs the REAL chain —
    probe → apply → the genuine polling loop (deadline from settings,
    cadence from the module constant) — and degrades honestly to
    ``unavailable`` (a routing signal, never a task failure). The
    ``_kubectl_stub`` tests patch the seam, so the loop's ``_read_crd``
    dispatches were never live before this.
    """
    import copy

    kc = tmp_path / "kubeconfig"
    kc.write_text("# live-fire stub kubeconfig\n")
    monkeypatch.setattr(settings, "kube_connection_mode", "kubeconfig")
    monkeypatch.setattr(settings, "kubeconfig_path", str(kc))
    monkeypatch.setattr(settings, "kubectl_path", "kubectl")
    monkeypatch.setattr(settings, "faultdrill_crd_group", _GROUP)
    monkeypatch.setattr(settings, "faultdrill_crd_established_timeout_seconds", 1)
    # 1s budget at 50ms cadence ≈ 20 polls: deterministic against
    # scheduler jitter, and fast.
    monkeypatch.setattr(crd_install, "_ESTABLISHED_POLL_SECONDS", 0.05)

    never_established = copy.deepcopy(_template_json())
    never_established["status"]["conditions"] = []

    _live_fire.responses.extend([
        CommandResult(
            exit_code=1, stdout="",
            stderr=(
                "Error from server (NotFound): "
                f"customresourcedefinitions.apiextensions.k8s.io \"faultdrills.{_GROUP}\" not found"
            ),
        ),
        CommandResult(
            exit_code=0,
            stdout=f"customresourcedefinitions.apiextensions.k8s.io/faultdrills.{_GROUP} created",
            stderr="",
        ),
        CommandResult(
            exit_code=0, stdout=json.dumps(never_established), stderr="",
        ),
    ])

    av = await crd_install.ensure_crd()
    assert (av.status, av.reason, av.usable) == (
        "unavailable", "established-timeout", False,
    )

    calls = _live_fire.calls
    # probe get → install apply → the REAL poll loop.
    assert calls[1]["cmd"][3:] == ["apply", "-f", "-"]
    polls = calls[2:]
    assert len(polls) >= 10, len(calls)
    for c in polls:
        assert c["cmd"][3:] == ["get", "crd", f"faultdrills.{_GROUP}", "-o", "json"]
        assert c["stdin_data"] == ""  # reads pipe nothing, every round


# ---------------------------------------------------------------------------
# 5c. Post-landing readback guard (M2 task 2.1, design D5)
#
#     The v2 experiment's ``exit(5)`` guard productised: a LANDED
#     faultdrill apply is read back programmatically and
#     ``spec.patches`` / ``spec.restorePatches`` must both survive —
#     a landed-but-stripped recipe reconciling on empty patches IS the
#     v1 bare-injection incident. Idempotence (one readback per CR per
#     epoch), the failed-apply face (``issue_disproven``'s jurisdiction,
#     never re-litigated here), the fail-closed read family, and the
#     dark-launch no-op are all pinned below.
# ---------------------------------------------------------------------------


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


def _cr_json(*, patches: bool = True, restore: bool = True, phase: str = "") -> dict:
    spec: dict = {
        "action": "secretSwap",
        "targetRef": {"kind": "Deployment", "name": "web", "namespace": "cms-demo"},
    }
    if patches:
        spec["patches"] = [{"op": "replace", "path": "/spec/x", "value": "y"}]
    if restore:
        spec["restorePatches"] = [{"op": "replace", "path": "/spec/x", "value": "z"}]
    cr = {
        "apiVersion": f"{_GROUP}/v1alpha1",
        "kind": CRD_KIND,
        "metadata": {"name": "fd-demo1", "namespace": "cms-demo"},
        "spec": spec,
    }
    if phase:
        cr["status"] = {"phase": phase}
    return cr


@pytest.fixture()
def _readback_stub(monkeypatch):
    """Route ``provider._kubectl`` through a canned CR read; count calls."""
    import chaos_agent.agent.providers.faultdrill.provider as fd_provider

    state = {"cr": None, "calls": 0, "last_v_args": None}

    async def fake_kubectl(sub, v_args, kubeconfig, *, stdin_data="", timeout=30.0):
        assert sub == "get"  # the readback is a READ, nothing else
        state["calls"] += 1
        state["last_v_args"] = list(v_args)
        return state["cr"]

    monkeypatch.setattr(fd_provider, "_kubectl", fake_kubectl)
    monkeypatch.setattr(settings, "faultdrill_crd_group", _GROUP)
    return state


async def test_readback_guard_stripped_recipe_hard_fails(_readback_stub):
    """v1 incident law: a landed CR whose recipe was pruned NEVER
    reaches reconciliation — hard-fail with the replanable wording."""
    _readback_stub["cr"] = _R(0, json.dumps(_cr_json(patches=False)))
    verdict = await FaultDrillProvider().verify_landing_readback(
        _landed_messages(), {},
    )
    assert verdict["ok"] is False
    assert verdict["reason"] == "stripped"
    assert verdict["handle"] == "cms-demo/fd-demo1"
    # "not found" is load-bearing: the abort detail feeds the replan
    # classifier (recipe layer — fix the recipe or route to SOP).
    assert "not found" in verdict["detail"]
    assert _readback_stub["calls"] == 1
    # the get addressed the CR by handle, not by list/label guesswork
    assert _readback_stub["last_v_args"] == [
        f"faultdrills.{_GROUP}", "fd-demo1", "-n", "cms-demo", "-o", "json",
    ]


async def test_readback_guard_intact_recipe_passes(_readback_stub):
    _readback_stub["cr"] = _R(0, json.dumps(_cr_json()))
    verdict = await FaultDrillProvider().verify_landing_readback(
        _landed_messages(), {},
    )
    assert verdict == {
        "ok": True, "reason": "", "handle": "cms-demo/fd-demo1", "detail": "",
    }
    assert _readback_stub["calls"] == 1


async def test_readback_guard_idempotence_skips_verified_landing(_readback_stub):
    """One readback per CR per epoch: the bookkeeping value suppresses
    the re-verify (a different handle still re-verifies)."""
    _readback_stub["cr"] = _R(0, json.dumps(_cr_json()))
    msgs = _landed_messages()
    state = {"fault_readback_verified": "cms-demo/fd-demo1"}
    assert await FaultDrillProvider().verify_landing_readback(
        msgs, state,
    ) is None
    assert _readback_stub["calls"] == 0  # zero dispatch, not a skipped verdict
    # a stale value (another CR's handle) does NOT suppress this one
    state = {"fault_readback_verified": "cms-demo/fd-other"}
    verdict = await FaultDrillProvider().verify_landing_readback(msgs, state)
    assert verdict["ok"] is True
    assert _readback_stub["calls"] == 1


async def test_readback_guard_ignores_failed_apply(_readback_stub):
    """The failed face is ``issue_disproven``'s jurisdiction: an errored
    apply never landed, so the guard never re-litigates it."""
    msgs = _landed_messages(result="Error: kubectl apply: the server rejected")
    assert await FaultDrillProvider().verify_landing_readback(msgs, {}) is None
    assert _readback_stub["calls"] == 0


async def test_readback_guard_ignores_pre_execution_rejection(_readback_stub):
    """Guard rejections happen BEFORE execution — not a landing, not an
    attempt (the shared ATTEMPT rule)."""
    msgs = _landed_messages(result="Error: guard rejected: manifest kind banned")
    assert await FaultDrillProvider().verify_landing_readback(msgs, {}) is None
    assert _readback_stub["calls"] == 0


async def test_readback_guard_no_faultdrill_traffic_is_none(_readback_stub):
    """Non-faultdrill traffic pays ZERO readback cost (the scan is the
    attribution vocabulary itself — no events, no dispatch)."""
    msgs = [
        _apply_call("tc-x", "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: cm\n"),
        ToolMessage(content="configmap/cm created", tool_call_id="tc-x", name="kubectl"),
    ]
    assert await FaultDrillProvider().verify_landing_readback(msgs, {}) is None
    assert _readback_stub["calls"] == 0


async def test_readback_guard_read_error_fails_closed(_readback_stub):
    """Integrity unproven IS integrity failed: a failed/garbage read
    aborts exactly like a stripped recipe (reason label distinguishes)."""
    _readback_stub["cr"] = _R(1, "", "Error from server (Forbidden): no get")
    verdict = await FaultDrillProvider().verify_landing_readback(
        _landed_messages(), {},
    )
    assert (verdict["ok"], verdict["reason"]) == (False, "read-error")
    assert "Forbidden" in verdict["detail"]
    # unparseable payload: same fail-closed, different evidence line
    _readback_stub["cr"] = _R(0, "{not json")
    verdict = await FaultDrillProvider().verify_landing_readback(
        _landed_messages(), {},
    )
    assert (verdict["ok"], verdict["reason"]) == (False, "read-error")
    assert "unparseable" in verdict["detail"]
    assert _readback_stub["calls"] == 2


async def test_readback_guard_unnameable_landing_fails_closed(_readback_stub):
    """A generateName-style landing carries no metadata.name: the CR is
    unaddressable (no readback, no handle, no reconcile path) — the
    guard does not wave it through on a technicality."""
    nameless = """apiVersion: drill.blade-ai.io/v1alpha1
kind: FaultDrill
spec:
  action: secretSwap
"""
    verdict = await FaultDrillProvider().verify_landing_readback(
        _landed_messages(nameless), {},
    )
    assert (verdict["ok"], verdict["reason"]) == (False, "unnameable")
    assert verdict["handle"] == ""
    assert _readback_stub["calls"] == 0  # nothing to even read


async def test_readback_guard_ns_fallback_chain(_readback_stub):
    """The readback addresses the CR by the SAME ns hydration chain as
    the recovery handle: manifest ns > -n flag > default."""
    nsless = _FAULTDRILL_MANIFEST.replace("  namespace: cms-demo\n", "")
    _readback_stub["cr"] = _R(0, json.dumps(_cr_json()))
    # flag fallback
    await FaultDrillProvider().verify_landing_readback(
        _landed_messages(nsless, v_args="-f - -n ops-ns"), {},
    )
    assert _readback_stub["last_v_args"][2:] == ["-n", "ops-ns", "-o", "json"]
    # default fallback
    await FaultDrillProvider().verify_landing_readback(
        _landed_messages(nsless), {},
    )
    assert _readback_stub["last_v_args"][2:] == ["-n", "default", "-o", "json"]


async def test_registry_dispatches_readback_only_when_enabled(_readback_stub):
    """Dark-launch invariant: the seam routes through registration —
    flag ON dispatches to the hook; flag OFF is a structural no-op."""
    settings.faultdrill_enabled = True
    FaultProviderRegistry.register_builtins()
    _readback_stub["cr"] = _R(0, json.dumps(_cr_json()))
    verdict = await FaultProviderRegistry.verify_landing_readback(
        _landed_messages(), {},
    )
    assert verdict is not None and verdict["ok"] is True
    assert _readback_stub["calls"] == 1

    settings.faultdrill_enabled = False
    FaultProviderRegistry.register_builtins()
    _readback_stub["calls"] = 0
    assert await FaultProviderRegistry.verify_landing_readback(
        _landed_messages(), {},
    ) is None
    assert _readback_stub["calls"] == 0


async def test_live_fire_readback_get_runs_the_real_chain(
    _live_fire, monkeypatch, tmp_path,
):
    """P5-style pin: the readback's ``get`` runs the REAL chain (guard
    command face, kubeconfig injection, channel preflight) — only the
    process spawn is faked. The guard ALLOWs a resource-qualified read
    with zero stdin."""
    kc = tmp_path / "kubeconfig"
    kc.write_text("# live-fire stub kubeconfig\n")
    monkeypatch.setattr(settings, "kube_connection_mode", "kubeconfig")
    monkeypatch.setattr(settings, "kubeconfig_path", str(kc))
    monkeypatch.setattr(settings, "kubectl_path", "kubectl")
    monkeypatch.setattr(settings, "kubewiz_cluster_uuid", "")
    monkeypatch.setattr(settings, "kubewiz_profile", "")
    monkeypatch.setattr(settings, "faultdrill_crd_group", _GROUP)

    _live_fire.responses.append(
        CommandResult(exit_code=0, stdout=json.dumps(_cr_json()), stderr=""),
    )
    verdict = await FaultDrillProvider().verify_landing_readback(
        _landed_messages(), {},
    )
    assert verdict["ok"] is True
    assert verdict["handle"] == "cms-demo/fd-demo1"

    calls = _live_fire.calls
    assert len(calls) == 1
    assert calls[0]["cmd"] == [
        "kubectl", "--kubeconfig", str(kc),
        "get", f"faultdrills.{_GROUP}", "fd-demo1", "-n", "cms-demo", "-o", "json",
    ]
    assert calls[0]["stdin_data"] == ""  # reads pipe nothing


# ---------------------------------------------------------------------------
# 6. Deterministic recover convergence (M2 task 2.3, design D4) + D1 gates
# ---------------------------------------------------------------------------


@pytest.fixture()
def _recover_stub(monkeypatch):
    """Route the convergence's cluster face (``provider._kubectl`` get /
    delete) and the reconciler's restore walk through canned scripts.

    ``gets`` / ``deletes`` are queued responses consumed in dispatch
    order (last one repeats — the same discipline as ``_kubectl_stub``);
    ``restore`` is ``_do_restore``'s canned verdict; the call journal
    doubles as the zero-write assertions (a Recovered replay must leave
    it empty). An empty queue FAILS the dispatch loudly rather than
    fabricating a response (a silent default success would mask the
    zero-write pins)."""
    import chaos_agent.agent.providers.faultdrill.provider as fd_provider
    import chaos_agent.agent.providers.faultdrill.reconciler as fd_reconciler

    state: dict = {
        "gets": [],
        "deletes": [],
        "restore": True,
        "calls": {
            "get": [], "delete": [], "restore": 0,
            "restore_state": None, "set_phase": [],
        },
    }

    async def fake_kubectl(sub, v_args, kubeconfig, *, stdin_data="", timeout=30.0):
        if sub == "get":
            state["calls"]["get"].append(list(v_args))
            seq = state["gets"]
            assert seq, "get dispatched but no get response stubbed"
            return seq[min(len(state["calls"]["get"]) - 1, len(seq) - 1)]
        assert sub == "delete", sub
        state["calls"]["delete"].append(list(v_args))
        seq = state["deletes"]
        assert seq, "delete dispatched but no delete response stubbed"
        return seq[min(len(state["calls"]["delete"]) - 1, len(seq) - 1)]

    async def fake_restore(task_state, kubeconfig):
        state["calls"]["restore"] += 1
        state["calls"]["restore_state"] = task_state
        return state["restore"]

    async def fake_set_phase(handle_value, kubeconfig, phase, extra):
        state["calls"]["set_phase"].append(
            (handle_value, phase, dict(extra or {}))
        )
        return True

    monkeypatch.setattr(fd_provider, "_kubectl", fake_kubectl)
    monkeypatch.setattr(fd_reconciler, "_do_restore", fake_restore)
    monkeypatch.setattr(fd_reconciler, "_set_phase", fake_set_phase)
    monkeypatch.setattr(
        fd_reconciler, "_utc_now_iso", lambda: "2026-09-19T00:00:00+00:00",
    )
    monkeypatch.setattr(settings, "faultdrill_crd_group", _GROUP)
    return state


_HANDLE = {"kind": "faultdrill_cr", "value": "cms-demo/fd-demo1"}


async def test_recover_pending_cr_deletion_is_the_convergence(_recover_stub):
    """Pending (no phase landed): the recipe never executed — deleting
    the CR IS the whole convergence, zero inject actions."""
    _recover_stub["gets"] = [_R(0, json.dumps(_cr_json()))]
    _recover_stub["deletes"] = [_R(0, "deleted")]
    result = await FaultDrillProvider().recover({}, _HANDLE, kubeconfig="")
    assert result.recovered is True
    assert result.level == "recovered"
    assert result.failure is None
    assert result.layer1["status"] == "passed"
    # zero inject actions: no restore, no phase write
    assert _recover_stub["calls"]["restore"] == 0
    assert _recover_stub["calls"]["set_phase"] == []
    assert len(_recover_stub["calls"]["delete"]) == 1
    assert _recover_stub["calls"]["delete"][0] == [
        f"faultdrills.{_GROUP}", "fd-demo1", "-n", "cms-demo",
        "--ignore-not-found",
    ]


async def test_recover_pending_literal_phase_also_deletes(_recover_stub):
    """A literal ``phase: Pending`` (the schema's value, pre-reconcile) is
    an unrecognized EXECUTION phase — same delete-only convergence as the
    empty phase."""
    _recover_stub["gets"] = [_R(0, json.dumps(_cr_json(phase="Pending")))]
    _recover_stub["deletes"] = [_R(0, "deleted")]
    result = await FaultDrillProvider().recover({}, _HANDLE, kubeconfig="")
    assert result.recovered is True
    assert _recover_stub["calls"]["restore"] == 0
    assert len(_recover_stub["calls"]["delete"]) == 1


async def test_recover_injected_converges_and_lands_recovered(_recover_stub):
    """Injected: guard-2 idempotent restorePatches replay + derived-secret
    deletion, landing phase=Recovered — the same walk the session
    reconciler's TTL verdict performs."""
    _recover_stub["gets"] = [_R(0, json.dumps(_cr_json(phase=PHASE_INJECTED)))]
    result = await FaultDrillProvider().recover({}, _HANDLE, kubeconfig="")
    assert result.recovered is True
    assert result.level == "recovered"
    assert result.failure is None
    # the recipe is read from the CLUSTER (task_state projection from spec)
    assert _recover_stub["calls"]["restore"] == 1
    assert _recover_stub["calls"]["restore_state"] == {
        "patches": [{"op": "replace", "path": "/spec/x", "value": "y"}],
        "restore_patches": [{"op": "replace", "path": "/spec/x", "value": "z"}],
        "invalid_secret": {},
        "target_ref": {
            "kind": "Deployment", "name": "web", "namespace": "cms-demo",
        },
    }
    assert len(_recover_stub["calls"]["set_phase"]) == 1
    handle, phase, extra = _recover_stub["calls"]["set_phase"][0]
    assert (handle, phase) == ("cms-demo/fd-demo1", PHASE_RECOVERED)
    assert "recoveredAt" in extra
    # Injected keeps the CR (its record is the recovery evidence)
    assert _recover_stub["calls"]["delete"] == []


async def test_recover_injected_restore_failure_lands_failed(_recover_stub):
    """Injected + failed restore: phase=Failed lands (fail-visible; the
    next replay takes the Failed terminal-cleanup branch) and the
    verdict is unrecovered + RECOVERY_FAILED — never a fabricated
    success."""
    _recover_stub["gets"] = [_R(0, json.dumps(_cr_json(phase=PHASE_INJECTED)))]
    _recover_stub["restore"] = False
    result = await FaultDrillProvider().recover({}, _HANDLE, kubeconfig="")
    assert result.recovered is False
    assert result.level == "unrecovered"
    assert result.failure is not None
    assert result.failure[0] == FailureCategory.RECOVERY_FAILED
    assert _recover_stub["calls"]["set_phase"] == [
        ("cms-demo/fd-demo1", PHASE_FAILED, {
            "restoreLog": "recover replay: restore failed (patches or "
                          "secret deletion errored)",
        }),
    ]
    # the CR survives for the Failed terminal-cleanup replay
    assert _recover_stub["calls"]["delete"] == []


async def test_recover_failed_cr_terminal_cleanup(_recover_stub):
    """Failed terminal (D4): best-effort restore, then delete the CR —
    the bounded-retry recipe's restore patches still get one honest
    attempt."""
    _recover_stub["gets"] = [_R(0, json.dumps(_cr_json(phase=PHASE_FAILED)))]
    _recover_stub["deletes"] = [_R(0, "deleted")]
    result = await FaultDrillProvider().recover({}, _HANDLE, kubeconfig="")
    assert result.recovered is True
    assert result.level == "recovered"
    assert _recover_stub["calls"]["restore"] == 1
    assert len(_recover_stub["calls"]["delete"]) == 1
    assert _recover_stub["calls"]["set_phase"] == []


async def test_recover_failed_cr_cleanup_incomplete_fails_visible(_recover_stub):
    """Failed + the delete refused: the cleanup is incomplete — fail
    visible (the fault may still be active), never a fabricated
    success."""
    _recover_stub["gets"] = [_R(0, json.dumps(_cr_json(phase=PHASE_FAILED)))]
    _recover_stub["deletes"] = [_R(1, "", "Error: forbidden: delete denied")]
    result = await FaultDrillProvider().recover({}, _HANDLE, kubeconfig="")
    assert result.recovered is False
    assert result.level == "unrecovered"
    assert result.failure is not None
    assert result.failure[0] == FailureCategory.RECOVERY_FAILED


async def test_recover_recovered_cr_is_zero_writes(_recover_stub):
    """Recovered: an idempotent replay of an already-converged CR — the
    cluster state itself is the verdict, ZERO writes of any kind."""
    _recover_stub["gets"] = [_R(0, json.dumps(_cr_json(phase=PHASE_RECOVERED)))]
    result = await FaultDrillProvider().recover({}, _HANDLE, kubeconfig="")
    assert result.recovered is True
    assert result.level == "recovered"
    assert result.failure is None
    assert len(_recover_stub["calls"]["get"]) == 1  # the single read
    assert _recover_stub["calls"]["restore"] == 0
    assert _recover_stub["calls"]["set_phase"] == []
    assert _recover_stub["calls"]["delete"] == []
    # the Layer-2 skip note is honest about WHY it is skipped
    assert any("converged deterministically" in w for w in result.warnings)


async def test_recover_not_found_is_zero_actions_unverified(_recover_stub):
    """Not-found residue (the ATTEMPT high-tolerance attribution's
    guaranteed leftover): read fails, the P12 listing finds nothing —
    zero actions + level="unverified" + failure=None. B85: ignorance is
    never a fabricated success NOR a fabricated failure; the row stays
    recoverable."""
    _recover_stub["gets"] = [
        _R(1, "", "Error from server (NotFound): faultdrills not found"),
        _R(0, json.dumps({"items": []})),  # the P12 cross-ns listing
    ]
    result = await FaultDrillProvider().recover({}, _HANDLE, kubeconfig="")
    assert result.recovered is False
    assert result.level == "unverified"
    assert result.failure is None
    assert len(_recover_stub["calls"]["get"]) == 2
    assert _recover_stub["calls"]["get"][1] == [
        f"faultdrills.{_GROUP}", "-A", "-o", "json",
    ]
    assert _recover_stub["calls"]["restore"] == 0
    assert _recover_stub["calls"]["set_phase"] == []
    assert _recover_stub["calls"]["delete"] == []
    assert any("zero recovery actions" in w for w in result.warnings)
    assert result.layer1["status"] == "skipped"  # not terminal → Layer 2 may judge


async def test_recover_unaddressable_handle_fails_visible(_recover_stub):
    """No addressable CR (dispatch handle carries no value AND the
    history yields none): the honest verdict is unrecovered with the
    failure category — and ZERO cluster dispatches."""
    result = await FaultDrillProvider().recover(
        {}, {"kind": "faultdrill_cr"}, kubeconfig="", messages=[],
    )
    assert result.recovered is False
    assert result.level == "unrecovered"
    assert result.failure is not None
    assert result.failure[0] == FailureCategory.RECOVERY_FAILED
    assert result.layer1["status"] == "skipped"
    assert _recover_stub["calls"]["get"] == []
    assert _recover_stub["calls"]["delete"] == []
    assert _recover_stub["calls"]["restore"] == 0


async def test_recover_p12_cross_namespace_redirect(_recover_stub):
    """P12: the handle's ns fallback (``default`` for a namespace-omitted
    manifest with no ``-n``) can be WRONG — kubectl landed the CR in the
    context namespace. The listing's unique same-name match
    re-addresses the recovery, exactly once."""
    _recover_stub["gets"] = [
        _R(1, "", "Error from server (NotFound): not found"),  # default ns
        _R(0, json.dumps({
            "items": [{"metadata": {"name": "fd-demo1", "namespace": "cms-demo"}}],
        })),
        _R(0, json.dumps(_cr_json(phase=PHASE_INJECTED))),  # re-entry read
    ]
    result = await FaultDrillProvider().recover(
        {}, {"kind": "faultdrill_cr", "value": "default/fd-demo1"}, kubeconfig="",
    )
    assert result.recovered is True
    assert len(_recover_stub["calls"]["get"]) == 3  # read → list → re-read
    assert _recover_stub["calls"]["get"][2] == [
        f"faultdrills.{_GROUP}", "fd-demo1", "-n", "cms-demo", "-o", "json",
    ]
    assert _recover_stub["calls"]["set_phase"][0][:2] == (
        "cms-demo/fd-demo1", PHASE_RECOVERED,
    )


async def test_recover_p12_ambiguous_twins_stay_not_found(_recover_stub):
    """P12 ambiguity: same-name CRs in TWO namespaces — the redirect
    must not guess. The not-found verdict stands (zero actions)."""
    _recover_stub["gets"] = [
        _R(1, "", "Error from server (NotFound): not found"),
        _R(0, json.dumps({
            "items": [
                {"metadata": {"name": "fd-demo1", "namespace": "ns-a"}},
                {"metadata": {"name": "fd-demo1", "namespace": "ns-b"}},
            ],
        })),
    ]
    result = await FaultDrillProvider().recover(
        {}, {"kind": "faultdrill_cr", "value": "default/fd-demo1"}, kubeconfig="",
    )
    assert result.recovered is False
    assert result.level == "unverified"
    assert result.failure is None
    assert len(_recover_stub["calls"]["get"]) == 2  # read + list, no re-entry
    assert _recover_stub["calls"]["delete"] == []
    assert _recover_stub["calls"]["restore"] == 0


async def test_layer1_destroy_hydrates_handle_from_messages(_recover_stub):
    """The generic flow's identity key is the experiment UID, which this
    UID-less carrier has none of — the dispatch passes an EMPTY uid and
    the CR reference is hydrated from the applied manifest in
    ``messages`` (the same two-stage hydration every identity seam
    runs)."""
    _recover_stub["gets"] = [_R(0, json.dumps(_cr_json()))]  # pending
    _recover_stub["deletes"] = [_R(0, "deleted")]
    r = await FaultDrillProvider().layer1_destroy(
        "", "", messages=_landed_messages(),
    )
    assert r.status == "passed"
    assert _recover_stub["calls"]["get"][0] == [
        f"faultdrills.{_GROUP}", "fd-demo1", "-n", "cms-demo", "-o", "json",
    ]
    assert len(_recover_stub["calls"]["delete"]) == 1


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
    _recover_stub["gets"] = [_R(0, json.dumps(_cr_json(phase=PHASE_INJECTED)))]
    state = {
        "task_id": "",
        "fault_handle": {"kind": "faultdrill_cr", "method": "faultdrill_cr"},
        "injection_method": "faultdrill_cr",
        "messages": _landed_messages(),
        "kubeconfig": "",
    }
    result_dict = await recover_verifier(state)
    assert result_dict["recover_layer1_type"] == "deterministic"
    assert result_dict["result"]["recovered"] is True
    assert result_dict["recover_verification"]["level"] == "recovered"
    assert len(_recover_stub["calls"]["set_phase"]) == 1


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
    namespaced, ``--ignore-not-found`` — the same command shape as
    ``_delete_cr``."""
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
# 8. Artifact ledger — sweep hook (keep-while-Injected) + registry seams
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
    """Route ``provider._kubectl`` (the sweep's read) and ``_delete_cr``
    through canned results; record every call."""
    import chaos_agent.agent.providers.faultdrill.provider as fd_provider

    state = {
        "get": _R(1, "", 'Error from server (NotFound): "faultdrills..."'),
        "gets": [],
        "delete": AsyncMock(return_value=True),
    }

    async def fake_kubectl(sub, v_args, kubeconfig, *, stdin_data="", timeout=30.0):
        assert sub == "get", f"the sweep READs; unexpected verb {sub!r}"
        state["gets"].append(list(v_args))
        return state["get"]

    monkeypatch.setattr(fd_provider, "_kubectl", fake_kubectl)
    monkeypatch.setattr(fd_provider, "_delete_cr", state["delete"])
    monkeypatch.setattr(settings, "faultdrill_crd_group", _GROUP)
    return state


async def test_sweep_unclaimed_artifact_returns_none(_sweep_stub):
    """Claim seam: a non-CR artifact (or a non-dict) is NOT this
    carrier's — ``None`` lets the registry scan move on, with zero API
    reads spent."""
    p = FaultDrillProvider()
    assert await p.sweep_artifact(
        {"type": "debug_pod", "name": "x"}, kubeconfig="/kc",
    ) is None
    assert await p.sweep_artifact("not-a-dict", kubeconfig="/kc") is None
    assert _sweep_stub["gets"] == []


async def test_sweep_unnameable_settles_without_a_read(_sweep_stub):
    """Nothing sweepable exists for a fact-free row — settle it rather
    than retrying an unnameable artifact forever."""
    p = FaultDrillProvider()
    assert await p.sweep_artifact(
        {"type": "faultdrill_cr", "namespace": "cms-demo"},
    ) is True
    assert await p.sweep_artifact(
        {"type": "faultdrill_cr", "name": "fd-demo1"},
    ) is True
    assert _sweep_stub["gets"] == []


async def test_sweep_not_found_settles_zero_write(_sweep_stub):
    """Already gone (external removal): the same idempotence class as
    ``_delete_cr``'s ``--ignore-not-found`` — settle with NO second
    write."""
    p = FaultDrillProvider()
    assert await p.sweep_artifact(dict(_FD_ARTIFACT), kubeconfig="/kc") is True
    assert _sweep_stub["delete"].await_count == 0
    assert _sweep_stub["gets"] == [[
        f"faultdrills.{_GROUP}", "fd-demo1", "-n", "cms-demo", "-o", "json",
    ]]


async def test_sweep_injected_keeps(_sweep_stub):
    """An Injected CR is still firing — deleting it mid-window would be
    an EARLY recovery. Keep; the next round re-examines."""
    _sweep_stub["get"] = _R(0, json.dumps(_cr_json(phase=PHASE_INJECTED)))
    p = FaultDrillProvider()
    assert await p.sweep_artifact(dict(_FD_ARTIFACT)) is False
    assert _sweep_stub["delete"].await_count == 0


async def test_sweep_recovered_deletes_the_audit_object(_sweep_stub):
    """The reconciler flips the phase but deliberately leaves the OBJECT
    (a Recovered CR is the audit record) — the sweep is the object's
    sweeper of record."""
    _sweep_stub["get"] = _R(0, json.dumps(_cr_json(phase=PHASE_RECOVERED)))
    p = FaultDrillProvider()
    assert await p.sweep_artifact(dict(_FD_ARTIFACT), kubeconfig="/kc") is True
    _sweep_stub["delete"].assert_awaited_once_with("cms-demo/fd-demo1", "/kc")


async def test_sweep_recovered_delete_failure_keeps(_sweep_stub):
    _sweep_stub["get"] = _R(0, json.dumps(_cr_json(phase=PHASE_RECOVERED)))
    _sweep_stub["delete"].return_value = False
    p = FaultDrillProvider()
    assert await p.sweep_artifact(dict(_FD_ARTIFACT)) is False


async def test_sweep_pending_and_failed_stay_with_recover_convergence(_sweep_stub):
    """Pending / Failed / unknown phases: the recover-convergence path
    owns their transition (Pending → delete, Failed → restore + delete);
    sweeping here could destroy a recipe the replay needs."""
    for phase in (PHASE_PENDING, PHASE_FAILED, "SomethingElse", ""):
        _sweep_stub["get"] = _R(0, json.dumps(_cr_json(phase=phase)))
        p = FaultDrillProvider()
        assert await p.sweep_artifact(dict(_FD_ARTIFACT)) is False
    assert _sweep_stub["delete"].await_count == 0


async def test_sweep_unreadable_payload_keeps(_sweep_stub):
    """Never mistake an unreadable object for a settled one."""
    p = FaultDrillProvider()
    _sweep_stub["get"] = _R(0, "not-json{")
    assert await p.sweep_artifact(dict(_FD_ARTIFACT)) is False
    _sweep_stub["get"] = _R(0, json.dumps(["a", "list"]))
    assert await p.sweep_artifact(dict(_FD_ARTIFACT)) is False


async def test_sweep_read_failure_not_notfound_keeps(_sweep_stub):
    """A channel flake / RBAC read failure is NOT absence: keep, retry
    the next round."""
    _sweep_stub["get"] = _R(1, "", "connection refused")
    p = FaultDrillProvider()
    assert await p.sweep_artifact(dict(_FD_ARTIFACT)) is False
    assert _sweep_stub["delete"].await_count == 0


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
    ``None``, and the registry spent zero API reads."""
    settings.faultdrill_enabled = True
    FaultProviderRegistry._providers = {}
    FaultProviderRegistry.register_builtins()
    assert await FaultProviderRegistry.sweep_artifact(
        {"type": "debug_pod", "name": "x"}, kubeconfig="/kc",
    ) is None
    assert _sweep_stub["gets"] == []


async def test_registry_sweep_routes_to_owning_carrier(_sweep_stub):
    settings.faultdrill_enabled = True
    FaultProviderRegistry._providers = {}
    FaultProviderRegistry.register_builtins()
    assert await FaultProviderRegistry.sweep_artifact(
        dict(_FD_ARTIFACT), kubeconfig="/kc",
    ) is True
    assert _sweep_stub["gets"] == [[
        f"faultdrills.{_GROUP}", "fd-demo1", "-n", "cms-demo", "-o", "json",
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
    assert _sweep_stub["gets"] == []


# ---------------------------------------------------------------------------
# 9. Task 2.7 residual pins (unit-test-group audit leftovers)
# ---------------------------------------------------------------------------


def _spec_props(crd: dict, field: str) -> dict:
    return (
        crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
        ["properties"]["spec"]["properties"][field]
    )


def test_invalid_secret_schema_is_reference_only():
    """凭证零内联立法（design L58）：invalidSecret 是闭式引用 schema —
    ``name`` + ``sourceName`` + ``registryHostOverride``，无 data/
    stringData 载荷通道，也无 ``preserve-unknown``（对比 patches 的
    开放）：一个带内联凭证的 CR 字段被 apiserver 的 structural
    schema 直接拒收/剪除 — 零内联是 CRD 结构性保证，非约定俗成。"""
    import yaml

    crd = yaml.safe_load(build_crd_yaml(_GROUP))
    inv = _spec_props(crd, "invalidSecret")
    assert set(inv["properties"]) == {
        "name", "sourceName", "registryHostOverride",
    }
    assert set(inv["required"]) == {"name", "sourceName"}
    assert "x-kubernetes-preserve-unknown-fields" not in inv
    assert "no credential material is ever inlined" in inv["description"]
    # The CONTRAST is the legislation: patches / restorePatches accept an
    # arbitrary JSON ``value`` (preserve-unknown, open) — only the
    # credential-bearing field is closed.
    for open_field in ("patches", "restorePatches"):
        assert _spec_props(crd, open_field)["items"].get(
            "x-kubernetes-preserve-unknown-fields"
        ) is True


def test_crd_template_carries_no_credential_channel():
    """防御钉扎：CRD 模板全文零内联载荷通道词 — 未来给 CR spec 加字段
    时此钉防止引入内联面（``(?<![a-z])data:`` 不误伤 ``metadata:``；
    ``credential`` 不在词表 — description 的零内联宣示本身就用它）。
    spec 属性集恰为六字段（无隐藏载荷字段）。"""
    import yaml

    template = build_crd_yaml(_GROUP)
    for banned in ("stringData", "password", "token", "secretData"):
        assert banned not in template
    assert not re.search(r"(?<![a-z])data:", template)
    crd = yaml.safe_load(template)
    assert set(
        crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
        ["properties"]["spec"]["properties"]
    ) == {
        "action", "targetRef", "patches", "invalidSecret",
        "restorePatches", "durationSeconds",
    }


def test_execute_time_apply_error_family_is_not_landed():
    """D7 二分的归因层锚：执行期 CR apply 失败（Forbidden 环境层 /
    schema 拒收配方层）一律 not-landed 家族 — 归因可撤回（修配方
    重试路径）且工件层零接管；降级（换 SOP 形态）的裁决不在归因层
    —通道安装面的降级判定族在 crd_install（probe/apply Forbidden →
    unavailable，本文件第 5 节已钉），replan 引导（M3）消费两类
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


# ---------------------------------------------------------------------------
# 9. Naming discipline — build_cr_name invariants (M3 task 3.3)
# ---------------------------------------------------------------------------


def test_cr_name_is_task_derived_and_reproducible():
    """命名纪律不变量「任务派生 + 同任务可复现」：名字是 task_id 的纯
    函数——同任务重跑/重规划/recover 汇聚同一对象（不堆积兄弟 CR），
    异任务分流。钉不变量而非固定拼写：摘要字面量不进断言，只钉形状
    （前缀 + 8 位小写 hex）与纯函数性。"""
    a1 = build_cr_name("task-alpha", "fd-")
    a2 = build_cr_name("task-alpha", "fd-")
    b = build_cr_name("task-beta", "fd-")
    assert a1 == a2, "same task must mint the same name (replan/recover converge)"
    assert a1 != b, "different tasks must not collide"
    assert re.fullmatch(r"fd-[0-9a-f]{8}", a1), (
        f"shape invariant broken: {a1!r} (expected <prefix><8 lowercase hex>)"
    )
    # Empty task id is still deterministic (never raises, never varies).
    assert build_cr_name("", "fd-") == build_cr_name("", "fd-")


def test_cr_name_prefix_follows_configuration():
    """命名纪律不变量「前缀取配置」：换前缀时摘要部分不变——任务派生域
    与前缀域正交（配置只改暴露面拼写，不改对象归并语义）。与
    build_crd_yaml(group) 同模式：prefix 是参数，crd.py 保持零 settings
    import 的纯数据模块纪律。"""
    default_form = build_cr_name("task-alpha", "fd-")
    custom_form = build_cr_name("task-alpha", "ops.")
    bare_form = build_cr_name("task-alpha", "")
    assert default_form.removeprefix("fd-") == custom_form.removeprefix("ops.")
    assert bare_form == default_form.removeprefix("fd-")


def test_cr_name_default_prefix_carries_zero_drill_signature():
    """命名纪律不变量「零 drill/chaos/blade 词根」（默认配置下）：与 SOP
    路径的 drill-rc- 前缀（classifier 形态判定信号）不同源——CR 名不是
    守卫信号（守卫靠 write-set 审批与 kind 白名单），名字只需足够平淡
    不在 kubectl get 扫视中自报演练身份。词根检查对整个名字做子串匹配
    （含配置前缀本身）。"""
    for task_id in (
        "task-1",
        "inject-5552c6e4",
        "pod-terminating-finalizers",
        "",
    ):
        name = build_cr_name(task_id, settings.faultdrill_name_prefix)
        for root in ("drill", "chaos", "blade"):
            assert root not in name.lower(), (
                f"drill signature {root!r} leaked into CR name {name!r} "
                f"(task={task_id!r})"
            )


@pytest.mark.asyncio
async def test_ensure_crd_hook_wraps_the_availability_verdict():
    """D2/D7 installability hook: the provider-neutral dict carries the
    decision family's verdict — ``usable=False`` is the degradation
    signal the route gate turns into SOP re-plan guidance, and the dict
    shape is the whole contract (the gate reads ``usable`` only, never
    a faultdrill-specific status vocabulary)."""
    p = FaultDrillProvider()
    unavailable = crd_install.CrdAvailability(
        "unavailable", "apply-forbidden",
        "RBAC denies creating customresourcedefinitions",
    )
    with patch(
        "chaos_agent.agent.providers.faultdrill.crd_install.ensure_crd",
        new=AsyncMock(return_value=unavailable),
    ):
        verdict = await p.ensure_crd(kubeconfig="/tmp/k")
    assert verdict == {
        "usable": False, "status": "unavailable",
        "reason": "apply-forbidden",
        "detail": "RBAC denies creating customresourcedefinitions",
    }
    ready = crd_install.CrdAvailability("ready", "", "schema compatible")
    with patch(
        "chaos_agent.agent.providers.faultdrill.crd_install.ensure_crd",
        new=AsyncMock(return_value=ready),
    ):
        verdict = await p.ensure_crd(kubeconfig="/tmp/k")
    assert verdict["usable"] is True
    assert verdict["status"] == "ready"
