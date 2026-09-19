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
    SCREENER_ROUTE_FAIL,
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
from chaos_agent.agent.target_guard.mechanism_writes import MechanismWriteEntry
from chaos_agent.agent.providers.registry import FaultProviderRegistry
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
    orig_faultdrill = settings.faultdrill_enabled
    yield
    settings.target_guard_enforcing = orig_enforce
    settings.skill_script_default_allow = orig_skill
    settings.carrier_liveness_ttl_seconds = orig_ttl
    settings.faultdrill_enabled = orig_faultdrill


def _approved_pod_a_in_ns():
    """Approved target: ns/pod-a + blade target cpu."""
    return freeze_approved_target(
        target={"namespace": "ns", "names": ["pod-a"]},
        params={"scope": "pod"},
        fault_scope="pod", fault_target="cpu", fault_action="fullload",
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
        fault_scope="node", fault_target="network", fault_action="drop",
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
                fault_scope=None, fault_target="cpu", fault_action=None,
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
    async def test_compound_escape_payload_is_banned_for_approved_pod(self):
        # R26/G-10 end-to-end tooth: the exec target IS the approved
        # pod, so identity drift cannot fire — the ONLY thing standing
        # between the compound payload and a full pass is the escape
        # legislation. Pre-fix probe (live, this repo): every direct /
        # wrapped escape form → REJECT_BANNED, while the compound form
        # (readonly head + escape primitive past the ``;``) sailed
        # through with route=pass — the head-only peek never saw the
        # nsenter, the payload classified as a plain pod mutation, and
        # the identity match passed it. Segment-level escape detection
        # must route it into carrier resolution, where the unregistered
        # approved pod fails closed.
        settings.target_guard_enforcing = True
        state = {
            "messages": [_ai_with_tool_call("kubectl", {
                "subcommand": "exec",
                "v_args": (
                    "pod-a -n ns -- "
                    "sh -c 'cat /etc/hosts; nsenter -t 1 -m sh'"
                ),
            })],
            "approved_target": _approved_pod_a_in_ns(),
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        assert "REJECT_BANNED" in delta["messages"][0].content

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "v_args",
        [
            "pod-a -n ns -- sh -c 'blade destroy aa11bb22cc33dd44; $(nsenter -t 1 -m sh)'",
            "pod-a -n ns -- sh -c 'cat /etc/hosts; (nsenter -t 1 -m sh)'",
            "pod-a -n ns -- sh -c 'cat /etc/hosts; \"$(nsenter -t 1 -m sh)\"'",
            "pod-a -n ns -- sh -c 'case x in a) nsenter -t 1 -m sh;; esac'",
        ],
        ids=["cmdsub-tail", "subshell", "dq-wrapped", "case-branch"],
    )
    async def test_structure_hidden_escape_payload_is_banned(self, v_args):
        # R33/G-12 end-to-end tooth: same approved-pod identity-match
        # setup as the G-10 tooth above, but the escape primitive rides
        # a shell STRUCTURE (command substitution / subshell / double-
        # quoted substitution) instead of a bare ';'. Pre-fix probe
        # (live): all forms passed end-to-end — the splitter's six-
        # separator vocabulary never produced a primitive-headed
        # segment, so the escape legislation never fired.
        settings.target_guard_enforcing = True
        state = {
            "messages": [_ai_with_tool_call("kubectl", {
                "subcommand": "exec", "v_args": v_args,
            })],
            "approved_target": _approved_pod_a_in_ns(),
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY, (
            "an escape primitive hidden in a shell structure must reach "
            "the same REJECT_BANNED as one past a bare ';'"
        )
        assert "REJECT_BANNED" in delta["messages"][0].content

    @pytest.mark.asyncio
    async def test_arg_tail_escape_parameter_text_is_not_rejected(self):
        # R35/G-13 end-to-end tooth: the closer's PARAMETER TAIL is the
        # host command's argument text — nsenter as echo's parameter
        # must not trigger the escape reject. Pre-fix probe (live):
        # scope=__escape__ → route=RETRY with REJECT_BANNED naming
        # 'nsenter' (a legal form killed by the G-12 closer split).
        settings.target_guard_enforcing = True
        state = {
            "messages": [_ai_with_tool_call("kubectl", {
                "subcommand": "exec",
                "v_args": (
                    "pod-a -n ns -- sh -c "
                    "'echo done $(date) nsenter -t 1 -m sh'"
                ),
            })],
            "approved_target": _approved_pod_a_in_ns(),
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS, (
            "an escape primitive in an argument tail is parameter text "
            "— the legal form must pass like its no-substitution twin"
        )

    @pytest.mark.asyncio
    async def test_heredoc_body_escape_text_is_not_rejected(self):
        # R36/G-14 end-to-end tooth: a restore script WRITTEN via the
        # carrier-blessed quoted-heredoc form, whose body merely
        # MENTIONS nsenter, is a legal pod-scoped write. Pre-fix probe
        # (live): scope=__escape__ → route=RETRY with reject_banned
        # naming 'nsenter' (text, not execution).
        settings.target_guard_enforcing = True
        state = {
            "messages": [_ai_with_tool_call("kubectl", {
                "subcommand": "exec",
                "v_args": (
                    "pod-a -n ns -- sh -c "
                    "'cat > /tmp/restore.sh <<\"EOF\"\n"
                    "nsenter -t 1 -m sh -c \"umount /tmp/stale-mount\"\n"
                    "EOF\necho done'"
                ),
            })],
            "approved_target": _approved_pod_a_in_ns(),
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS, (
            "a heredoc body is stdin text — an escape word inside it "
            "must not reject the legal write form"
        )

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
                "labels": {}, "fault_target": "cpu", "fault_action": "fullload",
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
    @patch("chaos_agent.agent.nodes.planning.tool_screener.interrupt", return_value="approved")
    async def test_drift_approved_different_kind_passes_without_rewriting_spec(self, _mock):
        """task-51193464 regression: approving a DIFFERENT-kind operation
        (creating the PVC the victim pod needs, under a node-scope approval —
        node secondary_scopes cover pod/deployment but NOT pvc, so this is a
        genuine drift card) is a one-shot pass-through, NOT a target change.
        Rewriting only names would freeze the corrupt hybrid anchor
        (scope=node, names=[<pvc-name>]) that turned the REAL node injection
        into yet another drift card. The anchor stays as confirmed."""
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                _ai_with_tool_call("kubectl", {
                    "subcommand": "patch",
                    "v_args": (
                        "pvc terminating-demo-claim -n ns "
                        "-p '{\"spec\":{}}'"
                    ),
                }, call_id="tc-1"),
            ],
            "approved_target": _approved_node_network(),
            "fault_spec": {
                "namespace": "", "scope": "node", "names": ["node-a"],
                "labels": {}, "fault_target": "network", "fault_action": "drop",
                "params": {}, "params_flags": [], "duration_seconds": 0,
                "source": "test", "user_description": "",
            },
        }
        delta = await tool_screener(state)
        # Approved → THIS call is allowed through.
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        assert delta["drift_reject_count"] == 0
        _mock.assert_called_once()
        # But the spec/anchor rewrite is SKIPPED: approving an auxiliary
        # resource operation is not a target change.
        assert "fault_spec" not in delta
        assert "approved_target" not in delta

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
            # CLI mode mirrors the #56 live scene: no interactive drift card,
            # so the second drift is the auto-reject hard stop.
            "interaction_mode": "cli",
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
        # W-56-5 (defect a): the hard stop routes FAIL → the reject terminal
        # node. The former RETRY here was a ghost termination: the graph kept
        # running, the fail error leaked into the next attempt, and the
        # terminal renderer stitched two stale reasons together (#56).
        assert delta["screener_route"] == SCREENER_ROUTE_FAIL
        assert route_after_screener({"screener_route": SCREENER_ROUTE_FAIL}) == "reject"
        # fail_state sets error field
        assert "error" in delta
        assert "failure_detail" in delta
        # The hard stop lands its own fresh safety_reason so the reject node's
        # single-source attribution names THIS termination (defect c).
        assert "human" in delta["safety_reason"]

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
                "labels": {}, "fault_target": "cpu", "fault_action": "fullload",
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

    @pytest.mark.asyncio
    async def test_readonly_plus_banned_defers_cleared_sibling(self):
        """B43: a screener-cleared sibling must NOT receive rejection text.

        The batch stays atomic (nothing executes — retry route), but the
        fabricated ToolMessage for the cleared call must say DEFERRED.
        Case-32: update_progress answered with "[target_guard] READONLY —
        adjust the tool_call and retry" read to the LLM like a guard
        verdict against the tool itself, delaying its re-issue by four
        iterations.
        """
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                AIMessage(content="", tool_calls=[
                    {"name": "update_progress", "args": {
                        "note": "mid-execution checkpoint",
                    }, "id": "tc-meta"},
                    {"name": "kubectl", "args": {
                        "command": ["apply", "-f", "x.yaml"],
                    }, "id": "tc-bad"},
                ]),
            ],
            "approved_target": _approved_pod_a_in_ns(),
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        by_id = {tm.tool_call_id: tm for tm in delta["messages"]}
        assert set(by_id) == {"tc-meta", "tc-bad"}
        # Cleared sibling: deferred, NOT a rejection — no guard verdict,
        # no "adjust and retry" instructions for a call that was fine.
        meta_tm = by_id["tc-meta"]
        assert "DEFERRED" in meta_tm.content
        assert "NOT rejected" in meta_tm.content
        assert "target_guard" not in meta_tm.content
        assert "READONLY" not in meta_tm.content
        # Rejected sibling: rejection rendering unchanged.
        assert "REJECT_BANNED" in by_id["tc-bad"].content

    @pytest.mark.asyncio
    async def test_finish_execution_passes_as_readonly(self):
        """B81: the prompt-taught STOP call must clear the screener.

        Case-36 retest: the model called update_progress + finish_execution
        in one batch (exactly this shape) at the end of a finished execution;
        the guard answered the finish_execution half with REJECT_UNKNOWN
        ("unrecognized tool, default-deny") because the classifier's
        control-tool whitelist never heard of it — the model then improvised
        a update_progress phase-write downgrade over three wasted rounds.
        The classifier whitelist now carries it; this pins the end-to-end
        shape: the whole batch passes, no fabricated ToolMessages.
        """
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                AIMessage(content="", tool_calls=[
                    {"name": "update_progress", "args": {
                        "log_append": [{"event": "final ledger"}],
                    }, "id": "tc-ledger"},
                    {"name": "finish_execution", "args": {
                        "summary": "all approved mutation steps issued",
                    }, "id": "tc-stop"},
                ]),
            ],
            "approved_target": _approved_pod_a_in_ns(),
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        assert not delta.get("messages")


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
        fault_scope="node", fault_target="disk", fault_action="fill",
    )


