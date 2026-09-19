"""Tests for the drill-target manifest contract (drill-target-contract).

A dedicated victim Deployment the task stages itself when the approved
target does not exist yet — case #38: the PVC-mounting target was deleted
out-of-band between drills and the first planning round looped 30 minutes
against the blanket workload-create ban. Three gates cover the channel:

  1. the CLASSIFIER's drill-target contract — form-level: exactly one
     container under spec.template.spec (no initContainers), no privilege
     surface, carrier-allow-set image, pvc/configMap/secret volumes only;
  2. the SCREENER's identity anchor — the ORDINARY drift net, because the
     drill target's name IS the approved identity (unlike an occupant's
     generated name, which can never match);
  3. the artifact registration — ``occupant_deployment`` vehicle keyed
     task-side (cleanup chain delete + the deployment-kind delete
     exemption), converging with the script channel's ``[drill-vehicle]``
     registration on the same artifact_id.

The downstream (artifact type, kind-matched exemption, cleanup deletion)
already existed for the script channel; this change only wires the manifest
channel's upstream to it.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage
from unittest.mock import patch

from chaos_agent.agent.nodes.planning.tool_screener import (
    SCREENER_ROUTE_PASS,
    SCREENER_ROUTE_RETRY,
    tool_screener,
)
from chaos_agent.agent.providers.k8s_native.classifier import (
    _allowed_manifest_kinds_text,
)
from chaos_agent.agent.target_guard import (
    SCOPE_BANNED,
    freeze_approved_target,
    infer_effective_target,
)
from chaos_agent.config.settings import settings


_IMAGE = "registry.example.com/probe:v1"


@pytest.fixture(autouse=True)
def _reset_settings():
    orig_enforce = settings.target_guard_enforcing
    orig_images = settings.recovery_carrier_allowed_images
    settings.recovery_carrier_allowed_images = _IMAGE
    yield
    settings.target_guard_enforcing = orig_enforce
    settings.recovery_carrier_allowed_images = orig_images


def _apply_args(stdin: str, sub: str = "apply", v_args: str = "-f -") -> dict:
    return {"subcommand": sub, "v_args": v_args, "stdin_data": stdin}


_COMPLIANT = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: drill-t
  namespace: prod
  labels:
    app: drill-t
spec:
  replicas: 1
  selector:
    matchLabels:
      app: drill-t
  template:
    metadata:
      labels:
        app: drill-t
    spec:
      containers:
      - name: target
        image: registry.example.com/probe:v1
        command: ["sleep", "7200"]
        volumeMounts:
        - name: data
          mountPath: /data
      volumes:
      - name: data
        persistentVolumeClaim:
          claimName: data-pvc
"""


def _approved(**overrides):
    target = {"namespace": "prod", "names": ["drill-t"]}
    target.update(overrides.pop("target", {}))
    kwargs = {
        "params": {"scope": "deployment"},
        "fault_scope": "deployment",
        "fault_target": "pod",
        "fault_action": "fill",
    }
    kwargs.update(overrides)
    return freeze_approved_target(target=target, **kwargs)


def _state(
    approved,
    manifest: str,
    tool_call_id: str = "tc-1",
    sub: str = "apply",
    v_args: str = "-f -",
):
    return {
        "task_id": "task-dt",
        "messages": [AIMessage(
            content="",
            tool_calls=[{
                "name": "kubectl",
                "args": _apply_args(manifest, sub=sub, v_args=v_args),
                "id": tool_call_id,
            }],
        )],
        "approved_target": approved,
    }


