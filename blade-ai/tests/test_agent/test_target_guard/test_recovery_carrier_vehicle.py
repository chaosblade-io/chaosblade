"""End-to-end tests for the recovery-carrier vehicle channel (recovery-carrier-standard).

The channel, in order:
  1. the SCREENER registers the shape-compliant ``kubectl run`` pod as a
     ``recovery_carrier`` vehicle artifact (task-side, zero cluster marker)
     when the ordinary net (in-net pod secondary scope + same ns) allows it;
  2. later execs into the registered carrier (token probe / timer arm /
     re-arm) ride the vehicle exemption — no identity drift;
  3. a successful arming exec flips the artifact ``recovery_armed`` with a
     deadline (``_mark_bounded_host_recovery``), keeping finalize's cleanup
     from deleting the carrier while its timer still counts;
  4. successful RBAC creates attach to the artifact's ``rbac_family`` and
     the stack cleanup deletes pod + binding + role + sa exactly once.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.agent.execution_artifacts import (
    cleanup_debug_pod_artifacts,
    collect_execution_artifacts,
    is_vehicle_name,
    vehicle_artifact_types,
)
from chaos_agent.agent.nodes.planning.tool_screener import (
    SCREENER_ROUTE_PASS,
    SCREENER_ROUTE_RETRY,
    tool_screener,
)
from chaos_agent.agent.target_guard import freeze_approved_target
from chaos_agent.config.settings import settings

_DISCOVERY = (
    "chaos_agent.tools.pod_discovery"
    ".discover_tool_pods_cluster_wide"
)

_DELETE_POD = (
    "chaos_agent.agent.nodes.execute._debug_pod.delete_debug_pod"
)


def _approved_workload():
    """A deployment-primary approval whose net includes pod + RBAC kinds."""
    return freeze_approved_target(
        target={"namespace": "prod", "names": ["drill-pvc-target"]},
        params={"scope": "deployment"},
        fault_scope="deployment", fault_target="disk", fault_action="fill",
    )


_RUN_ARGS = {
    "subcommand": "run",
    "v_args": (
        "drill-rc-a1b2c3 -n prod --image=busybox:1.36 --restart=Never "
        "--command -- sleep 7200"
    ),
}


@pytest.fixture(autouse=True)
def _carrier_settings():
    orig = (
        settings.target_guard_enforcing,
        settings.recovery_carrier_name_prefix,
        settings.recovery_carrier_allowed_images,
        settings.recovery_carrier_max_sleep_seconds,
    )
    settings.target_guard_enforcing = True
    settings.recovery_carrier_name_prefix = "drill-rc-"
    settings.recovery_carrier_allowed_images = "busybox:1.36,busybox:latest"
    settings.recovery_carrier_max_sleep_seconds = 86400
    yield
    (
        settings.target_guard_enforcing,
        settings.recovery_carrier_name_prefix,
        settings.recovery_carrier_allowed_images,
        settings.recovery_carrier_max_sleep_seconds,
    ) = orig


class _CarrierStateMixin:
    @staticmethod
    def _carrier_artifact(**extra) -> dict:
        artifact = {
            "artifact_id": "recovery_carrier:prod/drill-rc-a1b2c3",
            "type": "recovery_carrier",
            "status": "active",
            "task_id": "task-rc-1",
            "name": "drill-rc-a1b2c3",
            "namespace": "prod",
            "operation_family": "recovery_carrier",
            "created_tool_call_id": "tc-run",
            "rbac_family": [],
        }
        artifact.update(extra)
        return artifact

    @staticmethod
    def _state_for_exec(v_args: str, artifacts: list[dict]) -> dict:
        return {
            "task_id": "task-rc-1",
            "messages": [AIMessage(
                content="",
                tool_calls=[{
                    "name": "kubectl",
                    "args": {"subcommand": "exec", "v_args": v_args},
                    "id": "tc-exec",
                }],
            )],
            "approved_target": _approved_workload(),
            "execution_artifacts": artifacts,
        }


class TestScreenerRegistersCarrier(_CarrierStateMixin):
    """A compliant in-net run registers the vehicle artifact."""

    @pytest.mark.asyncio
    async def test_in_net_run_registers_artifact(self):
        state = {
            "task_id": "task-rc-1",
            "messages": [AIMessage(
                content="",
                tool_calls=[{
                    "name": "kubectl", "args": _RUN_ARGS, "id": "tc-run",
                }],
            )],
            "approved_target": _approved_workload(),
        }
        with (
            patch(
                "chaos_agent.agent.nodes.planning.tool_screener.interrupt",
            ) as mock_interrupt,
            patch(_DISCOVERY, new_callable=AsyncMock) as mock_discover,
        ):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        mock_interrupt.assert_not_called()
        # The registration pre-empts the tool-pod probe: the carrier is
        # task-side fact, not a cluster discovery question.
        mock_discover.assert_not_called()
        carriers = [
            a for a in delta.get("execution_artifacts") or []
            if a.get("type") == "recovery_carrier"
        ]
        assert len(carriers) == 1
        carrier = carriers[0]
        assert carrier["name"] == "drill-rc-a1b2c3"
        assert carrier["namespace"] == "prod"
        assert carrier["rbac_family"] == []
        # The four-way stack delete, recorded for the audit trail.
        assert [c["v_args"].split()[0] for c in carrier["cleanup"]] == [
            "pod", "rolebinding", "role", "serviceaccount",
        ]

    @pytest.mark.asyncio
    async def test_repeat_run_does_not_clobber_registered_state(self):
        # A repeat ALLOW for the same carrier (timeout re-issue /
        # idempotency probe) keeps the registered artifact intact —
        # rbac_family and the armed deadline are durable facts, and
        # resetting them would let finalize's cleanup delete a live timer.
        armed = self._carrier_artifact(
            status="recovery_armed",
            recovery_deadline_epoch=time.time() + 600,
            recovery_timeout_seconds=600,
            rbac_family=[
                {"kind": "serviceaccount", "name": "drill-rc-a1b2c3", "namespace": "prod"},
                {"kind": "role", "name": "drill-rc-a1b2c3", "namespace": "prod"},
                {"kind": "rolebinding", "name": "drill-rc-a1b2c3", "namespace": "prod"},
            ],
        )
        state = {
            "task_id": "task-rc-1",
            "messages": [AIMessage(
                content="",
                tool_calls=[{
                    "name": "kubectl", "args": _RUN_ARGS, "id": "tc-run-again",
                }],
            )],
            "approved_target": _approved_workload(),
            "execution_artifacts": [armed],
        }
        with (
            patch(
                "chaos_agent.agent.nodes.planning.tool_screener.interrupt",
            ) as mock_interrupt,
            patch(_DISCOVERY, new_callable=AsyncMock) as mock_discover,
        ):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        mock_interrupt.assert_not_called()
        mock_discover.assert_not_called()
        carriers = [
            a for a in delta.get("execution_artifacts") or []
            if a.get("type") == "recovery_carrier"
        ]
        assert len(carriers) == 1
        carrier = carriers[0]
        assert carrier["status"] == "recovery_armed"
        assert carrier["recovery_deadline_epoch"] == armed["recovery_deadline_epoch"]
        assert len(carrier["rbac_family"]) == 3

    @pytest.mark.asyncio
    async def test_out_of_namespace_run_keeps_drift_routing(self):
        # A shape-compliant run OUTSIDE the net (wrong namespace) keeps the
        # standard drift verdict — the marker exempts nothing by itself.
        run_args = {
            "subcommand": "run",
            "v_args": (
                "-n other-ns drill-rc-a1b2c3 --image=busybox:1.36 "
                "--restart=Never --command -- sleep 7200"
            ),
        }
        state = {
            "task_id": "task-rc-2",
            "messages": [AIMessage(
                content="",
                tool_calls=[{
                    "name": "kubectl", "args": run_args, "id": "tc-run2",
                }],
            )],
            "approved_target": _approved_workload(),
        }
        with (
            patch(
                "chaos_agent.agent.nodes.planning.tool_screener.interrupt",
                return_value="rejected",
            ) as mock_interrupt,
            patch(
                _DISCOVERY, new_callable=AsyncMock, return_value=[],
            ) as mock_discover,
        ):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        mock_interrupt.assert_called_once()
        mock_discover.assert_awaited_once()
        # Nothing registered while the run is refused.
        assert not any(
            a.get("type") == "recovery_carrier"
            for a in delta.get("execution_artifacts") or []
        )


class TestRegisteredCarrierExecExemption(_CarrierStateMixin):
    """Execs into the registered carrier are machinery access."""

    @pytest.mark.asyncio
    async def test_arming_exec_is_exempt_without_probe(self):
        arm_cmd = (
            "( sleep 300; curl -sk -X PATCH "
            "https://kubernetes.default.svc/apis/apps/v1/namespaces/prod/"
            "deployments/drill-pvc-target ) >/dev/null 2>&1 & echo armed"
        )
        state = self._state_for_exec(
            f"drill-rc-a1b2c3 -n prod -- sh -c '{arm_cmd}'",
            [self._carrier_artifact()],
        )
        with (
            patch(
                "chaos_agent.agent.nodes.planning.tool_screener.interrupt",
            ) as mock_interrupt,
            patch(_DISCOVERY, new_callable=AsyncMock) as mock_discover,
        ):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        mock_interrupt.assert_not_called()
        mock_discover.assert_not_called()

    @pytest.mark.asyncio
    async def test_token_probe_exec_is_exempt(self):
        state = self._state_for_exec(
            "drill-rc-a1b2c3 -n prod -- sh -c "
            "'TOKEN=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token); "
            "curl -sk -H \"Authorization: Bearer $TOKEN\" "
            "https://kubernetes.default.svc/api'",
            [self._carrier_artifact()],
        )
        with (
            patch(
                "chaos_agent.agent.nodes.planning.tool_screener.interrupt",
            ) as mock_interrupt,
            patch(_DISCOVERY, new_callable=AsyncMock) as mock_discover,
        ):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        mock_interrupt.assert_not_called()
        mock_discover.assert_not_called()

    @pytest.mark.asyncio
    async def test_unregistered_same_shape_pod_still_drifts(self):
        # Registration is the exemption's data source — a drill-rc-prefixed
        # name alone (no artifact) keeps full drift review. The net under a
        # deployment-primary approval still ns-anchors pod ops, so the
        # out-of-net shape to test is a foreign NAMESPACE.
        state = self._state_for_exec(
            "drill-rc-notreg -n other-ns -- sh -c 'touch /tmp/arm'",
            [],
        )
        with (
            patch(
                "chaos_agent.agent.nodes.planning.tool_screener.interrupt",
                return_value="rejected",
            ) as mock_interrupt,
            patch(
                _DISCOVERY, new_callable=AsyncMock, return_value=[],
            ) as mock_discover,
        ):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        mock_interrupt.assert_called_once()
        mock_discover.assert_awaited_once()

    def test_carrier_type_recognised_by_name_helpers(self):
        state = {
            "execution_artifacts": [self._carrier_artifact()],
        }
        assert is_vehicle_name("drill-rc-a1b2c3", state) is True
        assert "recovery_carrier" in vehicle_artifact_types(
            "drill-rc-a1b2c3", state,
        )
        # A pod-type carrier never exempts a deployment-domain operation.
        assert "occupant_deployment" not in vehicle_artifact_types(
            "drill-rc-a1b2c3", state,
        )


class TestArmingMarksAndRbacAttach(_CarrierStateMixin):
    """collect_execution_artifacts: arming + RBAC family bookkeeping."""

    @staticmethod
    def _messages(calls: list[tuple[str, dict, str]]) -> list:
        messages: list = []
        for name, args, call_id in calls:
            messages.append(AIMessage(
                content="", tool_calls=[{
                    "name": name, "args": args, "id": call_id,
                }],
            ))
            messages.append(ToolMessage(content="ok", tool_call_id=call_id))
        return messages

    def _existing(self) -> list[dict]:
        return [self._carrier_artifact()]

    def test_arming_exec_marks_recovery_armed_with_deadline(self):
        v_args = (
            "drill-rc-a1b2c3 -n prod -- sh -c "
            "'( sleep 300; curl -sk -X PATCH https://x ) >/dev/null 2>&1 "
            "& echo armed'"
        )
        messages = self._messages([
            ("kubectl", {"subcommand": "exec", "v_args": v_args}, "tc-arm"),
        ])
        artifacts = collect_execution_artifacts(
            messages, self._existing(), task_id="task-rc-1",
        )
        carrier = artifacts[0]
        assert carrier["status"] == "recovery_armed"
        assert carrier["recovery_timeout_seconds"] == 300
        assert carrier["recovery_deadline_epoch"] > time.time()

    def test_rearm_takes_max_sleep_over_pkill_literal(self):
        # The pkill pattern embeds the OLD timer's ``sleep 30[0]`` literal;
        # arming must read the NEW timer (600), not the stale literal (30).
        v_args = (
            "drill-rc-a1b2c3 -n prod -- sh -c "
            "\"pkill -f 'sleep 30[0]'; "
            "( sleep 600; curl -sk -X PATCH https://x ) >/dev/null 2>&1 "
            "& echo armed\""
        )
        messages = self._messages([
            ("kubectl", {"subcommand": "exec", "v_args": v_args}, "tc-rarm"),
        ])
        artifacts = collect_execution_artifacts(
            messages, self._existing(), task_id="task-rc-1",
        )
        assert artifacts[0]["recovery_timeout_seconds"] == 600

    def test_probe_exec_does_not_shorten_armed_window(self):
        # Keep-while-armed is a LOWER bound: a verification probe riding the
        # carrier (REST probes wrap ``sleep 2 && curl ...``) must not eat
        # into an armed window — otherwise finalize's cleanup deletes a
        # timer host whose countdown is still running.
        armed = self._carrier_artifact(
            status="recovery_armed",
            recovery_deadline_epoch=time.time() + 600,
            recovery_timeout_seconds=600,
            host_exec_tool_call_id="tc-arm",
        )
        probe_v_args = (
            "drill-rc-a1b2c3 -n prod -- sh -c "
            "'sleep 2; curl -sk https://kubernetes.default.svc/api'"
        )
        messages = self._messages([
            ("kubectl", {"subcommand": "exec", "v_args": probe_v_args}, "tc-probe"),
        ])
        artifacts = collect_execution_artifacts(
            messages, [armed], task_id="task-rc-1",
        )
        carrier = artifacts[0]
        assert carrier["status"] == "recovery_armed"
        assert carrier["recovery_timeout_seconds"] == 600
        assert carrier["recovery_deadline_epoch"] == armed["recovery_deadline_epoch"]
        # The replay guard moved to the probe's call id.
        assert carrier["host_exec_tool_call_id"] == "tc-probe"

    def test_longer_rearm_extends_armed_window(self):
        # A LONGER re-arm still extends the window (pkill + full re-arm of
        # a wider fault window).
        armed = self._carrier_artifact(
            status="recovery_armed",
            recovery_deadline_epoch=time.time() + 60,
            recovery_timeout_seconds=60,
            host_exec_tool_call_id="tc-arm",
        )
        v_args = (
            "drill-rc-a1b2c3 -n prod -- sh -c "
            "'( sleep 600; curl -sk -X PATCH https://x ) >/dev/null 2>&1 "
            "& echo armed'"
        )
        messages = self._messages([
            ("kubectl", {"subcommand": "exec", "v_args": v_args}, "tc-rarm"),
        ])
        artifacts = collect_execution_artifacts(
            messages, [armed], task_id="task-rc-1",
        )
        carrier = artifacts[0]
        assert carrier["recovery_timeout_seconds"] == 600
        assert carrier["recovery_deadline_epoch"] > armed["recovery_deadline_epoch"]

    def test_rbac_creates_attach_to_carrier(self):
        messages = self._messages([
            (
                "kubectl",
                {"subcommand": "create", "v_args": "sa drill-rc-a1b2c3 -n prod"},
                "tc-sa",
            ),
            (
                "kubectl",
                {
                    "subcommand": "create",
                    "v_args": (
                        "role drill-rc-a1b2c3 -n prod "
                        "--verb=get,patch --resource=deployments"
                    ),
                },
                "tc-role",
            ),
            (
                "kubectl",
                {
                    "subcommand": "create",
                    "v_args": (
                        "rolebinding drill-rc-a1b2c3 -n prod "
                        "--role=drill-rc-a1b2c3 "
                        "--serviceaccount=prod:drill-rc-a1b2c3"
                    ),
                },
                "tc-binding",
            ),
        ])
        artifacts = collect_execution_artifacts(
            messages, self._existing(), task_id="task-rc-1",
        )
        carrier = artifacts[0]
        kinds = sorted(m["kind"] for m in carrier["rbac_family"])
        assert kinds == ["role", "rolebinding", "serviceaccount"]

    def test_rbac_attach_is_idempotent_across_replay(self):
        messages = self._messages([
            (
                "kubectl",
                {"subcommand": "create", "v_args": "sa drill-rc-a1b2c3 -n prod"},
                "tc-sa",
            ),
        ])
        once = collect_execution_artifacts(
            messages, self._existing(), task_id="task-rc-1",
        )
        twice = collect_execution_artifacts(
            messages, once, task_id="task-rc-1",
        )
        sa_entries = [
            m for m in twice[0]["rbac_family"]
            if m["kind"] == "serviceaccount"
        ]
        assert len(sa_entries) == 1

    def test_rbac_with_foreign_name_does_not_attach(self):
        messages = self._messages([
            (
                "kubectl",
                {"subcommand": "create", "v_args": "sa other-sa -n prod"},
                "tc-sa2",
            ),
        ])
        artifacts = collect_execution_artifacts(
            messages, self._existing(), task_id="task-rc-1",
        )
        assert artifacts[0]["rbac_family"] == []

    def test_cross_ns_role_and_binding_attach_with_recovery_ns(self):
        # Cross-ns variant (kube-system recovery targets, e.g. the NXDOMAIN
        # case): the SA is the carrier's own identity and stays in the
        # carrier ns, while the Role/RoleBinding land in the RECOVERY ns so
        # they can grant verbs on cross-ns targets. All three attach to the
        # carrier by name, each member recording the ns its create command
        # actually targeted — cleanup later deletes each where it lives.
        messages = self._messages([
            (
                "kubectl",
                {"subcommand": "create", "v_args": "sa drill-rc-a1b2c3 -n prod"},
                "tc-sa",
            ),
            (
                "kubectl",
                {
                    "subcommand": "create",
                    "v_args": (
                        "role drill-rc-a1b2c3 -n kube-system "
                        "--verb=get,patch --resource=configmaps,deployments"
                    ),
                },
                "tc-role",
            ),
            (
                "kubectl",
                {
                    "subcommand": "create",
                    "v_args": (
                        "rolebinding drill-rc-a1b2c3 -n kube-system "
                        "--role=drill-rc-a1b2c3 "
                        "--serviceaccount=prod:drill-rc-a1b2c3"
                    ),
                },
                "tc-binding",
            ),
        ])
        artifacts = collect_execution_artifacts(
            messages, self._existing(), task_id="task-rc-1",
        )
        members = {
            (m["kind"], m["namespace"])
            for m in artifacts[0]["rbac_family"]
        }
        assert members == {
            ("serviceaccount", "prod"),
            ("role", "kube-system"),
            ("rolebinding", "kube-system"),
        }

    def test_cross_ns_attach_rewrites_cleanup_audit_ns(self):
        # B26 (run7 finding): the cleanup audit trail is pre-rendered at
        # registration with the CARRIER ns on every stack member. A cross-ns
        # member (Role/RoleBinding in the recovery ns) must have its entry
        # rewritten to where the object actually lives — an out-of-band
        # operator replaying the cleanup list would otherwise hit
        # --ignore-not-found and silently orphan the kube-system RBAC.
        carrier = self._carrier_artifact(cleanup=[
            {"tool": "kubectl", "subcommand": "delete",
             "v_args": "pod drill-rc-a1b2c3 -n prod --ignore-not-found"},
            {"tool": "kubectl", "subcommand": "delete",
             "v_args": "rolebinding drill-rc-a1b2c3 -n prod --ignore-not-found"},
            {"tool": "kubectl", "subcommand": "delete",
             "v_args": "role drill-rc-a1b2c3 -n prod --ignore-not-found"},
            {"tool": "kubectl", "subcommand": "delete",
             "v_args": "serviceaccount drill-rc-a1b2c3 -n prod --ignore-not-found"},
        ])
        messages = self._messages([
            (
                "kubectl",
                {"subcommand": "create", "v_args": "sa drill-rc-a1b2c3 -n prod"},
                "tc-sa",
            ),
            (
                "kubectl",
                {
                    "subcommand": "create",
                    "v_args": (
                        "role drill-rc-a1b2c3 -n kube-system "
                        "--verb=get,patch --resource=configmaps"
                    ),
                },
                "tc-role",
            ),
        ])
        artifacts = collect_execution_artifacts(
            messages, [carrier], task_id="task-rc-1",
        )
        cleanup_ns = {
            entry["v_args"].split()[0]: entry["v_args"]
            for entry in artifacts[0]["cleanup"]
        }
        # Cross-ns member rewritten to where it lives...
        assert cleanup_ns["role"] == (
            "role drill-rc-a1b2c3 -n kube-system --ignore-not-found"
        )
        # ...same-ns members (SA) and the pod itself keep the carrier ns.
        assert cleanup_ns["serviceaccount"] == (
            "serviceaccount drill-rc-a1b2c3 -n prod --ignore-not-found"
        )
        assert cleanup_ns["pod"] == (
            "pod drill-rc-a1b2c3 -n prod --ignore-not-found"
        )

    def test_cross_ns_sa_does_not_attach(self):
        # The SA is the carrier's identity — it only counts when created in
        # the carrier's own ns. A same-named SA created in a foreign ns is
        # someone else's object and must not join this carrier's family.
        messages = self._messages([
            (
                "kubectl",
                {
                    "subcommand": "create",
                    "v_args": "sa drill-rc-a1b2c3 -n kube-system",
                },
                "tc-sa",
            ),
        ])
        artifacts = collect_execution_artifacts(
            messages, self._existing(), task_id="task-rc-1",
        )
        assert artifacts[0]["rbac_family"] == []

    def test_flagless_create_defaults_member_to_carrier_ns(self):
        # A create with no ``-n`` inherits the kubectl default ns; members
        # recorded without an explicit ns keep the carrier ns, matching the
        # same-ns stack the standard prescribes.
        messages = self._messages([
            (
                "kubectl",
                {"subcommand": "create", "v_args": "role drill-rc-a1b2c3"},
                "tc-role",
            ),
        ])
        artifacts = collect_execution_artifacts(
            messages, self._existing(), task_id="task-rc-1",
        )
        members = artifacts[0]["rbac_family"]
        assert [
            (m["kind"], m["namespace"]) for m in members
        ] == [("role", "prod")]

    def test_cluster_scoped_rbac_attaches_to_five_object_carrier(self):
        # B28 (five-object carrier variant): a workload drill whose recovery
        # writes touch cluster-scoped resources (node taint/label restore)
        # extends the stack with ClusterRole + ClusterRoleBinding. Both are
        # cluster-scoped — no ``-n`` on their create commands — and must
        # attach by name like their namespaced siblings so the stack cleanup
        # deletes them instead of orphaning a global-scope RBAC grant.
        messages = self._messages([
            (
                "kubectl",
                {"subcommand": "create", "v_args": "sa drill-rc-a1b2c3 -n prod"},
                "tc-sa",
            ),
            (
                "kubectl",
                {"subcommand": "create", "v_args": "role drill-rc-a1b2c3 -n prod"},
                "tc-role",
            ),
            (
                "kubectl",
                {"subcommand": "create", "v_args": "rolebinding drill-rc-a1b2c3 -n prod"},
                "tc-binding",
            ),
            (
                "kubectl",
                {
                    "subcommand": "create",
                    "v_args": (
                        "clusterrole drill-rc-a1b2c3 "
                        "--resource=nodes --verb=get,patch"
                    ),
                },
                "tc-clusterrole",
            ),
            (
                "kubectl",
                {
                    "subcommand": "create",
                    "v_args": (
                        "clusterrolebinding drill-rc-a1b2c3 "
                        "--clusterrole=drill-rc-a1b2c3 "
                        "--serviceaccount=prod:drill-rc-a1b2c3"
                    ),
                },
                "tc-clusterbinding",
            ),
        ])
        artifacts = collect_execution_artifacts(
            messages, self._existing(), task_id="task-rc-1",
        )
        members = {
            (m["kind"], m["namespace"]) for m in artifacts[0]["rbac_family"]
        }
        assert members == {
            ("serviceaccount", "prod"),
            ("role", "prod"),
            ("rolebinding", "prod"),
            # Cluster-scoped members record their TRUE topology — no
            # namespace at all (R21/G-5) — so the registration agrees with
            # the member's own cleanup audit entry, which carries no ``-n``
            # (kubectl ignores ``-n`` on cluster-scoped kinds because the
            # object has none; the pre-R21 carrier-ns fallback contradicted
            # that entry and the exemption rejected its replay).
            ("clusterrole", ""),
            ("clusterrolebinding", ""),
        }

    def test_cluster_scoped_attach_appends_audit_cleanup_entry(self):
        # The registration-time audit trail pre-renders only the same-ns
        # four-way stack; a cluster-scoped member appended later must add
        # its OWN audit entry (no ``-n``) so an out-of-band operator
        # replaying the cleanup list also deletes the cluster-level
        # objects.
        carrier = self._carrier_artifact()
        messages = self._messages([
            (
                "kubectl",
                {
                    "subcommand": "create",
                    "v_args": (
                        "clusterrole drill-rc-a1b2c3 "
                        "--resource=nodes --verb=get,patch"
                    ),
                },
                "tc-clusterrole",
            ),
        ])
        artifacts = collect_execution_artifacts(
            messages, [carrier], task_id="task-rc-1",
        )
        cluster_entries = [
            entry for entry in artifacts[0].get("cleanup") or []
            if entry.get("subcommand") == "delete"
            and str(entry.get("v_args") or "").startswith("clusterrole ")
        ]
        assert cluster_entries == [{
            "tool": "kubectl",
            "subcommand": "delete",
            "v_args": "clusterrole drill-rc-a1b2c3 --ignore-not-found",
        }]

    def test_cluster_scoped_attach_is_idempotent_across_replay(self):
        # Message replay rescans the full history: a cluster-role create
        # seen twice must append its family member and audit entry once.
        messages = self._messages([
            (
                "kubectl",
                {
                    "subcommand": "create",
                    "v_args": (
                        "clusterrolebinding drill-rc-a1b2c3 "
                        "--clusterrole=drill-rc-a1b2c3 "
                        "--serviceaccount=prod:drill-rc-a1b2c3"
                    ),
                },
                "tc-clusterbinding",
            ),
        ])
        once = collect_execution_artifacts(
            messages, self._existing(), task_id="task-rc-1",
        )
        twice = collect_execution_artifacts(
            messages, once, task_id="task-rc-1",
        )
        members = [
            m for m in twice[0]["rbac_family"]
            if m["kind"] == "clusterrolebinding"
        ]
        assert len(members) == 1
        entries = [
            entry for entry in twice[0].get("cleanup") or []
            if str(entry.get("v_args") or "").startswith("clusterrolebinding ")
        ]
        assert len(entries) == 1

    def test_replay_does_not_push_deadline_forward(self):
        # collect rescans the FULL history every round; the arming message
        # must fire exactly once (seen-ids set), or every replay pushes the
        # deadline to now+window and the armed gate never expires — the
        # finalize/cancel cleanup would hold the carrier (and its RBAC
        # family) forever with no later round to consume it.
        arm_cmd = (
            "( sleep 600; curl -sk -X PATCH https://x ) >/dev/null 2>&1 "
            "& echo armed"
        )
        probe_cmd = "sleep 2; curl -sk https://kubernetes.default.svc/api"
        messages = self._messages([
            (
                "kubectl",
                {"subcommand": "exec", "v_args": (
                    f"drill-rc-a1b2c3 -n prod -- sh -c '{arm_cmd}'"
                )},
                "tc-arm",
            ),
            (
                "kubectl",
                {"subcommand": "exec", "v_args": (
                    f"drill-rc-a1b2c3 -n prod -- sh -c '{probe_cmd}'"
                )},
                "tc-probe",
            ),
        ])
        first = collect_execution_artifacts(
            messages, self._existing(), task_id="task-rc-1",
        )
        deadline = first[0]["recovery_deadline_epoch"]
        time.sleep(0.05)
        second = collect_execution_artifacts(
            messages, first, task_id="task-rc-1",
        )
        time.sleep(0.05)
        third = collect_execution_artifacts(
            messages, second, task_id="task-rc-1",
        )
        assert second[0]["recovery_deadline_epoch"] == deadline
        assert third[0]["recovery_deadline_epoch"] == deadline

    def test_replay_does_not_revive_cleaned_carrier(self):
        # Once cleanup has swept the stack, replayed history must not flip
        # the artifact back to armed (the pod is gone; the arming exec is
        # history).
        cleaned = self._carrier_artifact(
            status="cleaned",
            recovery_deadline_epoch=time.time() + 600,
            host_exec_tool_call_id="tc-old",
        )
        arm_cmd = (
            "( sleep 600; curl -sk -X PATCH https://x ) >/dev/null 2>&1 "
            "& echo armed"
        )
        messages = self._messages([
            (
                "kubectl",
                {"subcommand": "exec", "v_args": (
                    f"drill-rc-a1b2c3 -n prod -- sh -c '{arm_cmd}'"
                )},
                "tc-arm",
            ),
        ])
        artifacts = collect_execution_artifacts(
            messages, [cleaned], task_id="task-rc-1",
        )
        assert artifacts[0]["status"] == "cleaned"

    def test_manual_pod_delete_disarms_armed_carrier(self):
        # The LLM's manual four-way delete (standard §6) partially executed
        # (pod deleted, RBAC deletes not yet run): the confirmed pod delete
        # disarms the artifact so the next cleanup round sweeps the RBAC
        # family despite the old deadline — otherwise the family has no
        # system-side sweeper until the now-meaningless deadline passes.
        armed = self._carrier_artifact(
            status="recovery_armed",
            recovery_deadline_epoch=time.time() + 600,
            recovery_timeout_seconds=600,
        )
        messages = self._messages([
            (
                "kubectl",
                {
                    "subcommand": "delete",
                    "v_args": (
                        "pod drill-rc-a1b2c3 -n prod --ignore-not-found"
                    ),
                },
                "tc-del",
            ),
        ])
        artifacts = collect_execution_artifacts(
            messages, [armed], task_id="task-rc-1",
        )
        assert artifacts[0]["status"] == "active"
        assert "recovery_deadline_epoch" not in artifacts[0]

    def test_delete_replay_does_not_revive_cleaned_carrier(self):
        cleaned = self._carrier_artifact(status="cleaned")
        messages = self._messages([
            (
                "kubectl",
                {
                    "subcommand": "delete",
                    "v_args": (
                        "pod drill-rc-a1b2c3 -n prod --ignore-not-found"
                    ),
                },
                "tc-del",
            ),
        ])
        artifacts = collect_execution_artifacts(
            messages, [cleaned], task_id="task-rc-1",
        )
        assert artifacts[0]["status"] == "cleaned"


class TestStackCleanup(_CarrierStateMixin):
    """cleanup_debug_pod_artifacts: keep-while-armed + four-way delete."""

    @pytest.mark.asyncio
    async def test_armed_carrier_survives_until_deadline(self):
        artifact = self._carrier_artifact(
            status="recovery_armed",
            recovery_deadline_epoch=time.time() + 600,
            recovery_timeout_seconds=600,
        )
        with patch(_DELETE_POD, new_callable=AsyncMock) as mock_delete:
            updated, cleaned = await cleanup_debug_pod_artifacts(
                [artifact], kubeconfig="kc", task_id="task-rc-1",
            )
        mock_delete.assert_not_called()
        assert cleaned == []
        assert updated[0]["status"] == "recovery_armed"

    @pytest.mark.asyncio
    async def test_expired_armed_carrier_is_stack_deleted(self):
        artifact = self._carrier_artifact(
            status="recovery_armed",
            recovery_deadline_epoch=time.time() - 1,
            recovery_timeout_seconds=300,
            rbac_family=[
                {"kind": "serviceaccount", "name": "drill-rc-a1b2c3", "namespace": "prod"},
                {"kind": "role", "name": "drill-rc-a1b2c3", "namespace": "prod"},
                {"kind": "rolebinding", "name": "drill-rc-a1b2c3", "namespace": "prod"},
            ],
        )
        with patch(
            _DELETE_POD, new_callable=AsyncMock, return_value="confirmed",
        ) as mock_delete:
            updated, cleaned = await cleanup_debug_pod_artifacts(
                [artifact], kubeconfig="kc", task_id="task-rc-1",
            )
        # Four deletes, each tried exactly once: pod + family, in the
        # binding → role → sa order.
        calls = [
            (c.args[0], c.kwargs.get("kind", "pod"))
            for c in mock_delete.await_args_list
        ]
        assert calls == [
            ("drill-rc-a1b2c3", "pod"),
            ("drill-rc-a1b2c3", "rolebinding"),
            ("drill-rc-a1b2c3", "role"),
            ("drill-rc-a1b2c3", "serviceaccount"),
        ]
        assert updated[0]["status"] == "cleaned"
        assert cleaned == ["drill-rc-a1b2c3"]

    @pytest.mark.asyncio
    async def test_cross_ns_family_deleted_where_it_lives(self):
        # Cross-ns variant: pod + SA in the carrier ns, Role/RoleBinding in
        # the recovery ns — each member is deleted in the ns recorded on it,
        # not blanket in the carrier ns.
        artifact = self._carrier_artifact(
            status="recovery_armed",
            recovery_deadline_epoch=time.time() - 1,
            recovery_timeout_seconds=300,
            rbac_family=[
                {"kind": "serviceaccount", "name": "drill-rc-a1b2c3", "namespace": "prod"},
                {"kind": "role", "name": "drill-rc-a1b2c3", "namespace": "kube-system"},
                {"kind": "rolebinding", "name": "drill-rc-a1b2c3", "namespace": "kube-system"},
            ],
        )
        with patch(
            _DELETE_POD, new_callable=AsyncMock, return_value="confirmed",
        ) as mock_delete:
            updated, cleaned = await cleanup_debug_pod_artifacts(
                [artifact], kubeconfig="kc", task_id="task-rc-1",
            )
        calls = [
            (c.args[0], c.kwargs.get("kind", "pod"), c.kwargs.get("namespace"))
            for c in mock_delete.await_args_list
        ]
        assert calls == [
            ("drill-rc-a1b2c3", "pod", "prod"),
            ("drill-rc-a1b2c3", "rolebinding", "kube-system"),
            ("drill-rc-a1b2c3", "role", "kube-system"),
            ("drill-rc-a1b2c3", "serviceaccount", "prod"),
        ]
        assert updated[0]["status"] == "cleaned"
        assert cleaned == ["drill-rc-a1b2c3"]

    @pytest.mark.asyncio
    async def test_nsless_family_member_falls_back_to_carrier_ns(self):
        # Members recorded before members carried their own ns have no
        # namespace key — cleanup falls back to the carrier ns, so
        # in-flight artifacts from before the upgrade still sweep whole.
        artifact = self._carrier_artifact(
            status="recovery_armed",
            recovery_deadline_epoch=time.time() - 1,
            recovery_timeout_seconds=300,
            rbac_family=[
                {"kind": "serviceaccount", "name": "drill-rc-a1b2c3"},
                {"kind": "role", "name": "drill-rc-a1b2c3"},
            ],
        )
        with patch(
            _DELETE_POD, new_callable=AsyncMock, return_value="confirmed",
        ) as mock_delete:
            updated, cleaned = await cleanup_debug_pod_artifacts(
                [artifact], kubeconfig="kc", task_id="task-rc-1",
            )
        calls = [
            (c.args[0], c.kwargs.get("kind", "pod"), c.kwargs.get("namespace"))
            for c in mock_delete.await_args_list
        ]
        assert calls == [
            ("drill-rc-a1b2c3", "pod", "prod"),
            ("drill-rc-a1b2c3", "role", "prod"),
            ("drill-rc-a1b2c3", "serviceaccount", "prod"),
        ]
        assert updated[0]["status"] == "cleaned"

    @pytest.mark.asyncio
    async def test_never_armed_carrier_is_stack_deleted(self):
        # A task that died before arming still sweeps the stack — with no
        # RBAC attached, the sweep is the pod alone.
        artifact = self._carrier_artifact()
        with patch(
            _DELETE_POD, new_callable=AsyncMock, return_value="confirmed",
        ) as mock_delete:
            updated, cleaned = await cleanup_debug_pod_artifacts(
                [artifact], kubeconfig="kc", task_id="task-rc-1",
            )
        assert mock_delete.await_count == 1
        assert updated[0]["status"] == "cleaned"
        assert cleaned == ["drill-rc-a1b2c3"]

    @pytest.mark.asyncio
    async def test_unconfirmed_delete_still_marks_cleaned(self):
        # Fire-and-forget: one attempt, no retry — the pod's bounded sleep
        # skeleton lets an unlanded delete lapse on its own.
        artifact = self._carrier_artifact()
        with patch(
            _DELETE_POD, new_callable=AsyncMock, return_value="unconfirmed",
        ):
            updated, cleaned = await cleanup_debug_pod_artifacts(
                [artifact], kubeconfig="kc", task_id="task-rc-1",
            )
        assert updated[0]["status"] == "cleaned"
        assert cleaned == ["drill-rc-a1b2c3"]


class TestPodVictimCarrierRunDrift:
    """Carrier-run identity anchoring under a scope=pod victim (run6 finding).

    A compliant recovery-carrier ``kubectl run`` under a pod-scoped
    approval hits the MAIN names comparison (same kind — no secondary
    path like a deployment victim's), and the carrier is a NEW pod whose
    name can never equal the victim's: identity is therefore anchored by
    scope + namespace with the carrier SHAPE as the security boundary
    (design D7). These tests pin that anchoring and its limits.
    """

    @staticmethod
    def _pod_victim() -> "ApprovedTarget":
        from chaos_agent.agent.target_guard.types import ApprovedTarget

        return ApprovedTarget(
            scope="pod", namespace="default",
            names=("drill-reorder-target",),
        )

    @staticmethod
    def _carrier_run(
        name: str = "drill-rc-nxd300",
        namespace: str = "default",
        **extra: object,
    ) -> "EffectiveTarget":
        from chaos_agent.agent.target_guard.types import EffectiveTarget

        return EffectiveTarget(
            scope="pod", namespace=namespace, names=(name,),
            raw_command=f"kubectl run {name} -n {namespace}",
            is_recovery_carrier=True, **extra,
        )

    def test_same_ns_carrier_run_passes_identity(self):
        # The run6 shape exactly: pod victim, carrier run in the SAME
        # namespace — the structural false drift is gone, identity
        # passes (carrier-agnostic checks still run in the guard).
        from chaos_agent.agent.target_guard.drift_policy import K8sDriftPolicy

        decision = K8sDriftPolicy().check_identity_drift(
            self._pod_victim(), self._carrier_run(),
        )
        assert decision is None

    def test_cross_ns_carrier_run_still_rejected(self):
        # The namespace check runs BEFORE the carrier exemption and is
        # untouched by it: a carrier built outside the victim's
        # namespace is real drift.
        from chaos_agent.agent.target_guard.drift_policy import K8sDriftPolicy

        decision = K8sDriftPolicy().check_identity_drift(
            self._pod_victim(),
            self._carrier_run(namespace="kube-system"),
        )
        assert decision is not None
        assert decision.verdict.value == "reject_drift"
        assert "namespace drift" in decision.reason

    def test_non_carrier_run_keeps_name_drift(self):
        # The exemption is keyed on the classifier's five-condition shape
        # marker — an ordinary ``kubectl run`` with a foreign name under
        # a pod victim stays the classic resource-selection drift.
        from chaos_agent.agent.target_guard.drift_policy import K8sDriftPolicy
        from chaos_agent.agent.target_guard.types import EffectiveTarget

        effective = EffectiveTarget(
            scope="pod", namespace="default",
            names=("some-other-pod",),
            raw_command="kubectl run some-other-pod -n default",
        )
        decision = K8sDriftPolicy().check_identity_drift(
            self._pod_victim(), effective,
        )
        assert decision is not None
        assert decision.verdict.value == "reject_drift"
        assert "resource selection drift" in decision.reason

    def test_carrier_marker_with_fault_target_not_exempt(self):
        # Defensive: a carrier-shaped run that ALSO carries a blade
        # fault_target is not the recovery-carrier use case — the names
        # comparison applies to it exactly as before.
        from chaos_agent.agent.target_guard.drift_policy import K8sDriftPolicy

        decision = K8sDriftPolicy().check_identity_drift(
            self._pod_victim(),
            self._carrier_run(fault_target="pod-network"),
        )
        assert decision is not None
        assert decision.verdict.value == "reject_drift"