def _approved_node_process():
    return freeze_approved_target(
        target={"namespace": "", "names": ["node-a"]},
        params={"scope": "node"},
        fault_scope="node", fault_target="process", fault_action="stop",
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

    def test_process_bounded_crictl_stop_loop_is_bounded(self):
        # The terminate-style sustained kill the skill documents (path B):
        # a rounds-capped crictl-stop loop armed with a timer that pkills
        # the loop. Double-bounded, so the carrier gate clears it.
        cmd = (
            "chroot /host sh -c 'systemd-run --on-active=60s --unit=stoploop "
            "sh -c \"pkill -f crictl-stoploop\" && for i in 1 2 3 4; do "
            "crictl stop -t 0 abc123; sleep 15; done'"
        )
        assert host_operation_has_bounded_recovery(cmd, "process") is True

    def test_process_one_shot_crictl_stop_is_bounded_discrete(self):
        # Discrete mode: an instantaneous container stop the kubelet
        # self-heals — no window to arm, nothing to undo.
        cmd = "chroot /host sh -c 'crictl stop -t 0 abc123'"
        assert host_operation_has_bounded_recovery(cmd, "process") is True

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
    async def test_process_bounded_crictl_stop_loop_passes(self):
        """End-to-end regression for task inject-e47de3e8.

        The skill's documented terminate-style sustained process kill — a
        rounds-capped ``crictl stop`` loop armed with a systemd-run timer
        whose payload pkills the loop — was rejected at BOTH carrier gates
        (no ``crictl stop`` family mapping; recoverability knew only
        suspend/resume), burning six minutes of carrier detours. With the
        family mapping and the bounded-loop recognition in place, the same
        shape clears the screener.
        """
        settings.target_guard_enforcing = True
        cmd = (
            "node-debugger-node-a-abc12 -n kubewiz -- chroot /host sh -c "
            "'systemd-run --on-active=60s --unit=stoploop sh -c \"pkill -f "
            "crictl-stoploop\" && for i in 1 2 3 4; do crictl stop -t 0 "
            "abc123; sleep 15; done'"
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
    async def test_process_one_shot_crictl_stop_is_rejected(self):
        # One-shot terminate has no loop to bound and no early-end handle —
        # it keeps failing closed even though the family now maps.
        settings.target_guard_enforcing = True
        cmd = (
            "node-debugger-node-a-abc12 -n kubewiz -- chroot /host sh -c "
            "'crictl stop -t 0 abc123'"
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

    @pytest.mark.asyncio
    async def test_network_timeout_bounded_listener_passes(self):
        """A port-occupation fault as a timeout-bounded ``nc -l`` listener.

        The port is held only while the listener runs — ending the process
        IS the recovery, no inverse rule exists. The documented skill form
        (Node_网络故障_节点端口占用) clears the screener; the unbounded
        listener keeps failing closed at the recoverability gate.
        """
        settings.target_guard_enforcing = True
        cmd = (
            "node-debugger-node-a-abc12 -n kubewiz -- chroot /host sh -c "
            "'timeout 300 nc -l -p 8080 -k'"
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

    @pytest.mark.asyncio
    async def test_disk_timeout_bounded_burn_passes(self):
        """An IO-pressure burn wrapped in timeout(1) self-terminates.

        The wrapping timeout kills the burner, so the pressure self-ends —
        no reclaim pairing is needed (fills do NOT qualify for this bound).
        """
        settings.target_guard_enforcing = True
        cmd = (
            "node-debugger-node-a-abc12 -n kubewiz -- chroot /host sh -c "
            "'timeout 300 sh -c \"while true; do dd if=/dev/zero "
            "of=/host/tmp/burn bs=1M count=512 oflag=direct; done\"'"
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
    async def test_process_timer_armed_freezer_suspend_passes(self):
        """The documented cgroup-freezer suspend: THAW timer armed BEFORE
        the FROZEN write, aimed at the same freezer.state."""
        settings.target_guard_enforcing = True
        freezer = "/sys/fs/cgroup/freezer/kubepods/abc123/freezer.state"
        cmd = (
            "node-debugger-node-a-abc12 -n kubewiz -- chroot /host sh -c "
            "'systemd-run --on-active=120s --unit=blade-thaw sh -c \"echo "
            f"THAWED > {freezer}\"; sleep 1; echo FROZEN > {freezer}'"
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
    async def test_process_one_shot_crictl_stop_discrete_passes(self):
        """Discrete mode through the exec-carrier channel: an instantaneous
        container stop the kubelet self-heals. Regression anchor: task
        inject-ffb519da ran the same mutation via kubectl-debug-direct;
        both channels must judge the same command shape identically."""
        settings.target_guard_enforcing = True
        cmd = (
            "node-debugger-node-a-abc12 -n kubewiz -- chroot /host sh -c "
            "'crictl stop -t 0 abc123'"
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
    async def test_process_bare_freezer_freeze_is_rejected(self):
        # A freeze with no armed thaw has no rescue — keep failing closed.
        settings.target_guard_enforcing = True
        cmd = (
            "node-debugger-node-a-abc12 -n kubewiz -- chroot /host sh -c "
            "'echo FROZEN > /sys/fs/cgroup/freezer/kubepods/abc123/"
            "freezer.state'"
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
            fault_target="network", confidence=ConfidenceLevel.HIGH,
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
            fault_scope="node", fault_target="network", fault_action="drop",
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
            fault_scope="node", fault_target="network", fault_action="drop",
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


class TestMechanismBanReplanGuidance:
    """A mechanism-banned rejection must point to replan, not 'adjust & retry'.

    Regression for task-190c94e8: the plan's injection mechanism was creating a
    holder Pod via ``kubectl apply``. The guard bans workload kinds, but the
    rejection carried a non-empty suggestion (the accepted-kind list), which the
    feedback layer read as a reshapeable form issue and appended "adjust the
    tool_call as above and retry". The model then spiralled through doomed
    variants (positional args, PV patch) instead of switching mechanism. A
    mechanism ban has NO compliant reshape of the same call — the honest guidance
    is to request_replan.
    """

    def _banned_decision(self, mechanism_banned: bool, suggestion: str):
        eff = EffectiveTarget(
            scope="__banned__", namespace="",
            confidence=ConfidenceLevel.HIGH,
            raw_command="kubectl apply -f -",
            mechanism_banned=mechanism_banned,
            reject_detail="the manifest contains a non-whitelisted resource kind (Pod)",
            reject_suggestion=suggestion,
        )
        return {
            "verdict": GuardVerdict.REJECT_BANNED.value,
            "reason": eff.reject_detail,
            "suggestion": suggestion,
            "is_hard_floor": mechanism_banned,
            "effective": eff,
        }

    def test_mechanism_ban_says_replan_not_retry(self):
        msg = _format_rejection_for_llm(
            self._banned_decision(True, "Accepted kinds: configmap, secret."),
            False, None,
        )
        assert "request_replan" in msg
        assert "banned by policy" in msg
        # The misleading reshape guidance must be suppressed.
        assert "adjust the tool_call" not in msg
        assert "not a dead-end" not in msg

    def test_form_issue_still_says_adjust_and_retry(self):
        # A genuine form issue (no mechanism ban) keeps the retry guidance so the
        # model's exploration space is not collapsed by mistake.
        msg = _format_rejection_for_llm(
            self._banned_decision(False, "Use an approved debug pod instead."),
            False, None,
        )
        assert "not a dead-end" in msg
        assert "adjust the tool_call" in msg
        assert "request_replan" not in msg


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
                fault_target="network", confidence=ConfidenceLevel.HIGH,
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


# ---------------------------------------------------------------------------
# Armed-before-inject gate (inject-cc2d5080)
# ---------------------------------------------------------------------------


def _approved_workload_deploy_a():
    """Workload-scope approval: secondary_scopes statically include the
    carrier RBAC family, so this is the net where the gate lives."""
    return freeze_approved_target(
        target={"namespace": "ns", "names": ["deploy-a"]},
        params={"scope": "deployment"},
        fault_scope="deployment", fault_target="pod", fault_action="fill",
    )


def _carrier_artifact(status: str = "active") -> dict:
    return {
        "artifact_id": "recovery_carrier:ns/drill-rc-x",
        "type": "recovery_carrier",
        "status": status,
        "task_id": "task-1",
        "name": "drill-rc-x",
        "namespace": "ns",
        "operation_family": "recovery_carrier",
    }


class TestArmedBeforeInjectGate:
    """A kubectl object-write injection under a write-set that admits the
    carrier family must not run while no recovery carrier is registered —
    the inject-cc2d5080 shape (carrier refused → forced injection → fault
    with no timer armed). The rejection is retryable form guidance, not a
    mechanism ban: stacking the carrier IS the reshape."""

    @pytest.mark.asyncio
    async def test_object_write_without_carrier_is_rejected(self):
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                _ai_with_tool_call("kubectl", {
                    "subcommand": "scale",
                    "v_args": "deployment deploy-a -n ns --replicas=0",
                }),
            ],
            "approved_target": _approved_workload_deploy_a(),
            "execution_artifacts": [],
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        body = delta["messages"][0].content
        assert "armed-before-inject" in body
        # Retryable FORM guidance, not a mechanism-ban hard floor.
        assert "not a dead-end" in body
        assert "MECHANISM is banned" not in body
        assert "recovery-carrier.md" in body
        # F1-A: the reason names the generalized underwriter (vehicle),
        # not just the carrier form the suggestion teaches.
        assert "recovery vehicle" in body

    @pytest.mark.asyncio
    async def test_object_write_with_registered_carrier_passes(self):
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                _ai_with_tool_call("kubectl", {
                    "subcommand": "scale",
                    "v_args": "deployment deploy-a -n ns --replicas=0",
                }),
            ],
            "approved_target": _approved_workload_deploy_a(),
            "execution_artifacts": [_carrier_artifact("active")],
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        assert "messages" not in delta

    @pytest.mark.asyncio
    async def test_cleaned_carrier_does_not_count_as_armed(self):
        """A cleaned carrier is gone — a fresh object write would again run
        un-armed, so the gate re-arms on the next injection."""
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                _ai_with_tool_call("kubectl", {
                    "subcommand": "scale",
                    "v_args": "deployment deploy-a -n ns --replicas=0",
                }),
            ],
            "approved_target": _approved_workload_deploy_a(),
            "execution_artifacts": [_carrier_artifact("cleaned")],
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        assert "armed-before-inject" in delta["messages"][0].content

    @staticmethod
    def _cleaned_carrier_with_family() -> dict:
        carrier = _carrier_artifact("cleaned")
        carrier["rbac_family"] = [
            {"kind": "rolebinding", "name": "drill-rc-x", "namespace": "ns"},
            {"kind": "role", "name": "drill-rc-x", "namespace": "ns"},
            {"kind": "serviceaccount", "name": "drill-rc-x", "namespace": "ns"},
        ]
        return carrier

    @pytest.mark.asyncio
    async def test_cleaned_carrier_rbac_replay_is_cleanup(self):
        """F1-C (probe-measured): the framework sweep is fire-and-forget —
        it marks the artifact ``cleaned`` after ONE delete attempt, so a
        partial failure leaves live assets under a cleaned registration.
        The idempotent ``--ignore-not-found`` replay of the §6 four-way
        delete is TEARDOWN of the task's own registered machinery, never
        an injection, and must not be ordered to re-stack a carrier
        first (pre-fix this exact state rendered REJECT_BANNED "stack
        the recovery carrier FIRST" for deleting a leftover Role)."""
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                _ai_with_tool_call("kubectl", {
                    "subcommand": "delete",
                    "v_args": "role drill-rc-x -n ns --ignore-not-found",
                }),
            ],
            "approved_target": _approved_workload_deploy_a(),
            "execution_artifacts": [self._cleaned_carrier_with_family()],
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        assert "messages" not in delta

    @pytest.mark.asyncio
    async def test_cleaned_carrier_pod_replay_is_cleanup(self):
        """The carrier's own pod delete — same idempotent replay, same
        teardown semantics (status is irrelevant to the registry fact)."""
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                _ai_with_tool_call("kubectl", {
                    "subcommand": "delete",
                    "v_args": "pod drill-rc-x -n ns --ignore-not-found",
                }),
            ],
            "approved_target": _approved_workload_deploy_a(),
            "execution_artifacts": [self._cleaned_carrier_with_family()],
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        assert "messages" not in delta

    @pytest.mark.asyncio
    async def test_carrier_batch_delete_is_cleanup(self):
        """G-4/R20: a legal kubectl BATCH delete — the §6 four-way sweep
        collapsed into ONE mixed-kind call (`pod/carrier,role/carrier`) —
        is teardown spelled in batch form. The carrier-gate exemption
        must survive the parser boundary: pre-fix the comma-joined
        "name" matched no registration and this exact cleanup was
        screened as a fault write."""
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                _ai_with_tool_call("kubectl", {
                    "subcommand": "delete",
                    "v_args": (
                        "pod/drill-rc-x,role/drill-rc-x -n ns "
                        "--ignore-not-found"
                    ),
                }),
            ],
            "approved_target": _approved_workload_deploy_a(),
            "execution_artifacts": [self._cleaned_carrier_with_family()],
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        assert "messages" not in delta

    @pytest.mark.asyncio
    async def test_carrier_batch_delete_with_unregistered_name_rejected(self):
        """The poison law survives batch expansion: a batch pairing the
        registered carrier pod with an UNREGISTERED pod deletes an object
        this task did not build — no exemption, the armed-before-inject
        gate governs (retry guidance, same as any object write)."""
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                _ai_with_tool_call("kubectl", {
                    "subcommand": "delete",
                    "v_args": (
                        "pod drill-rc-x,other-pod -n ns "
                        "--ignore-not-found"
                    ),
                }),
            ],
            "approved_target": _approved_workload_deploy_a(),
            "execution_artifacts": [self._cleaned_carrier_with_family()],
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        assert "armed-before-inject" in delta["messages"][0].content

    @pytest.mark.asyncio
    async def test_sa_alias_cleanup_replay_passes(self):
        """Kind matching canonicalises both sides — the ``sa`` alias
        (classifier resolves it to serviceaccount) matches the
        registered member's kind."""
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                _ai_with_tool_call("kubectl", {
                    "subcommand": "delete",
                    "v_args": "sa drill-rc-x -n ns --ignore-not-found",
                }),
            ],
            "approved_target": _approved_workload_deploy_a(),
            "execution_artifacts": [self._cleaned_carrier_with_family()],
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        assert "messages" not in delta

    @pytest.mark.asyncio
    async def test_unregistered_delete_still_gated(self):
        """The exemption is earned by the registry, not the verb: a
        kind+ns-loose delete of an object NO vehicle registration names
        is still an object write and keeps the gate."""
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                _ai_with_tool_call("kubectl", {
                    "subcommand": "delete",
                    "v_args": "role other-role -n ns --ignore-not-found",
                }),
            ],
            "approved_target": _approved_workload_deploy_a(),
            "execution_artifacts": [self._cleaned_carrier_with_family()],
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        assert "armed-before-inject" in delta["messages"][0].content

    def test_predicate_batch_subset_semantics(self):
        """Unit tooth on the predicate's subset contract: the classifier
        currently collapses multi-name deletes to the first positional
        (``kubectl delete role a b`` parses as names=('a',)), so the
        batch-poisoning shape is unreachable through the real channel
        today — the subset semantics are defense-in-depth for the day
        the parser gains multi-name support, anchored here directly:
        one unregistered name poisons the batch."""
        from chaos_agent.agent.nodes.planning.tool_screener import (
            _vehicle_delete_is_cleanup,
        )
        state = {"execution_artifacts": [self._cleaned_carrier_with_family()]}
        poisoned = EffectiveTarget(
            scope="role", namespace="ns", names=("drill-rc-x", "other-role"),
        )
        assert _vehicle_delete_is_cleanup(
            {"subcommand": "delete"}, poisoned, {}, state,
        ) is False
        clean = EffectiveTarget(
            scope="role", namespace="ns", names=("drill-rc-x",),
        )
        assert _vehicle_delete_is_cleanup(
            {"subcommand": "delete"}, clean, {}, state,
        ) is True

    @pytest.mark.asyncio
    async def test_carrier_stacking_create_is_never_blocked(self):
        """The carrier-stacking verb itself must pass the gate: ``create`` is
        NOT an object-write injection verb (KUBECTL_WRITE_SUBCOMMANDS), so
        stacking the SA under the workload net is never refused — the gate
        must not deadlock the very remedy it demands."""
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                _ai_with_tool_call("kubectl", {
                    "subcommand": "create",
                    "v_args": "serviceaccount drill-rc-x -n ns",
                }),
            ],
            "approved_target": _approved_workload_deploy_a(),
            "execution_artifacts": [],
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        assert "messages" not in delta

    @pytest.mark.asyncio
    async def test_node_domain_object_write_is_outside_the_gate(self):
        """The node net's secondary_scopes carry NO carrier family — node
        faults ride the host carrier (debug pod + systemd-run timer),
        case-legislated and outside this object-write first cut. A cordon
        on an approved node passes with no recovery_carrier registered."""
        settings.target_guard_enforcing = True
        state = {
            "messages": [
                _ai_with_tool_call("kubectl", {
                    "subcommand": "cordon",
                    "v_args": "node-a",
                }),
            ],
            "approved_target": _approved_node_network(),
            "execution_artifacts": [],
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        assert "messages" not in delta

    def test_write_set_helpers_single_source_semantics(self):
        """_carrier_family_in_write_set: workload net True, node net False
        (the discriminating boundary), mechanism_entries contribute the
        same way; _recovery_vehicle_registered: vehicle_cache read-first
        so a same-batch run + injection pair sees the registration;
        occupant forms underwrite, a debug pod does not."""
        from chaos_agent.agent.nodes.planning.tool_screener import (
            _carrier_family_in_write_set,
            _recovery_vehicle_registered,
        )

        workload = approved_from_dict(_approved_workload_deploy_a())
        node = approved_from_dict(_approved_node_network())
        assert _carrier_family_in_write_set(workload) is True
        assert _carrier_family_in_write_set(node) is False

        empty_state = {"execution_artifacts": []}
        assert _recovery_vehicle_registered({}, empty_state) is False
        assert _recovery_vehicle_registered(
            {"execution_artifacts": [_carrier_artifact()]}, empty_state,
        ) is True
        # The occupant forms underwrite too: a task-built target's
        # cleanup record deletes the asset — and the fault riding it —
        # wholesale (the drill-target lifecycle contract)
        assert _recovery_vehicle_registered(
            {"execution_artifacts": [
                _carrier_artifact() | {"type": "occupant_deployment"},
            ]}, empty_state,
        ) is True
        # A debug pod does NOT: it is a probe channel — a debug pod
        # plus an un-armed injection is still an un-armed injection
        assert _recovery_vehicle_registered(
            {"execution_artifacts": [
                _carrier_artifact() | {"type": "debug_pod"},
            ]}, empty_state,
        ) is False


# ---------------------------------------------------------------------------
# CR-channel route gate (openspec faultdrill-cr-channel, design D3 source 3)
# ---------------------------------------------------------------------------

_FAULTDRILL_MANIFEST = """apiVersion: drill.blade-ai.io/v1alpha1
kind: FaultDrill
metadata:
  name: fd-demo1
  namespace: ns
spec:
  action: secretSwap
"""


def _approved_cr_channel_pod(bt: str, ba: str, *, entry: bool = True) -> dict:
    """Pod-victim approval whose case manifest legislates the FaultDrill CR
    write (the widened write-set contract that admits a faultdrill-scope
    apply at all), with the frozen intent verbs parameterised so the
    three-class verdict is the only variable under test."""
    me = (
        (MechanismWriteEntry(scope="faultdrill", namespace="ns", names=("fd-demo1",)),)
        if entry else ()
    )
    return freeze_approved_target(
        target={"namespace": "ns", "names": ["pod-a"]},
        params={"scope": "pod"},
        fault_scope="pod", fault_target=bt, fault_action=ba,
        mechanism_entries=me,
    )


def _cr_apply_call(sub: str = "apply") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{
            "id": "tc-1", "name": "kubectl",
            "args": {
                "subcommand": sub, "v_args": "-f -",
                "stdin_data": _FAULTDRILL_MANIFEST,
            },
        }],
    )


