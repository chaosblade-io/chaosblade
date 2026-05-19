"""Tests for intent_clarification node — dialogue, routing, and fault convergence."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chaos_agent.agent.nodes.intent_clarification import (
    CLASSIFY_INTENT_TOOL,
    _ensure_visible_content,
    _extract_classify_intent,
    _merge_known_params_into_fault_intent,
    submit_fault_intent,
    MAX_DIALOGUE_ROUNDS,
    make_intent_clarification,
)
from chaos_agent.agent.prompts.sections.intent import (
    get_intent_completeness_section,
)


def _make_llm_response(tool_calls=None, content=""):
    """Create a proper AIMessage with given tool_calls and content."""
    return AIMessage(
        content=content,
        tool_calls=tool_calls or [],
        id="test_msg_id",
        response_metadata={},
    )


def _classify_tc(intent: str, confidence: float):
    return {
        "name": "classify_intent",
        "id": f"call_cls_{intent}",
        "args": {"intent": intent, "confidence": confidence},
    }


def _submit_fault_tc(**kwargs):
    """Create a submit_fault_intent tool call with the structured signature.

    Defaults to a minimum valid set (the 4 required fields + a sensible
    namespace) so most callers can just do ``_submit_fault_tc()``. Pass
    keyword overrides to model partial / mismatched LLM submissions.
    """
    defaults = {
        "fault_type": "pod-cpu-fullload",
        "scope": "pod",
        "target": "cpu",
        "action": "fullload",
        "namespace": "default",
    }
    defaults.update(kwargs)
    return {
        "name": "submit_fault_intent",
        "id": "call_submit_1",
        "args": defaults,
    }


def _ask_human_tc(question: str = "What do you mean?"):
    return {
        "name": "ask_human",
        "id": "call_ask_1",
        "args": {"question": question},
    }


class TestExtractClassifyIntent:

    def test_extracts_valid_intents(self):
        for intent in ("recover", "chat"):
            result = _extract_classify_intent([_classify_tc(intent, 0.9)])
            assert result["intent"] == intent
            assert result["confidence"] == 0.9

    def test_clamps_invalid_intent_to_chat(self):
        # Old intents that the LLM might produce from training memory must
        # all collapse to chat (the safety net).
        for stale in ("inject", "query", "explore"):
            tc = [{"name": "classify_intent", "args": {"intent": stale, "confidence": 0.5}}]
            result = _extract_classify_intent(tc)
            assert result["intent"] == "chat"

    def test_clamps_confidence_out_of_range(self):
        result = _extract_classify_intent([_classify_tc("chat", 1.5)])
        assert result["confidence"] == 1.0
        result2 = _extract_classify_intent([_classify_tc("chat", -0.3)])
        assert result2["confidence"] == 0.0

    def test_parses_string_confidence(self):
        tc = [{"name": "classify_intent", "args": {"intent": "chat", "confidence": "0.8"}}]
        result = _extract_classify_intent(tc)
        assert result["confidence"] == 0.8

    def test_returns_none_for_empty_tool_calls(self):
        assert _extract_classify_intent([]) is None

    def test_ignores_non_classify_intent_calls(self):
        assert _extract_classify_intent([_ask_human_tc()]) is None


class TestSubmitFaultIntentTool:
    """Tests for the real submit_fault_intent @tool function."""

    def test_submit_fault_intent_returns_ack(self):
        result = submit_fault_intent.invoke({
            "fault_type": "node-cpu-fullload",
            "scope": "node",
            "target": "cpu",
            "action": "fullload",
            "namespace": "default",
        })
        assert "已提交" in result

    def test_submit_fault_intent_with_optional_args(self):
        # Full structured submission with every optional field — what
        # the prompt now instructs the LLM to do.
        result = submit_fault_intent.invoke({
            "fault_type": "pod-network-delay",
            "scope": "pod",
            "target": "network",
            "action": "delay",
            "namespace": "cms-demo",
            "labels": {"app": "nginx"},
            "params": {"percent": "80", "timeout": "600"},
            "user_description": "给 nginx 注入 80% 网络延迟",
        })
        assert "已提交" in result

    def test_submit_fault_intent_namespace_defaults(self):
        # namespace omitted → default "default" applies via signature
        # default. node-scope conventionally uses default namespace.
        result = submit_fault_intent.invoke({
            "fault_type": "node-cpu-fullload",
            "scope": "node",
            "target": "cpu",
            "action": "fullload",
        })
        assert "已提交" in result

    def test_submit_fault_intent_args_schema_has_required_fields(self):
        # Schema dump sanity: the @lc_tool decorator must surface the
        # five required-or-defaulted fields plus the four optionals so
        # the LLM bound to this tool sees the full structure.
        schema = submit_fault_intent.args_schema.model_json_schema()
        props = set(schema.get("properties", {}).keys())
        required = set(schema.get("required", []))
        assert {"fault_type", "scope", "target", "action"} <= required
        assert {"namespace", "names", "labels", "params", "user_description"} <= props


class TestIntentClarificationNode:

    @pytest.mark.asyncio
    async def test_already_confirmed_intent_pass_through(self):
        node = make_intent_clarification(llm=None)
        result = await node({"confirmed_intent": "inject", "messages": []})
        assert result == {}

    @pytest.mark.asyncio
    async def test_no_llm_defaults_to_chat(self):
        node = make_intent_clarification(llm=None)
        result = await node({"confirmed_intent": None, "messages": []})
        assert result["confirmed_intent"] == "chat"

    @pytest.mark.asyncio
    async def test_max_dialogue_rounds_forces_goodbye(self):
        node = make_intent_clarification(llm=AsyncMock())
        state = {"confirmed_intent": None, "messages": [],
                 "clarification_round": 0, "dialogue_round": MAX_DIALOGUE_ROUNDS}
        result = await node(state)
        assert result["confirmed_intent"] == "chat"
        assert "再见" in result["messages"][0].content

    @pytest.mark.asyncio
    async def test_submit_fault_intent_fast_path_bootstraps_session_store(
        self, tmp_path
    ):
        """Regression: when the fast-path allocates ``op_task_id`` it
        MUST also call ``SessionStore.create_session(...)`` so the
        on-disk ``memory/tasks/<op_task_id>.json`` exists. Before the
        fix the TUI ``/turn`` flow never registered the task with the
        SessionStore, leaving ``memory/tasks/`` empty for every TUI-
        mode injection — replay had nothing to play, and the boot
        ``PendingTasksCard`` had no way to pick up the in-flight
        injection on TUI restart.
        """
        from chaos_agent.memory.session_store import (
            SessionStore,
            set_global_session_store,
        )
        store = SessionStore(task_dir=tmp_path / "tasks")
        set_global_session_store(store)
        try:
            mock_llm = AsyncMock()
            ai_msg = AIMessage(
                content="",
                tool_calls=[_submit_fault_tc(
                    fault_type="pod-cpu-fullload",
                    scope="pod",
                    target="cpu",
                    action="fullload",
                    namespace="production",
                )],
                id="ai_submit_bootstrap",
            )
            tool_msg = ToolMessage(
                content="✓ 故障注入意图已提交。",
                name="submit_fault_intent",
                tool_call_id="call_submit_bootstrap",
            )
            messages = [
                HumanMessage(content="执行", id="human_b"),
                ai_msg,
                tool_msg,
            ]
            node = make_intent_clarification(llm=mock_llm)
            state = {
                "confirmed_intent": None,
                "messages": messages,
                "clarification_round": 0,
                "dialogue_round": 2,
                "fault_intent": {},
                "tui_session_id": "sess_bootstrap_test",
            }
            result = await node(state)
            assert result["confirmed_intent"] == "inject"
            op_task_id = result["task_id"]
            assert op_task_id.startswith("task-")
            # The on-disk JSON file must exist immediately after the
            # fast-path returns.
            task_json = tmp_path / "tasks" / f"{op_task_id}.json"
            assert task_json.exists(), (
                f"Expected SessionStore to create {task_json} when the "
                "inject fast-path allocates op_task_id; got nothing."
            )
            import json as _json
            data = _json.loads(task_json.read_text())
            assert data["taskId"] == op_task_id
            assert data["operation"] == "inject"
            assert data["tui_session_id"] == "sess_bootstrap_test"
            # The IntentClarificationSummary handoff must be the first
            # entry in the task file (P0-7-6 contract).
            assert len(data["messages"]) >= 1
            first = data["messages"][0]
            assert first["type"] == "system"
            assert first["content"].startswith("[Intent Clarification Summary]")
        finally:
            set_global_session_store(None)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_submit_fault_intent_fast_path(self):
        """Fast-path: when a trailing ToolMessage is from submit_fault_intent
        and the source AIMessage carries structured args, the node skips
        the LLM call and transitions to confirmed_intent='inject' using
        the LLM-supplied args directly."""
        mock_llm = AsyncMock()

        # AIMessage carries the submit tool_call with full structured args.
        # ToolNode then runs the tool and produces the ToolMessage below.
        # ``_extract_submit_args`` walks back from the end skipping
        # ToolMessages until it finds this AIMessage.
        ai_msg = AIMessage(
            content="",
            tool_calls=[_submit_fault_tc(
                fault_type="pod-cpu-fullload",
                scope="pod",
                target="cpu",
                action="fullload",
                namespace="production",
                labels={"app": "account"},
            )],
            id="ai_submit_1",
        )
        tool_msg = ToolMessage(
            content="✓ 故障注入意图已提交，正在进入执行确认阶段。",
            name="submit_fault_intent",
            tool_call_id="call_submit_1",
        )
        human_msg = HumanMessage(content="执行", id="human_1")
        messages = [human_msg, ai_msg, tool_msg]

        node = make_intent_clarification(llm=mock_llm)
        state = {
            "confirmed_intent": None,
            "messages": messages,
            "clarification_round": 0,
            "dialogue_round": 2,
            "fault_intent": {},
        }
        result = await node(state)
        assert result["confirmed_intent"] == "inject"
        # Values must come from the LLM's structured args, not from
        # programmatic regex extraction of the dialogue.
        assert result["fault_intent"]["fault_type"] == "pod-cpu-fullload"
        assert result["fault_intent"]["scope"] == "pod"
        assert result["fault_intent"]["namespace"] == "production"
        assert result["fault_intent"]["labels"] == {"app": "account"}
        assert result["intent_confidence"] == 1.0
        # LLM should NOT have been called (fast-path skips it)
        mock_llm.bind_tools.assert_not_called()

    @pytest.mark.asyncio
    async def test_fast_path_detects_submit_in_tool_batch(self):
        """Fast-path works even if submit_fault_intent is not the last
        ToolMessage in a batch (e.g. model called both kubectl and
        submit_fault_intent in the same AIMessage)."""
        mock_llm = AsyncMock()

        human_msg = HumanMessage(content="执行", id="human_1")
        ai_msg = AIMessage(
            content="",
            tool_calls=[
                {"name": "kubectl", "id": "call_kubectl_1",
                 "args": {"subcommand": "get"}},
                _submit_fault_tc(
                    fault_type="pod-cpu-fullload",
                    scope="pod",
                    target="cpu",
                    action="fullload",
                    namespace="production",
                ),
            ],
            id="ai_batch_1",
        )
        # ToolNode processed both tools — submit first, kubectl second.
        # Trailing ToolMessage order does not matter to fast-path.
        submit_tool_msg = ToolMessage(
            content="✓ 故障注入意图已提交",
            name="submit_fault_intent",
            tool_call_id="call_submit_1",
        )
        kubectl_tool_msg = ToolMessage(
            content="NAME   READY   STATUS\npod-1  1/1     Running",
            name="kubectl",
            tool_call_id="call_kubectl_1",
        )
        messages = [human_msg, ai_msg, submit_tool_msg, kubectl_tool_msg]

        node = make_intent_clarification(llm=mock_llm)
        state = {
            "confirmed_intent": None,
            "messages": messages,
            "clarification_round": 0,
            "dialogue_round": 2,
            "fault_intent": {},
        }
        result = await node(state)
        assert result["confirmed_intent"] == "inject"
        assert result["fault_intent"]["scope"] == "pod"
        mock_llm.bind_tools.assert_not_called()

    @pytest.mark.asyncio
    async def test_classify_recover_routes_correctly(self):
        mock_llm = AsyncMock()
        response = _make_llm_response(
            tool_calls=[_classify_tc("recover", 0.9)])
        mock_llm.bind_tools = MagicMock(
            return_value=AsyncMock(ainvoke=AsyncMock(return_value=response)))

        node = make_intent_clarification(llm=mock_llm)
        state = {"confirmed_intent": None, "messages": [MagicMock()],
                 "clarification_round": 0, "dialogue_round": 0}
        result = await node(state)
        assert result["confirmed_intent"] == "recover"

    @pytest.mark.asyncio
    async def test_chat_with_ask_human_still_exits(self):
        """Multi-invocation model: classify_intent(chat) always means goodbye,
        even if ask_human is also called. ask_human gets stripped."""
        mock_llm = AsyncMock()
        response = _make_llm_response(
            content="你好！我是 Chaos Agent。",
            tool_calls=[
                _classify_tc("chat", 0.9),
                _ask_human_tc("想了解故障注入能力吗？"),
            ],
        )
        mock_llm.bind_tools = MagicMock(
            return_value=AsyncMock(ainvoke=AsyncMock(return_value=response)))

        node = make_intent_clarification(llm=mock_llm)
        state = {"confirmed_intent": None, "messages": [MagicMock()],
                 "clarification_round": 0, "dialogue_round": 0}
        result = await node(state)
        assert result["confirmed_intent"] == "chat"
        assert result["intent_confidence"] == 0.9
        msg = result["messages"][0]
        assert msg.content == "你好！我是 Chaos Agent。"
        # ask_human is stripped (it's not classify_intent/submit_fault_intent,
        # but _filter_internal_tools_from_response keeps non-internal tools)
        # In practice ask_human passes through the filter since it's not internal
        filtered_tc = msg.tool_calls
        assert all(tc["name"] != "classify_intent" for tc in filtered_tc)

    @pytest.mark.asyncio
    async def test_chat_without_ask_human_exits(self):
        mock_llm = AsyncMock()
        response = _make_llm_response(
            content="再见！",
            tool_calls=[_classify_tc("chat", 0.95)],
        )
        mock_llm.bind_tools = MagicMock(
            return_value=AsyncMock(ainvoke=AsyncMock(return_value=response)))

        node = make_intent_clarification(llm=mock_llm)
        state = {"confirmed_intent": None, "messages": [MagicMock()],
                 "clarification_round": 0, "dialogue_round": 0}
        result = await node(state)
        assert result["confirmed_intent"] == "chat"

    @pytest.mark.asyncio
    async def test_ask_human_only_routes_to_tools(self):
        mock_llm = AsyncMock()
        response = _make_llm_response(
            content="让我了解一下你想做什么。",
            tool_calls=[_ask_human_tc("你想注入什么类型的故障？")],
        )
        mock_llm.bind_tools = MagicMock(
            return_value=AsyncMock(ainvoke=AsyncMock(return_value=response)))

        node = make_intent_clarification(llm=mock_llm)
        state = {"confirmed_intent": None, "messages": [MagicMock()],
                 "clarification_round": 0, "dialogue_round": 0}
        result = await node(state)
        assert "confirmed_intent" not in result
        assert result["clarification_round"] == 1
        assert result["dialogue_round"] == 1

    @pytest.mark.asyncio
    async def test_pure_text_response_continues(self):
        mock_llm = AsyncMock()
        response = _make_llm_response(content="Hello there!")
        mock_llm.bind_tools = MagicMock(
            return_value=AsyncMock(ainvoke=AsyncMock(return_value=response)))

        node = make_intent_clarification(llm=mock_llm)
        state = {"confirmed_intent": None, "messages": [MagicMock()],
                 "clarification_round": 0, "dialogue_round": 0}
        result = await node(state)
        assert "confirmed_intent" not in result
        assert result["dialogue_round"] == 1

    @pytest.mark.asyncio
    async def test_llm_failure_fallback_to_chat(self):
        mock_llm = AsyncMock()
        mock_llm.bind_tools = MagicMock(
            return_value=AsyncMock(ainvoke=AsyncMock(side_effect=Exception("boom"))))

        node = make_intent_clarification(llm=mock_llm)
        state = {"confirmed_intent": None, "messages": [MagicMock()],
                 "clarification_round": 0, "dialogue_round": 0}
        result = await node(state)
        assert result["confirmed_intent"] == "chat"

    @pytest.mark.asyncio
    async def test_classify_intent_schema(self):
        assert CLASSIFY_INTENT_TOOL["name"] == "classify_intent"
        props = CLASSIFY_INTENT_TOOL["parameters"]["properties"]
        assert "intent" in props
        assert "confidence" in props
        assert props["intent"]["enum"] == ["recover", "chat"]

    @pytest.mark.asyncio
    async def test_submit_fault_intent_is_real_tool(self):
        """submit_fault_intent is now a real @tool with a structured schema
        (fault_type / scope / target / action / namespace + optional fields)."""
        assert submit_fault_intent.name == "submit_fault_intent"
        # It should be callable with the new structured signature and
        # return the ack string consumed by the dialogue gateway.
        result = submit_fault_intent.invoke({
            "fault_type": "pod-cpu-fullload",
            "scope": "pod",
            "target": "cpu",
            "action": "fullload",
            "namespace": "default",
        })
        assert "已提交" in result

    @pytest.mark.asyncio
    async def test_submit_fault_intent_tool_call_goes_to_toolnode(self):
        """When model calls submit_fault_intent, it routes to ToolNode
        (Priority 2: has_tool_calls) — not directly to inject."""
        mock_llm = AsyncMock()
        response = _make_llm_response(
            content="好的，提交故障注入意图。",
            tool_calls=[_submit_fault_tc()],
        )
        mock_llm.bind_tools = MagicMock(
            return_value=AsyncMock(ainvoke=AsyncMock(return_value=response)))

        node = make_intent_clarification(llm=mock_llm)
        state = {"confirmed_intent": None, "messages": [MagicMock()],
                 "clarification_round": 0, "dialogue_round": 0}
        result = await node(state)
        # submit_fault_intent is a real tool → has_tool_calls path
        # No confirmed_intent yet (that happens after ToolNode + fast-path)
        assert "confirmed_intent" not in result
        assert result["clarification_round"] == 1
        # submit_fault_intent tool_call should be preserved in the message
        msg = result["messages"][0]
        assert any(tc["name"] == "submit_fault_intent" for tc in msg.tool_calls)

    @pytest.mark.asyncio
    async def test_submit_fault_with_other_tools_all_pass_through(self):
        """submit_fault_intent + ask_human: both are real tools, both pass through
        to ToolNode (Priority 2: has_tool_calls path)."""
        mock_llm = AsyncMock()
        response = _make_llm_response(
            content="好的，我来注入。",
            tool_calls=[_submit_fault_tc(), _ask_human_tc("确认一下？")],
        )
        mock_llm.bind_tools = MagicMock(
            return_value=AsyncMock(ainvoke=AsyncMock(return_value=response)))

        node = make_intent_clarification(llm=mock_llm)
        state = {"confirmed_intent": None, "messages": [MagicMock()],
                 "clarification_round": 0, "dialogue_round": 0}
        result = await node(state)
        # Both are real tools → has_tool_calls path, no confirmed_intent
        assert "confirmed_intent" not in result
        assert result["clarification_round"] == 1
        msg = result["messages"][0]
        # Both tool calls should be preserved for ToolNode
        assert any(tc["name"] == "submit_fault_intent" for tc in msg.tool_calls)

    @pytest.mark.asyncio
    async def test_classify_recover_strips_classify_tool_call(self):
        """classify_intent(recover) should not leave orphaned tool_call in messages."""
        mock_llm = AsyncMock()
        response = _make_llm_response(
            content="好的，我来帮你恢复。",
            tool_calls=[_classify_tc("recover", 0.9)])
        mock_llm.bind_tools = MagicMock(
            return_value=AsyncMock(ainvoke=AsyncMock(return_value=response)))

        node = make_intent_clarification(llm=mock_llm)
        state = {"confirmed_intent": None, "messages": [MagicMock()],
                 "clarification_round": 0, "dialogue_round": 0}
        result = await node(state)
        assert result["confirmed_intent"] == "recover"
        msg = result["messages"][0]
        assert not getattr(msg, "tool_calls", [])

    @pytest.mark.asyncio
    async def test_kubectl_tool_call_passes_through(self):
        """LLM calling kubectl (cluster Q&A) → no confirmed_intent,
        message passes through, rounds increment, ToolNode runs next."""
        mock_llm = AsyncMock()
        kubectl_tc = {
            "name": "kubectl",
            "id": "call_kubectl_1",
            "args": {"subcommand": "get", "args": ["pods", "-A"]},
        }
        response = _make_llm_response(
            content="Let me check the cluster state.",
            tool_calls=[kubectl_tc],
        )
        mock_llm.bind_tools = MagicMock(
            return_value=AsyncMock(ainvoke=AsyncMock(return_value=response)))

        node = make_intent_clarification(llm=mock_llm)
        state = {"confirmed_intent": None, "messages": [MagicMock()],
                 "clarification_round": 0, "dialogue_round": 0}
        result = await node(state)

        assert "confirmed_intent" not in result
        assert result["clarification_round"] == 1
        assert result["dialogue_round"] == 1
        # kubectl tool_call must remain so ToolNode picks it up.
        msg = result["messages"][0]
        assert any(tc["name"] == "kubectl" for tc in msg.tool_calls)

    @pytest.mark.asyncio
    async def test_read_skill_resource_tool_call_passes_through(self):
        """LLM calling read_skill_resource (capability Q&A) → same path:
        no confirmed_intent, ToolNode runs, then back to intent_clarification."""
        mock_llm = AsyncMock()
        read_tc = {
            "name": "read_skill_resource",
            "id": "call_read_1",
            "args": {"resource": "chaos_types.yaml"},
        }
        response = _make_llm_response(
            content="Let me look up the available chaos types.",
            tool_calls=[read_tc],
        )
        mock_llm.bind_tools = MagicMock(
            return_value=AsyncMock(ainvoke=AsyncMock(return_value=response)))

        node = make_intent_clarification(llm=mock_llm)
        state = {"confirmed_intent": None, "messages": [MagicMock()],
                 "clarification_round": 0, "dialogue_round": 0}
        result = await node(state)

        assert "confirmed_intent" not in result
        assert result["clarification_round"] == 1
        msg = result["messages"][0]
        assert any(tc["name"] == "read_skill_resource" for tc in msg.tool_calls)

    @pytest.mark.asyncio
    async def test_chat_classify_does_not_increment_rounds(self):
        """classify_intent(chat) exits immediately — no round increment in result."""
        mock_llm = AsyncMock()
        response = _make_llm_response(
            content="你好！",
            tool_calls=[
                _classify_tc("chat", 0.9),
                _ask_human_tc("想了解什么？"),
            ],
        )
        mock_llm.bind_tools = MagicMock(
            return_value=AsyncMock(ainvoke=AsyncMock(return_value=response)))

        node = make_intent_clarification(llm=mock_llm)
        state = {"confirmed_intent": None, "messages": [MagicMock()],
                 "clarification_round": 2, "dialogue_round": 5}
        result = await node(state)
        assert result["confirmed_intent"] == "chat"
        assert "dialogue_round" not in result
        assert "clarification_round" not in result


class TestExtractSubmitArgsCoercion:
    """Pin the coercion rules in ``_extract_submit_args`` for tool_call
    args that arrive in non-canonical shapes.

    Real-world background: some LLM function-calling builds (notably
    qwen variants) JSON-stringify ``list[str]`` and ``dict[str, str]``
    arguments instead of nesting them as proper JSON arrays / objects.
    The arg arrives as e.g. ``params="{\\\"percent\\\":\\\"80\\\"}"``
    instead of ``params={"percent":"80"}``. The previous extractor did
    ``(args.get("params") or {}).items()``, which on a string blew
    up with ``AttributeError: 'str' object has no attribute 'items'``
    and surfaced as a turn-level crash for the user (task: turn-...).

    These tests pin the layered coercion so a future refactor can't
    silently regress the recovery path.
    """

    def _build_messages(self, args: dict) -> list:
        """Helper: synthesise the AIMessage + ToolMessage pair the
        intent_clarification fast-path expects to see."""
        return [
            HumanMessage(content="对节点注入 cpu 故障", id="h1"),
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "submit_fault_intent",
                    "id": "call_submit_qwen_str",
                    "args": args,
                }],
                id="ai_submit_qwen",
            ),
            ToolMessage(
                content="✓ 故障注入意图已提交",
                name="submit_fault_intent",
                tool_call_id="call_submit_qwen_str",
            ),
        ]

    @pytest.mark.asyncio
    async def test_json_stringified_list_and_dict_args_recovered(self):
        """Reproduces the original crash session: qwen serialised
        ``names`` as ``"[\\"node-1\\"]"`` and ``params`` as
        ``"{\\"percent\\":\\"80\\"}"``. Both must round-trip back to
        Python list / dict; nothing throws; fast-path commits intent."""
        mock_llm = AsyncMock()
        messages = self._build_messages({
            "fault_type": "node-cpu-fullload",
            "scope": "node",
            "target": "cpu",
            "action": "fullload",
            "namespace": "cms-demo",
            # JSON-stringified — the bug shape.
            "names": '["cn-hongkong.10.0.1.63"]',
            "params": '{"percent": "80", "timeout": "600"}',
            "user_description": "对节点 cn-hongkong.10.0.1.63 注入 CPU 满载",
        })
        node = make_intent_clarification(llm=mock_llm)
        result = await node({
            "confirmed_intent": None,
            "messages": messages,
            "clarification_round": 0,
            "dialogue_round": 1,
            "fault_intent": {},
        })
        assert result["confirmed_intent"] == "inject"
        fi = result["fault_intent"]
        assert fi["names"] == ["cn-hongkong.10.0.1.63"]
        assert fi["params"] == {"percent": "80", "timeout": "600"}
        # LLM should NOT have been re-invoked — fast-path committed.
        mock_llm.bind_tools.assert_not_called()

    @pytest.mark.asyncio
    async def test_unparseable_dict_string_degrades_to_empty_dict(self):
        """If the JSON-shaped string is malformed, params degrades to
        ``{}`` instead of crashing; the programmatic fallback path
        (``_merge_known_params_into_fault_intent``) can still recover
        the real values from earlier dialogue."""
        mock_llm = AsyncMock()
        messages = self._build_messages({
            "fault_type": "node-cpu-fullload",
            "scope": "node",
            "target": "cpu",
            "action": "fullload",
            "namespace": "default",
            "names": ["node-1"],
            # Malformed JSON — close brace before the value.
            "params": '{"percent": 80,}',
        })
        node = make_intent_clarification(llm=mock_llm)
        result = await node({
            "confirmed_intent": None,
            "messages": messages,
            "clarification_round": 0,
            "dialogue_round": 1,
            "fault_intent": {},
        })
        # No crash; fast-path still commits the rest of the intent.
        assert result["confirmed_intent"] == "inject"
        assert result["fault_intent"]["names"] == ["node-1"]

    @pytest.mark.asyncio
    async def test_bare_string_name_wraps_to_single_element_list(self):
        """``names`` arriving as a bare non-JSON string (LLM dropped
        brackets when there was only one resource) wraps into a
        single-element list — same as the previous behaviour, kept
        for back-compat."""
        mock_llm = AsyncMock()
        messages = self._build_messages({
            "fault_type": "node-cpu-fullload",
            "scope": "node",
            "target": "cpu",
            "action": "fullload",
            "namespace": "default",
            # Bare string, NOT JSON-shaped.
            "names": "node-7",
            "params": {"percent": "80"},
        })
        node = make_intent_clarification(llm=mock_llm)
        result = await node({
            "confirmed_intent": None,
            "messages": messages,
            "clarification_round": 0,
            "dialogue_round": 1,
            "fault_intent": {},
        })
        assert result["confirmed_intent"] == "inject"
        assert result["fault_intent"]["names"] == ["node-7"]

    @pytest.mark.asyncio
    async def test_numeric_param_values_coerced_to_str(self):
        """LLMs occasionally emit numeric params (``80`` / ``true``)
        instead of strings. Downstream code formats params with
        ``%s`` / ``f"{k}={v}"``, which works either way, but we
        normalise to ``str`` so the inject pipeline sees a uniform
        ``dict[str, str]``."""
        mock_llm = AsyncMock()
        messages = self._build_messages({
            "fault_type": "pod-network-delay",
            "scope": "pod",
            "target": "network",
            "action": "delay",
            "namespace": "cms-demo",
            "names": ["nginx"],
            "params": {"percent": 80, "timeout": 600, "verbose": True},
        })
        node = make_intent_clarification(llm=mock_llm)
        result = await node({
            "confirmed_intent": None,
            "messages": messages,
            "clarification_round": 0,
            "dialogue_round": 1,
            "fault_intent": {},
        })
        assert result["confirmed_intent"] == "inject"
        assert result["fault_intent"]["params"] == {
            "percent": "80",
            "timeout": "600",
            "verbose": "True",
        }


class TestFastPathLLMArgsPriority:
    """Cover the LLM-args-first / programmatic-fallback merge logic.

    The fast-path now reads structured args directly from the most
    recent submit_fault_intent tool_call, with the regex-based
    ``_merge_known_params_into_fault_intent`` reduced to a safety net
    for legacy LLM builds that pass partial / no args. These four
    cases pin the priority ordering:

      existing fault_intent  <  programmatic fallback  <  LLM args (when non-empty)
    """

    @pytest.mark.asyncio
    async def test_llm_supplies_full_args(self):
        """Full structured submission — values come from LLM args, not regex."""
        mock_llm = AsyncMock()
        ai_msg = AIMessage(
            content="",
            tool_calls=[_submit_fault_tc(
                fault_type="pod-network-delay",
                scope="pod",
                target="network",
                action="delay",
                namespace="cms-demo",
                names=["nginx-7d4f-abc12"],
                params={"percent": "80", "timeout": "600"},
            )],
            id="ai_full_1",
        )
        tool_msg = ToolMessage(
            content="✓ 故障注入意图已提交",
            name="submit_fault_intent",
            tool_call_id="call_submit_1",
        )
        messages = [
            HumanMessage(content="给 cms-demo 注入 80% 网络延迟 10 分钟", id="h1"),
            ai_msg,
            tool_msg,
        ]
        node = make_intent_clarification(llm=mock_llm)
        result = await node({
            "confirmed_intent": None,
            "messages": messages,
            "clarification_round": 0,
            "dialogue_round": 1,
            "fault_intent": {},
        })
        assert result["confirmed_intent"] == "inject"
        fi = result["fault_intent"]
        assert fi["fault_type"] == "pod-network-delay"
        assert fi["scope"] == "pod"
        assert fi["target"] == "network"
        assert fi["action"] == "delay"
        assert fi["namespace"] == "cms-demo"
        assert fi["names"] == ["nginx-7d4f-abc12"]
        # ``params`` values are coerced to str by ``_extract_submit_args``.
        assert fi["params"] == {"percent": "80", "timeout": "600"}
        mock_llm.bind_tools.assert_not_called()

    @pytest.mark.asyncio
    async def test_llm_partial_args_fallback_fills_gap(self):
        """LLM omits namespace; regex fallback recovers it from the
        AI summary's ``**命名空间**：default`` line."""
        mock_llm = AsyncMock()
        ai_summary = AIMessage(
            content=(
                "故障注入意图摘要：\n"
                "* **故障类型**：CPU 满载\n"
                "* **作用范围**：Node\n"
                "* **目标节点**：cn-hongkong.10.0.1.101\n"
                "* **命名空间**：default (节点级故障默认)"
            ),
            id="ai_summary_1",
        )
        # Now the LLM sends submit_fault_intent but forgets namespace.
        ai_submit = AIMessage(
            content="",
            tool_calls=[_submit_fault_tc(
                fault_type="node-cpu-fullload",
                scope="node",
                target="cpu",
                action="fullload",
                namespace="",  # ← forgotten
                names=["cn-hongkong.10.0.1.101"],
            )],
            id="ai_submit_1",
        )
        tool_msg = ToolMessage(
            content="✓ 故障注入意图已提交",
            name="submit_fault_intent",
            tool_call_id="call_submit_1",
        )
        messages = [
            HumanMessage(content="注入cpu故障", id="h1"),
            ai_summary,
            HumanMessage(content="确认", id="h2"),
            ai_submit,
            tool_msg,
        ]
        node = make_intent_clarification(llm=mock_llm)
        result = await node({
            "confirmed_intent": None,
            "messages": messages,
            "clarification_round": 0,
            "dialogue_round": 3,
            "fault_intent": {},
        })
        assert result["confirmed_intent"] == "inject"
        # Namespace recovered by either: (a) regex fallback parsing the
        # AI summary, or (b) node-scope default. Either is acceptable
        # — both produce ``default`` for this case.
        assert result["fault_intent"]["namespace"] == "default"
        # LLM-supplied fields still win where present.
        assert result["fault_intent"]["fault_type"] == "node-cpu-fullload"
        assert result["fault_intent"]["names"] == ["cn-hongkong.10.0.1.101"]
        mock_llm.bind_tools.assert_not_called()

    @pytest.mark.asyncio
    async def test_llm_no_args_fallback_does_everything(self):
        """Older qwen builds may emit submit_fault_intent with empty args.
        The fallback regex extractor must still produce a complete
        intent — behaviour identical to the pre-fix code path."""
        mock_llm = AsyncMock()
        # Simulated old qwen: tool_call with no recognisable structured
        # args. ``_extract_submit_args`` returns {} → fallback runs.
        ai_submit = AIMessage(
            content="",
            tool_calls=[{
                "name": "submit_fault_intent",
                "id": "call_submit_1",
                "args": {},
            }],
            id="ai_submit_old",
        )
        tool_msg = ToolMessage(
            content="✓ 故障注入意图已提交",
            name="submit_fault_intent",
            tool_call_id="call_submit_1",
        )
        # Conversation history packed with regex-extractable signals so
        # the fallback can still complete the intent. The AI summary
        # uses the markdown-bold ``**命名空间**：cms-demo`` shape that
        # the production-shaped regex (post-bug-fix) recognises.
        messages = [
            HumanMessage(content="对 pod 注入 cpu 故障", id="h1"),
            AIMessage(
                content=(
                    "故障注入意图摘要：\n"
                    "* **作用范围**：pod\n"
                    "* **目标**：cpu\n"
                    "* **命名空间**：cms-demo\n"
                    "* **目标节点**：nginx-1"
                ),
                id="ai_summary_old",
            ),
            HumanMessage(content="确认", id="h2"),
            ai_submit,
            tool_msg,
        ]
        node = make_intent_clarification(llm=mock_llm)
        result = await node({
            "confirmed_intent": None,
            "messages": messages,
            "clarification_round": 0,
            "dialogue_round": 2,
            "fault_intent": {},
        })
        assert result["confirmed_intent"] == "inject"
        fi = result["fault_intent"]
        # Regex fallback recovers all four required fields.
        assert fi["scope"] == "pod"
        assert fi["target"] == "cpu"
        assert fi["action"] == "fullload"
        assert fi["namespace"] == "cms-demo"
        mock_llm.bind_tools.assert_not_called()

    @pytest.mark.asyncio
    async def test_node_scope_default_namespace_when_omitted(self):
        """LLM passes scope='node' but omits namespace and the dialogue
        contains no namespace hint anywhere — node-scope convention
        default ``"default"`` applies so the required check still passes."""
        mock_llm = AsyncMock()
        ai_submit = AIMessage(
            content="",
            tool_calls=[_submit_fault_tc(
                fault_type="node-cpu-fullload",
                scope="node",
                target="cpu",
                action="fullload",
                namespace="",  # ← omitted, no dialogue hint either
                names=["cn-hongkong.10.0.1.101"],
            )],
            id="ai_submit_1",
        )
        tool_msg = ToolMessage(
            content="✓ 故障注入意图已提交",
            name="submit_fault_intent",
            tool_call_id="call_submit_1",
        )
        # Deliberately sparse history — no namespace mentions anywhere
        # so neither LLM nor regex can produce one. Only the node-scope
        # default kicks in.
        messages = [
            HumanMessage(content="对节点注入 CPU 故障", id="h1"),
            HumanMessage(content="cn-hongkong.10.0.1.101", id="h2"),
            ai_submit,
            tool_msg,
        ]
        node = make_intent_clarification(llm=mock_llm)
        result = await node({
            "confirmed_intent": None,
            "messages": messages,
            "clarification_round": 0,
            "dialogue_round": 2,
            "fault_intent": {},
        })
        assert result["confirmed_intent"] == "inject"
        assert result["fault_intent"]["scope"] == "node"
        assert result["fault_intent"]["namespace"] == "default"
        mock_llm.bind_tools.assert_not_called()


