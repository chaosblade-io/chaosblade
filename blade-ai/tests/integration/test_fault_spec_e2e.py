"""End-to-end integration tests for the FaultSpec refactor.

These tests verify that the data flow we redesigned actually works
across node boundaries — that ``state.fault_spec`` written at the
entry point or by ``intent_clarification`` is correctly carried
through ``safety_check`` / ``confirmation_gate`` / ``baseline_capture``
without being lost or shadowed.

The original bug these tests guard against:
  NL path → state.target.names was always missing → baseline_capture
  couldn't resolve {node_name} / {pod_name} templates → debug pods
  never created → "0/1 succeeded" forever.

After the refactor:
  spec.names is pinned at intent_clarification convergence and
  reachable by every downstream consumer via read_fault_spec(state).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from chaos_agent.agent.fault_spec import (
    SOURCE_TUI,
    FaultSpec,
    read_fault_spec,
)
from chaos_agent.agent.nodes.baseline_capture import (
    BaselineCommand,
    _resolve_templates,
)
from chaos_agent.agent.nodes.confirmation_gate import _freeze_from_state
from chaos_agent.agent.nodes.memory_nodes import load_memory
from chaos_agent.agent.nodes.safety_check import safety_check
from chaos_agent.config.settings import settings


# ---------------------------------------------------------------------------
# NL path — the original bug case
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestNlPathFaultSpecFlow:
    """The original bug: NL path → baseline_capture saw empty names.

    Verify the full path: TUI entry → placeholder spec → intent_clarification
    rewrites spec → consumers all read the rewritten spec.
    """

    async def test_nl_path_baseline_capture_sees_names(self, tmp_memory_dir, monkeypatch):
        """The smoking-gun test for the original bug — baseline_capture
        must see spec.names = [user-pinned-name] after intent_clarification."""
        monkeypatch.setattr(settings, "working_dir", tmp_memory_dir.parent)
        monkeypatch.setattr(settings, "kubeconfig_path", "")

        # Stage 1: TUI entry writes a placeholder spec (turn.py first_turn).
        placeholder = FaultSpec.placeholder_nl(
            user_description="对节点 cn-hongkong.10.0.1.120 注入 CPU 满载 80%",
            source=SOURCE_TUI,
        )
        state = {
            "task_id": "task-e2e-nl",
            "tui_session_id": "sess-nl",
            "interaction_mode": "tui",
            "operation": "inject",
            "fault_spec": placeholder.to_dict(),
            "input": placeholder.user_description,
            "needs_confirmation": True,
            "safety_status": "pending",
        }

        # Stage 2: load_memory runs. spec is still placeholder.
        with patch("chaos_agent.persistence.task_store.get_task_store",
                   new=AsyncMock()), \
             patch("chaos_agent.agent.nodes.memory_nodes.OperationalMemory") as MockMem, \
             patch("chaos_agent.agent.nodes.memory_nodes.sync_to_store",
                   new=AsyncMock()):
            MockMem.return_value.read.return_value = ""
            updates = await load_memory(state)
            state.update(updates)

        # Stage 3: simulate intent_clarification convergence — the LLM
        # emits ``submit_fault_intent`` with the user-pinned names.
        # We bypass the LLM and directly write the spec that the
        # node would produce via ``FaultSpec.from_intent_args``.
        intent_args = {
            "fault_type": "node-cpu-fullload",
            "scope": "node",
            "target": "cpu",
            "action": "fullload",
            "namespace": "default",
            "names": ["cn-hongkong.10.0.1.120"],
            "labels": {},
            "params": {"percent": "80", "timeout": "600"},
            "user_description": placeholder.user_description,
        }
        converged_spec = FaultSpec.from_intent_args(
            intent_args, existing=placeholder, source=SOURCE_TUI,
        )
        state["fault_spec"] = converged_spec.to_dict()
        state["skill_name"] = "k8s-chaos-skills"
        state["confirmed_intent"] = "inject"

        # Stage 4: safety_check reads spec, should NOT reject.
        with patch("chaos_agent.agent.nodes.safety_check.sync_to_store",
                   new=AsyncMock()):
            sc_result = await safety_check(state)
            state.update(sc_result)
        assert state["safety_status"] in ("safe", "warning", "confirm_required"), (
            f"safety_check rejected NL path inject: {state.get('safety_reason')}"
        )

        # Stage 5: confirmation_gate's _freeze_from_state. Verify approved
        # target dict is populated from spec (the inverse projection).
        frozen = _freeze_from_state(state)
        assert frozen is not None
        assert frozen["scope"] == "node"
        assert frozen["names"] == ["cn-hongkong.10.0.1.120"]
        assert frozen["blade_target"] == "cpu"
        assert frozen["blade_action"] == "fullload"

        # Stage 6 — THE KEY ASSERTION: baseline_capture's template
        # resolution sees the right node name.
        cmds = [BaselineCommand(
            "Node CPU info", "describe", "node {node_name}",
        )]
        resolved = _resolve_templates(cmds, state)
        assert len(resolved) == 1
        assert resolved[0]["_unresolved"] is False, (
            "Original bug: baseline_capture saw empty names because "
            "state.target.names was never populated in NL mode."
        )
        assert "cn-hongkong.10.0.1.120" in resolved[0]["v_args"], (
            f"baseline_capture didn't resolve {{node_name}}: {resolved[0]}"
        )

    async def test_namespace_wide_intent_survives(self, tmp_memory_dir, monkeypatch):
        """Namespace-wide intent (no specific names, no labels) must
        still be ``is_complete`` and pass through all consumers."""
        monkeypatch.setattr(settings, "working_dir", tmp_memory_dir.parent)
        monkeypatch.setattr(settings, "kubeconfig_path", "")

        # Direct write a namespace-wide spec
        spec = FaultSpec.from_intent_args({
            "scope": "pod",
            "target": "cpu",
            "action": "fullload",
            "namespace": "prod",
            "names": [],
            "labels": {},
            "params": {"timeout": "300"},
        })
        state = {
            "task_id": "task-nsw",
            "operation": "inject",
            "fault_spec": spec.to_dict(),
            "skill_name": "k8s-chaos-skills",
            "needs_confirmation": True,
            "safety_status": "pending",
        }

        with patch("chaos_agent.agent.nodes.safety_check.sync_to_store",
                   new=AsyncMock()):
            sc_result = await safety_check(state)
            state.update(sc_result)
        # Namespace-wide spec passes safety check (scope/blade_target/
        # blade_action all set + namespace given for namespace-scoped).
        assert state["safety_status"] in ("safe", "warning", "confirm_required")

        # freeze should produce a namespace-wide approval
        frozen = _freeze_from_state(state)
        assert frozen is not None
        assert frozen["is_namespace_wide"] is True
        assert frozen["names"] == []


# ---------------------------------------------------------------------------
# CLI structured path — spec persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCliStructuredSpecPersistence:
    """When CLI structured input populates a complete FaultSpec at the
    entry point, that spec must survive all the way to baseline_capture
    without any consumer mutating it or losing fields."""

    async def test_structured_spec_round_trip(self, tmp_memory_dir, monkeypatch):
        monkeypatch.setattr(settings, "working_dir", tmp_memory_dir.parent)
        monkeypatch.setattr(settings, "kubeconfig_path", "")

        original_spec = FaultSpec.from_cli_structured({
            "scope": "pod",
            "target": "cpu",
            "action": "fullload",
            "namespace": "production",
            "target_name": "my-app-pod",
            "labels": {"app": "my-app"},
            "params": {"percent": "80", "timeout": "600"},
            "params_flags": ["read"],
            "duration": 600,
        })
        state = {
            "task_id": "task-e2e-structured",
            "operation": "inject",
            "fault_spec": original_spec.to_dict(),
            "skill_name": "k8s-chaos-skills",
            "needs_confirmation": False,
            "safety_status": "pending",
        }

        # Each node reads the spec; afterwards it must be unchanged.
        with patch("chaos_agent.agent.nodes.safety_check.sync_to_store",
                   new=AsyncMock()):
            sc_result = await safety_check(state)
            state.update(sc_result)

        # spec didn't get clobbered — re-read and compare
        retrieved = read_fault_spec(state)
        assert retrieved == original_spec, (
            "Spec was modified by safety_check — should be read-only"
        )

        # Same after confirmation_gate's freeze (it reads, doesn't write spec)
        _ = _freeze_from_state(state)
        retrieved2 = read_fault_spec(state)
        assert retrieved2 == original_spec, (
            "Spec was modified by _freeze_from_state — should be read-only"
        )

        # baseline_capture's template resolution
        cmds = [BaselineCommand(
            "Pod inspect", "describe", "pod {pod_name} -n {namespace}",
        )]
        resolved = _resolve_templates(cmds, state)
        assert resolved[0]["_unresolved"] is False
        assert "my-app-pod" in resolved[0]["v_args"]
        assert "production" in resolved[0]["v_args"]

        # spec still unchanged after baseline_capture
        retrieved3 = read_fault_spec(state)
        assert retrieved3 == original_spec


# ---------------------------------------------------------------------------
# Approval cycle — confirmation_gate produces approved_target from spec
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestApprovalCycleFromSpec:
    """approved_target dict (consumed by target_guard) must be derivable
    from any complete spec — this is the contract between the FaultSpec
    refactor and the target-drift guard subsystem."""

    async def test_approved_target_mirrors_spec(self):
        spec = FaultSpec(
            scope="pod", namespace="ns", names=("pod-a", "pod-b"),
            labels={"app": "demo"},
            blade_target="mem", blade_action="ram",
            params={"size": "100"},
        )
        state = {"fault_spec": spec.to_dict()}
        approved = _freeze_from_state(state)

        assert approved is not None
        assert approved["scope"] == spec.scope
        assert approved["namespace"] == spec.namespace
        assert approved["names"] == list(spec.names)
        assert approved["labels"] == dict(spec.labels)
        assert approved["blade_target"] == spec.blade_target
        assert approved["blade_action"] == spec.blade_action
        assert approved["lock_fault_type"] is True  # default

    async def test_freeze_returns_none_when_no_spec(self):
        state = {}  # no fault_spec — defensive
        assert _freeze_from_state(state) is None


# ---------------------------------------------------------------------------
# CLI NL path — does NOT go through intent_clarification (only TUI does).
# spec must still be derivable from LLM's planning actions for the inject
# to actually run.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCliNlPathSpecDerivation:
    """CLI NL (``blade-ai inject --input "..."``) skips intent_clarification
    (route_after_load_memory checks interaction_mode). The placeholder
    spec written at entry has empty scope/blade_target/blade_action/names.

    For the inject to actually proceed, the spec must get populated
    DURING agent_loop's planning phase — either:
      - extract_planning_metadata derives scope/target/action from
        skill_case_content, OR
      - agent_loop derives namespace/names from LLM's kubectl get probes.

    Without that, safety_check rejects with "No target specified".
    """

    async def test_cli_nl_placeholder_spec_can_reach_safety_check(
        self, tmp_memory_dir, monkeypatch,
    ):
        """Full CLI NL flow: entry placeholder → LLM planning →
        extract_planning_metadata derives scope/blade_target/blade_action
        + agent_loop derives namespace/names → safety_check sees a
        complete spec and accepts."""
        from langchain_core.messages import AIMessage, ToolMessage
        from chaos_agent.agent.nodes.extract_planning_metadata import (
            extract_planning_metadata,
        )
        from chaos_agent.agent.nodes.agent_loop import (
            _derive_spec_fields_from_kubectl_get,
        )

        monkeypatch.setattr(settings, "working_dir", tmp_memory_dir.parent)
        monkeypatch.setattr(settings, "kubeconfig_path", "")

        placeholder = FaultSpec.from_cli_nl(
            input_text="对节点 my-node 注入 CPU 满载",
            kwargs={"duration": 600},
        )
        state = {
            "task_id": "task-cli-nl",
            "operation": "inject",
            "interaction_mode": "cli",
            "fault_spec": placeholder.to_dict(),
            "input": placeholder.user_description,
            "skill_name": "k8s-chaos-skills",
            "needs_confirmation": False,
            "safety_status": "pending",
            "messages": [
                # Simulated read_skill_resource ToolMessage with a
                # blade command pattern that derives scope/target/action.
                AIMessage(
                    content="",
                    tool_calls=[{
                        "name": "read_skill_resource",
                        "args": {
                            "skill_name": "k8s-chaos-skills",
                            "resource_path": "references/catalogue/Node_CPU/cpu_fullload.md",
                        },
                        "id": "tc_read",
                    }],
                ),
                ToolMessage(
                    content=(
                        "**故障现象**：节点 CPU 满载\n\n"
                        "**注入命令**：\n"
                        "```\n"
                        "blade create node-cpu fullload --cpu-percent 80\n"
                        "```\n"
                        "**注入验证**：node CPU usage > 90%\n"
                    ),
                    tool_call_id="tc_read",
                    name="read_skill_resource",
                ),
            ],
        }

        # Stage 1: extract_planning_metadata derives scope/blade_target/blade_action
        ep_result = await extract_planning_metadata(state)
        state.update(ep_result)
        spec_after_ep = read_fault_spec(state)
        assert spec_after_ep.scope == "node", (
            "extract_planning_metadata should derive scope from blade command pattern"
        )
        assert spec_after_ep.blade_target == "cpu"
        assert spec_after_ep.blade_action == "fullload"

        # Stage 2: simulate agent_loop deriving namespace/names from a
        # ``kubectl get`` probe LLM issued.
        derived = _derive_spec_fields_from_kubectl_get(
            v_args="node my-node",
            blacklist=[],
        )
        updates = {k: v for k, v in derived.items()
                   if not getattr(spec_after_ep, k, None)}
        if updates:
            state["fault_spec"] = spec_after_ep.replace(**updates).to_dict()

        # Stage 3: safety_check must now accept (spec is_complete)
        with patch("chaos_agent.agent.nodes.safety_check.sync_to_store",
                   new=AsyncMock()):
            sc_result = await safety_check(state)
        assert sc_result["safety_status"] != "rejected", (
            f"safety_check rejected even after lazy derivation: "
            f"{sc_result.get('safety_reason')}"
        )

        # And the derived names should be visible to baseline_capture
        final_spec = read_fault_spec(state)
        assert final_spec.names == ("my-node",)
        assert final_spec.scope == "node"


# ---------------------------------------------------------------------------
# Legacy fallback — old test fixtures still work but emit a warning
# ---------------------------------------------------------------------------


class TestLegacyFallbackWarning:
    """When a caller passes legacy ``state.target`` / ``state.blade_*``
    without a ``fault_spec``, ``read_fault_spec`` reconstructs from
    them but logs a WARNING — so the missing-spec bug surfaces in
    operator logs instead of being silently papered over."""

    def test_legacy_fallback_works_and_warns(self, caplog):
        import logging
        caplog.set_level(logging.WARNING)

        state = {
            "target": {"namespace": "ns", "names": ["legacy-pod"]},
            "blade_scope": "pod",
            "blade_target": "cpu",
            "blade_action": "fullload",
        }
        spec = read_fault_spec(state)
        assert spec is not None
        assert spec.namespace == "ns"
        assert spec.names == ("legacy-pod",)
        assert spec.scope == "pod"

        # Make sure the WARNING fired
        assert any(
            "fault_spec missing" in record.message and record.levelno == logging.WARNING
            for record in caplog.records
        ), "Legacy fallback should emit a WARNING about missing fault_spec"

    def test_no_fallback_when_spec_present(self, caplog):
        import logging
        caplog.set_level(logging.WARNING)

        spec = FaultSpec(scope="pod", namespace="ns", names=("p1",),
                         blade_target="cpu", blade_action="fullload")
        state = {"fault_spec": spec.to_dict()}
        retrieved = read_fault_spec(state)
        assert retrieved == spec

        # No warning emitted
        warns = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "fault_spec missing" in r.message
        ]
        assert warns == []
