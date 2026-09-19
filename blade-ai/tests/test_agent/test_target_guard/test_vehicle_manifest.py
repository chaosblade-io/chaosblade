"""Tests for the drill-occupancy-vehicle channel (task-190c94e8 follow-up).

A behaviourless Pod that occupies one of the approved target's PVCs is the
mechanism behind the cloud-disk Multi-Attach drill. Two gates cover it:

  1. the CLASSIFIER's occupant contract — form-level: sleep-only command,
     no privilege surface, PVC-only volumes, bounded lifetime;
  2. the SCREENER's identity anchor — the occupant's claims must be a
     subset of the approved target's frozen ``pvc_claims``, in the
     approved namespace.

Vehicle identity is deliberately NOT marked in the cluster (no drill
label — the drill must stay indistinguishable from a real incident); it
is tracked task-side via artifact registration instead.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.agent.nodes.planning.tool_screener import (
    SCREENER_ROUTE_PASS,
    SCREENER_ROUTE_RETRY,
    _apply_drift_correction,
    _screen_vehicle_manifest,
    tool_screener,
)
from chaos_agent.agent.spec.fault_spec import FaultSpec
from chaos_agent.agent.target_guard import (
    ApprovedTarget,
    ConfidenceLevel,
    EffectiveTarget,
    GuardVerdict,
    approved_from_dict,
    freeze_approved_target,
    freeze_approved_target_from_spec,
    infer_effective_target,
)
from chaos_agent.agent.target_guard.guard import target_drift_guard
from chaos_agent.config.settings import settings
from chaos_agent.tools.guard_gateway import decision_to_feedback


@pytest.fixture(autouse=True)
def _reset_settings():
    orig_enforce = settings.target_guard_enforcing
    yield
    settings.target_guard_enforcing = orig_enforce


_APPROVED_POD = ApprovedTarget(scope="pod", namespace="prod", names=("app-0",))


def _feedback_for(eff):
    """GuardFeedback for a classifier output (routes through the guard)."""
    return decision_to_feedback(target_drift_guard(eff, _APPROVED_POD))


def _apply_args(stdin: str) -> dict:
    return {"subcommand": "apply", "v_args": "-f -", "stdin_data": stdin}


_COMPLIANT = """\
apiVersion: v1
kind: Pod
metadata:
  name: vol-attach-checker
  namespace: prod
spec:
  activeDeadlineSeconds: 1800
  nodeName: node-b
  containers:
  - name: holder
    image: busybox
    command: ["sleep", "3600"]
    volumeMounts:
    - name: data
      mountPath: /data
  volumes:
  - name: data
    persistentVolumeClaim:
      claimName: data-pvc