class TestDrillTargetContract:
    """Classifier: a single-Deployment apply is judged by the drill-target
    contract — every violation branch has its own anchor (B34 discipline)."""

    def test_compliant_manifest_classifies_as_drill_target(self):
        eff = infer_effective_target("kubectl", _apply_args(_COMPLIANT))
        assert eff.scope == "deployment"
        assert eff.is_drill_target_manifest is True
        assert eff.names == ("drill-t",)
        assert eff.namespace == "prod"
        assert eff.mechanism_banned is False

    def test_configmap_and_secret_volumes_pass(self):
        """T5 user ruling (2026-09-11): all three data-volume kinds, once."""
        manifest = _COMPLIANT.replace(
            """      - name: data
        persistentVolumeClaim:
          claimName: data-pvc
""",
            """      - name: cfg
        configMap:
          name: app-cfg
      - name: creds
        secret:
          secretName: app-creds
""",
        )
        eff = infer_effective_target("kubectl", _apply_args(manifest))
        assert eff.is_drill_target_manifest is True

    def test_two_containers_rejected(self):
        manifest = _COMPLIANT.replace(
            "      - name: target\n",
            "      - name: target\n      - name: side\n"
            "        image: registry.example.com/probe:v1\n",
        )
        eff = infer_effective_target("kubectl", _apply_args(manifest))
        assert eff.scope == "__banned__"
        assert "exactly one container" in (eff.reject_detail or "")

    def test_init_containers_rejected(self):
        manifest = _COMPLIANT.replace(
            "    spec:\n      containers:\n",
            "    spec:\n      initContainers:\n      - name: init\n"
            "        image: registry.example.com/probe:v1\n"
            "        command: [\"true\"]\n      containers:\n",
        )
        eff = infer_effective_target("kubectl", _apply_args(manifest))
        assert eff.scope == "__banned__"
        assert "initContainers" in (eff.reject_detail or "")

    def test_privileged_container_rejected(self):
        """T3 at the CORRECT path: spec.template.spec.containers[].securityContext."""
        manifest = _COMPLIANT.replace(
            '        command: ["sleep", "7200"]\n',
            '        command: ["sleep", "7200"]\n'
            "        securityContext:\n          privileged: true\n",
        )
        eff = infer_effective_target("kubectl", _apply_args(manifest))
        assert eff.scope == "__banned__"
        assert "privileged" in (eff.reject_detail or "")

    def test_host_network_rejected(self):
        """T3 pod-level flags live under spec.template.spec, not spec."""
        manifest = _COMPLIANT.replace(
            "    spec:\n      containers:\n",
            "    spec:\n      hostNetwork: true\n      containers:\n",
        )
        eff = infer_effective_target("kubectl", _apply_args(manifest))
        assert eff.scope == "__banned__"
        assert "hostNetwork" in (eff.reject_detail or "")

    def test_hostpath_volume_rejected(self):
        manifest = _COMPLIANT.replace(
            """      - name: data
        persistentVolumeClaim:
          claimName: data-pvc
""",
            "      - name: host\n        hostPath:\n"
            "          path: /\n",
        )
        eff = infer_effective_target("kubectl", _apply_args(manifest))
        assert eff.scope == "__banned__"
        assert "hostPath" in (eff.reject_detail or "")

    def test_disallowed_volume_kind_rejected(self):
        manifest = _COMPLIANT.replace(
            """      - name: data
        persistentVolumeClaim:
          claimName: data-pvc
""",
            "      - name: scratch\n        emptyDir: {}\n",
        )
        eff = infer_effective_target("kubectl", _apply_args(manifest))
        assert eff.scope == "__banned__"
        assert "persistentVolumeClaim, configMap or secret" in (
            eff.reject_detail or ""
        )

    def test_off_allowlist_image_rejected(self):
        manifest = _COMPLIANT.replace(_IMAGE, "evil.example.com/rootkit:v9")
        eff = infer_effective_target("kubectl", _apply_args(manifest))
        assert eff.scope == "__banned__"
        assert "not in the carrier image allowlist" in (eff.reject_detail or "")

    def test_missing_template_spec_rejected(self):
        """Path trap: a Deployment without spec.template.spec is a violation,
        not a silent pass of every template-level check."""
        manifest = _COMPLIANT.replace(
            "  template:\n    metadata:\n      labels:\n"
            "        app: drill-t\n    spec:\n",
            "  template:\n    metadata:\n      labels:\n        app: drill-t\n",
        )
        eff = infer_effective_target("kubectl", _apply_args(manifest))
        assert eff.scope == "__banned__"
        assert "spec.template.spec" in (eff.reject_detail or "")

    def test_generate_name_rejected(self):
        manifest = _COMPLIANT.replace(
            "  name: drill-t\n", "  generateName: drill-t-\n"
        )
        eff = infer_effective_target("kubectl", _apply_args(manifest))
        assert eff.scope == "__banned__"
        assert "generateName is not allowed" in (eff.reject_detail or "")

    def test_violation_is_form_issue_not_mechanism_ban(self):
        """A contract violation has a compliant reshape path (fix and re-apply
        the SAME manifest) — it must NOT carry the replan-only marker."""
        manifest = _COMPLIANT.replace(_IMAGE, "evil.example.com/rootkit:v9")
        eff = infer_effective_target("kubectl", _apply_args(manifest))
        assert eff.mechanism_banned is False
        assert "re-apply the SAME manifest" in (eff.reject_suggestion or "")