class TestHookIntegration:
    """Tests for PreReasoningHook integration (merge_hook_updates)."""

    @pytest.mark.asyncio
    async def test_hook_updates_merged_not_overwritten(self):
        """Hook RemoveMessages + LLM response both appear in result messages."""
        from langchain_core.messages import RemoveMessage

        mock_llm = AsyncMock()
        response = _make_llm_response(content="再见！",
                                      tool_calls=[_classify_tc("chat", 0.95)])
        mock_llm.bind_tools = MagicMock(
            return_value=AsyncMock(ainvoke=AsyncMock(return_value=response)))

        hook_updates = {
            "messages": [
                RemoveMessage(id="old_msg_1"),
                RemoveMessage(id="old_msg_2"),
            ],
            "compressed_summary": "摘要内容",
        }
        mock_hook = AsyncMock(return_value=hook_updates)

        node = make_intent_clarification(llm=mock_llm, hook=mock_hook)
        state = {"confirmed_intent": None, "messages": [MagicMock()],
                 "clarification_round": 0, "dialogue_round": 0}
        result = await node(state)

        assert result["confirmed_intent"] == "chat"
        assert result["compressed_summary"] == "摘要内容"
        # Messages: [RemoveMessage x2] + [filtered AIMessage]
        msgs = result["messages"]
        remove_msgs = [m for m in msgs if isinstance(m, RemoveMessage)]
        assert len(remove_msgs) == 2
        ai_msgs = [m for m in msgs if isinstance(m, AIMessage)]
        assert len(ai_msgs) == 1
        assert ai_msgs[0].content == "再见！"

    @pytest.mark.asyncio
    async def test_hook_empty_does_not_affect_result(self):
        """When hook returns empty dict, result is unchanged."""
        mock_llm = AsyncMock()
        response = _make_llm_response(content="Hello!")
        mock_llm.bind_tools = MagicMock(
            return_value=AsyncMock(ainvoke=AsyncMock(return_value=response)))

        mock_hook = AsyncMock(return_value={})
        node = make_intent_clarification(llm=mock_llm, hook=mock_hook)
        state = {"confirmed_intent": None, "messages": [MagicMock()],
                 "clarification_round": 0, "dialogue_round": 0}
        result = await node(state)

        assert result["dialogue_round"] == 1
        assert len(result["messages"]) == 1
        assert result["messages"][0].content == "Hello!"

    @pytest.mark.asyncio
    async def test_fast_path_cleans_old_messages(self):
        """Fast-path emits RemoveMessages for old dialogue history."""
        from langchain_core.messages import HumanMessage, RemoveMessage

        mock_llm = AsyncMock()

        # Simulate 6 messages in state + final ToolMessage from submit_fault_intent
        old_messages = [
            HumanMessage(content=f"msg-{i}", id=f"msg_id_{i}")
            for i in range(5)
        ]
        tool_msg = ToolMessage(
            content="✓ 故障注入意图已提交",
            name="submit_fault_intent",
            tool_call_id="call_submit_1",
            id="tool_msg_id",
        )
        old_messages.append(tool_msg)

        node = make_intent_clarification(llm=mock_llm)
        state = {
            "confirmed_intent": None,
            "messages": old_messages,
            "clarification_round": 0,
            "dialogue_round": 3,
            "fault_intent": {
                "fault_type": "cpu-fullload",
                "scope": "pod",
                "target": "cpu",
                "action": "fullload",
                "namespace": "default",
                "labels": "app=myapp",
            },
        }
        result = await node(state)

        assert result["confirmed_intent"] == "inject"
        msgs = result["messages"]
        # Should have RemoveMessages for messages[:-4] = first 2 messages
        remove_msgs = [m for m in msgs if isinstance(m, RemoveMessage)]
        assert len(remove_msgs) == 2
        assert remove_msgs[0].id == "msg_id_0"
        assert remove_msgs[1].id == "msg_id_1"
        # Summary SystemMessage
        from langchain_core.messages import SystemMessage
        sys_msgs = [m for m in msgs if isinstance(m, SystemMessage)]
        assert len(sys_msgs) == 1
        assert "[Intent Clarification Summary]" in sys_msgs[0].content

    @pytest.mark.asyncio
    async def test_hook_compaction_with_fast_path(self):
        """Hook compaction + fast-path: both sets of RemoveMessages merge."""
        from langchain_core.messages import HumanMessage, RemoveMessage

        mock_llm = AsyncMock()

        hook_updates = {
            "messages": [RemoveMessage(id="hook_remove_1")],
        }
        mock_hook = AsyncMock(return_value=hook_updates)

        old_messages = [
            HumanMessage(content=f"msg-{i}", id=f"msg_id_{i}")
            for i in range(5)
        ]
        tool_msg = ToolMessage(
            content="✓ 故障注入意图已提交",
            name="submit_fault_intent",
            tool_call_id="call_submit_1",
            id="tool_msg_id",
        )
        old_messages.append(tool_msg)

        node = make_intent_clarification(llm=mock_llm, hook=mock_hook)
        state = {
            "confirmed_intent": None,
            "messages": old_messages,
            "clarification_round": 0,
            "dialogue_round": 0,
            "fault_intent": {
                "fault_type": "cpu-fullload",
                "scope": "pod",
                "target": "cpu",
                "action": "fullload",
                "namespace": "default",
                "labels": "app=myapp",
            },
        }
        result = await node(state)

        assert result["confirmed_intent"] == "inject"
        msgs = result["messages"]
        remove_msgs = [m for m in msgs if isinstance(m, RemoveMessage)]
        # 1 from hook + 2 from dialogue cleanup (6 - 4 = 2 old messages)
        assert len(remove_msgs) == 3
        assert remove_msgs[0].id == "hook_remove_1"


