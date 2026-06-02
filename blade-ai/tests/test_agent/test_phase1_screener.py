"""Tests for ``chaos_agent.agent.nodes.phase1_screener``.

Covers the Phase 1 (planning) tool-call guard introduced after the
task-ce9647931ce1 incident. Scenarios:

  - No messages / non-AIMessage / no tool_calls → pass
  - Pure read-only batch (kubectl_ro, activate_skill, blade_status, ...) → pass
  - Direct mutation tool (blade_create, blade_destroy) → retry + rejection
  - kubectl exec ... blade create bypass → retry (recursive classifier)
  - kubectl create -f chaosblade.yaml → retry
  - Mixed batch (one mutation + one read) → retry, both get ToolMessages
  - Classifier exception → fail-open with WARNING log
  - Log-only mode (settings flag) → pass through with WARNING
  - route_after_phase1_screener honours state.screener_route
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chaos_agent.agent.nodes.phase1_screener import (
    PHASE1_SCREENER_ROUTE_PASS,
    PHASE1_SCREENER_ROUTE_RETRY,
    phase1_screener,
    route_after_phase1_screener,
)
from chaos_agent.config.settings import settings


@pytest.fixture(autouse=True)
def _reset_settings():
    """Snapshot + restore the two feature flags around every test."""
    orig_enforce = getattr(settings, "phase1_screener_enforcing", True)
    orig_skill = settings.skill_script_default_allow
    yield
    settings.phase1_screener_enforcing = orig_enforce
    settings.skill_script_default_allow = orig_skill


def _ai(name: str, args: dict, call_id: str = "tc-1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def _ai_multi(calls: list[tuple[str, dict, str]]):
    return AIMessage(
        content="",
        tool_calls=[{"name": n, "args": a, "id": cid} for n, a, cid in calls],
    )


# ---------------------------------------------------------------------------
# Pass-through cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestPassThrough:
    async def test_empty_messages(self):
        result = await phase1_screener({"messages": []})
        assert result["screener_route"] == PHASE1_SCREENER_ROUTE_PASS

    async def test_last_is_not_ai(self):
        result = await phase1_screener({"messages": [HumanMessage(content="hi")]})
        assert result["screener_route"] == PHASE1_SCREENER_ROUTE_PASS

    async def test_ai_without_tool_calls(self):
        result = await phase1_screener(
            {"messages": [AIMessage(content="final summary")]}
        )
        assert result["screener_route"] == PHASE1_SCREENER_ROUTE_PASS

    async def test_pure_readonly_batch_passes(self):
        """The full set of Phase 1 read-only tools should pass cleanly."""
        msg = _ai_multi([
            ("activate_skill", {"skill_name": "k8s-chaos-skills"}, "tc-1"),
            ("read_skill_resource", {
                "skill_name": "k8s-chaos-skills",
                "resource_path": "use_cases/pod_cpu.md",
            }, "tc-2"),
            ("read_knowledge_resource", {"filename": "chaosblade-cli.md"}, "tc-3"),
            ("blade_status", {"uid": ""}, "tc-4"),
            ("kubectl_ro", {
                "subcommand": "get",
                "v_args": "pods -n cms-demo",
            }, "tc-5"),
            ("read_file", {"file_path": "/tmp/foo.txt"}, "tc-6"),
            ("save_fault_plan", {
                "plan_content": "...",
                "task_id": "task-x",
            }, "tc-7"),
        ])
        result = await phase1_screener({"messages": [msg]})
        assert result["screener_route"] == PHASE1_SCREENER_ROUTE_PASS
        assert "messages" not in result  # no fabricated rejections


# ---------------------------------------------------------------------------
# Rejection cases — direct mutation tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestDirectMutationRejection:
    async def test_blade_create_rejected(self):
        msg = _ai("blade_create", {
            "scope": "pod", "target": "cpu", "action": "fullload",
            "namespace": "cms-demo", "names": "pod-x",
        })
        result = await phase1_screener({"messages": [msg]})
        assert result["screener_route"] == PHASE1_SCREENER_ROUTE_RETRY
        assert len(result["messages"]) == 1
        tm = result["messages"][0]
        assert isinstance(tm, ToolMessage)
        assert "phase1_readonly_violation" in tm.content
        # Critical: error must NOT list alternative tools
        assert "try one of" not in tm.content.lower()
        assert "alternative" not in tm.content.lower()
        # Must point to the legitimate forward path
        assert "final summary text" in tm.content.lower()

    async def test_blade_destroy_rejected(self):
        msg = _ai("blade_destroy", {"uid": "abc123"})
        result = await phase1_screener({"messages": [msg]})
        assert result["screener_route"] == PHASE1_SCREENER_ROUTE_RETRY
        assert "phase1_readonly_violation" in result["messages"][0].content


# ---------------------------------------------------------------------------
# Rejection cases — kubectl bypass paths (the actual exploit)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestKubectlBypassRejection:
    async def test_kubectl_exec_blade_create_rejected(self):
        """The exact bypass observed in task-ce9647931ce1."""
        msg = _ai("kubectl", {
            "subcommand": "exec",
            "v_args": (
                "otel-c-tool-5pmkc -n chaosblade -- "
                "blade create k8s pod-cpu fullload "
                "--names accounting-xyz --namespace cms-demo "
                "--cpu-percent 80 --timeout 600"
            ),
        })
        result = await phase1_screener({"messages": [msg]})
        assert result["screener_route"] == PHASE1_SCREENER_ROUTE_RETRY
        tm = result["messages"][0]
        assert "phase1_readonly_violation" in tm.content

    async def test_kubectl_apply_chaosblade_rejected(self):
        msg = _ai("kubectl", {
            "subcommand": "apply",
            "v_args": "-f chaosblade.yaml",
        })
        result = await phase1_screener({"messages": [msg]})
        assert result["screener_route"] == PHASE1_SCREENER_ROUTE_RETRY

    async def test_kubectl_delete_rejected(self):
        msg = _ai("kubectl", {
            "subcommand": "delete",
            "v_args": "pod some-pod -n ns",
        })
        result = await phase1_screener({"messages": [msg]})
        assert result["screener_route"] == PHASE1_SCREENER_ROUTE_RETRY


# ---------------------------------------------------------------------------
# Mixed batches — must produce one ToolMessage per tool_call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestMixedBatch:
    async def test_one_mutation_one_read_yields_two_tool_messages(self):
        msg = _ai_multi([
            ("kubectl_ro", {"subcommand": "get", "v_args": "pods"}, "tc-1"),
            ("blade_create", {
                "scope": "pod", "target": "cpu", "action": "fullload",
                "namespace": "ns", "names": "pod",
            }, "tc-2"),
        ])
        result = await phase1_screener({"messages": [msg]})
        assert result["screener_route"] == PHASE1_SCREENER_ROUTE_RETRY
        # Two ToolMessages — one rejection, one skip notice
        assert len(result["messages"]) == 2
        ids = {tm.tool_call_id for tm in result["messages"]}
        assert ids == {"tc-1", "tc-2"}
        contents = {tm.tool_call_id: tm.content for tm in result["messages"]}
        assert "phase1_readonly_violation" in contents["tc-2"]
        assert "skipped" in contents["tc-1"].lower()


# ---------------------------------------------------------------------------
# Resilience
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestResilience:
    async def test_classifier_exception_fails_open(self, monkeypatch, caplog):
        """If the classifier itself raises, the call passes through with
        a WARNING. Fail-closing here would create unrecoverable loops."""
        import logging
        from chaos_agent.agent.nodes import phase1_screener as mod

        def _raise(*args, **kwargs):
            raise ValueError("simulated classifier bug")

        monkeypatch.setattr(mod, "infer_effective_target", _raise)
        msg = _ai("kubectl_ro", {"subcommand": "get", "v_args": "pods"})
        with caplog.at_level(logging.WARNING):
            result = await phase1_screener({"messages": [msg]})
        assert result["screener_route"] == PHASE1_SCREENER_ROUTE_PASS
        assert any(
            "classifier raised" in r.message for r in caplog.records
        ), "Expected WARNING log for classifier exception"

    async def test_log_only_mode_does_not_reject(self, caplog):
        """When BLADE_AI_PHASE1_SCREENER_ENFORCING=false, mutations log
        but still pass through (rely on Layer A/D to be the actual
        backstop)."""
        import logging
        settings.phase1_screener_enforcing = False
        msg = _ai("blade_create", {
            "scope": "pod", "target": "cpu", "action": "fullload",
            "namespace": "ns", "names": "pod",
        })
        with caplog.at_level(logging.WARNING):
            result = await phase1_screener({"messages": [msg]})
        assert result["screener_route"] == PHASE1_SCREENER_ROUTE_PASS
        assert "messages" not in result
        assert any(
            "log-only" in r.message for r in caplog.records
        ), "Expected log-only WARNING"


# ---------------------------------------------------------------------------
# Routing dispatcher
# ---------------------------------------------------------------------------


class TestRouting:
    def test_route_default_is_pass(self):
        assert route_after_phase1_screener({}) == PHASE1_SCREENER_ROUTE_PASS

    def test_route_reads_state_field(self):
        assert route_after_phase1_screener(
            {"screener_route": PHASE1_SCREENER_ROUTE_RETRY},
        ) == PHASE1_SCREENER_ROUTE_RETRY
        assert route_after_phase1_screener(
            {"screener_route": PHASE1_SCREENER_ROUTE_PASS},
        ) == PHASE1_SCREENER_ROUTE_PASS
