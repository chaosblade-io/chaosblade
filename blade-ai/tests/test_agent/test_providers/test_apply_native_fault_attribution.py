"""Adversarial regression matrix — apply-native unbounded fault attribution.

Root cause (#65 ``Pod_网络故障_NetworkPolicy误配``, inject-2c2faed7): a
``kubectl apply``/``create -f`` of a PERSISTENT fault object (NetworkPolicy
et al.) lives exactly as long as the object — no experiment UID, no
``activeDeadlineSeconds``, no self-timeout — so its ONLY bounded recovery is
the recovery-carrier timer, precisely like a mutation verb. But the whole
attribution stack keyed on the mutation VERB table
(``KUBECTL_WRITE_SUBCOMMANDS``), which never contained ``apply``/``create``;
the armed-before-inject gate borrowed that same table (CONFLATION) and so
inherited the blind spot: the fault landed with no timer armed.

Fork A threads ONE canonical predicate — ``is_apply_native_fault_injection``
(recognised by MANIFEST KIND, never by verb) — through every consumption
face. This matrix pins each face adversarially:

  L1 the pure predicate (single source of truth)
  L2 the armed-gate helper ``_is_object_write_injection`` (face #7)
  L3 issue-time attribution (face #1, unit + registry dispatch)
  L4 history-scan threading (faces #2/#3/#4/#6)

Three hard invariants the fix must NOT break, each pinned below:
  * the verb table stays mutation-only — an ordinary Deployment/ConfigMap
    ``apply`` is never claimed (``test_apply_verb_is_not_claimed`` parity);
  * the FaultDrill CR is never stolen by k8s_native (registration order);
  * carrier scaffolding (SA/Role create) is never claimed, and command-mode
    ``exec``/``debug`` stay exempt at the armed gate (case-legislated timers).
"""

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.agent.nodes.execute._injection_detection import (
    classify_issue_time_method,
)
from chaos_agent.agent.nodes.planning.tool_screener import (
    _is_object_write_injection,
)
from chaos_agent.agent.providers.k8s_native.classifier import (
    PERSISTENT_FAULT_MANIFEST_KINDS,
    is_apply_native_fault_injection,
)
from chaos_agent.agent.providers.k8s_native.provider import K8sNativeProvider
from chaos_agent.agent.providers.message_scanning import (
    KUBECTL_WRITE_SUBCOMMANDS,
    scan_native_issue_disproven,
)


# ---------------------------------------------------------------------------
# Manifest fixtures + builders
# ---------------------------------------------------------------------------


def _manifest(kind, api="v1", name="drill-x"):
    return (
        f"apiVersion: {api}\n"
        f"kind: {kind}\n"
        f"metadata:\n"
        f"  name: {name}\n"
        f"  namespace: default\n"
    )


# persistent-fault family (PERSISTENT_FAULT_MANIFEST_KINDS members)
NETPOL = _manifest("NetworkPolicy", "networking.k8s.io/v1", "drill-netpol-abc")
CONFIGMAP = _manifest("ConfigMap", "v1", "drill-cm")
SECRET = _manifest("Secret", "v1", "drill-secret")
PVC = _manifest("PersistentVolumeClaim", "v1", "drill-pvc")
PV = _manifest("PersistentVolume", "v1", "drill-pv")
NAMESPACE = _manifest("Namespace", "v1", "drill-ns")
# NOT persistent-fault: ordinary workload / service / carrier scaffolding
DEPLOYMENT = _manifest("Deployment", "apps/v1", "victim")
STATEFULSET = _manifest("StatefulSet", "apps/v1", "victim")
SERVICE = _manifest("Service", "v1", "victim-svc")
SA = _manifest("ServiceAccount", "v1", "drill-rc-abc")
ROLE = _manifest("Role", "rbac.authorization.k8s.io/v1", "drill-rc-abc")
# the CR channel — k8s_native must defer, never steal
FAULTDRILL = _manifest("FaultDrill", "drill.blade-ai.io/v1alpha1", "fd-demo")


def _apply_args(stdin, sub="apply"):
    return {"subcommand": sub, "v_args": "-f -", "stdin_data": stdin}


def _call(args, tc_id="t1"):
    return AIMessage(
        content="",
        tool_calls=[{"id": tc_id, "name": "kubectl", "args": args}],
    )


def _result(content, tc_id="t1"):
    return ToolMessage(content=content, tool_call_id=tc_id, name="kubectl")


# ---------------------------------------------------------------------------
# L1 — the canonical predicate (single source of truth)
# ---------------------------------------------------------------------------