class TestChannelBoundary:
    """Deployment enters NO generic whitelist — only the dedicated gate."""

    def test_mixed_with_whitelisted_kind_keeps_mechanism_ban(self):
        manifest = (
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: cm\n"
            f"---\n{_COMPLIANT}"
        )
        eff = infer_effective_target("kubectl", _apply_args(manifest))
        assert eff.scope == "__banned__"
        assert eff.mechanism_banned is True

    def test_mixed_with_pod_uses_pod_mix_branch(self):
        manifest = _COMPLIANT + (
            "---\napiVersion: v1\nkind: Pod\nmetadata:\n  name: p\n"
        )
        eff = infer_effective_target("kubectl", _apply_args(manifest))
        assert eff.scope == "__banned__"
        assert eff.mechanism_banned is False
        assert "mixes a Pod" in (eff.reject_detail or "")

    def test_statefulset_keeps_mechanism_ban(self):
        manifest = _COMPLIANT.replace("kind: Deployment", "kind: StatefulSet")
        eff = infer_effective_target("kubectl", _apply_args(manifest))
        assert eff.scope == "__banned__"
        assert eff.mechanism_banned is True

    def test_allowed_kinds_text_excludes_deployment(self):
        assert "deployment" not in _allowed_manifest_kinds_text().lower()

    def test_ban_suggestion_points_to_drill_target_contract(self):
        """The old suggestion funnelled every workload need to 'inject into a
        workload that already exists' — the new one names the contract path."""
        manifest = _COMPLIANT.replace("kind: Deployment", "kind: DaemonSet")
        eff = infer_effective_target("kubectl", _apply_args(manifest))
        suggestion = eff.reject_suggestion or ""
        assert "drill-target contract" in suggestion
        # The blanket guidance survives for genuinely out-of-scope kinds.
        assert "workload that already exists" in suggestion


