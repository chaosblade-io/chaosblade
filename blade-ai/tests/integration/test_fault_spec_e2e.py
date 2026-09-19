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

from chaos_agent.agent.spec.fault_spec import (
    SOURCE_TUI,
    FaultSpec,
    read_fault_spec,
)
from chaos_agent.agent.nodes.baseline.baseline_capture import (
    BaselineCommand,
    _resolve_templates,
)
from chaos_agent.agent.nodes.gates.confirmation_gate import _freeze_from_state
from chaos_agent.agent.nodes.store.memory_nodes import load_memory
from chaos_agent.agent.nodes.gates.safety_check import safety_check
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
             patch("chaos_agent.agent.nodes.store.memory_nodes.OperationalMemory") as MockMem, \
             patch("chaos_agent.agent.nodes.store.memory_nodes.sync_to_store",
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
        with patch("chaos_agent.agent.nodes.gates.safety_check.sync_to_store",
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
        assert frozen["fault_target"] == "cpu"
        assert frozen["fault_action"] == "fullload"

        # Stage 6 — THE KEY ASSERTION: baseline_capture's template
        # resolution sees the right node name.
        cmds = [BaselineCommand(
            "Node CPU info", "kubectl describe node {node_name}",
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

        with patch("chaos_agent.agent.nodes.gates.safety_check.sync_to_store",
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
            "params": {"percent": "80"},
            "params_flags": ["read"],
            # New duration contract: duration travels ONLY through
            # ``duration`` / ``duration_seconds`` — params.timeout is
            # rejected outright (regression anchor).
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
        with patch("chaos_agent.agent.nodes.gates.safety_check.sync_to_store",
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
            "Pod inspect", "kubectl describe pod {pod_name} -n {namespace}",
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
            fault_target="mem", fault_action="ram",
            params={"size": "100"},
        )
        state = {"fault_spec": spec.to_dict()}
        approved = _freeze_from_state(state)

        assert approved is not None
        assert approved["scope"] == spec.scope
        assert approved["namespace"] == spec.namespace
        assert approved["names"] == list(spec.names)
        assert approved["labels"] == dict(spec.labels)
        assert approved["fault_target"] == spec.fault_target
        assert approved["fault_action"] == spec.fault_action
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
    (route_pipeline_start checks interaction_mode). The placeholder
    spec written at entry has empty scope/blade_target/blade_action/names
    (B76 lazy-derivation — from_cli_nl anchors nothing except the
    prepositional node form; "对节点 X 注入" deliberately does not).

    For the inject to actually proceed, the spec must get populated
    DURING agent_loop's planning phase — either:
      - extract_planning_metadata lands the planner's explicit identity
        declaration (finish_planning fault_scope/target/action — after
        the #49 hijack legislation NOTHING is mined from
        skill_case_content; a case doc is a menu, not the contract), OR
      - agent_loop derives namespace/names from LLM's kubectl get probes.

    Without that, safety_check rejects with "No target specified".
    """

    async def test_cli_nl_placeholder_spec_can_reach_safety_check(
        self, tmp_memory_dir, monkeypatch,
    ):
        """Full CLI NL flow: entry placeholder → LLM planning →
        extract_planning_metadata lands the declared identity triple
        + agent_loop derives namespace/names → safety_check sees a
        complete spec and accepts."""
        from langchain_core.messages import AIMessage, ToolMessage
        from chaos_agent.agent.nodes.planning.extract_planning_metadata import (
            extract_planning_metadata,
        )
        from chaos_agent.agent.nodes.execute.agent_loop import (
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
                # The planner's final round: the identity travels on the
                # finish_planning call itself (the control-signal carrier
                # the tool body never consumes). Post-#49 this declaration
                # is the ONLY path the identity reaches the spec — the blade
                # command in the case doc above is deliberately ignored.
                AIMessage(
                    content="",
                    tool_calls=[{
                        "name": "finish_planning",
                        "args": {
                            "summary": "plan",
                            "fault_scope": "node",
                            "fault_target": "cpu",
                            "fault_action": "fullload",
                        },
                        "id": "tc_fp",
                    }],
                ),
                ToolMessage(
                    content="Planning finalized. Summary: plan",
                    tool_call_id="tc_fp",
                    name="finish_planning",
                ),
            ],
        }

        # Stage 1: extract_planning_metadata lands the DECLARED identity
        ep_result = await extract_planning_metadata(state)
        state.update(ep_result)
        spec_after_ep = read_fault_spec(state)
        assert spec_after_ep.scope == "node", (
            "extract_planning_metadata should land the declared scope "
            "(post-#49: never mined from the skill-case document)"
        )
        assert spec_after_ep.fault_target == "cpu"
        assert spec_after_ep.fault_action == "fullload"

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
        with patch("chaos_agent.agent.nodes.gates.safety_check.sync_to_store",
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
# B14: duration contract backfill on the CLI NL planning exit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestDurationContractBackfill:
    """B14: the CLI NL path's duration contract must not stay empty.

    from_cli_nl intentionally leaves duration_seconds=0 for an intent
    node the pipeline route never visits (route_pipeline_start sends CLI
    NL straight to agent_loop). Without the planning-exit backfill the
    contract is empty end-to-end (audit snapshots show duration_seconds=0)
    and kubectl-native sleep timers ran with no recorded window
    (#8/#9 evidence: user-asked 300s executed as 300s, unrecorded).

    The planner's finish_planning now carries an explicit duration_seconds
    declaration; extract_planning_metadata combines it with any hard-pinned
    entry value (CLI --duration) monotonically and applies
    ensure_min_duration (unspecified → configured default; declared
    values verbatim).
    """

    @pytest.fixture(autouse=True)
    def _isolate_experiment_timeout(self):
        # Pin the operator default to the code floor so floor assertions
        # do not depend on the host machine's ~/.blade-ai/config.json (a
        # stale experiment_timeout there wins the unspecified-duration
        # path via max(configured, floor)).
        from chaos_agent.config.settings import blade_ai_context
        from chaos_agent.utils.fault_type import _DEFAULT_MIN_DURATION

        with blade_ai_context(experiment_timeout=_DEFAULT_MIN_DURATION):
            yield

    def _nl_state(self, messages: list, *, duration: int = 0) -> dict:
        placeholder = FaultSpec.from_cli_nl(
            input_text="对节点 my-node 注入 CPU 满载",
            kwargs={"duration": duration},
        )
        return {
            "task_id": "task-dur-backfill",
            "operation": "inject",
            "interaction_mode": "cli",
            "fault_spec": placeholder.to_dict(),
            "input": placeholder.user_description,
            "skill_name": "k8s-chaos-skills",
            "needs_confirmation": False,
            "safety_status": "pending",
            "messages": messages,
        }

    def _planning_messages(self, *finish_args_list: dict) -> list:
        """Message history ending in one finish_planning round per args dict.

        The LAST round is the planning exit the node reads; earlier rounds
        model nudge/replan re-runs that the newest declaration supersedes.
        Every round carries the fault-identity declaration
        (fault_scope/fault_target/fault_action): the CLI NL entry point
        leaves the spec's identity empty (B76 lazy-derivation — nothing is
        mined at entry), and after the #49 hijack legislation the identity
        lands on the spec ONLY from this planner declaration, never from
        the skill-case document.
        """
        from langchain_core.messages import AIMessage, ToolMessage

        skill_case = (
            "**故障现象**：节点 CPU 满载\n\n"
            "**注入命令**：\n"
            "```\n"
            "blade create node-cpu fullload --cpu-percent 80\n"
            "```\n"
            "**注入验证**：node CPU usage > 90%\n"
        )
        messages: list = [
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
                content=skill_case,
                tool_call_id="tc_read",
                name="read_skill_resource",
            ),
        ]
        for i, extra_args in enumerate(finish_args_list):
            tc_id = f"tc_fp_{i}"
            messages.append(AIMessage(
                content="",
                tool_calls=[{
                    "name": "finish_planning",
                    "args": {
                        "summary": "plan",
                        "fault_scope": "node",
                        "fault_target": "cpu",
                        "fault_action": "fullload",
                        **extra_args,
                    },
                    "id": tc_id,
                }],
            ))
            messages.append(ToolMessage(
                content="Planning finalized. Summary: plan",
                tool_call_id=tc_id,
                name="finish_planning",
            ))
        return messages

    async def _run_extract(self, state: dict) -> dict:
        from chaos_agent.agent.nodes.planning.extract_planning_metadata import (
            extract_planning_metadata,
        )
        return await extract_planning_metadata(state)

    async def test_declared_duration_below_floor_preserved(
        self, tmp_memory_dir, monkeypatch,
    ):
        """finish_planning(duration_seconds=100) → the explicit 100s is
        honoured verbatim (l4-contract-faithfulness: below-floor values
        warn, never clamp upward); the identity declaration on the same
        call still lands on the spec alongside."""
        monkeypatch.setattr(settings, "working_dir", tmp_memory_dir.parent)
        monkeypatch.setattr(settings, "kubeconfig_path", "")

        state = self._nl_state(
            self._planning_messages({"duration_seconds": 100}),
        )
        result = await self._run_extract(state)

        spec = read_fault_spec({**state, **result})
        assert spec is not None
        assert spec.duration_seconds == 100, (
            "declared 100s must pass through verbatim, not be clamped "
            "up to the 300s floor"
        )
        assert spec.scope == "node"
        assert spec.fault_target == "cpu"
        assert spec.fault_action == "fullload"

    async def test_undeclared_duration_gets_floor(
        self, tmp_memory_dir, monkeypatch,
    ):
        """No declaration + no entry --duration → floor default (300)."""
        monkeypatch.setattr(settings, "working_dir", tmp_memory_dir.parent)
        monkeypatch.setattr(settings, "kubeconfig_path", "")

        state = self._nl_state(self._planning_messages({}))
        result = await self._run_extract(state)

        spec = read_fault_spec({**state, **result})
        assert spec is not None
        assert spec.duration_seconds == 300, (
            "undeclared duration must land on the floor default, not stay 0"
        )

    async def test_hard_pinned_duration_not_downgraded(
        self, tmp_memory_dir, monkeypatch,
    ):
        """Entry --duration 900 (hard-pinned) + no declaration → stays 900;
        the contract is monotonic — declarations and floors only raise it."""
        monkeypatch.setattr(settings, "working_dir", tmp_memory_dir.parent)
        monkeypatch.setattr(settings, "kubeconfig_path", "")

        state = self._nl_state(
            self._planning_messages({}), duration=900,
        )
        result = await self._run_extract(state)

        spec = read_fault_spec({**state, **result})
        assert spec is not None
        assert spec.duration_seconds == 900

    async def test_latest_declaration_wins(
        self, tmp_memory_dir, monkeypatch,
    ):
        """A superseded finish_planning(900) round must NOT leak its
        declaration after a newer undeclared round — the newest planning
        exit governs, so the floor default (300) applies, not 900."""
        monkeypatch.setattr(settings, "working_dir", tmp_memory_dir.parent)
        monkeypatch.setattr(settings, "kubeconfig_path", "")

        state = self._nl_state(
            self._planning_messages(
                {"duration_seconds": 900},   # superseded round
                {},                           # newest planning exit
            ),
        )
        result = await self._run_extract(state)

        spec = read_fault_spec({**state, **result})
        assert spec is not None
        assert spec.duration_seconds == 300, (
            "the newest finish_planning round governs; the superseded "
            "900s declaration must not leak through"
        )

    async def test_complete_spec_duration_untouched(
        self, tmp_memory_dir, monkeypatch,
    ):
        """TUI path: the spec is already complete (duration set at intent
        convergence) and the newest finish_planning declares nothing →
        the node is a no-op for the spec (idempotent, no rewrite)."""
        monkeypatch.setattr(settings, "working_dir", tmp_memory_dir.parent)
        monkeypatch.setattr(settings, "kubeconfig_path", "")

        spec = FaultSpec(
            scope="node", namespace="", names=("my-node",),
            fault_target="cpu", fault_action="fullload",
            duration_seconds=300,
        )
        state = {
            "task_id": "task-dur-tui",
            "operation": "inject",
            "interaction_mode": "tui",
            "fault_spec": spec.to_dict(),
            "input": "",
            "skill_name": "k8s-chaos-skills",
            "needs_confirmation": False,
            "safety_status": "pending",
            "messages": self._planning_messages({}),
        }
        result = await self._run_extract(state)

        assert result.get("fault_spec") is None, (
            "a complete spec with an at-floor duration must not be rewritten"
        )

    async def test_case_resource_path_backfilled(
        self, tmp_memory_dir, monkeypatch,
    ):
        """Same planning-exit spec-backfill family: the planner already
        hands the chosen case path to finish_planning (skill_case_content
        extraction reads it), but the spec's own case_resource_path
        audit/hint field stayed empty (B-项观察项). Write-once — a
        dialogue-settled path on the spec always wins."""
        monkeypatch.setattr(settings, "working_dir", tmp_memory_dir.parent)
        monkeypatch.setattr(settings, "kubeconfig_path", "")

        state = self._nl_state(
            self._planning_messages({
                "skill_case_resource": "references/catalogue/Node_CPU/cpu_fullload.md",
            }),
        )
        result = await self._run_extract(state)

        spec = read_fault_spec({**state, **result})
        assert spec is not None
        assert spec.case_resource_path == (
            "references/catalogue/Node_CPU/cpu_fullload.md"
        )
        # The duration floor backfill still applies alongside.
        assert spec.duration_seconds == 300


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
            "fault_scope": "pod",
            "fault_target": "cpu",
            "fault_action": "fullload",
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
                         fault_target="cpu", fault_action="fullload")
        state = {"fault_spec": spec.to_dict()}
        retrieved = read_fault_spec(state)
        assert retrieved == spec

        # No warning emitted
        warns = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "fault_spec missing" in r.message
        ]
        assert warns == []
