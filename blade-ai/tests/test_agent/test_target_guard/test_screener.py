"""Tests for ``chaos_agent.agent.nodes.tool_screener``.

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

from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chaos_agent.agent.nodes.tool_screener import (
    SCREENER_ROUTE_PASS,
    SCREENER_ROUTE_REPLAN,
    SCREENER_ROUTE_RETRY,
    route_after_screener,
    tool_screener,
)
from chaos_agent.agent.target_guard import freeze_approved_target
from chaos_agent.config.settings import settings


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_settings():
    """Snapshot + restore the two feature flags around every test."""
    orig_enforce = settings.target_guard_enforcing
    orig_skill = settings.skill_script_default_allow
    yield
    settings.target_guard_enforcing = orig_enforce
    settings.skill_script_default_allow = orig_skill


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
    @patch("chaos_agent.agent.nodes.tool_screener.interrupt", return_value="approved")
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


# ---------------------------------------------------------------------------
# Enforcing mode — REJECT_DRIFT path (interrupt confirmation)
# ---------------------------------------------------------------------------


class TestEnforcingDriftInterrupt:
    @pytest.mark.asyncio
    @patch("chaos_agent.agent.nodes.tool_screener.interrupt", return_value="approved")
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
    @patch("chaos_agent.agent.nodes.tool_screener.interrupt", return_value="rejected")
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
    @patch("chaos_agent.agent.nodes.tool_screener.interrupt", return_value="rejected")
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
        # rejection and can [REPLAN] to seek approval.
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
        from chaos_agent.agent.nodes import tool_screener as ts
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