class TestDrillTargetScreening:
    """Screener: the ORDINARY drift net anchors the identity; ALLOW
    registers the occupant_deployment vehicle artifact."""

    @pytest.mark.asyncio
    async def test_allowed_apply_registers_occupant_deployment(self):
        settings.target_guard_enforcing = True
        delta = await tool_screener(_state(_approved(), _COMPLIANT))
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        artifacts = [
            a for a in delta.get("execution_artifacts") or []
            if a.get("type") == "occupant_deployment"
        ]
        assert len(artifacts) == 1
        assert artifacts[0]["artifact_id"] == "occupant_deployment:prod/drill-t"
        assert artifacts[0]["cleanup"]["v_args"] == (
            "deployment drill-t -n prod --ignore-not-found"
        )
        assert artifacts[0]["operation_family"] == "drill_target"
        assert "messages" not in delta  # no fabricated rejection

    @pytest.mark.asyncio
    async def test_name_outside_approved_set_is_standard_drift(self):
        settings.target_guard_enforcing = True
        manifest = _COMPLIANT.replace("name: drill-t", "name: drill-t-evil")
        with patch(
            "chaos_agent.agent.nodes.planning.tool_screener.interrupt",
            return_value="rejected",
        ):
            delta = await tool_screener(_state(_approved(), manifest))
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        msgs = delta.get("messages") or []
        assert len(msgs) == 1
        assert msgs[0].status == "error"
        # Standard drift education semantics, not a bespoke anchor rejection.
        assert "drift" in msgs[0].content.lower()
        artifacts = [
            a for a in delta.get("execution_artifacts") or []
            if a.get("type") == "occupant_deployment"
        ]
        assert not artifacts  # nothing registers while refused

    @pytest.mark.asyncio
    async def test_prefix_variant_is_still_drift(self):
        """A same-prefix name must not ride a fuzzy match (fail-closed)."""
        settings.target_guard_enforcing = True
        manifest = _COMPLIANT.replace("name: drill-t", "name: drill-t-2")
        with patch(
            "chaos_agent.agent.nodes.planning.tool_screener.interrupt",
            return_value="rejected",
        ):
            delta = await tool_screener(_state(_approved(), manifest))
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY

    @pytest.mark.asyncio
    async def test_namespace_mismatch_is_standard_drift(self):
        settings.target_guard_enforcing = True
        manifest = _COMPLIANT.replace("namespace: prod", "namespace: other")
        with patch(
            "chaos_agent.agent.nodes.planning.tool_screener.interrupt",
            return_value="rejected",
        ):
            delta = await tool_screener(_state(_approved(), manifest))
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY

    @pytest.mark.asyncio
    async def test_label_only_approval_anchors_by_labels(self):
        """A label-only approval (spec.names empty, target not yet in the
        cluster so label resolution misses) still anchors — by its LABEL
        set, not by names. The ordinary drift net is "names_ok OR
        labels_ok" (drift_policy L409-417): with approved.names empty and
        the manifest's labels a superset of approved.labels, labels_ok is
        the anchor. This is the drift net's standing semantics for every
        kubectl call (a label-scoped approval anchors any call whose
        labels cover it) — the drill-target branch inherits it unchanged,
        it does not introduce a new surface."""
        settings.target_guard_enforcing = True
        approved = _approved(target={"names": [], "labels": {"app": "drill-t"}})
        delta = await tool_screener(_state(approved, _COMPLIANT))
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        artifacts = [
            a for a in delta.get("execution_artifacts") or []
            if a.get("type") == "occupant_deployment"
        ]
        assert len(artifacts) == 1

    @pytest.mark.asyncio
    async def test_no_selector_fails_closed(self):
        """The REAL empty-anchor form: approved.names empty AND the
        manifest's labels do not cover approved.labels — neither names_ok
        nor labels_ok can hold, so the drift net rejects (fail-closed).
        The form contract is unaffected: this is standard drift semantics,
        same rejection as any other kubectl call with no anchor."""
        settings.target_guard_enforcing = True
        approved = _approved(target={"names": [], "labels": {"app": "other"}})
        with patch(
            "chaos_agent.agent.nodes.planning.tool_screener.interrupt",
            return_value="rejected",
        ):
            delta = await tool_screener(_state(approved, _COMPLIANT))
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        msgs = delta.get("messages") or []
        assert len(msgs) == 1
        assert msgs[0].status == "error"
        artifacts = [
            a for a in delta.get("execution_artifacts") or []
            if a.get("type") == "occupant_deployment"
        ]
        assert not artifacts

    @pytest.mark.asyncio
    async def test_replayed_batch_registers_once(self):
        """A screening round replays the pending batch as a whole — the same
        apply twice must not duplicate the artifact (dedup by artifact_id)."""
        settings.target_guard_enforcing = True
        approved = _approved()
        state = {
            "task_id": "task-dt",
            "messages": [AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "kubectl",
                        "args": _apply_args(_COMPLIANT),
                        "id": "tc-1",
                    },
                    {
                        "name": "kubectl",
                        "args": _apply_args(_COMPLIANT),
                        "id": "tc-2",
                    },
                ],
            )],
            "approved_target": approved,
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        artifacts = [
            a for a in delta.get("execution_artifacts") or []
            if a.get("type") == "occupant_deployment"
        ]
        assert len(artifacts) == 1


class TestDrillTargetLifecycle:
    """Post-registration governance: patch (injection), delete (recovery),
    cleanup chain, and convergence with the script channel."""

    @staticmethod
    def _registered_state():
        return {
            "task_id": "task-dt",
            "execution_artifacts": [{
                "artifact_id": "occupant_deployment:prod/drill-t",
                "type": "occupant_deployment",
                "status": "active",
                "task_id": "task-dt",
                "name": "drill-t",
                "namespace": "prod",
                "operation_family": "drill_target",
                "cleanup": {
                    "tool": "kubectl",
                    "subcommand": "delete",
                    "v_args": "deployment drill-t -n prod --ignore-not-found",
                },
            }],
        }

    @pytest.mark.asyncio
    async def test_patch_injection_passes_after_registration(self):
        """The fault-arming patch rides the ordinary approved-target path —
        the vehicle registration must not change injection verdicts."""
        settings.target_guard_enforcing = True
        state = self._registered_state()
        state["approved_target"] = _approved()
        state["messages"] = [AIMessage(
            content="",
            tool_calls=[{
                "name": "kubectl",
                "args": {
                    "subcommand": "patch",
                    "v_args": (
                        "deployment drill-t -n prod "
                        "-p {\"spec\":{\"template\":{\"spec\":"
                        "{\"volumes\":null}}}}"
                    ),
                },
                "id": "tc-patch",
            }],
        )]
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS

    @pytest.mark.asyncio
    async def test_delete_deployment_after_registration_passes(self):
        """Recovery delete: identity match alone would ALLOW it; the
        registration is the durable belt (kind-matched exemption)."""
        settings.target_guard_enforcing = True
        state = self._registered_state()
        state["approved_target"] = _approved()
        state["messages"] = [AIMessage(
            content="",
            tool_calls=[{
                "name": "kubectl",
                "args": {
                    "subcommand": "delete",
                    "v_args": "deployment drill-t -n prod --ignore-not-found",
                },
                "id": "tc-del",
            }],
        )]
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS

    def test_registration_converges_with_script_channel(self):
        """The manifest channel's artifact and the script channel's
        ``[drill-vehicle]`` registration must produce the SAME artifact_id —
        that is what makes the two channels dedup instead of double-register."""
        from chaos_agent.agent.execution_artifacts import _drill_vehicle_artifact

        script_side = _drill_vehicle_artifact(
            {"kind": "deployment", "name": "drill-t", "namespace": "prod"},
            task_id="task-dt",
            operation_family="drill_target",
            tool_call_id="tc-1",
        )
        assert script_side["artifact_id"] == "occupant_deployment:prod/drill-t"
        assert script_side["cleanup"]["v_args"] == (
            "deployment drill-t -n prod --ignore-not-found"
        )

    def test_registered_name_yields_deployment_kind_vehicle_types(self):
        """The L1186-1193 exemption keys on vehicle_artifact_types matching
        the deployment scope — pin the lookup for the registered target."""
        from chaos_agent.agent.execution_artifacts import vehicle_artifact_types

        types = vehicle_artifact_types("drill-t", self._registered_state())
        assert types == frozenset({"occupant_deployment"})