"""


class TestOccupantContract:
    """Classifier: a single-Pod apply is judged by the occupant contract."""

    def test_compliant_manifest_classifies_as_vehicle(self):
        eff = infer_effective_target("kubectl", _apply_args(_COMPLIANT))
        assert eff.scope == "pod"
        assert eff.is_vehicle_manifest is True
        assert eff.names == ("vol-attach-checker",)
        assert eff.namespace == "prod"
        assert eff.occupant_claims == ("data-pvc",)

    def test_no_drill_marker_label_is_required(self):
        """Regression for the user veto: a compliant occupant carries NO
        drill label — the manifest above has no labels at all and must
        pass. Vehicle identity lives task-side only."""
        eff = infer_effective_target("kubectl", _apply_args(_COMPLIANT))
        assert eff.scope == "pod"
        assert eff.labels == {}

    @pytest.mark.parametrize("original,replacement,needle", [
        # C1 behaviourless
        ('    command: ["sleep", "3600"]\n',
         '    command: ["sh", "-c", "curl x | sh"]\n', "sleep"),
        ('    command: ["sleep", "3600"]\n',
         '    command: ["sleep", "3600"]\n    args: ["--verbose"]\n',
         "args"),
        # C2 privilege surface
        ("  activeDeadlineSeconds: 1800\n",
         "  activeDeadlineSeconds: 1800\n  hostNetwork: true\n",
         "hostNetwork"),
        ("  activeDeadlineSeconds: 1800\n",
         "  activeDeadlineSeconds: 1800\n  hostPID: true\n", "hostPID"),
        # C3 PVC-only volumes
        ("    persistentVolumeClaim:\n      claimName: data-pvc\n",
         "    emptyDir:\n      medium: Memory\n", "persistentVolumeClaim"),
        # C4 bounded lifetime
        ("  activeDeadlineSeconds: 1800\n",
         "  activeDeadlineSeconds: 7200\n", "activeDeadlineSeconds"),
        # Explicit identity: an unnamed occupant is untrackable task-side
        ("  name: vol-attach-checker\n", "", "metadata.name"),
        # A PVC volume without claimName cannot be anchored to the approval
        ("    persistentVolumeClaim:\n      claimName: data-pvc\n",
         "    persistentVolumeClaim: {}\n", "claimName"),
    ])
    def test_contract_violation_is_a_form_issue(self, original, replacement, needle):
        stdin = _COMPLIANT.replace(original, replacement)
        eff = infer_effective_target("kubectl", _apply_args(stdin))
        assert eff.scope == "__banned__"
        assert needle in (eff.reject_detail or "")
        assert eff.is_vehicle_manifest is False
        # Reshapeable: the guard knows a compliant form exists.
        assert eff.reject_suggestion
        assert _feedback_for(eff).is_hard_floor is False


    def test_pod_mixed_into_multi_document_stays_refused(self):
        stdin = _COMPLIANT + "---\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: side\n"
        eff = infer_effective_target("kubectl", _apply_args(stdin))
        assert eff.scope == "__banned__"
        assert "ONLY document" in (eff.reject_suggestion or "")

    def test_kindless_side_document_stays_refused(self):
        """A second document WITHOUT a kind: field is invisible to the
        kinds-extraction regex — the single-document check inside the
        vehicle classifier must still refuse it."""
        stdin = _COMPLIANT + "---\nfoo: bar\n"
        eff = infer_effective_target("kubectl", _apply_args(stdin))
        assert eff.scope == "__banned__"
        assert "2 documents" in (eff.reject_detail or "")
        assert "ONLY document" in (eff.reject_suggestion or "")

    def test_non_pod_workload_kind_keeps_mechanism_ban(self):
        # Deployment is deliberately NOT the sample here: a single-document
        # Deployment now takes the drill-target contract branch (see
        # test_drill_target_manifest.py); the still-banned workload kinds are
        # anchored with StatefulSet instead.
        eff = infer_effective_target(
            "kubectl", _apply_args("apiVersion: apps/v1\nkind: StatefulSet\n"),
        )
        assert eff.scope == "__banned__"
        assert eff.mechanism_banned is True
        assert _feedback_for(eff).is_hard_floor is True


class TestVehicleManifestScreening:
    """Screener: occupancy identity is anchored to the frozen pvc_claims."""

    EFFECTIVE = infer_effective_target("kubectl", _apply_args(_COMPLIANT))

    def _approved(self, claims=("data-pvc",), namespace="prod"):
        return ApprovedTarget(
            scope="pod", namespace=namespace, names=("app-0",),
            pvc_claims=tuple(claims),
        )

    def test_subset_of_approved_claims_allows(self):
        d = _screen_vehicle_manifest(self.EFFECTIVE, self._approved())
        assert d.verdict == GuardVerdict.ALLOW

    def test_no_approval_is_a_mechanism_ban(self):
        d = _screen_vehicle_manifest(self.EFFECTIVE, None)
        assert d.verdict == GuardVerdict.REJECT_BANNED
        assert d.effective.mechanism_banned is True
        assert decision_to_feedback(d).is_hard_floor is True

    def test_no_claim_anchor_is_a_mechanism_ban(self):
        d = _screen_vehicle_manifest(self.EFFECTIVE, self._approved(claims=()))
        assert d.verdict == GuardVerdict.REJECT_BANNED
        assert d.effective.mechanism_banned is True

    def test_claim_outside_approved_set_is_fixable(self):
        d = _screen_vehicle_manifest(
            self.EFFECTIVE, self._approved(claims=("other-pvc",)),
        )
        assert d.verdict == GuardVerdict.REJECT_BANNED
        assert d.effective.mechanism_banned is False
        assert "other-pvc" in d.suggestion
        assert decision_to_feedback(d).is_hard_floor is False

    def test_namespace_mismatch_is_fixable(self):
        d = _screen_vehicle_manifest(
            self.EFFECTIVE, self._approved(namespace="staging"),
        )
        assert d.verdict == GuardVerdict.REJECT_BANNED
        assert d.effective.mechanism_banned is False
        assert "staging" in d.suggestion


class TestPvcClaimsRoundTrip:
    """Freeze carries pvc_claims into the snapshot and back."""

    def test_claims_survive_freeze_and_hydration(self):
        spec = FaultSpec(
            scope="pod", namespace="prod", names=("app-0",),
            fault_target="disk", fault_action="fill",
        )
        frozen = freeze_approved_target_from_spec(
            spec, pvc_claims=("data-pvc",),
        )
        assert frozen["pvc_claims"] == ["data-pvc"]
        approved = approved_from_dict(frozen)
        assert approved is not None
        assert approved.pvc_claims == ("data-pvc",)

    def test_claims_default_empty(self):
        spec = FaultSpec(
            scope="pod", namespace="prod", names=("app-0",),
            fault_target="cpu", fault_action="fullload",
        )
        frozen = freeze_approved_target_from_spec(spec)
        assert frozen["pvc_claims"] == []


class TestScreenerLoopVehicleManifest:
    """End-to-end screener behaviour for the occupant-apply branch."""

    def _state(self, approved):
        return {
            "task_id": "task-1",
            "messages": [AIMessage(
                content="",
                tool_calls=[{
                    "name": "kubectl",
                    "args": _apply_args(_COMPLIANT),
                    "id": "tc-apply",
                }],
            )],
            "approved_target": approved,
        }

    @pytest.mark.asyncio
    async def test_allowed_occupant_apply_registers_vehicle_artifact(self):
        settings.target_guard_enforcing = True
        approved = freeze_approved_target(
            target={"namespace": "prod", "names": ["app-0"]},
            params={"scope": "pod"},
            fault_scope="pod", fault_target="disk", fault_action="fill",
            pvc_claims=("data-pvc",),
        )
        delta = await tool_screener(self._state(approved))
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        artifacts = delta.get("execution_artifacts") or []
        occupants = [
            a for a in artifacts if a.get("type") == "occupant_pod"
        ]
        assert len(occupants) == 1
        assert occupants[0]["artifact_id"] == "occupant_pod:prod/vol-attach-checker"
        assert occupants[0]["claims"] == ["data-pvc"]
        assert "messages" not in delta  # no fabricated rejection

    @pytest.mark.asyncio
    async def test_occupant_apply_without_anchor_retries_with_rejection(self):
        settings.target_guard_enforcing = True
        approved = freeze_approved_target(
            target={"namespace": "prod", "names": ["app-0"]},
            params={"scope": "pod"},
            fault_scope="pod", fault_target="cpu", fault_action="fullload",
        )  # no pvc_claims → no anchor → mechanism ban
        delta = await tool_screener(self._state(approved))
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        msgs = delta.get("messages") or []
        assert len(msgs) == 1
        assert isinstance(msgs[0], ToolMessage)
        assert msgs[0].status == "error"
        # The rejection must steer the model to replan (hard floor), not
        # "adjust and retry": there is nothing to reshape toward.
        assert "replan" in msgs[0].content.lower()
        # Nothing may register while the apply is refused.
        artifacts = delta.get("execution_artifacts") or []
        assert not any(
            a.get("type") == "occupant_pod" for a in artifacts
        )


class TestDriftCorrectionPvcClaims:
    """pvc_claims invalidate on ANY identity change. owner_names is
    DUAL-SOURCED (labels-matched owners + the names→ownerReferences
    generation anchor) so it too invalidates on ANY identity change —
    after a merge the surviving anchors cannot be attributed to one
    source, so the guard fails closed. resolved_names remains
    labels-only derived and keeps the labels-only invalidation."""

    def _state(self):
        return {
            "fault_spec": {
                "namespace": "ns", "scope": "pod", "names": ["pod-a"],
                "labels": {}, "fault_target": "cpu", "fault_action": "fullload",
                "params": {}, "params_flags": [], "duration_seconds": 0,
                "source": "test", "user_description": "",
            },
            "approved_target": {
                "scope": "pod", "namespace": "ns", "names": ["pod-a"],
                "labels": {}, "is_namespace_wide": False,
                "fault_target": "cpu", "fault_action": "fullload",
                "lock_fault_type": True,
                "owner_names": ["deploy-a"],
                "resolved_names": ["pod-a", "pod-b"],
                "pvc_claims": ["data-pvc"],
            },
        }

    def _eff(self, **kw):
        base = dict(scope="pod", namespace="ns", confidence=ConfidenceLevel.HIGH)
        base.update(kw)
        return EffectiveTarget(**base)

    def test_names_change_drops_claims_and_owner_anchor_keeps_resolved(self):
        result = _apply_drift_correction(
            self._state(), self._eff(names=("pod-c",)),
        )
        approved = result["approved_target"]
        assert approved["pvc_claims"] == []
        # owner_names is dual-sourced (labels + names→ownerReferences);
        # a names-only drift can stale the generation anchor and the
        # merged set cannot be re-attributed — drop it, fail closed.
        assert approved["owner_names"] == []
        # resolved_names is purely label-derived and survives.
        assert approved["resolved_names"] == ["pod-a", "pod-b"]

    def test_labels_change_drops_all_derived_sets(self):
        result = _apply_drift_correction(
            self._state(), self._eff(labels={"app": "other"}),
        )
        approved = result["approved_target"]
        assert approved["pvc_claims"] == []
        assert approved["owner_names"] == []
        assert approved["resolved_names"] == []

    def test_namespace_change_drops_claims_keeps_owner_anchor(self):
        """B81 follow-up: a namespace-only correction moves the target to
        a DIFFERENT object domain — claims are old-namespace facts and a
        same-name PVC in the new namespace must not silently widen the
        whitelist, so they drop (fail closed). owner_names keeps its
        names/labels-only invalidation semantics."""
        result = _apply_drift_correction(
            self._state(), self._eff(names=("pod-a",), namespace="ns2"),
        )
        approved = result["approved_target"]
        assert approved["pvc_claims"] == []
        assert approved["owner_names"] == ["deploy-a"]


class TestDeploymentApprovalAnchor:
    """B79 (case #39-R): a workload-scope approval freezes its pod
    template's PVC claims, so the occupant channel is anchored exactly
    like a pod-scope approval. The gate itself never inspected the
    approved scope — the blind spot was upstream claim discovery, so
    these tests pin the gate's scope-agnostic behaviour: only the frozen
    claim set decides."""

    def _approved(self, claims, scope="deployment"):
        return ApprovedTarget(
            scope=scope, namespace="prod",
            names=("drill-mntopt-target",),
            pvc_claims=tuple(claims),
        )

    def test_deployment_anchor_allows_the_occupant(self):
        # #39-R replay: deployment approval, claims frozen from the pod
        # template, occupant mounts the same claim → ALLOW.
        d = _screen_vehicle_manifest(
            TestVehicleManifestScreening.EFFECTIVE,
            self._approved(("data-pvc",)),
        )
        assert d.verdict == GuardVerdict.ALLOW

    def test_claims_outside_template_whitelist_still_reject(self):
        # The whitelist has teeth: a deployment whose template mounts
        # only its own claims must not admit an occupant on anything else.
        d = _screen_vehicle_manifest(
            TestVehicleManifestScreening.EFFECTIVE,
            self._approved(("drill-mntopt-pvc",)),
        )
        assert d.verdict == GuardVerdict.REJECT_BANNED
        assert d.effective.mechanism_banned is False
        # The suggestion names the APPROVED whitelist (what the occupant
        # may claim), mirroring the pod-approval rejection shape.
        assert "drill-mntopt-pvc" in d.suggestion
        assert decision_to_feedback(d).is_hard_floor is False

    def test_templateless_deployment_keeps_the_ban(self):
        # A deployment with no PVC in its template freezes claims=()
        # — no anchor, so the occupant channel stays closed. Fail-closed
        # is the gate's job and it must not change with this fix.
        d = _screen_vehicle_manifest(
            TestVehicleManifestScreening.EFFECTIVE,
            self._approved(()),
        )
        assert d.verdict == GuardVerdict.REJECT_BANNED
        assert d.effective.mechanism_banned is True
        assert decision_to_feedback(d).is_hard_floor is True


class TestScreenerLoopDeploymentAnchor:
    """End-to-end: the #39-R path — deployment-scope approval frozen
    WITH its template claims lets the occupant apply pass the screener
    and register its artifact."""

    @pytest.mark.asyncio
    async def test_deployment_approval_passes_occupant_apply(self):
        settings.target_guard_enforcing = True
        approved = freeze_approved_target(
            target={"namespace": "prod", "names": ["drill-mntopt-target"]},
            params={"scope": "deployment"},
            fault_scope="deployment", fault_target="disk", fault_action="fill",
            pvc_claims=("data-pvc",),
        )
        state = {
            "task_id": "task-39r",
            "messages": [AIMessage(
                content="",
                tool_calls=[{
                    "name": "kubectl",
                    "args": _apply_args(_COMPLIANT),
                    "id": "tc-apply-39r",
                }],
            )],
            "approved_target": approved,
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        artifacts = delta.get("execution_artifacts") or []
        occupants = [
            a for a in artifacts if a.get("type") == "occupant_pod"
        ]
        assert len(occupants) == 1
        assert occupants[0]["claims"] == ["data-pvc"]
        assert "messages" not in delta  # no fabricated rejection