class TestEnsureVisibleContent:
    """End-to-end behaviour of the content-only fallback (no reasoning_content).

    After removing the reasoning_content fallback path, _ensure_visible_content
    only returns response.content or a templated fallback. reasoning_content is
    never exposed to the user — it's captured separately in the session file
    via _filter_internal_tools_raw for auditability.
    """

    def _resp(self, content="", reasoning=""):
        msg = AIMessage(content=content, id="t")
        if reasoning:
            msg.additional_kwargs = {"reasoning_content": reasoning}
        return msg

    def test_content_preferred_when_present(self):
        out = _ensure_visible_content(
            self._resp(content="好的，已经收到。", reasoning="ignore me"),
            intent="inject",
        )
        assert out == "好的，已经收到。"

    def test_reasoning_not_used_when_content_empty(self):
        # reasoning_content is NEVER exposed to the user, regardless of
        # whether it looks "clean" or "leaky". The session file captures
        # it separately for audit.
        out = _ensure_visible_content(
            self._resp(content="", reasoning="OK, I'll get the cluster status."),
            intent="",
        )
        # Must fall through to template, NOT use reasoning
        assert "cluster status" not in out
        assert out  # non-empty template

    def test_leaky_reasoning_replaced_with_template(self):
        # Even "leaky" reasoning is never exposed — same path as above
        leaky = (
            "用户提供了节点名称：cn-hongkong.10.0.1.62。 回顾上下文：\n"
            "• 意图：注入故障 (inject)\n• 故障类型：node-cpu-fullload"
        )
        out = _ensure_visible_content(
            self._resp(content="", reasoning=leaky), intent="inject"
        )
        assert "用户提供了" not in out
        assert "回顾上下文" not in out
        assert "已记下" in out or "好的" in out

    def test_empty_content_and_empty_reasoning_uses_template(self):
        out = _ensure_visible_content(self._resp(content=""), intent="chat")
        assert "再回来" in out or "找我" in out

    def test_inject_template_exists(self):
        out = _ensure_visible_content(self._resp(content=""), intent="inject")
        assert out
        assert out != "好的,我在听,请继续告诉我你想做什么。"