class TestDrillTargetChannelBoundary:
    """Channel faces verified by the 2026-09-11 code review (probe-then-fix:
    the facts were asserted against the pre-fix code first, the fixes landed
    second). Two invariants:

      - the STAGING channels are apply/create only — every other -f
        subcommand keeps the standing workload-kind mechanism ban;
      - registration follows EXECUTION, not just the ALLOW verdict — a
        human-approved drift card and log-only pass-through execute the
        create, so both register.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "sub", ["replace", "patch", "set", "delete", "edit"],
    )
    async def test_non_staging_subcommand_stays_banned(self, sub):
        """replace/patch/set/delete/edit -f - with a CONTRACT-COMPLIANT
        Deployment manifest for the APPROVED name must NOT enter the
        drill-target contract. kubectl replace 404s on an absent object — it
        can never stage a new target, so through the manifest channel it
        admits only re-shaping a PRE-EXISTING deployment, which would
        register it as task-owned and put a persistent workload on the
        cleanup (delete) chain. The other -f subs serve no staging purpose
        either; they keep the mechanism ban (probe P1: all five previously
        ALLOWed + registered)."""
        settings.target_guard_enforcing = True
        approved = _approved()
        delta = await tool_screener(_state(approved, _COMPLIANT, sub=sub))
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        msgs = delta.get("messages") or []
        assert len(msgs) == 1
        assert msgs[0].status == "error"
        # mechanism_banned coda — the standing workload-kind ban wording.
        assert "MECHANISM is banned" in msgs[0].content
        assert "non-whitelisted resource kind" in msgs[0].content
        artifacts = [
            a for a in delta.get("execution_artifacts") or []
            if a.get("type") == "occupant_deployment"
        ]
        assert not artifacts

    def test_non_staging_subcommand_classifier_ban(self):
        """Classifier-level anchor for the same gate: the contract flag is
        never set outside the apply/create staging channels."""
        eff = infer_effective_target(
            "kubectl", _apply_args(_COMPLIANT, sub="replace"),
        )
        assert eff.scope == SCOPE_BANNED
        assert eff.mechanism_banned is True
        assert eff.is_drill_target_manifest is False

    @pytest.mark.asyncio
    async def test_create_subcommand_enters_contract(self):
        """create -f - is the second staging channel — guard against
        over-narrowing the gate to apply alone."""
        settings.target_guard_enforcing = True
        delta = await tool_screener(
            _state(_approved(), _COMPLIANT, sub="create"),
        )
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        artifacts = [
            a for a in delta.get("execution_artifacts") or []
            if a.get("type") == "occupant_deployment"
        ]
        assert len(artifacts) == 1

    @pytest.mark.asyncio
    async def test_drift_approved_execution_registers(self):
        """A human-approved drift card executes the drifted apply — the
        Deployment it creates still lands on the cleanup chain. Registration
        follows EXECUTION (the contract's whole point is no orphaned target),
        not just the ALLOW verdict (probe P2: previously zero registration)."""
        settings.target_guard_enforcing = True
        approved = _approved()
        drifted = _COMPLIANT.replace("name: drill-t\n", "name: drill-t-2\n")
        with patch(
            "chaos_agent.agent.nodes.planning.tool_screener.interrupt",
            return_value="approved",
        ):
            delta = await tool_screener(_state(approved, drifted))
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        artifacts = [
            a for a in delta.get("execution_artifacts") or []
            if a.get("type") == "occupant_deployment"
        ]
        assert len(artifacts) == 1
        assert artifacts[0]["name"] == "drill-t-2"
        assert "deployment drill-t-2" in artifacts[0]["cleanup"]["v_args"]

    @pytest.mark.asyncio
    async def test_logonly_pass_through_registers_executed_create(self):
        """Log-only mode: a drifted drill-target apply passes through with
        NO human card and still executes — the registration still happens.
        Enforcement and cleanup-tracking are orthogonal: the guard is
        observational here, the orphan guarantee is not (probe P3/P4: the
        ALLOW path registered while the drifted one did not — the gap sat
        inside this feature's own surface)."""
        settings.target_guard_enforcing = False
        approved = _approved()
        drifted = _COMPLIANT.replace("name: drill-t\n", "name: drill-t-2\n")
        delta = await tool_screener(_state(approved, drifted))
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        artifacts = [
            a for a in delta.get("execution_artifacts") or []
            if a.get("type") == "occupant_deployment"
        ]
        assert len(artifacts) == 1
        assert artifacts[0]["name"] == "drill-t-2"

    # -- imperative channel (no -f): the R4 review's third face ----------
    #
    # ``kubectl create deployment NAME --image=...`` succeeds iff the
    # object is ABSENT — exactly the staging scenario the manifest channel
    # legislates for — yet it carried no manifest for T1-T5 to inspect and
    # registered nothing on the cleanup chain (probe: previously ALLOWed
    # for the SAME approved identity, image outside the carrier allow-set
    # and all). The fix is a mechanism ban at the classifier plus an
    # every-phase ToolGuard backstop (tests/test_tools/test_guard.py).

    @pytest.mark.parametrize(
        "v_args",
        [
            "deployment drill-t --image=nginx -n prod",
            "job drill-j --image=nginx -n prod",
            "cronjob drill-c --image=nginx -n prod",
            "deployment/drill-t --image=nginx -n prod",
        ],
    )
    def test_imperative_workload_create_banned_at_classifier(self, v_args):
        """Imperative create of ANY workload kind is banned — the form has
        no compliant shape because the only inspectable staging form is the
        manifest contract (probe: same-identity deployment previously
        ALLOWed with an off-allowlist image)."""
        eff = infer_effective_target(
            "kubectl", {"subcommand": "create", "v_args": v_args},
        )
        assert eff.scope == SCOPE_BANNED
        assert eff.mechanism_banned is True
        assert eff.is_drill_target_manifest is False

    @pytest.mark.parametrize(
        "v_args,scope",
        [
            ("namespace drill-ns", "namespace"),
            ("secret drill-sec --from-literal=a=b -n prod", "secret"),
            ("quota drill-q --hard=cpu=1 -n prod", "resourcequota"),
        ],
    )
    def test_imperative_non_workload_create_stays_classified(
        self, v_args, scope,
    ):
        """Guard against over-banning: the imperative kinds that carry no
        containers (the manifest whitelist's siblings) keep their generic
        classification — the admission-control quota drill depends on it."""
        eff = infer_effective_target(
            "kubectl", {"subcommand": "create", "v_args": v_args},
        )
        assert eff.scope == scope
        assert eff.mechanism_banned is False

    @pytest.mark.asyncio
    async def test_imperative_create_rejected_and_unregistered(self):
        """End-to-end closure of the R4 probe scenario: the approved
        drill-t, an imperative create naming the SAME identity — now RETRY
        with the mechanism-ban coda and ZERO cleanup registration."""
        settings.target_guard_enforcing = True
        approved = _approved()
        state = {
            "task_id": "task-dt",
            "messages": [AIMessage(content="", tool_calls=[{
                "name": "kubectl",
                "args": {
                    "subcommand": "create",
                    "v_args": "deployment drill-t --image=nginx -n prod",
                },
                "id": "tc-imp-1",
            }])],
            "approved_target": approved,
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        msgs = delta.get("messages") or []
        assert len(msgs) == 1
        assert msgs[0].status == "error"
        # mechanism_banned coda + the imperative-specific cause.
        assert "MECHANISM is banned" in msgs[0].content
        assert "imperative 'kubectl create deployment'" in msgs[0].content
        artifacts = [
            a for a in delta.get("execution_artifacts") or []
            if a.get("type") == "occupant_deployment"
        ]
        assert not artifacts


class TestStdinManifestWideningFlags:
    """Round-3 cascade finding (probe P5/P6/P7): the stdin manifest
    channels consumed v_args for the namespace ONLY — a range-widening
    flag (--prune / --all / -A / …) rode through EVERY manifest channel
    (drill-target contract, occupant pod, generic kind whitelist), and
    apply --prune's deletion face bypassed every identity anchor. The
    gate sits at the SHARED manifest entry so one check covers all three
    channels; it is a FORM issue (drop the flag, re-send the same
    manifest), with explicit ``=false`` off-forms passing as inert."""

    _OCCUPANT = """\
apiVersion: v1
kind: Pod
metadata:
  name: occ-t
  namespace: prod
spec:
  activeDeadlineSeconds: 600
  containers:
  - name: occ
    image: registry.example.com/probe:v1
    command: ["sleep", "600"]
  volumes:
  - name: data
    persistentVolumeClaim:
      claimName: data-pvc
"""

    _CONFIGMAP = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: cm-t
  namespace: prod
data:
  k: v
"""

    @pytest.mark.parametrize(
        "flag",
        [
            "--prune",
            "--prune=true",
            # ``--prune-allowlist -f -`` would be a DIFFERENT command:
            # pflag absorbs the next token as the allowlist value, so
            # kubectl itself fails with ``Unexpected args: [-]`` (probe-
            # verified) — the guard's UNKNOWN "no '-f -' flag" verdict
            # for that shape is the accurate one. Only value-carried
            # forms are widening-flag form issues here.
            "--prune-allowlist core/v1/ConfigMap",
            "--prune-allowlist=core/v1/ConfigMap",
            "--all",
            "--all=true",
            "-A",
            "--all-namespaces",
            "--all-namespaces=true",
        ],
    )
    def test_widening_flag_is_form_issue(self, flag):
        """Every widening form is a reshapeable form issue, never a
        mechanism ban — the compliant form exists (same manifest, no
        flag)."""
        eff = infer_effective_target("kubectl", {
            "subcommand": "apply",
            "v_args": f"{flag} -f -",
            "stdin_data": _COMPLIANT,
        })
        assert eff.scope == SCOPE_BANNED
        assert eff.mechanism_banned is False
        # The detail names the FLAG (separated values are absorbed by
        # pflag and are not part of the origin token).
        flag_name = flag.split()[0].split("=", 1)[0]
        assert flag_name in (eff.reject_detail or "")
        assert "WITHOUT the flag" in (eff.reject_suggestion or "")

    @pytest.mark.parametrize(
        "manifest",
        [_COMPLIANT, _OCCUPANT, _CONFIGMAP],
        ids=["drill-target", "occupant-pod", "configmap-whitelist"],
    )
    def test_all_three_channels_covered(self, manifest):
        """One gate, three channels — the shared stdin-manifest entry is
        the whole point of the fix (probe P7: all three previously passed
        --prune through)."""
        eff = infer_effective_target("kubectl", {
            "subcommand": "apply",
            "v_args": "--prune -l app=real -f -",
            "stdin_data": manifest,
        })
        assert eff.scope == SCOPE_BANNED
        assert eff.mechanism_banned is False
        assert eff.is_drill_target_manifest is False

    @pytest.mark.parametrize(
        "off", ["--prune=false", "--all=false", "--all-namespaces=false"],
    )
    def test_explicit_off_forms_pass(self, off):
        """An explicit ``=false`` off-form is inert — the call's effect
        stays inside the manifest text, so it classifies normally."""
        eff = infer_effective_target("kubectl", {
            "subcommand": "apply",
            "v_args": f"{off} -f -",
            "stdin_data": _COMPLIANT,
        })
        assert eff.is_drill_target_manifest is True
        assert eff.mechanism_banned is False

    @pytest.mark.parametrize(
        "shorthand", ["-A", "-An", "-Anw", "-Aw"],
    )
    def test_combined_shorthand_still_carries_the_widening(self, shorthand):
        """pflag bundles shorthand flags (``-An prod`` = ``-A -n prod``);
        ``-A`` is the only kubectl shorthand spelled with a capital A, so
        any single-dash token containing it carries the all-namespaces
        widening (probe: ``apply -An prod -f -`` passed the literal ``-A``
        check untouched). The plain ``-n`` sibling stays inert."""
        eff = infer_effective_target("kubectl", {
            "subcommand": "apply",
            "v_args": f"{shorthand} prod -f -" if shorthand != "-A" else "-A -f -",
            "stdin_data": _COMPLIANT,
        })
        assert eff.scope == SCOPE_BANNED
        assert eff.mechanism_banned is False
        assert shorthand in (eff.reject_detail or "")

    def test_plain_namespace_shorthand_is_not_widening(self):
        # ``-n`` bundles no capital A: the sibling form classifies normally.
        eff = infer_effective_target("kubectl", {
            "subcommand": "apply",
            "v_args": "-n prod -f -",
            "stdin_data": _COMPLIANT,
        })
        assert eff.is_drill_target_manifest is True

    @pytest.mark.parametrize(
        "sub", ["apply", "create", "replace", "delete", "patch"],
    )
    def test_every_dash_f_subcommand_hits_the_shared_gate(self, sub):
        """One gate, every manifest consumer: replace/delete/patch -f
        ride the same shared entry, so the widening refusal precedes even
        their own kinds check."""
        eff = infer_effective_target("kubectl", {
            "subcommand": sub,
            "v_args": "--prune -f -",
            "stdin_data": self._CONFIGMAP,
        })
        assert eff.scope == SCOPE_BANNED
        assert eff.mechanism_banned is False

    @pytest.mark.parametrize(
        "bundle", ["-wAn", "-RAn", "-Anw"],
    )
    def test_boolean_then_a_bundle_is_widening(self, bundle):
        """Round-4 walk semantics: an ``A`` preceded only by BOOLEAN
        shorthands (w/R — probe-verified pflag bundles on ``apply -RAn``)
        sits at a shorthand position and carries the widening."""
        eff = infer_effective_target("kubectl", {
            "subcommand": "apply",
            "v_args": f"{bundle} prod -f -",
            "stdin_data": _COMPLIANT,
        })
        assert eff.scope == SCOPE_BANNED
        assert eff.mechanism_banned is False

    @pytest.mark.parametrize(
        "inert",
        ["-nApp", "-lapp=App", "-oApp", "-nProd", "-inA"],
    )
    def test_value_absorbed_capital_a_is_not_widening(self, inert):
        """Round-4 false-positive closure: a capital A INSIDE a value
        (``-nApp`` = namespace "App", ``-lapp=App`` = selector — kubectl
        probe-verified: ``get pods -nApp`` parses the namespace as
        "App") is inert. The interim substring fix rejected all of
        these while letting ``-nProd`` through — same shape, opposite
        verdicts."""
        eff = infer_effective_target("kubectl", {
            "subcommand": "apply",
            "v_args": f"{inert} -f -" if not inert.startswith("-in") else f"{inert} prod -f -",
            "stdin_data": _COMPLIANT,
        })
        assert eff.is_drill_target_manifest is True
        assert eff.mechanism_banned is False

    def test_shorthand_explicit_off_form_passes(self):
        """pflag supports ``-A=false`` (probe-verified on ``get``) — the
        explicit off-form is inert, same rule as the long flags."""
        eff = infer_effective_target("kubectl", {
            "subcommand": "apply",
            "v_args": "-A=false -n prod -f -",
            "stdin_data": _COMPLIANT,
        })
        assert eff.is_drill_target_manifest is True
        assert eff.mechanism_banned is False

    @pytest.mark.asyncio
    async def test_prune_rejected_end_to_end(self):
        """End-to-end closure of probe P6: the approved, name-matched
        apply carrying --prune — RETRY, ZERO registration, form-issue
        wording (previously PASS + registered with the deletion face
        riding through)."""
        settings.target_guard_enforcing = True
        approved = _approved()
        delta = await tool_screener(_state(
            approved, _COMPLIANT, v_args="--prune -l app=real -f -",
        ))
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        msgs = delta.get("messages") or []
        assert len(msgs) == 1
        assert msgs[0].status == "error"
        assert "beyond the manifest" in msgs[0].content
        artifacts = [
            a for a in delta.get("execution_artifacts") or []
            if a.get("type") == "occupant_deployment"
        ]
        assert not artifacts