class TestApplyNativePredicate:
    """``is_apply_native_fault_injection``: apply/create of a persistent
    fault object, recognised by KIND. Every other face reuses this."""

    @pytest.mark.parametrize("manifest", [
        NETPOL, CONFIGMAP, SECRET, PVC, PV, NAMESPACE,
    ])
    def test_persistent_fault_kinds_are_injections(self, manifest):
        assert is_apply_native_fault_injection(
            "kubectl", _apply_args(manifest),
        )

    def test_create_variant_also_recognised(self):
        # ``create -f -`` is the same unbounded-fault shape as ``apply``
        assert is_apply_native_fault_injection(
            "kubectl", _apply_args(NETPOL, sub="create"),
        )

    @pytest.mark.parametrize("manifest", [
        DEPLOYMENT, STATEFULSET, SERVICE, SA, ROLE,
    ])
    def test_non_persistent_fault_kinds_rejected(self, manifest):
        # ordinary workloads / services / carrier scaffolding are NOT the
        # persistent-fault family — claiming them would over-reach.
        assert not is_apply_native_fault_injection(
            "kubectl", _apply_args(manifest),
        )

    def test_faultdrill_cr_is_not_stolen(self):
        # registration-order invariant: k8s_native defers the CR channel.
        assert not is_apply_native_fault_injection(
            "kubectl", _apply_args(FAULTDRILL),
        )

    def test_mixed_manifest_is_rejected(self):
        # a document stream that ALSO touches a non-fault kind is not a
        # pure persistent-fault injection — fail closed (subset, not any).
        mixed = NETPOL + "---\n" + DEPLOYMENT
        assert not is_apply_native_fault_injection(
            "kubectl", _apply_args(mixed),
        )

    @pytest.mark.parametrize("sub", ["patch", "delete", "get", "replace"])
    def test_non_apply_create_verbs_rejected(self, sub):
        # the predicate is the apply-native HALF only; mutation verbs are
        # the armed gate's other half (L2), not this predicate's concern.
        args = {"subcommand": sub, "v_args": "x", "stdin_data": NETPOL}
        assert not is_apply_native_fault_injection("kubectl", args)

    def test_apply_without_stdin_is_unprovable(self):
        # ``-f /path.yaml``: no stdin_data → cannot prove the KIND → fail
        # closed rather than guess.
        args = {"subcommand": "apply", "v_args": "-f /tmp/x.yaml"}
        assert not is_apply_native_fault_injection("kubectl", args)

    def test_apply_with_kindless_stdin_rejected(self):
        assert not is_apply_native_fault_injection(
            "kubectl", _apply_args("foo: bar\n"),
        )

    @pytest.mark.parametrize("sub", ["exec", "debug"])
    def test_command_mode_excluded(self, sub):
        # command-mode timers are case-legislated, not structurally provable.
        args = {"subcommand": sub, "v_args": "iptables -A INPUT -j DROP"}
        assert not is_apply_native_fault_injection("kubectl", args)

    def test_non_kubectl_tool_rejected(self):
        assert not is_apply_native_fault_injection(
            "blade_create", {"stdin_data": NETPOL},
        )

    def test_persistent_set_membership(self):
        # the category is defined by KIND, minus the CR channel.
        assert "faultdrill" not in PERSISTENT_FAULT_MANIFEST_KINDS
        assert "networkpolicy" in PERSISTENT_FAULT_MANIFEST_KINDS


# ---------------------------------------------------------------------------
# L2 — the armed-before-inject gate helper (face #7, the #65 fix site)
# ---------------------------------------------------------------------------


class TestArmedGateObjectWrite:
    """``_is_object_write_injection``: mutation verb ∪ apply-native, with
    command-mode exec/debug deliberately excluded."""

    @pytest.mark.parametrize("sub", ["delete", "patch", "scale"])
    def test_mutation_verbs_trigger(self, sub):
        # fixture sanity: these are genuine write subcommands
        assert sub in KUBECTL_WRITE_SUBCOMMANDS
        assert _is_object_write_injection(
            "kubectl", {"subcommand": sub, "v_args": "x"},
        )

    def test_apply_native_persistent_fault_triggers(self):
        # THE #65 FIX: an apply of a persistent fault object now arms the
        # gate — before Fork A the borrowed verb table left it ungated.
        assert _is_object_write_injection("kubectl", _apply_args(NETPOL))

    @pytest.mark.parametrize("manifest", [DEPLOYMENT, SA, FAULTDRILL])
    def test_non_fault_apply_does_not_trigger(self, manifest):
        # scaffolding / ordinary workload / CR: no false arming (a false
        # positive here would deadlock the run demanding a carrier).
        assert not _is_object_write_injection("kubectl", _apply_args(manifest))

    @pytest.mark.parametrize("sub", ["exec", "debug"])
    def test_command_mode_still_exempt(self, sub):
        # precision constraint preserved: command-mode injections stay out.
        args = {"subcommand": sub, "v_args": "iptables -A INPUT -j DROP"}
        assert not _is_object_write_injection("kubectl", args)

    def test_read_only_does_not_trigger(self):
        assert not _is_object_write_injection(
            "kubectl", {"subcommand": "get", "v_args": "pods"},
        )