class TestMergeKnownParams:
    """Tests for _merge_known_params_into_fault_intent — programmatic
    parameter extraction from HumanMessage text."""

    def test_scope_extraction(self):
        msgs = [HumanMessage(content="给 pod 注入 CPU 故障")]
        merged = _merge_known_params_into_fault_intent(msgs, {})
        assert merged["scope"] == "pod"
        assert merged["target"] == "cpu"

    def test_target_extraction_from_chinese(self):
        msgs = [HumanMessage(content="压测一下内存")]
        merged = _merge_known_params_into_fault_intent(msgs, {})
        assert merged["target"] == "mem"

    def test_write_once_prevents_overwrite(self):
        msgs = [HumanMessage(content="pod CPU 故障")]
        merged = _merge_known_params_into_fault_intent(msgs, {"scope": "node"})
        # Already set to "node" — should not overwrite to "pod"
        assert merged["scope"] == "node"

    def test_override_signal_allows_update(self):
        msgs = [HumanMessage(content="改成 pod scope")]
        merged = _merge_known_params_into_fault_intent(msgs, {"scope": "node"})
        # Override mode — "改成" allows updating
        assert merged["scope"] == "pod"

    def test_action_derived_from_target(self):
        msgs = [HumanMessage(content="给 pod 注入网络故障")]
        merged = _merge_known_params_into_fault_intent(msgs, {})
        assert merged["target"] == "network"
        assert merged["action"] == "delay"

    def test_existing_action_not_overwritten(self):
        msgs = [HumanMessage(content="pod 网络")]
        merged = _merge_known_params_into_fault_intent(msgs, {"action": "loss"})
        # action already set — derivation shouldn't overwrite
        assert merged["action"] == "loss"

    def test_percent_extraction(self):
        msgs = [HumanMessage(content="CPU 占用 80%")]
        merged = _merge_known_params_into_fault_intent(msgs, {})
        assert merged.get("params", {}).get("percent") == "80"

    def test_timeout_extraction(self):
        msgs = [HumanMessage(content="时间 600秒")]
        merged = _merge_known_params_into_fault_intent(msgs, {})
        assert merged.get("params", {}).get("timeout") == "600"

    def test_empty_messages_no_change(self):
        merged = _merge_known_params_into_fault_intent([], {"scope": "pod"})
        assert merged == {"scope": "pod"}

    def test_multiple_messages_latest_wins(self):
        msgs = [
            HumanMessage(content="给 node 注入故障"),
            HumanMessage(content="pod CPU 80%"),
        ]
        merged = _merge_known_params_into_fault_intent(msgs, {})
        # Reversed iteration: latest message first → "pod" wins
        assert merged["scope"] == "pod"


