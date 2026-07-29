"""Tests for ``chaos_agent.agent.nodes.planning.tool_screener``.

Covers:
  - log-only mode (default): all verdicts pass through to phase2_tools
  - enforcing mode + same target → pass
  - enforcing mode + drift → interrupt (approve → pass, reject → retry)
  - enforcing mode + banned/unknown → retry with fabricated rejections
  - mixed verdicts in a multi-tool_call AIMessage
  - approved_target=None defence
  - drift after prior rejection → hard terminate
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chaos_agent.agent.nodes.planning.tool_screener import (
    SCREENER_ROUTE_PASS,
    SCREENER_ROUTE_REPLAN,
    SCREENER_ROUTE_RETRY,
    _format_rejection_for_llm,
    route_after_screener,
    tool_screener,
)
from chaos_agent.agent.target_guard import (
    ConfidenceLevel,
    EffectiveTarget,
    GuardVerdict,
    approved_from_dict,
    freeze_approved_target,
)
from chaos_agent.agent.target_guard.carriers import (
    CarrierRejectReason,
    CarrierResolution,
    host_operation_has_bounded_recovery,
    is_host_carrier_call,
    _parse_host_exec,
)
from chaos_agent.config.settings import settings


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_settings():
    """Snapshot + restore the feature flags around every test."""
    orig_enforce = settings.target_guard_enforcing
    orig_skill = settings.skill_script_default_allow
    orig_ttl = settings.carrier_liveness_ttl_seconds
    yield
    settings.target_guard_enforcing = orig_enforce
    settings.skill_script_default_allow = orig_skill
    settings.carrier_liveness_ttl_seconds = orig_ttl


def _approved_pod_a_in_ns():
    """Approved target: ns/pod-a + blade target cpu."""
    return freeze_approved_target(
        target={"namespace": "ns", "names": ["pod-a"]},
        params={"scope": "pod"},
        blade_scope="pod", blade_target="cpu", blade_action="fullload",
    )


def _ai_with_tool_call(name: str, args: dict, call_id: str = "tc-1"):
    """Build an AIMessage carrying a single tool_call."""
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id}],
    )


def _approved_node_network():
    return freeze_approved_target(
        target={"namespace": "", "names": ["node-a"]},
        params={"scope": "node"},
        blade_scope="node", blade_target="network", blade_action="drop",
    )


def _debug_artifact(*, family: str = "network"):
    return {
        "artifact_id": "uid-debug-1",
        "type": "debug_pod",
        "status": "active",
        "task_id": "task-1",
        "name": "node-debugger-node-a-abc12",
        "namespace": "kubewiz",
        "uid": "uid-debug-1",
        "target": {"scope": "node", "name": "node-a"},
        "operation_family": family,
        "debug_profile": "sysadmin",
        "privileged": True,
    }


def _bounded_network_host_command() -> str:
    return (
        "chroot /host sh -c 'iptables -I OUTPUT -j DROP && "
        "iptables -I INPUT -j DROP && "
        'nohup sh -c "sleep 600 && iptables -D OUTPUT -j DROP && '
        'iptables -D INPUT -j DROP" '
        ">/dev/null 2>&1 &'"
    )


# ---------------------------------------------------------------------------
# Log-only mode (default flag = False)
# ---------------------------------------------------------------------------


class TestLogOnlyMode:
    @pytest.mark.asyncio
    async def test_log_only_passes_drift_through(self):
        # Even with clear drift, log-only mode must not block.
        settings.target_guard_enforcing = False
        state = {
            "messages": [
                HumanMessage(content="inject"),
                _ai_with_tool_call("blade_create", {
                    "scope": "pod", "target": "cpu", "namespace": "ns",
                    "names": ["pod-OTHER"],
                }),
            ],
            "approved_target": _approved_pod_a_in_ns(),
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        # No fabricated ToolMessages in log-only mode
        assert "messages" not in delta

    @pytest.mark.asyncio
    async def test_log_only_passes_banned_through(self):
        settings.target_guard_enforcing = False
        state = {
            "messages": [
                _ai_with_tool_call("kubectl", {"command": ["apply", "-f", "x.yaml"]}),
            ],
            "approved_target": _approved_pod_a_in_ns(),
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS

    @pytest.mark.asyncio
    async def test_profile_external_tool_is_rejected_even_in_log_only_mode(self):
        settings.target_guard_enforcing = False
        state = {
            "messages": [_ai_with_tool_call("kubectl", {"command": ["get", "pods"]})],
            "fault_spec": {"scope": "host"},
            "kube_connection_mode": "ssh",
            "ssh_host": "host.example",
            "approved_target": _approved_pod_a_in_ns(),
        }

        delta = await tool_screener(state)

        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        # The capability gate is what refused — assert on the FACTS it now
        # reports (which tool, which profile owns it, which one is in force)
        # rather than on the old template "environment capability profile".
        # That template was the same sentence for every tool in every profile,
        # so anchoring to it could not tell a precise refusal from a vague one.
        body = delta["messages"][0].content
        assert "'kubectl' is provided for the k8s profile" in body
        assert "environment in force is 'host'" in body


# ---------------------------------------------------------------------------
# Enforcing mode — ALLOW path
# ---------------------------------------------------------------------------


class TestEnforcingAllow:
    @pytest.mark.asyncio
    async def test_same_target_passes(self):
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                _ai_with_tool_call("blade_create", {
                    "scope": "pod", "target": "cpu", "namespace": "ns",
                    "names": ["pod-a"],
                }),
            ],
            "approved_target": _approved_pod_a_in_ns(),
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        assert "messages" not in delta

    @pytest.mark.asyncio
    async def test_readonly_passes(self):
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                _ai_with_tool_call("kubectl", {"command": ["get", "pods"]}),
            ],
            "approved_target": _approved_pod_a_in_ns(),
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS

    @pytest.mark.asyncio
    @patch("chaos_agent.agent.nodes.planning.tool_screener.interrupt", return_value="approved")
    async def test_production_kubectl_shape_drift_caught(self, _mock_interrupt):
        # Regression: the screener MUST classify the real production
        # kubectl tool shape {subcommand, v_args}. Earlier the
        # classifier only knew the legacy {command: list[str]} shape,
        # so every real kubectl call slipped through (or got rejected
        # as UNKNOWN). This test fires on the actual production shape
        # to lock the contract.
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                AIMessage(content="", tool_calls=[{
                    "name": "kubectl",
                    "args": {
                        "subcommand": "exec",
                        "v_args": "pod-a -n ns -- blade create k8s node-cpu fullload --node node-7",
                    },
                    "id": "tc-prod",
                }]),
            ],
            "approved_target": _approved_pod_a_in_ns(),
        }
        delta = await tool_screener(state)
        # The inner blade escapes to node-7, which is scope=node — a
        # scope drift. interrupt() fires; mock approves → pass.
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        _mock_interrupt.assert_called_once()

    @pytest.mark.asyncio
    async def test_production_kubectl_shape_readonly_passes(self):
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                AIMessage(content="", tool_calls=[{
                    "name": "kubectl",
                    "args": {"subcommand": "get", "v_args": "pods -n ns"},
                    "id": "tc-ro",
                }]),
            ],
            "approved_target": _approved_pod_a_in_ns(),
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS

    @pytest.mark.asyncio
    async def test_method_switch_blade_to_kubectl_passes(self):
        # Approved blade cpu on pod-a; LLM switches to kubectl scale on
        # same pod — method autonomy, must pass.
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                _ai_with_tool_call("kubectl", {
                    "command": ["scale", "deploy/pod-a", "--replicas=0", "-n", "ns"],
                }),
            ],
            # approved is at pod scope; this call is deployment scope.
            # That's actually a scope mismatch — for the test we want
            # method switch on SAME scope. Use a deployment-approved
            # target for this case.
            "approved_target": freeze_approved_target(
                target={"namespace": "ns", "names": ["pod-a"]},
                params={"scope": "deployment"},
                blade_scope=None, blade_target="cpu", blade_action=None,
            ),
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS

    @pytest.mark.asyncio
    async def test_registered_debug_carrier_maps_back_to_approved_node(self):
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                _ai_with_tool_call("kubectl", {
                    "subcommand": "exec",
                    "v_args": (
                        "node-debugger-node-a-abc12 -n kubewiz -- "
                        f"{_bounded_network_host_command()}"
                    ),
                }),
            ],
            "approved_target": _approved_node_network(),
            "execution_artifacts": [_debug_artifact()],
        }
        with patch(
            "chaos_agent.agent.nodes.planning.tool_screener.registered_carrier_is_current",
            new=AsyncMock(return_value=True),
        ):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS

    @pytest.mark.asyncio
    async def test_registered_debug_carrier_allows_readonly_host_probe(self):
        settings.target_guard_enforcing = True
        state = {
            "messages": [_ai_with_tool_call("kubectl", {
                "subcommand": "exec",
                "v_args": (
                    "node-debugger-node-a-abc12 -n kubewiz -- "
                    "chroot /host sh -c 'command -v iptables'"
                ),
            })],
            "approved_target": _approved_node_network(),
            "execution_artifacts": [_debug_artifact()],
        }
        with patch(
            "chaos_agent.agent.nodes.planning.tool_screener.registered_carrier_is_current",
            new=AsyncMock(return_value=True),
        ):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS

    @pytest.mark.asyncio
    async def test_direct_host_binary_cannot_bypass_bounded_recovery(self):
        settings.target_guard_enforcing = True
        state = {
            "messages": [_ai_with_tool_call("kubectl", {
                "subcommand": "exec",
                "v_args": (
                    "node-debugger-node-a-abc12 -n kubewiz -- "
                    "/host/sbin/iptables -I OUTPUT -j DROP"
                ),
            })],
            "approved_target": _approved_node_network(),
            "execution_artifacts": [_debug_artifact()],
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        assert "REJECT_BANNED" in delta["messages"][0].content

    @pytest.mark.asyncio
    async def test_blade_destroy_allows_uid_from_current_failed_create(self):
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                ToolMessage(
                    content=(
                        "Error: injection FAILED permanently. Experiment CRD was "
                        "created (UID: ef329886e1b933f4) but the fault CANNOT take effect"
                    ),
                    name="blade_create",
                    tool_call_id="create-1",
                    status="error",
                ),
                _ai_with_tool_call(
                    "blade_destroy", {"uid": "ef329886e1b933f4"}, "destroy-1",
                ),
            ],
            "approved_target": _approved_node_network(),
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS

    @pytest.mark.asyncio
    async def test_blade_destroy_rejects_foreign_uid_even_in_log_only_mode(self):
        settings.target_guard_enforcing = False
        state = {
            "messages": [_ai_with_tool_call(
                "blade_destroy", {"uid": "foreign123456789"}, "destroy-1",
            )],
            "approved_target": _approved_node_network(),
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        assert "not produced by this task" in delta["messages"][0].content

    @pytest.mark.asyncio
    async def test_registered_carrier_uses_transport_namespace_when_omitted(self):
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                _ai_with_tool_call("kubectl", {
                    "subcommand": "exec",
                    "v_args": (
                        "node-debugger-node-a-abc12 -- "
                        f"{_bounded_network_host_command()}"
                    ),
                }),
            ],
            "approved_target": _approved_node_network(),
            "execution_artifacts": [_debug_artifact()],
        }
        with patch(
            "chaos_agent.agent.nodes.planning.tool_screener.registered_carrier_is_current",
            new=AsyncMock(return_value=True),
        ):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS

    @pytest.mark.asyncio
    async def test_registered_carrier_cannot_switch_fault_family(self):
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                _ai_with_tool_call("kubectl", {
                    "subcommand": "exec",
                    "v_args": (
                        "node-debugger-node-a-abc12 -n kubewiz -- "
                        "chroot /host fallocate -l 1G /tmp/fill"
                    ),
                }),
            ],
            "approved_target": _approved_node_network(),
            "execution_artifacts": [_debug_artifact()],
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        assert "REJECT_BANNED" in delta["messages"][0].content

    @pytest.mark.asyncio
    @pytest.mark.parametrize("host_command", [
        "chroot /host sh -c 'iptables -I OUTPUT -j DROP && fallocate -l 1G /tmp/fill'",
        "chroot /host sh -c 'iptables -I OUTPUT -j DROP && rm -rf /tmp/data'",
    ])
    async def test_registered_carrier_rejects_mixed_or_dangerous_commands(
        self, host_command,
    ):
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                _ai_with_tool_call("kubectl", {
                    "subcommand": "exec",
                    "v_args": (
                        "node-debugger-node-a-abc12 -n kubewiz -- "
                        f"{host_command}"
                    ),
                }),
            ],
            "approved_target": _approved_node_network(),
            "execution_artifacts": [_debug_artifact()],
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        assert "REJECT_BANNED" in delta["messages"][0].content

    @pytest.mark.asyncio
    async def test_recreated_debug_pod_uid_is_rejected(self):
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                _ai_with_tool_call("kubectl", {
                    "subcommand": "exec",
                    "v_args": (
                        "node-debugger-node-a-abc12 -n kubewiz -- "
                        f"{_bounded_network_host_command()}"
                    ),
                }),
            ],
            "approved_target": _approved_node_network(),
            "execution_artifacts": [_debug_artifact()],
        }
        with patch(
            "chaos_agent.agent.nodes.planning.tool_screener.registered_carrier_is_current",
            new=AsyncMock(return_value=False),
        ):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        assert "REJECT_BANNED" in delta["messages"][0].content

    @pytest.mark.asyncio
    async def test_carrier_verification_exception_fails_closed(self):
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                _ai_with_tool_call("kubectl", {
                    "subcommand": "exec",
                    "v_args": (
                        "node-debugger-node-a-abc12 -n kubewiz -- "
                        f"{_bounded_network_host_command()}"
                    ),
                }),
            ],
            "approved_target": _approved_node_network(),
            "execution_artifacts": [_debug_artifact()],
        }
        with patch(
            "chaos_agent.agent.nodes.planning.tool_screener.registered_carrier_is_current",
            new=AsyncMock(side_effect=RuntimeError("lookup failed")),
        ):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        assert "REJECT_BANNED" in delta["messages"][0].content

    @pytest.mark.asyncio
    async def test_malformed_carrier_artifact_fails_closed(self):
        settings.target_guard_enforcing = True
        artifact = _debug_artifact()
        artifact["target"] = "malformed"
        state = {
            "messages": [
                _ai_with_tool_call("kubectl", {
                    "subcommand": "exec",
                    "v_args": (
                        "node-debugger-node-a-abc12 -n kubewiz -- "
                        f"{_bounded_network_host_command()}"
                    ),
                }),
            ],
            "approved_target": _approved_node_network(),
            "execution_artifacts": [artifact],
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        assert "REJECT_BANNED" in delta["messages"][0].content

    @pytest.mark.asyncio
    async def test_registered_carrier_requires_sysadmin_profile(self):
        settings.target_guard_enforcing = True
        artifact = _debug_artifact()
        artifact["debug_profile"] = "general"
        state = {
            "messages": [
                _ai_with_tool_call("kubectl", {
                    "subcommand": "exec",
                    "v_args": (
                        "node-debugger-node-a-abc12 -n kubewiz -- "
                        f"{_bounded_network_host_command()}"
                    ),
                }),
            ],
            "approved_target": _approved_node_network(),
            "execution_artifacts": [artifact],
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY

    @pytest.mark.asyncio
    async def test_registered_carrier_requires_observed_privileged_container(self):
        settings.target_guard_enforcing = True
        artifact = _debug_artifact()
        artifact["privileged"] = False
        state = {
            "messages": [
                _ai_with_tool_call("kubectl", {
                    "subcommand": "exec",
                    "v_args": (
                        "node-debugger-node-a-abc12 -n kubewiz -- "
                        f"{_bounded_network_host_command()}"
                    ),
                }),
            ],
            "approved_target": _approved_node_network(),
            "execution_artifacts": [artifact],
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY

    @pytest.mark.asyncio
    async def test_registered_carrier_rejects_unbounded_host_mutation(self):
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                _ai_with_tool_call("kubectl", {
                    "subcommand": "exec",
                    "v_args": (
                        "node-debugger-node-a-abc12 -n kubewiz -- "
                        "chroot /host iptables -I OUTPUT -j DROP"
                    ),
                }),
            ],
            "approved_target": _approved_node_network(),
            "execution_artifacts": [_debug_artifact()],
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY

    @pytest.mark.asyncio
    async def test_registered_carrier_requires_inverse_for_every_inserted_rule(self):
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                _ai_with_tool_call("kubectl", {
                    "subcommand": "exec",
                    "v_args": (
                        "node-debugger-node-a-abc12 -n kubewiz -- chroot /host "
                        "sh -c 'iptables -I OUTPUT -j DROP && "
                        "iptables -I INPUT -j DROP && nohup sh -c "
                        '"sleep 600 && iptables -D OUTPUT -j DROP" '
                        ">/dev/null 2>&1 &'"
                    ),
                }),
            ],
            "approved_target": _approved_node_network(),
            "execution_artifacts": [_debug_artifact()],
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY

    @pytest.mark.asyncio
    async def test_registered_carrier_rejects_stacked_host_mutation(self):
        settings.target_guard_enforcing = True
        artifact = _debug_artifact()
        artifact["status"] = "recovery_armed"
        state = {
            "messages": [
                _ai_with_tool_call("kubectl", {
                    "subcommand": "exec",
                    "v_args": (
                        "node-debugger-node-a-abc12 -n kubewiz -- "
                        f"{_bounded_network_host_command()}"
                    ),
                }),
            ],
            "approved_target": _approved_node_network(),
            "execution_artifacts": [artifact],
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY


# ---------------------------------------------------------------------------
# Enforcing mode — REJECT_DRIFT path (interrupt confirmation)
# ---------------------------------------------------------------------------


class TestEnforcingDriftInterrupt:
    @pytest.mark.asyncio
    @patch("chaos_agent.agent.nodes.planning.tool_screener.interrupt", return_value="approved")
    async def test_drift_approved_updates_spec_and_passes(self, _mock):
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                _ai_with_tool_call("blade_create", {
                    "scope": "pod", "target": "cpu", "namespace": "ns",
                    "names": ["pod-OTHER"],
                }, call_id="tc-1"),
            ],
            "approved_target": _approved_pod_a_in_ns(),
            "fault_spec": {
                "namespace": "ns", "scope": "pod", "names": ["pod-a"],
                "labels": {}, "blade_target": "cpu", "blade_action": "fullload",
                "params": {}, "params_flags": [], "duration_seconds": 0,
                "source": "test", "user_description": "",
            },
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        assert delta["drift_reject_count"] == 0
        # fault_spec corrected
        assert delta["fault_spec"]["names"] == ["pod-OTHER"]
        # approved_target refrozen
        assert "pod-OTHER" in delta["approved_target"]["names"]
        _mock.assert_called_once()
        # interrupt payload has correct shape
        payload = _mock.call_args[0][0]
        assert payload["type"] == "target_change"
        assert list(payload["proposed"]["names"]) == ["pod-OTHER"]

    @pytest.mark.asyncio
    @patch("chaos_agent.agent.nodes.planning.tool_screener.interrupt", return_value="rejected")
    async def test_drift_rejected_increments_counter_and_retries(self, _mock):
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                _ai_with_tool_call("blade_create", {
                    "scope": "pod", "target": "cpu", "namespace": "ns",
                    "names": ["pod-OTHER"],
                }, call_id="tc-1"),
            ],
            "approved_target": _approved_pod_a_in_ns(),
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        assert delta["drift_reject_count"] == 1
        # Rejection ToolMessages present
        assert len(delta["messages"]) == 1
        assert isinstance(delta["messages"][0], ToolMessage)
        assert "REJECT_DRIFT" in delta["messages"][0].content

    @pytest.mark.asyncio
    async def test_second_drift_after_rejection_terminates(self):
        # After one rejection, next drift hard-terminates (no interrupt).
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                _ai_with_tool_call("blade_create", {
                    "scope": "pod", "target": "cpu", "namespace": "ns",
                    "names": ["pod-OTHER"],
                }),
            ],
            "approved_target": _approved_pod_a_in_ns(),
            "drift_reject_count": 1,
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        # fail_state sets error field
        assert "error" in delta
        assert "failure_detail" in delta

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_cross_scope_node_op_allowed_as_secondary(self):
        """kubectl cordon node under pod approval is allowed (secondary scope)
        because kubectl-native injection methods may need node operations
        (e.g. taint nodes to cause Pod Pending)."""
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                _ai_with_tool_call("kubectl", {
                    "command": ["cordon", "node-1"],
                }),
            ],
            "approved_target": _approved_pod_a_in_ns(),
            "fault_spec": {
                "namespace": "ns", "scope": "pod", "names": ["pod-a"],
                "labels": {}, "blade_target": "cpu", "blade_action": "fullload",
                "params": {}, "params_flags": [], "duration_seconds": 0,
                "source": "test", "user_description": "",
            },
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS


# ---------------------------------------------------------------------------
# Enforcing mode — REJECT_BANNED / REJECT_UNKNOWN path (retry)
# ---------------------------------------------------------------------------


class TestEnforcingRetry:
    @pytest.mark.asyncio
    async def test_banned_kubectl_apply_triggers_retry(self):
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                _ai_with_tool_call("kubectl", {
                    "command": ["apply", "-f", "x.yaml"],
                }, call_id="tc-2"),
            ],
            "approved_target": _approved_pod_a_in_ns(),
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        assert "replan_requested" not in delta or not delta.get("replan_requested")
        # ToolMessage carries the rejection reason
        tm = delta["messages"][0]
        assert tm.tool_call_id == "tc-2"
        assert "REJECT_BANNED" in tm.content

    @pytest.mark.asyncio
    async def test_skill_script_default_ban_triggers_retry(self):
        settings.target_guard_enforcing = True
        settings.skill_script_default_allow = False
        state = {
            "messages": [
                _ai_with_tool_call("_execute_skill_script", {"path": "/x"}),
            ],
            "approved_target": _approved_pod_a_in_ns(),
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY

    @pytest.mark.asyncio
    async def test_skill_script_opt_in_passes_through(self):
        # Bug fix: when the operator flips skill_script_default_allow
        # to True, the screener must actually let the call through.
        # Previously the classifier returned UNKNOWN even with opt-in,
        # which the guard still rejected, making the flag a no-op.
        settings.target_guard_enforcing = True
        settings.skill_script_default_allow = True
        state = {
            "messages": [
                _ai_with_tool_call("_execute_skill_script", {"path": "/x"}),
            ],
            "approved_target": _approved_pod_a_in_ns(),
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS

    @pytest.mark.asyncio
    async def test_unknown_tool_triggers_retry(self):
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                _ai_with_tool_call("mystery_mcp_tool", {"foo": 1}),
            ],
            "approved_target": _approved_pod_a_in_ns(),
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        assert "REJECT_UNKNOWN" in delta["messages"][0].content


# ---------------------------------------------------------------------------
# Mixed verdicts in one AIMessage — DRIFT wins over BANNED
# ---------------------------------------------------------------------------


class TestMixedVerdicts:
    @pytest.mark.asyncio
    @patch("chaos_agent.agent.nodes.planning.tool_screener.interrupt", return_value="rejected")
    async def test_drift_plus_banned_routes_to_interrupt(self, _mock):
        # When at least one drift is present alongside other rejects,
        # the screener prioritises drift path (interrupt). If user
        # rejects, all tool_calls get fabricated rejection messages.
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                AIMessage(content="", tool_calls=[
                    {"name": "blade_create", "args": {
                        "scope": "pod", "target": "cpu", "namespace": "ns",
                        "names": ["pod-OTHER"],
                    }, "id": "tc-A"},
                    {"name": "kubectl", "args": {
                        "command": ["apply", "-f", "x.yaml"],
                    }, "id": "tc-B"},
                ]),
            ],
            "approved_target": _approved_pod_a_in_ns(),
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        assert delta["drift_reject_count"] == 1
        # BOTH tool_calls get a fabricated rejection (LangChain requires
        # 1:1 tool_call ↔ ToolMessage pairing).
        assert len(delta["messages"]) == 2
        ids = {tm.tool_call_id for tm in delta["messages"]}
        assert ids == {"tc-A", "tc-B"}


# ---------------------------------------------------------------------------
# approved_target=None defence
# ---------------------------------------------------------------------------


class TestNoApproval:
    @pytest.mark.asyncio
    async def test_no_approval_log_only_passes(self):
        # Without an approval, log-only mode must still pass through —
        # we don't want to retroactively block existing flows during
        # grey rollout.
        settings.target_guard_enforcing = False
        state = {
            "messages": [
                _ai_with_tool_call("blade_create", {
                    "scope": "pod", "target": "cpu", "namespace": "ns",
                    "names": ["pod-a"],
                }),
            ],
            "approved_target": None,
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS

    @pytest.mark.asyncio
    async def test_no_approval_enforcing_rejects_destructive(self):
        # Defence-in-depth: enforcing mode + no approval + destructive
        # call → UNKNOWN verdict → retry path. The LLM sees the
        # rejection and can issue a structured replan request to seek approval.
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                _ai_with_tool_call("blade_create", {
                    "scope": "pod", "target": "cpu", "namespace": "ns",
                    "names": ["pod-a"],
                }),
            ],
            "approved_target": None,
        }
        delta = await tool_screener(state)
        # No approval on real scope → guard returns REJECT_UNKNOWN →
        # retry path.
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY

    @pytest.mark.asyncio
    async def test_no_approval_readonly_still_passes(self):
        # Read-only tools always pass, even without approval.
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                _ai_with_tool_call("kubectl", {"command": ["get", "pods"]}),
            ],
            "approved_target": None,
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS


# ---------------------------------------------------------------------------
# route_after_screener — sentinel mapping
# ---------------------------------------------------------------------------


class TestRouteAfterScreener:
    def test_pass_route(self):
        assert route_after_screener({"screener_route": SCREENER_ROUTE_PASS}) == "pass"

    def test_replan_route(self):
        assert route_after_screener({"screener_route": SCREENER_ROUTE_REPLAN}) == "replan"

    def test_retry_route(self):
        assert route_after_screener({"screener_route": SCREENER_ROUTE_RETRY}) == "retry"

    def test_missing_route_defaults_to_pass(self):
        # Defence: an unset/None value never strands the graph.
        assert route_after_screener({}) == "pass"
        assert route_after_screener({"screener_route": None}) == "pass"

    def test_unknown_value_defaults_to_pass(self):
        assert route_after_screener({"screener_route": "bogus"}) == "pass"


# ---------------------------------------------------------------------------
# Defensive: empty / non-AIMessage tail
# ---------------------------------------------------------------------------


class TestFailOpen:
    """The screener must NEVER kill the turn on its own exception.

    A classifier crash should produce a logged error + ALLOW route,
    not a propagated exception that aborts execute_loop. Otherwise a
    bug in the guard becomes a worse outage than the bug it's trying
    to prevent.
    """

    @pytest.mark.asyncio
    async def test_classifier_crash_routes_to_pass(self, monkeypatch):
        settings.target_guard_enforcing = True

        def _boom(*_args, **_kwargs):
            raise RuntimeError("synthetic classifier crash")

        # Patch the classifier call inside the screener module so it
        # always raises. The screener should catch and ALLOW.
        from chaos_agent.agent.nodes.planning import tool_screener as ts
        monkeypatch.setattr(ts, "infer_effective_target", _boom)

        state = {
            "messages": [
                _ai_with_tool_call("blade_create", {
                    "scope": "pod", "target": "cpu", "namespace": "ns",
                    "names": ["pod-a"],
                }),
            ],
            "approved_target": _approved_pod_a_in_ns(),
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        # No fabricated rejection messages — the crashed call is
        # treated as ALLOW so the ToolNode runs it normally.
        assert "messages" not in delta


class TestDefensiveEdgeCases:
    @pytest.mark.asyncio
    async def test_no_messages_passes(self):
        delta = await tool_screener({"messages": [], "approved_target": _approved_pod_a_in_ns()})
        assert delta["screener_route"] == SCREENER_ROUTE_PASS

    @pytest.mark.asyncio
    async def test_last_message_is_human_passes(self):
        delta = await tool_screener({
            "messages": [HumanMessage(content="hi")],
            "approved_target": _approved_pod_a_in_ns(),
        })
        assert delta["screener_route"] == SCREENER_ROUTE_PASS

    @pytest.mark.asyncio
    async def test_ai_without_tool_calls_passes(self):
        delta = await tool_screener({
            "messages": [AIMessage(content="all done")],
            "approved_target": _approved_pod_a_in_ns(),
        })
        assert delta["screener_route"] == SCREENER_ROUTE_PASS


def _approved_node_disk():
    return freeze_approved_target(
        target={"namespace": "", "names": ["node-a"]},
        params={"scope": "node"},
        blade_scope="node", blade_target="disk", blade_action="fill",
    )


def _approved_node_process():
    return freeze_approved_target(
        target={"namespace": "", "names": ["node-a"]},
        params={"scope": "node"},
        blade_scope="node", blade_target="process", blade_action="stop",
    )


class TestHostEntryUnwrap:
    """Hardening 1: recognise a host entry token behind one ``sh -c`` layer."""

    def test_direct_chroot_is_parsed(self):
        parsed = _parse_host_exec(
            "pod-a -n ns -- chroot /host iptables -I OUTPUT -j DROP"
        )
        assert parsed is not None
        assert parsed[0] == "pod-a"
        assert parsed[2].startswith("chroot")

    def test_shell_wrapped_chroot_is_parsed(self):
        parsed = _parse_host_exec(
            "pod-a -n ns -- sh -c 'chroot /host iptables -I OUTPUT -j DROP'"
        )
        assert parsed is not None
        assert parsed[0] == "pod-a"
        # host_command keeps the full inner so classify/recovery see everything.
        assert "chroot" in parsed[2]

    def test_shell_wrapped_non_host_command_stays_closed(self):
        assert _parse_host_exec("pod-a -n ns -- sh -c 'ls -la /'") is None

    def test_is_host_carrier_call_detects_wrapped_form(self):
        assert is_host_carrier_call("kubectl", {
            "subcommand": "exec",
            "v_args": "pod-a -n ns -- sh -c 'chroot /host tc qdisc add ...'",
        }) is True

    def test_is_host_carrier_call_detects_direct_host_binary(self):
        assert is_host_carrier_call("kubectl", {
            "subcommand": "exec",
            "v_args": "pod-a -n ns -- /host/sbin/iptables -V",
        }) is True

    def test_is_host_carrier_call_ignores_plain_exec(self):
        assert is_host_carrier_call("kubectl", {
            "subcommand": "exec",
            "v_args": "pod-a -n ns -- cat /etc/hostname",
        }) is False


class TestBoundedRecoveryFamilies:
    """Hardening 2: honest bounded-recovery contracts for process and disk."""

    def test_process_suspend_resume_is_bounded(self):
        cmd = "chroot /host sh -c 'kill -STOP 1234 && sleep 300 && kill -CONT 1234'"
        assert host_operation_has_bounded_recovery(cmd, "process") is True

    def test_process_terminate_is_not_bounded(self):
        cmd = "chroot /host sh -c 'kill -9 1234'"
        assert host_operation_has_bounded_recovery(cmd, "process") is False

    def test_process_stop_without_cont_is_not_bounded(self):
        cmd = "chroot /host sh -c 'kill -STOP 1234 && sleep 300'"
        assert host_operation_has_bounded_recovery(cmd, "process") is False

    def test_disk_fill_with_truncate_reclaim_is_bounded(self):
        cmd = (
            "chroot /host sh -c 'dd if=/dev/zero of=/host/tmp/fill bs=1M "
            "count=1024 && sleep 600 && truncate -s 0 /host/tmp/fill'"
        )
        assert host_operation_has_bounded_recovery(cmd, "disk") is True

    def test_disk_fill_with_fallocate_dig_reclaim_is_bounded(self):
        cmd = (
            "chroot /host sh -c 'fallocate -l 1G /host/tmp/fill && "
            "sleep 600 && fallocate -d /host/tmp/fill'"
        )
        assert host_operation_has_bounded_recovery(cmd, "disk") is True

    def test_disk_fill_without_reclaim_is_not_bounded(self):
        cmd = (
            "chroot /host sh -c 'dd if=/dev/zero of=/host/tmp/fill bs=1M "
            "count=1024 && sleep 600'"
        )
        assert host_operation_has_bounded_recovery(cmd, "disk") is False

    def test_disk_reclaim_of_other_path_is_not_bounded(self):
        cmd = (
            "chroot /host sh -c 'dd if=/dev/zero of=/host/tmp/fill bs=1M "
            "count=1024 && sleep 600 && truncate -s 0 /host/tmp/other'"
        )
        assert host_operation_has_bounded_recovery(cmd, "disk") is False

    # -- systemd-run --on-active timer variants -----------------------------

    def test_network_systemd_run_timer_is_bounded(self):
        cmd = (
            "chroot /host sh -c 'iptables -I OUTPUT -j DROP && "
            "iptables -I INPUT -j DROP && "
            "systemd-run --on-active=600s sh -c \"iptables -D OUTPUT -j DROP "
            "&& iptables -D INPUT -j DROP\"'"
        )
        assert host_operation_has_bounded_recovery(cmd, "network") is True

    def test_network_systemd_run_without_inverse_is_not_bounded(self):
        cmd = (
            "chroot /host sh -c 'iptables -I OUTPUT -j DROP && "
            "systemd-run --on-active=600s sh -c \"echo done\"'"
        )
        assert host_operation_has_bounded_recovery(cmd, "network") is False

    def test_process_systemd_run_timer_is_bounded(self):
        cmd = (
            "chroot /host sh -c 'kill -STOP 1234 && "
            "systemd-run --on-active=300s sh -c \"kill -CONT 1234\"'"
        )
        assert host_operation_has_bounded_recovery(cmd, "process") is True

    def test_disk_systemd_run_timer_is_bounded(self):
        cmd = (
            "chroot /host sh -c 'dd if=/dev/zero of=/host/tmp/fill bs=1M "
            "count=1024 && "
            "systemd-run --on-active=600s sh -c \"truncate -s 0 /host/tmp/fill\"'"
        )
        assert host_operation_has_bounded_recovery(cmd, "disk") is True

    def test_cpu_systemd_run_timer_is_bounded(self):
        cmd = (
            "chroot /host sh -c 'stress --cpu 4 & "
            r"systemd-run --on-active=300s sh -c " + r'"kill $!"' + "'"
        )
        assert host_operation_has_bounded_recovery(cmd, "cpu") is True


class TestCarrierHardeningIntegration:
    """End-to-end screening for the newly covered carrier forms."""

    @pytest.mark.asyncio
    async def test_shell_wrapped_network_injection_passes(self):
        settings.target_guard_enforcing = True
        wrapped = (
            "node-debugger-node-a-abc12 -n kubewiz -- sh -c "
            "'chroot /host sh -c \"iptables -I OUTPUT -j DROP && "
            "sleep 600 && iptables -D OUTPUT -j DROP\"'"
        )
        state = {
            "messages": [_ai_with_tool_call("kubectl", {
                "subcommand": "exec", "v_args": wrapped,
            })],
            "approved_target": _approved_node_network(),
            "execution_artifacts": [_debug_artifact()],
        }
        with patch(
            "chaos_agent.agent.nodes.planning.tool_screener.registered_carrier_is_current",
            new=AsyncMock(return_value=True),
        ):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS

    @pytest.mark.asyncio
    async def test_on_create_timer_injection_passes_regression(self):
        """End-to-end regression for task-be05d1ad.

        The real drill emitted a correct, self-recovering network fault whose
        reversal was scheduled with ``systemd-run --on-create=600s``. The old
        ``_SYSTEMD_TIMER`` literal recognised ONLY ``--on-active``, so the
        bounded-recovery check failed → carrier resolution fell through → the
        call was classified SCOPE_ESCAPE → REJECT_BANNED, and the model spun.
        With recoverability judged by STRUCTURE (any ``--on-*`` timer + inverse)
        the exact same call now clears the screener.
        """
        settings.target_guard_enforcing = True
        wrapped = (
            "node-debugger-node-a-abc12 -n kubewiz -- sh -c "
            "'chroot /host sh -c \"iptables -I OUTPUT -j DROP && "
            "systemd-run --on-create=600s iptables -D OUTPUT -j DROP\"'"
        )
        state = {
            "messages": [_ai_with_tool_call("kubectl", {
                "subcommand": "exec", "v_args": wrapped,
            })],
            "approved_target": _approved_node_network(),
            "execution_artifacts": [_debug_artifact()],
        }
        with patch(
            "chaos_agent.agent.nodes.planning.tool_screener.registered_carrier_is_current",
            new=AsyncMock(return_value=True),
        ):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS

    @pytest.mark.asyncio
    async def test_disk_fill_with_truncate_reclaim_passes(self):
        settings.target_guard_enforcing = True
        cmd = (
            "node-debugger-node-a-abc12 -n kubewiz -- chroot /host sh -c "
            "'dd if=/dev/zero of=/host/tmp/fill bs=1M count=1024 && "
            "sleep 600 && truncate -s 0 /host/tmp/fill'"
        )
        state = {
            "messages": [_ai_with_tool_call("kubectl", {
                "subcommand": "exec", "v_args": cmd,
            })],
            "approved_target": _approved_node_disk(),
            "execution_artifacts": [_debug_artifact(family="disk")],
        }
        with patch(
            "chaos_agent.agent.nodes.planning.tool_screener.registered_carrier_is_current",
            new=AsyncMock(return_value=True),
        ):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS

    @pytest.mark.asyncio
    async def test_process_suspend_resume_passes(self):
        settings.target_guard_enforcing = True
        cmd = (
            "node-debugger-node-a-abc12 -n kubewiz -- chroot /host sh -c "
            "'kill -STOP 1234 && sleep 300 && kill -CONT 1234'"
        )
        state = {
            "messages": [_ai_with_tool_call("kubectl", {
                "subcommand": "exec", "v_args": cmd,
            })],
            "approved_target": _approved_node_process(),
            "execution_artifacts": [_debug_artifact(family="process")],
        }
        with patch(
            "chaos_agent.agent.nodes.planning.tool_screener.registered_carrier_is_current",
            new=AsyncMock(return_value=True),
        ):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS

    @pytest.mark.asyncio
    async def test_process_terminate_is_rejected(self):
        settings.target_guard_enforcing = True
        cmd = (
            "node-debugger-node-a-abc12 -n kubewiz -- chroot /host sh -c "
            "'kill -9 1234'"
        )
        state = {
            "messages": [_ai_with_tool_call("kubectl", {
                "subcommand": "exec", "v_args": cmd,
            })],
            "approved_target": _approved_node_process(),
            "execution_artifacts": [_debug_artifact(family="process")],
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        assert "REJECT_BANNED" in delta["messages"][0].content

    @pytest.mark.asyncio
    async def test_systemd_run_network_injection_passes(self):
        settings.target_guard_enforcing = True
        cmd = (
            "node-debugger-node-a-abc12 -n kubewiz -- chroot /host sh -c "
            "'iptables -I OUTPUT -j DROP && iptables -I INPUT -j DROP && "
            'systemd-run --on-active=600s sh -c "iptables -D OUTPUT -j DROP '
            '&& iptables -D INPUT -j DROP"\''
        )
        state = {
            "messages": [_ai_with_tool_call("kubectl", {
                "subcommand": "exec", "v_args": cmd,
            })],
            "approved_target": _approved_node_network(),
            "execution_artifacts": [_debug_artifact()],
        }
        with patch(
            "chaos_agent.agent.nodes.planning.tool_screener.registered_carrier_is_current",
            new=AsyncMock(return_value=True),
        ):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS


# ---------------------------------------------------------------------------
# Carrier liveness freshness window (skip live re-probe for fresh carriers)
# ---------------------------------------------------------------------------


_PROBE = "chaos_agent.agent.nodes.planning.tool_screener.registered_carrier_is_current"
_DISCOVER = "chaos_agent.agent.nodes.planning.tool_screener.discover_unregistered_carrier"


def _fresh_artifact(
    *, pod: str, node: str, epoch: float, task_id: str = "task-1",
    status: str = "active", family: str = "network",
):
    return {
        "artifact_id": f"uid-{pod}",
        "type": "debug_pod",
        "status": status,
        "task_id": task_id,
        "name": pod,
        "namespace": "kubewiz",
        "uid": f"uid-{pod}",
        "target": {"scope": "node", "name": node},
        "operation_family": family,
        "debug_profile": "sysadmin",
        "privileged": True,
        "confirmed_live_epoch": epoch,
    }


def _exec_call(pod: str, call_id: str = "tc-1"):
    return {
        "name": "kubectl",
        "args": {
            "subcommand": "exec",
            "v_args": f"{pod} -n kubewiz -- {_bounded_network_host_command()}",
        },
        "id": call_id,
    }


def _single_carrier_state(*, epoch: float, task_id: str = "task-1", status: str = "active"):
    pod = "node-debugger-node-a-abc12"
    return {
        "messages": [AIMessage(content="", tool_calls=[_exec_call(pod)])],
        "approved_target": _approved_node_network(),
        "execution_artifacts": [
            _fresh_artifact(pod=pod, node="node-a", epoch=epoch,
                            task_id=task_id, status=status),
        ],
        "task_id": "task-1",
    }


class TestCarrierLivenessWindow:
    @pytest.mark.asyncio
    async def test_fresh_registered_carrier_skips_live_probe(self):
        # Single-node injection (the common case): a freshly-registered,
        # this-task active carrier passes WITHOUT the live kubectl re-probe.
        settings.target_guard_enforcing = True
        settings.carrier_liveness_ttl_seconds = 120
        state = _single_carrier_state(epoch=time.time())
        # If the probe is (wrongly) called it returns False → would REJECT,
        # so asserting PASS + not-called both prove the skip.
        probe = AsyncMock(return_value=False)
        with patch(_PROBE, new=probe):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        probe.assert_not_called()

    @pytest.mark.asyncio
    async def test_stale_carrier_falls_back_to_live_probe(self):
        settings.target_guard_enforcing = True
        settings.carrier_liveness_ttl_seconds = 120
        state = _single_carrier_state(epoch=time.time() - 3600)  # older than ttl
        probe = AsyncMock(return_value=True)
        with patch(_PROBE, new=probe):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        probe.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stale_carrier_rejected_when_probe_fails(self):
        settings.target_guard_enforcing = True
        settings.carrier_liveness_ttl_seconds = 120
        state = _single_carrier_state(epoch=time.time() - 3600)
        with patch(_PROBE, new=AsyncMock(return_value=False)):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        assert "REJECT_BANNED" in delta["messages"][0].content

    @pytest.mark.asyncio
    async def test_carrier_from_other_task_is_probed(self):
        # Fresh + active but registered by a DIFFERENT task → no fast path.
        settings.target_guard_enforcing = True
        settings.carrier_liveness_ttl_seconds = 120
        state = _single_carrier_state(epoch=time.time(), task_id="other-task")
        probe = AsyncMock(return_value=True)
        with patch(_PROBE, new=probe):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        probe.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_discovered_carrier_skips_redundant_probe(self):
        # Live-discovered (unregistered) carriers were JUST confirmed by a fresh
        # in-band kubectl get pod inside discover_unregistered_carrier
        # (privileged + approved node + uid). Re-probing via
        # registered_carrier_is_current would be a redundant second in-band read
        # on the very API path a network fault severs — so it is skipped.
        settings.target_guard_enforcing = True
        settings.carrier_liveness_ttl_seconds = 120
        pod = "node-debugger-node-a-zzz99"
        state = {
            "messages": [AIMessage(content="", tool_calls=[_exec_call(pod)])],
            "approved_target": _approved_node_network(),
            "execution_artifacts": [],  # nothing registered → discovery path
            "task_id": "task-1",
        }
        synthetic_effective = EffectiveTarget(
            scope="node", namespace="", names=("node-a",),
            blade_target="network", confidence=ConfidenceLevel.HIGH,
            raw_command="kubectl exec ...",
        )
        synthetic_artifact = {  # no task_id / confirmed_live_epoch
            "status": "active", "privileged": True,
            "target": {"scope": "node", "name": "node-a"},
        }
        # If the redundant re-probe is (wrongly) called it returns False →
        # would REJECT, so PASS + not-called both prove the skip.
        probe = AsyncMock(return_value=False)
        with patch(
            _DISCOVER,
            new=AsyncMock(return_value=CarrierResolution.allow(
                synthetic_effective, synthetic_artifact,
            )),
        ), patch(_PROBE, new=probe):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        probe.assert_not_called()

    @pytest.mark.asyncio
    async def test_recovery_armed_second_mutation_still_rejected(self):
        # The window must not let a recovery_armed carrier take a second
        # mutation: _resolve_carrier_from_artifact rejects it before the window
        # is even consulted.
        #
        # This test used to patch discovery to return None, which HID a real
        # bypass: every rejection fell through to live discovery, and discovery
        # synthesises an artifact with ``status="active"`` — so against a live
        # cluster the armed carrier would have been re-admitted for its second
        # mutation. Discovery is now scoped to gates a cluster read can actually
        # overturn, so the correct assertion is that it is never reached.
        settings.target_guard_enforcing = True
        settings.carrier_liveness_ttl_seconds = 120
        state = _single_carrier_state(epoch=time.time(), status="recovery_armed")
        discover = AsyncMock()
        with patch(_DISCOVER, new=discover):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        discover.assert_not_called()
        assert "REJECT_BANNED" in delta["messages"][0].content

    @pytest.mark.asyncio
    async def test_batch_fresh_carriers_pass_with_probe_unavailable(self):
        # Reproduces + fixes the az-outage false-reject: many approved nodes,
        # all with fresh carriers, while the live probe API path is dead.
        settings.target_guard_enforcing = True
        settings.carrier_liveness_ttl_seconds = 120
        nodes = [f"node-{i}" for i in range(5)]
        approved = freeze_approved_target(
            target={"namespace": "", "names": nodes},
            params={"scope": "node"},
            blade_scope="node", blade_target="network", blade_action="drop",
        )
        now = time.time()
        artifacts = []
        tool_calls = []
        for i, node in enumerate(nodes):
            pod = f"node-debugger-{node}-p{i}"
            artifacts.append(_fresh_artifact(pod=pod, node=node, epoch=now))
            tool_calls.append(_exec_call(pod, call_id=f"tc-{i}"))
        state = {
            "messages": [AIMessage(content="", tool_calls=tool_calls)],
            "approved_target": approved,
            "execution_artifacts": artifacts,
            "task_id": "task-1",
        }
        probe = AsyncMock(side_effect=RuntimeError("api server unreachable"))
        with patch(_PROBE, new=probe):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        probe.assert_not_called()

    @pytest.mark.asyncio
    async def test_ttl_zero_disables_window(self):
        # ttl<=0 → window off → every exec is live-probed (pre-optimization).
        settings.target_guard_enforcing = True
        settings.carrier_liveness_ttl_seconds = 0
        state = _single_carrier_state(epoch=time.time())  # fresh, but ttl=0
        probe = AsyncMock(return_value=True)
        with patch(_PROBE, new=probe):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        probe.assert_awaited_once()


class TestNodeDriftHint:
    """REJECT_DRIFT on a node-scope task lists approved nodes for re-targeting."""

    def _node_approved(self):
        return approved_from_dict(freeze_approved_target(
            target={"namespace": "", "names": ["node-a", "node-b"]},
            params={"scope": "node"},
            blade_scope="node", blade_target="network", blade_action="drop",
        ))

    def _drift_decision(self, eff_scope: str):
        eff = EffectiveTarget(
            scope=eff_scope,
            namespace="",
            names=("node-x",) if eff_scope == "node" else (),
            confidence=ConfidenceLevel.HIGH,
            raw_command="kubectl debug node/node-x",
        )
        return {
            "verdict": GuardVerdict.REJECT_DRIFT.value,
            "reason": "resource selection drift",
            "suggestion": "approved target: scope=node",
            "is_hard_floor": False,
            "effective": eff,
        }

    def test_node_drift_lists_approved_nodes(self):
        msg = _format_rejection_for_llm(
            self._drift_decision("node"), False, self._node_approved(),
        )
        assert "Approved nodes: [node-a, node-b]" in msg
        assert "kubectl debug node/<approved-node>" in msg

    def test_non_node_effective_drift_gets_no_node_hint(self):
        # Namespace drift (effective scope != node) must not spuriously emit
        # the node-selection hint.
        msg = _format_rejection_for_llm(
            self._drift_decision("pod"), False, self._node_approved(),
        )
        assert "Approved nodes:" not in msg

    def test_banned_verdict_gets_no_node_hint(self):
        decision = self._drift_decision("node")
        decision["verdict"] = GuardVerdict.REJECT_BANNED.value
        msg = _format_rejection_for_llm(decision, False, self._node_approved())
        assert "Approved nodes:" not in msg


class TestCarrierRejectReasonIsTruthful:
    """Every carrier gate must report ITSELF, never another gate's cause.

    Regression for task-866648cc. Resolution used to answer ``tuple | None``, so
    the screener could only guess which of ~12 gates fired and hard-coded "the
    exec target is not an approved debug pod ... neither registered ... nor
    live-discoverable" for all of them. The drill's debug pod WAS registered and
    resolvable (a read-only probe through the same artifact cleared fine); the
    real gate was a missing self-reversal on an otherwise valid
    ``tc qdisc add ... netem loss 100%``. Told the pod was at fault, the model
    spent nine minutes re-proving the pod (phase Running, ``privileged: true``)
    and never revisited the command.

    So each case below asserts BOTH directions: the true gate's wording is
    present, AND the wording of the gates that did NOT fire is absent. The
    negative half is what actually catches a misattribution — a reason can be
    specific and still be a lie.
    """

    # Signature phrases, one per gate. Deliberately short so a reword of the
    # surrounding sentence does not break the test, while a change of WHICH
    # gate is being reported does.
    _SIG_NOT_REGISTERED = "is not a debug-pod artifact registered by this task"
    _SIG_FAMILY = "fault family"
    _SIG_BOUNDED = "does not self-recover"
    _SIG_PRIVILEGED = "as NOT privileged when it was created"
    _SIG_NOT_ACTIVE = "not 'active'"
    _SIG_NO_NODE = "does not pin it to a node"

    async def _screen(self, host_command: str, artifacts: list[dict]) -> str:
        """Run the screener and return the rejection text shown to the LLM."""
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                _ai_with_tool_call("kubectl", {
                    "subcommand": "exec",
                    "v_args": f"node-debugger-node-a-abc12 -n kubewiz -- {host_command}",
                }),
            ],
            "approved_target": _approved_node_network(),
            "execution_artifacts": artifacts,
            "task_id": "task-1",
        }
        # Registered carriers resolve in-memory; the liveness window is left at
        # its default so a fresh artifact needs no live probe. Discovery is
        # patched to a definite "not found" so the POD_NOT_REGISTERED case does
        # not depend on cluster access.
        with patch(
            _DISCOVER,
            new=AsyncMock(return_value=CarrierResolution.reject(
                CarrierRejectReason.POD_NOT_DISCOVERABLE,
                "pod 'node-debugger-node-a-abc12' is not registered by this "
                "task and a live read could not confirm it: NotFound",
            )),
        ), patch(_PROBE, new=AsyncMock(return_value=True)):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        return str(delta["messages"][0].content)

    async def _screen_with_live_read(
        self, host_command: str, artifacts: list[dict], meta: tuple,
    ) -> str:
        """Same, but let the REAL discovery path run against a stubbed read.

        ``_screen`` stubs discovery wholesale, so it cannot exercise the gates
        discovery itself produces (POD_NOT_DISCOVERABLE / NODE_NOT_APPROVED).
        Here only the cluster read is stubbed.
        """
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                _ai_with_tool_call("kubectl", {
                    "subcommand": "exec",
                    "v_args": f"node-debugger-node-a-abc12 -n kubewiz -- {host_command}",
                }),
            ],
            "approved_target": _approved_node_network(),
            "execution_artifacts": artifacts,
            "task_id": "task-1",
        }
        with patch(
            "chaos_agent.tools.kubectl._debug_pod_metadata",
            new=AsyncMock(return_value=meta),
        ), patch(_PROBE, new=AsyncMock(return_value=True)):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        return str(delta["messages"][0].content)

    @pytest.mark.asyncio
    async def test_family_mismatch_blames_the_command_not_the_pod(self):
        # A registered, privileged, active carrier + a MUTATING command whose
        # fault family (disk) does not match the approved family (network).
        # This must be rejected, and the rejection must blame the command's
        # family, not the pod. (Earlier this test used `chroot /host crictl ps`
        # — but that is a READ-ONLY probe and must PASS, not be rejected; see
        # ``test_readonly_host_escape_probe_passes``. A real family mismatch
        # needs a mutation of the wrong family, hence ``fallocate`` = disk.)
        msg = await self._screen(
            "chroot /host fallocate -l 1G /tmp/fill", [_debug_artifact()],
        )
        assert self._SIG_FAMILY in msg
        # The pod is fine — saying otherwise is what caused the nine-minute loop.
        assert self._SIG_NOT_REGISTERED not in msg
        assert self._SIG_PRIVILEGED not in msg
        assert self._SIG_BOUNDED not in msg
        # And the fix offered must be about the command's shape.
        assert "APPROVED fault family" in msg

    @pytest.mark.asyncio
    async def test_readonly_host_escape_probe_passes(self):
        # A host-entry wrapper (chroot/nsenter) around a READ-ONLY inner command
        # is a diagnostic probe used to locate the target before injecting — NOT
        # an injection. It must PASS, even with no registered carrier, because
        # it mutates nothing. Regression guard for task-3a360709 [139], where
        # `chroot /host crictl ps` was wrongly REJECT_BANNED as an "uncleared
        # host-escape primitive", forcing the model to detour.
        for probe in (
            "chroot /host crictl ps --name app -o json",
            "chroot /host crictl inspect abc123",
            "chroot /host ps aux",
            "chroot /host cat /proc/net/dev",
            "chroot /host ip addr show",
            "nsenter -t 1 -n ip addr show",
            "nsenter -t 1 -m -- cat /proc/mounts",
        ):
            state = {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[{
                            "id": "c1", "name": "kubectl",
                            "args": {"subcommand": "exec",
                                     "v_args": f"dbg -n default -- {probe}"},
                        }],
                    ),
                ],
                "approved_target": _approved_node_network(),
                "execution_artifacts": [],  # no carrier — readonly needs none
                "task_id": "task-1",
            }
            delta = await tool_screener(state)
            assert delta.get("screener_route", SCREENER_ROUTE_PASS) == \
                SCREENER_ROUTE_PASS, f"read-only probe wrongly blocked: {probe}"

    @pytest.mark.asyncio
    async def test_mutating_host_escape_still_rejected_without_carrier(self):
        # The counterpart to the readonly test: a MUTATING host-escape command
        # with no registered carrier must still be rejected. Confirms the
        # readonly pass-through did not open a hole for real injections.
        for mutation in (
            "chroot /host iptables -A OUTPUT -j DROP",
            "chroot /host tc qdisc add dev eth0 root netem loss 30%",
            "chroot /host sh -c 'iptables -A OUTPUT -j DROP'",
            "chroot /host cat /etc/passwd > /host/x",
            "chroot /host crictl stop abc123",
        ):
            state = {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[{
                            "id": "c1", "name": "kubectl",
                            "args": {"subcommand": "exec",
                                     "v_args": f"dbg -n default -- {mutation}"},
                        }],
                    ),
                ],
                "approved_target": _approved_node_network(),
                "execution_artifacts": [],
                "task_id": "task-1",
            }
            with patch(
                _DISCOVER,
                new=AsyncMock(return_value=CarrierResolution.reject(
                    CarrierRejectReason.POD_NOT_DISCOVERABLE,
                    "pod 'dbg' is not registered by this task",
                )),
            ), patch(_PROBE, new=AsyncMock(return_value=True)):
                delta = await tool_screener(state)
            assert delta["screener_route"] != SCREENER_ROUTE_PASS, \
                f"mutating host-escape wrongly passed: {mutation}"

    @pytest.mark.asyncio
    async def test_unbounded_mutation_blames_the_missing_reversal(self):
        # task-866648cc [161]: correct family, correct carrier, no reversal.
        # This was one `&& sleep N && <inverse>` away from passing.
        msg = await self._screen(
            "nsenter -t 1830491 -n -- tc qdisc add dev eth0 root netem loss 100%",
            [_debug_artifact()],
        )
        assert self._SIG_BOUNDED in msg
        assert self._SIG_NOT_REGISTERED not in msg
        # The model must be told the command itself is accepted.
        assert "only the missing reversal blocks it" in msg
        # And the reversal must be named for THIS family, not described in the
        # abstract: the wording is forwarded from ``recoverability.assess``,
        # which is the only layer that knows a network fault wants
        # ``iptables -D`` / ``tc qdisc del`` while disk wants ``truncate -s 0``.
        assert "tc qdisc del" in msg

    @pytest.mark.asyncio
    async def test_unregistered_pod_still_blames_the_pod(self):
        """Nothing registered AND the live read cannot confirm it.

        Note which gate actually answers: POD_NOT_REGISTERED is retryable, so
        discovery always runs after it and its verdict REPLACES the registered
        one. The wording the model sees here is therefore
        POD_NOT_DISCOVERABLE's — POD_NOT_REGISTERED's own detail only ever
        reaches the logs and the ``carrier_gate`` field. Both blame the pod, so
        the model is not misled either way.
        """
        msg = await self._screen(
            _bounded_network_host_command(), [],
        )
        assert "not registered by this task" in msg
        assert self._SIG_BOUNDED not in msg
        assert self._SIG_FAMILY not in msg

    @pytest.mark.asyncio
    async def test_no_node_binding_blames_the_missing_node(self):
        # Registered and privileged, but the artifact does not pin a NODE, so
        # which host the exec would enter is unknown. Must not be reported as a
        # command problem.
        msg = await self._screen(
            _bounded_network_host_command(),
            [_debug_artifact() | {"target": {"scope": "pod", "name": "node-a"}}],
        )
        assert self._SIG_NO_NODE in msg
        assert self._SIG_BOUNDED not in msg
        assert self._SIG_FAMILY not in msg

    @pytest.mark.asyncio
    async def test_undiscoverable_pod_blames_the_live_read(self):
        # Discovery's own gate: nothing registered and the cluster read says
        # the pod is absent. The reason must name the READ, not the command.
        msg = await self._screen_with_live_read(
            _bounded_network_host_command(), [], ({}, "NotFound"),
        )
        assert "a live read could not confirm it" in msg
        assert self._SIG_BOUNDED not in msg
        assert self._SIG_FAMILY not in msg

    @pytest.mark.asyncio
    async def test_node_outside_approval_blames_the_node(self):
        # Discovery found a real privileged debug pod — on the WRONG node. The
        # reason must name the node and the approved set, so the model can
        # re-target instead of re-litigating the pod.
        msg = await self._screen_with_live_read(
            _bounded_network_host_command(), [],
            ({"name": "node-debugger-node-a-abc12", "namespace": "kubewiz",
              "uid": "u9", "node": "node-ZZZ", "privileged": True}, ""),
        )
        assert "node-ZZZ" in msg
        assert "not in the approved target set" in msg
        assert "node-a" in msg  # the approved set is spelled out
        assert self._SIG_BOUNDED not in msg
        assert self._SIG_FAMILY not in msg

    @pytest.mark.asyncio
    async def test_unprivileged_carrier_blames_privilege(self):
        artifact = _debug_artifact()
        artifact["privileged"] = False
        msg = await self._screen(_bounded_network_host_command(), [artifact])
        assert self._SIG_PRIVILEGED in msg
        assert self._SIG_BOUNDED not in msg
        assert self._SIG_FAMILY not in msg

    @pytest.mark.asyncio
    async def test_cleaned_carrier_blames_carrier_status(self):
        artifact = _debug_artifact()
        artifact["status"] = "cleaned"
        msg = await self._screen(_bounded_network_host_command(), [artifact])
        # A cleaned artifact is skipped by ``find_active_debug_pod``, so the
        # gate that fires is "no ACTIVE registered carrier matches" — which the
        # live-discovery fallback then re-confirms against the cluster. Either
        # way the answer must be about the CARRIER, never about the command.
        assert "not registered by this task" in msg or self._SIG_NOT_ACTIVE in msg
        assert self._SIG_BOUNDED not in msg
        assert self._SIG_FAMILY not in msg

    @pytest.mark.asyncio
    async def test_recovery_armed_carrier_blames_pending_rollback(self):
        # An armed rollback is a real, distinct gate: the carrier is healthy and
        # registered, but a second mutation must wait for the first to expire.
        artifact = _debug_artifact()
        artifact["status"] = "recovery_armed"
        artifact["recovery_deadline_epoch"] = time.time() + 600
        msg = await self._screen(_bounded_network_host_command(), [artifact])
        assert self._SIG_NOT_ACTIVE in msg
        assert "rollback timer is already armed" in msg
        # Not a command problem, and not an unregistered pod.
        assert self._SIG_BOUNDED not in msg
        assert self._SIG_FAMILY not in msg
        assert self._SIG_NOT_REGISTERED not in msg

    @pytest.mark.asyncio
    async def test_wrong_registered_family_names_both_families(self):
        # Carrier registered for a disk drill, command is a network fault.
        msg = await self._screen(
            _bounded_network_host_command(), [_debug_artifact(family="disk")],
        )
        assert self._SIG_FAMILY in msg
        assert "disk" in msg and "network" in msg
        assert self._SIG_NOT_REGISTERED not in msg

    @pytest.mark.asyncio
    async def test_stale_carrier_blames_identity_not_registration(self):
        # Registered + outside the liveness window + live re-read disagrees.
        settings.carrier_liveness_ttl_seconds = 120
        artifact = _debug_artifact()
        artifact["confirmed_live_epoch"] = time.time() - 3600
        state = {
            "messages": [
                _ai_with_tool_call("kubectl", {
                    "subcommand": "exec",
                    "v_args": (
                        "node-debugger-node-a-abc12 -n kubewiz -- "
                        f"{_bounded_network_host_command()}"
                    ),
                }),
            ],
            "approved_target": _approved_node_network(),
            "execution_artifacts": [artifact],
            "task_id": "task-1",
        }
        settings.target_guard_enforcing = True
        with patch(_PROBE, new=AsyncMock(return_value=False)):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        msg = str(delta["messages"][0].content)
        assert "no longer matches the identity registered" in msg
        # It WAS registered — the failure is identity drift, not absence.
        assert self._SIG_NOT_REGISTERED not in msg
        assert self._SIG_BOUNDED not in msg

    @pytest.mark.asyncio
    async def test_crashed_reprobe_says_unconfirmed_not_mismatched(self):
        """A re-read that RAISED must not be reported as a re-read that DISAGREED.

        Both fail closed, so a verdict-only assertion cannot tell them apart —
        which is exactly how the original defect survived. "The pod changed" and
        "we could not look" are different facts and only one was observed; the
        first fix of this bug reported the former for both.
        """
        settings.carrier_liveness_ttl_seconds = 0  # force the re-probe path
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                _ai_with_tool_call("kubectl", {
                    "subcommand": "exec",
                    "v_args": (
                        "node-debugger-node-a-abc12 -n kubewiz -- "
                        f"{_bounded_network_host_command()}"
                    ),
                }),
            ],
            "approved_target": _approved_node_network(),
            "execution_artifacts": [_debug_artifact()],
            "task_id": "task-1",
        }
        with patch(_PROBE, new=AsyncMock(side_effect=RuntimeError("boom"))):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        msg = str(delta["messages"][0].content)
        assert "could not be completed" in msg
        assert "identity is unconfirmed" in msg
        # Must NOT claim an observation that never happened.
        assert "no longer matches the identity registered" not in msg

    @pytest.mark.asyncio
    async def test_unparseable_exec_fix_is_about_shape_not_recovery(self):
        """Cause and fix must describe the same condition.

        A malformed exec (no pod name) is a SYNTAX problem. Falling back to the
        generic carrier/self-recovery template would point the model at the
        wrong subsystem — the same contradiction that made task-866648cc trust
        the wrong half of its rejection.
        """
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                _ai_with_tool_call("kubectl", {
                    "subcommand": "exec",
                    "v_args": f"-n kubewiz -- {_bounded_network_host_command()}",
                }),
            ],
            "approved_target": _approved_node_network(),
            "execution_artifacts": [_debug_artifact()],
            "task_id": "task-1",
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        msg = str(delta["messages"][0].content)
        assert "could not be parsed into" in msg
        assert "Re-issue the exec in the shape the guard can read" in msg
        # The generic catch-all (carrier + self-recovery) must not appear.
        assert "ANY accepted primitive" not in msg

    @pytest.mark.asyncio
    async def test_missing_approval_fix_is_about_confirming_intent(self):
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                _ai_with_tool_call("kubectl", {
                    "subcommand": "exec",
                    "v_args": (
                        "node-debugger-node-a-abc12 -n kubewiz -- "
                        f"{_bounded_network_host_command()}"
                    ),
                }),
            ],
            "approved_target": {},  # nothing frozen yet
            "execution_artifacts": [_debug_artifact()],
            "task_id": "task-1",
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        msg = str(delta["messages"][0].content)
        assert "no approved target is on record" in msg
        assert "Confirm the fault intent first" in msg
        assert "ANY accepted primitive" not in msg


class TestLiveDiscoveryOnlyRetriesRecoverableGates:
    """A command-level verdict must not trigger a live cluster read.

    Two reasons. Correctness: ``discover_unregistered_carrier`` synthesises an
    artifact with an EMPTY ``operation_family``, so retrying a FAMILY_MISMATCH
    through it would skip the registered carrier's family check — a bypass.
    Cost: under an in-progress network fault that extra in-band ``kubectl get
    pod`` rides the very API path the fault is severing.
    """

    @pytest.mark.asyncio
    async def test_family_mismatch_does_not_probe_the_cluster(self):
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                _ai_with_tool_call("kubectl", {
                    "subcommand": "exec",
                    "v_args": (
                        "node-debugger-node-a-abc12 -n kubewiz -- "
                        # A MUTATING command of the wrong family (disk vs the
                        # approved network). A read-only probe like
                        # ``crictl ps`` would (correctly) PASS and never reach
                        # the family gate — see
                        # ``test_readonly_host_escape_probe_passes``.
                        "chroot /host fallocate -l 1G /tmp/fill"
                    ),
                }),
            ],
            "approved_target": _approved_node_network(),
            "execution_artifacts": [_debug_artifact()],
            "task_id": "task-1",
        }
        discover = AsyncMock()
        with patch(_DISCOVER, new=discover), patch(
            _PROBE, new=AsyncMock(return_value=True),
        ):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        discover.assert_not_called()

    @pytest.mark.asyncio
    async def test_unregistered_pod_does_probe_the_cluster(self):
        # The race this fallback exists for: ``kubectl debug`` timed out before
        # emitting its metadata marker, so nothing is registered but the pod is
        # live and legitimate.
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                _ai_with_tool_call("kubectl", {
                    "subcommand": "exec",
                    "v_args": (
                        "node-debugger-node-a-abc12 -n kubewiz -- "
                        f"{_bounded_network_host_command()}"
                    ),
                }),
            ],
            "approved_target": _approved_node_network(),
            "execution_artifacts": [],
            "task_id": "task-1",
        }
        resolved = CarrierResolution.allow(
            EffectiveTarget(
                scope="node", namespace="", names=("node-a",),
                blade_target="network", confidence=ConfidenceLevel.HIGH,
                raw_command="kubectl exec ...",
            ),
            {"status": "active", "privileged": True,
             "target": {"scope": "node", "name": "node-a"}},
        )
        discover = AsyncMock(return_value=resolved)
        with patch(_DISCOVER, new=discover), patch(
            _PROBE, new=AsyncMock(return_value=False),
        ):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        discover.assert_awaited_once()