# ---------------------------------------------------------------------------
# L3 — issue-time attribution (face #1): unit + registry dispatch
# ---------------------------------------------------------------------------


class TestIssueTimeAttribution:
    def test_netpol_apply_attributes_kubectl_native(self):
        assert K8sNativeProvider().issue_time_method(
            "kubectl", _apply_args(NETPOL),
        ) == "kubectl_native"

    def test_configmap_apply_attributes_kubectl_native(self):
        assert K8sNativeProvider().issue_time_method(
            "kubectl", _apply_args(CONFIGMAP),
        ) == "kubectl_native"

    def test_deployment_apply_not_claimed(self):
        # verb table stays mutation-only: an ordinary apply is NOT claimed
        # (test_apply_verb_is_not_claimed parity at the attribution face).
        assert K8sNativeProvider().issue_time_method(
            "kubectl", _apply_args(DEPLOYMENT),
        ) is None

    def test_faultdrill_apply_not_stolen(self):
        assert K8sNativeProvider().issue_time_method(
            "kubectl", _apply_args(FAULTDRILL),
        ) is None

    def test_serviceaccount_create_not_claimed(self):
        assert K8sNativeProvider().issue_time_method(
            "kubectl", _apply_args(SA, sub="create"),
        ) is None

    def test_patch_mutation_unchanged(self):
        assert K8sNativeProvider().issue_time_method(
            "kubectl", {"subcommand": "patch", "v_args": "x"},
        ) == "kubectl_native"

    def test_dispatch_reaches_k8s_native(self):
        # integration: the generic registry dispatch entry recognises the
        # apply-native fault without naming a carrier-specific token.
        assert classify_issue_time_method(
            "kubectl", _apply_args(NETPOL), is_host=False,
        ) == "kubectl_native"

    def test_dispatch_does_not_claim_deployment_apply(self):
        assert classify_issue_time_method(
            "kubectl", _apply_args(DEPLOYMENT), is_host=False,
        ) is None


# ---------------------------------------------------------------------------
# L4 — history-scan threading (faces #2 detect / #4 recency / #6 attempted
#      / #3 issue_disproven). The default ``is_native_injection=None`` keeps
#      every scan byte-identical for the other carriers.
# ---------------------------------------------------------------------------


class TestScanThreading:
    _OK = "networkpolicy.networking.k8s.io/drill-netpol-abc created"
    _FAIL = 'Error: networkpolicies.networking.k8s.io "drill-netpol-abc" not found'

    def _msgs(self, result_content):
        return [_call(_apply_args(NETPOL)), _result(result_content)]

    def test_detect_recognises_apply_native(self):
        p = K8sNativeProvider()
        assert p.detect(self._msgs(self._OK), is_host=False) == "kubectl_native"

    def test_injection_recency_zero(self):
        p = K8sNativeProvider()
        assert p.injection_recency(self._msgs(self._OK), is_host=False) == 0

    def test_was_injection_attempted(self):
        p = K8sNativeProvider()
        assert p.was_injection_attempted(self._msgs(self._OK)) is True

    def test_failed_apply_native_is_disproven_via_predicate(self):
        # Cleanest adversarial proof: SAME failed apply. Without the
        # predicate the ``apply`` verb is invisible to the write-subcommand
        # scan (the pre-fix blind spot) → not disproven. Threading the
        # canonical predicate recognises the attempt → the failed result
        # revokes it. The ``None`` default stays byte-identical.
        failed = self._msgs(self._FAIL)
        write_subs = frozenset({"scale", "patch", "cordon"})  # apply absent
        assert scan_native_issue_disproven(failed, write_subs) is False
        assert scan_native_issue_disproven(
            failed, write_subs,
            is_native_injection=is_apply_native_fault_injection,
        ) is True

    def test_successful_apply_native_not_disproven(self):
        # a landed apply stands (not counter-evidence) even once recognised.
        ok = self._msgs(self._OK)
        write_subs = frozenset({"scale", "patch", "cordon"})
        assert scan_native_issue_disproven(
            ok, write_subs,
            is_native_injection=is_apply_native_fault_injection,
        ) is False

    def test_provider_issue_disproven_wiring(self):
        p = K8sNativeProvider()
        assert p.issue_disproven(self._msgs(self._FAIL)) is True