class TestExtractNamesSourceContract:
    """Pin the ``source`` contract on ``_extract_names``.

    Production crash this defends against: in session sess_94082a67c656
    the agent ran ``kubectl get nodes`` and listed every returned node
    back to the user as "以下是集群中的节点列表：- X - Y - Z" (12
    bare node names in a markdown bullet block). Greedy bare-name
    regex on that AIMessage harvested ALL 12 names into
    ``fault_intent.names``, which the Confirmed Parameters prompt
    section then surfaced as "names already confirmed (12 nodes)".
    The user had picked exactly one — the LLM's reasoning_content
    ended up debating "do I trust the user or the prompt?". We
    suppress the bare-name path on AI source so list dumps cannot
    poison the merged intent.
    """

    def test_ai_listing_does_not_harvest_bare_names(self):
        # The exact AIMessage shape from the failing session.
        from chaos_agent.agent.nodes.intent_clarification import (
            _extract_names,
        )
        text = (
            "以下是集群中的节点列表：\n\n"
            "- cn-hongkong.10.0.1.101\n"
            "- cn-hongkong.10.0.1.120\n"
            "- cn-hongkong.10.0.1.154\n"
            "- cn-hongkong.10.0.1.38\n"
            "- cn-hongkong.10.0.1.51\n"
            "- cn-hongkong.10.0.1.60\n"
            "- cn-hongkong.10.0.1.61\n"
            "- cn-hongkong.10.0.1.62\n"
            "- cn-hongkong.10.0.1.63\n"
            "- cn-hongkong.10.0.1.79\n"
            "- cn-hongkong.10.0.16.55\n"
            "- cn-hongkong.10.0.16.56\n"
        )
        # Source ``ai`` must reject the greedy match.
        assert _extract_names(text, source="ai") == []

    def test_ai_ack_pattern_still_yields_single_name(self):
        # The bounded ack pattern is still honoured for AI source —
        # it can only produce one name, so it cannot poison the merge.
        from chaos_agent.agent.nodes.intent_clarification import (
            _extract_names,
        )
        text = "好的，**目标节点**已确认为 cn-hongkong.10.0.1.63。"
        assert _extract_names(text, source="ai") == ["cn-hongkong.10.0.1.63"]

    def test_human_source_keeps_greedy_multi_match(self):
        # User typing two nodes in one message is legitimate; the
        # human path must keep the greedy behaviour.
        from chaos_agent.agent.nodes.intent_clarification import (
            _extract_names,
        )
        text = "对 cn-hongkong.10.0.1.63 和 cn-hongkong.10.0.1.79 注入"
        names = _extract_names(text, source="human")
        assert "cn-hongkong.10.0.1.63" in names
        assert "cn-hongkong.10.0.1.79" in names

    def test_default_source_any_back_compat(self):
        # Direct callers without the source kwarg keep the pre-fix
        # greedy behaviour to avoid breaking unrelated code paths.
        from chaos_agent.agent.nodes.intent_clarification import (
            _extract_names,
        )
        text = "对 cn-hongkong.10.0.1.63 和 cn-hongkong.10.0.1.79"
        names = _extract_names(text)
        assert len(names) == 2

    def test_merge_does_not_poison_names_from_ai_listing(self):
        # End-to-end: the bug-shape conversation. AI lists 12 nodes;
        # user picks 1; ``_merge_known_params_into_fault_intent``
        # must end up with [.63] only, not [.63, .101, .120, ...].
        msgs = [
            HumanMessage(content="列出来", id="h1"),
            AIMessage(
                content=(
                    "以下是集群中的节点列表：\n"
                    "- cn-hongkong.10.0.1.101\n"
                    "- cn-hongkong.10.0.1.120\n"
                    "- cn-hongkong.10.0.1.63\n"
                    "- cn-hongkong.10.0.1.79\n"
                ),
                id="ai_list",
            ),
            HumanMessage(content="cn-hongkong.10.0.1.63", id="h2"),
        ]
        merged = _merge_known_params_into_fault_intent(msgs, {})
        assert merged.get("names") == ["cn-hongkong.10.0.1.63"]