class TestCrChannelRouteGate:
    """D3 source 3 (write-set 审批门三分类程序化校验): a FaultDrill CR
    creation that already passed write-set admission (case-manifest
    faultdrill entry → ALLOW at the drift guard) is still subject to the
    programmatic three-class routing check — the channel belongs to
    apiserver-write recovery only, and a symmetric-revert-reachable fault
    (blade vocabulary) routed into it is rejected with re-plan guidance
    (spec scenario "零工坊 case 误路由被审批门拒绝")."""

    @pytest.mark.asyncio
    async def test_symmetric_revert_domain_misroute_is_rejected(self):
        settings.target_guard_enforcing = True
        settings.faultdrill_enabled = True
        state = {
            "messages": [_cr_apply_call()],
            "approved_target": _approved_cr_channel_pod("cpu", "fullload"),
            "execution_artifacts": [],
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        body = delta["messages"][0].content
        assert "cr-channel route" in body
        # The reason names the declared verbs and the domain verdict.
        assert "cpu" in body and "fullload" in body
        assert "symmetric-revert" in body
        assert "blade destroy" in body
        # Retryable form guidance: the fix is a re-plan onto the correct
        # channel, not a mechanism ban (same family as armed-before-inject).
        assert "not a dead-end" in body
        assert "MECHANISM is banned" not in body
        assert "blade create" in body
        assert "recovery-carrier.md" in body

    @pytest.mark.asyncio
    async def test_apiserver_write_domain_passes(self):
        # k8s-native vocabulary verbs (apiserver-write family): the
        # legitimate CR-channel shape — admission via the manifest entry,
        # no route objection, and a cluster that can accept the write
        # (the installability seam reports an established CRD).
        settings.target_guard_enforcing = True
        settings.faultdrill_enabled = True
        state = {
            "messages": [_cr_apply_call()],
            "approved_target": _approved_cr_channel_pod("image", "corrupt"),
            "execution_artifacts": [],
            "kubeconfig": "/tmp/fd-route-gate.kubeconfig",
        }
        seam = AsyncMock(return_value={"usable": True, "status": "ready"})
        with patch.object(FaultProviderRegistry, "ensure_crd", new=seam):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        assert "messages" not in delta
        # The lazy-install seam WAS consulted with the task's resolved
        # kubeconfig (the first admitted CR write is installability-
        # checked — spec scenario "首次注入时惰性安装").
        seam.assert_awaited_once_with(
            kubeconfig="/tmp/fd-route-gate.kubeconfig",
        )

    @pytest.mark.asyncio
    async def test_disabled_channel_rejects_any_cr_apply(self):
        # Dark launch: while faultdrill_enabled is False the channel's own
        # guards (landing readback, session reconciler) are short-circuited,
        # so ANY CR apply in that window is an unguarded bare write —
        # rejected regardless of verb domain.
        settings.target_guard_enforcing = True
        settings.faultdrill_enabled = False
        state = {
            "messages": [_cr_apply_call()],
            "approved_target": _approved_cr_channel_pod("image", "corrupt"),
            "execution_artifacts": [],
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        body = delta["messages"][0].content
        assert "cr-channel route" in body
        assert "not enabled" in body
        # The fix names the standard SOP channel, not the CR channel.
        assert "recovery-carrier.md" in body

    @pytest.mark.asyncio
    async def test_replace_form_is_not_gated(self):
        # Only the CREATING verbs (apply/create) are gated: replace -f CR
        # (the re-recipe replay path) keeps manifest-entry admission only —
        # the route gate owns channel entry, not every faultdrill-scope
        # write. (A delete -f CR is additionally subject to the
        # armed-before-inject gate — delete IS an object-write verb there —
        # which is the pre-existing behaviour for LLM-side deletes of any
        # carrier asset; provider-side recovery deletes go through the
        # programmatic transport, not this face.)
        settings.target_guard_enforcing = True
        settings.faultdrill_enabled = True
        state = {
            "messages": [_cr_apply_call("replace")],
            "approved_target": _approved_cr_channel_pod("cpu", "fullload"),
            "execution_artifacts": [],
        }
        seam = AsyncMock(return_value={"usable": True})
        with patch.object(FaultProviderRegistry, "ensure_crd", new=seam):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        assert "messages" not in delta
        # The installability check rides the CREATING-verbs gate only.
        seam.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_verbs_are_not_decidable_so_pass(self):
        # A fallback does not block what it cannot classify: with both
        # frozen verbs empty the write-set admission (manifest entry) is
        # the guard on record, and the route check stays silent.
        settings.target_guard_enforcing = True
        settings.faultdrill_enabled = True
        state = {
            "messages": [_cr_apply_call()],
            "approved_target": _approved_cr_channel_pod("", ""),
            "execution_artifacts": [],
        }
        seam = AsyncMock(return_value={"usable": True, "status": "installed"})
        with patch.object(FaultProviderRegistry, "ensure_crd", new=seam):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        assert "messages" not in delta

    @pytest.mark.asyncio
    @patch(
        "chaos_agent.agent.nodes.planning.tool_screener.interrupt",
        return_value="rejected",
    )
    async def test_no_manifest_entry_stays_scope_drift(self, _mock):
        # Existing admission behaviour pinned (the route gate never sees
        # this call): without a case-manifest faultdrill entry the apply is
        # scope-drift rejected BEFORE any routing verdict — a mis-route
        # with no legislation behind it never reaches the channel at all.
        settings.target_guard_enforcing = True
        settings.faultdrill_enabled = True
        state = {
            "messages": [_cr_apply_call()],
            "approved_target": _approved_cr_channel_pod("cpu", "fullload", entry=False),
            "execution_artifacts": [],
        }
        delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        assert delta["drift_reject_count"] == 1
        body = delta["messages"][0].content
        assert "REJECT_DRIFT" in body
        assert "scope drift" in body
        assert "faultdrill" in body
        # The route gate did not fire (its rejection would be BANNED, not
        # DRIFT — and would not increment the drift counter).
        assert "cr-channel route" not in body

    def test_domain_predicate_unit_teeth(self):
        from chaos_agent.agent.nodes.planning.tool_screener import (
            _declared_verbs_in_symmetric_revert_domain,
        )

        blade = approved_from_dict(_approved_cr_channel_pod("cpu", "fullload"))
        k8s_domain = approved_from_dict(_approved_cr_channel_pod("image", "corrupt"))
        empty = approved_from_dict(_approved_cr_channel_pod("", ""))
        target_only = approved_from_dict(_approved_cr_channel_pod("cpu", "patch"))
        action_only = approved_from_dict(_approved_cr_channel_pod("pod", "fullload"))
        assert _declared_verbs_in_symmetric_revert_domain(blade) is True
        assert _declared_verbs_in_symmetric_revert_domain(k8s_domain) is False
        # EITHER axis marking the domain is decisive (the carrier
        # vocabularies are disjoint, so a mixed pair is a blade-shaped
        # fault the plan mis-declared, not an apiserver-write fault).
        assert _declared_verbs_in_symmetric_revert_domain(target_only) is True
        assert _declared_verbs_in_symmetric_revert_domain(action_only) is True
        # Not decidable → not blocked.
        assert _declared_verbs_in_symmetric_revert_domain(empty) is False

    @pytest.mark.asyncio
    async def test_crd_unavailable_rejects_with_sop_degradation_guidance(self):
        # D7 degradation branch: the installability seam reports the CRD
        # uninstallable (probe / install / Established / compatibility
        # family) — the apply is rejected BEFORE any CR attempt round,
        # with re-plan guidance onto the SOP form (a routing branch, not
        # a task failure). Spec scenario "CRD 不可装时降级路由".
        settings.target_guard_enforcing = True
        settings.faultdrill_enabled = True
        state = {
            "messages": [_cr_apply_call()],
            "approved_target": _approved_cr_channel_pod("image", "corrupt"),
            "execution_artifacts": [],
            "kubeconfig": "/tmp/fd-route-gate.kubeconfig",
        }
        seam = AsyncMock(return_value={
            "usable": False, "status": "unavailable",
            "reason": "apply-forbidden",
            "detail": "RBAC denies creating customresourcedefinitions",
        })
        with patch.object(FaultProviderRegistry, "ensure_crd", new=seam):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        body = delta["messages"][0].content
        assert "cr-channel route" in body
        # The decision family's verdict is surfaced for the audit log.
        assert "apply-forbidden" in body
        assert "RBAC denies creating" in body
        # Degradation guidance: the SOP route, retryable (not a ban).
        assert "degradation branch" in body
        assert "recovery-carrier.md" in body
        assert "not a dead-end" in body
        assert "MECHANISM is banned" not in body
        seam.assert_awaited_once_with(
            kubeconfig="/tmp/fd-route-gate.kubeconfig",
        )

    @pytest.mark.asyncio
    async def test_install_seam_not_consulted_while_dark_launched(self):
        # Dark launch spends ZERO install traffic: the flag rejection
        # fires before the seam is ever consulted.
        settings.target_guard_enforcing = True
        settings.faultdrill_enabled = False
        state = {
            "messages": [_cr_apply_call()],
            "approved_target": _approved_cr_channel_pod("image", "corrupt"),
            "execution_artifacts": [],
        }
        seam = AsyncMock()
        with patch.object(FaultProviderRegistry, "ensure_crd", new=seam):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        assert "not enabled" in delta["messages"][0].content
        seam.assert_not_called()

    @pytest.mark.asyncio
    async def test_install_seam_not_consulted_on_vocabulary_misroute(self):
        # Check-order pin: the vocabulary objection is pure-Python and
        # fires BEFORE the installability seam — a mis-routed fault
        # never spends an install roundtrip.
        settings.target_guard_enforcing = True
        settings.faultdrill_enabled = True
        state = {
            "messages": [_cr_apply_call()],
            "approved_target": _approved_cr_channel_pod("cpu", "fullload"),
            "execution_artifacts": [],
        }
        seam = AsyncMock()
        with patch.object(FaultProviderRegistry, "ensure_crd", new=seam):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        assert "symmetric-revert" in delta["messages"][0].content
        seam.assert_not_called()

    @pytest.mark.asyncio
    async def test_install_seam_none_passes_through(self):
        # ``None`` = no registered provider claims the install
        # responsibility (the defensive window): the gate does not
        # invent a verdict — the apply proceeds under its own
        # not-landed error family.
        settings.target_guard_enforcing = True
        settings.faultdrill_enabled = True
        state = {
            "messages": [_cr_apply_call()],
            "approved_target": _approved_cr_channel_pod("image", "corrupt"),
            "execution_artifacts": [],
        }
        seam = AsyncMock(return_value=None)
        with patch.object(FaultProviderRegistry, "ensure_crd", new=seam):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        assert "messages" not in delta