class TestCompletenessSignal:

    def test_all_required_filled_emits_critical(self):
        section = get_intent_completeness_section({
            "scope": "pod", "target": "cpu", "action": "fullload",
            "namespace": "default", "labels": "app=myapp",
        })
        assert "⚠️ ALL REQUIRED" in section
        assert "submit_fault_intent" in section

    def test_missing_fields_lists_them(self):
        section = get_intent_completeness_section({"scope": "pod"})
        assert "Still missing" in section
        assert "target" in section
        assert "namespace" in section

    def test_pod_scope_requires_resource(self):
        section = get_intent_completeness_section({
            "scope": "pod", "target": "cpu", "action": "fullload", "namespace": "default",
        })
        # pod scope without names/labels → still missing target_resource
        assert "target_resource" in section

    def test_node_scope_requires_names(self):
        section = get_intent_completeness_section({
            "scope": "node", "target": "cpu", "action": "fullload", "namespace": "default",
        })
        assert "target_node" in section

    def test_pod_with_labels_is_complete(self):
        section = get_intent_completeness_section({
            "scope": "pod", "target": "cpu", "action": "fullload",
            "namespace": "default", "labels": "app=myapp",
        })
        assert "⚠️ ALL REQUIRED" in section

    def test_empty_intent_lists_all_required(self):
        section = get_intent_completeness_section({})
        assert "scope" in section
        assert "target" in section

    def test_empty_fault_intent_returns_empty_string(self):
        # None fault_intent → no dynamic section
        section = get_intent_completeness_section(None)
        assert section == ""

    def test_confirmed_parameters_block_present(self):
        section = get_intent_completeness_section({
            "scope": "pod", "target": "cpu",
        })
        assert "Confirmed Parameters" in section
        assert "Do NOT re-ask" in section
