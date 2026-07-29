"""Tests for intent_clarification node — dialogue, routing, and fault convergence."""

from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from tests._helpers import intent_dict_from_result
from chaos_agent.agent.nodes.planning.intent_clarification import (
    _allocate_operation_task_id,
    _extract_recover_task_id,
    submit_fault_intent,
    recover_task,
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


def _recover_tc(task_id: str = "task-test123"):
    return {
        "name": "recover_task",
        "id": "call_recover_1",
        "args": {"task_id": task_id},
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


def test_tui_turn_ids_allocate_distinct_operation_task_ids():
    """Each TS TUI turn must become a fresh operation task when dispatched."""

    first = _allocate_operation_task_id("turn-first")
    second = _allocate_operation_task_id("turn-second")

    assert first.startswith("task-")
    assert second.startswith("task-")
    assert first != second


def test_cli_task_id_is_still_reused():
    """CLI callers may pre-mint a task id before entering the graph."""

    assert _allocate_operation_task_id("task-existing") == "task-existing"


class TestExtractRecoverTaskId:
    """Tests for _extract_recover_task_id helper."""

    def test_extracts_task_id_from_recover_tool_call(self):
        ai_msg = AIMessage(
            content="",
            tool_calls=[_recover_tc("task-abc123")],
            id="ai_1",
        )
        tool_msg = ToolMessage(
            content="Recover request received for task: task-abc123",
            tool_call_id="call_recover_1",
            name="recover_task",
            id="tool_1",
        )
        result = _extract_recover_task_id([ai_msg, tool_msg])
        assert result == "task-abc123"

    def test_returns_empty_for_no_recover_tool_call(self):
        ai_msg = AIMessage(content="hello", tool_calls=[], id="ai_1")
        result = _extract_recover_task_id([ai_msg])
        assert result == ""

    def test_returns_empty_for_empty_messages(self):
        assert _extract_recover_task_id([]) == ""


class TestRecoverTaskTool:
    """Tests for the recover_task @tool function."""

    def test_recover_task_returns_ack(self):
        result = recover_task.invoke({"task_id": "task-xyz"})
        assert "task-xyz" in result


class TestSubmitFaultIntentTool:
    """Tests for the real submit_fault_intent @tool function."""

    def test_submit_fault_intent_returns_ack(self):
        result = submit_fault_intent.invoke({
            "fault_type": "node-cpu-fullload",
            "scope": "node",
            "target": "cpu",
            "action": "fullload",
            "fault_revision": 0,
            "namespace": "default",
        })
        assert "已提交" in result

    def test_submit_fault_intent_with_optional_args(self):
        # Full structured submission with every optional field — what
        # the prompt now instructs the LLM to do.
        result = submit_fault_intent.invoke({
            "fault_type": "pod-network-drop",
            "scope": "pod",
            "target": "network",
            "action": "drop",
            "fault_revision": 0,
            "namespace": "cms-demo",
            "labels": {"app": "nginx"},
            "params": {"interface": "eth0", "timeout": "600"},
            "user_description": "给 nginx 注入网络丢包",
        })
        assert "已提交" in result

    def test_submit_fault_intent_namespace_defaults(self):
        # namespace omitted → empty string default. The function just
        # returns an ack; downstream merge logic handles defaulting.
        result = submit_fault_intent.invoke({
            "fault_type": "node-cpu-fullload",
            "scope": "node",
            "target": "cpu",
            "action": "fullload",
            "fault_revision": 0,
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

    def test_schema_still_advertises_typed_collections_to_llm(self):
        # The BeforeValidator must NOT leak into the JSON schema the LLM
        # sees — otherwise the LLM might be tempted to pass strings
        # deliberately. The schema for names/labels/params should still
        # describe array / object types (with `null` allowed for the
        # optional default), exactly as before the validator was added.
        schema = submit_fault_intent.args_schema.model_json_schema()
        props = schema["properties"]
        # `names` accepts list[str] | null
        names_types = {b.get("type") for b in props["names"]["anyOf"]}
        assert names_types == {"array", "null"}
        # `labels` / `params` accept dict[str, str] | null
        for f in ("labels", "params"):
            types = {b.get("type") for b in props[f]["anyOf"]}
            assert types == {"object", "null"}, f"{f} schema drift: {types}"

    def test_submit_fault_intent_accepts_json_stringified_names(self):
        # Reproduces the failing tool_call from sess_27ec8f3ef6b2 L30:
        # qwen-class LLM emitted ``names`` as a JSON string. Pre-fix,
        # this raised ``Input should be a valid list`` at the @lc_tool
        # boundary and the dialogue terminated. Post-fix the
        # BeforeValidator coerces the string into a list before
        # Pydantic's type check runs.
        result = submit_fault_intent.invoke({
            "fault_type": "node-disk-fill",
            "scope": "node",
            "target": "disk",
            "action": "fill",
            "fault_revision": 0,
            "namespace": "cms-demo",
            "names": '["cn-hongkong.10.0.1.101"]',
        })
        assert "已提交" in result

    def test_submit_fault_intent_accepts_json_stringified_dicts(self):
        # Companion to the names case: ``params`` and ``labels`` are
        # also commonly JSON-stringified by qwen-class models.
        result = submit_fault_intent.invoke({
            "fault_type": "node-disk-fill",
            "scope": "node",
            "target": "disk",
            "action": "fill",
            "fault_revision": 0,
            "namespace": "cms-demo",
            "names": '["cn-hongkong.10.0.1.101"]',
            "labels": '{"app": "nginx"}',
            "params": '{"path": "/var/lib/containerd", "percent": "90", "timeout": "300"}',
        })
        assert "已提交" in result

    def test_submit_fault_intent_extracts_json_strings_into_real_types(self):
        # Belt-and-braces: confirm the BeforeValidator actually parsed
        # the strings into real list / dict (not just "didn't raise").
        # We construct the args model directly and inspect the parsed
        # values.
        validated = submit_fault_intent.args_schema.model_validate({
            "fault_type": "pod-network-drop",
            "scope": "pod",
            "target": "network",
            "action": "drop",
            "fault_revision": 0,
            "namespace": "cms-demo",
            "names": '["pod-a", "pod-b"]',
            "labels": '{"app": "nginx", "tier": "frontend"}',
            "params": '{"interface": "eth0"}',
        })
        assert validated.names == ["pod-a", "pod-b"]
        assert validated.labels == {"app": "nginx", "tier": "frontend"}
        assert validated.params == {"interface": "eth0"}

    def test_submit_fault_intent_native_collections_still_accepted(self):
        # Don't regress the happy path: real list / dict (the textbook
        # function-calling shape) must continue to validate cleanly.
        validated = submit_fault_intent.args_schema.model_validate({
            "fault_type": "pod-cpu-fullload",
            "scope": "pod",
            "target": "cpu",
            "action": "fullload",
            "fault_revision": 0,
            "namespace": "cms-demo",
            "names": ["accounting-7d4f"],
            "params": {"percent": "80", "timeout": "300"},
        })
        assert validated.names == ["accounting-7d4f"]
        assert validated.params == {"percent": "80", "timeout": "300"}
        assert validated.labels is None

    def test_submit_fault_intent_malformed_dict_degrades_to_none(self):
        # The coerce contract for params/labels is fail-soft: anything
        # that's neither a real dict nor a JSON-stringified dict
        # (e.g. a plain string "bad", a list, or a number) is degraded
        # to None — equivalent to "field omitted". This is intentional:
        # a single bad arg from the LLM must not nuke the entire turn,
        # because _extract_submit_args + the dialogue history fallback
        # can still recover the real values. We assert the degradation
        # rather than raise so a regression that re-introduces strict
        # validation (and breaks the resilience promise) is caught.
        validated = submit_fault_intent.args_schema.model_validate({
            "fault_type": "x", "scope": "x", "target": "x", "action": "x",
            "fault_revision": 0,
            "params": ["this", "is", "a", "list"],   # dict expected
            "labels": "not-a-json-object",
        })
        assert validated.params is None
        assert validated.labels is None

    def test_submit_fault_intent_plain_name_string_wraps_to_single_list(self):
        # A non-JSON-shaped string for ``names`` (no surrounding ``[``
        # / ``]``) is treated as a single name typed without brackets —
        # the only ambiguity-tolerant branch in _coerce_to_list. A
        # regression that drops this branch would force the LLM into
        # always emitting JSON arrays, losing a degree of robustness.
        validated = submit_fault_intent.args_schema.model_validate({
            "fault_type": "x", "scope": "pod", "target": "x", "action": "x",
            "fault_revision": 0,
            "names": "single-pod-name",
        })
        assert validated.names == ["single-pod-name"]


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
    async def test_submit_fault_intent_fast_path_does_not_bootstrap_session_store(
        self, tmp_path
    ):
        """Option A invariant: ``intent_clarification``'s inject fast-path
        allocates the ``task-<hex>`` id (so tracker continuity carries
        into ``intent_confirm``) but MUST NOT call
        ``SessionStore.create_session(...)`` — the bootstrap is
        deferred to ``intent_confirm.approved`` so a user-initiated
        rejection at the confirm gate doesn't leave an orphan task file
        on disk.
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
            # The on-disk JSON file must NOT exist yet — bootstrap is
            # deferred to ``intent_confirm.approved``.
            task_json = tmp_path / "tasks" / f"{op_task_id}.json"
            assert not task_json.exists(), (
                f"Expected no task file at {task_json} after the inject "
                "fast-path; bootstrap should fire in intent_confirm.approved."
            )
            # Likewise the in-memory active session must be unset until
            # approval commits.
            assert not store.has_active(op_task_id)
            # And the messages delta must NOT carry an
            # ``[Intent Clarification Summary]`` SystemMessage — that
            # marker is built and inserted by ``intent_confirm.approved``
            # at the same moment the task file is bootstrapped.
            from langchain_core.messages import SystemMessage
            for m in result.get("messages", []) or []:
                if isinstance(m, SystemMessage):
                    assert not str(getattr(m, "content", "")).startswith(
                        "[Intent Clarification Summary]"
                    ), (
                        "intent_clarification must not emit the summary "
                        "marker — that's the intent_confirm contract."
                    )
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
        assert intent_dict_from_result(result)["fault_type"] == "pod-cpu-fullload"
        assert intent_dict_from_result(result)["scope"] == "pod"
        assert intent_dict_from_result(result)["namespace"] == "production"
        assert intent_dict_from_result(result)["labels"] == {"app": "account"}
        assert result["intent_confidence"] == 1.0
        # LLM should NOT have been called (fast-path skips it)
        mock_llm.bind_tools.assert_not_called()

    @pytest.mark.asyncio
    async def test_host_intent_fast_path_does_not_require_namespace(self):
        """Semantic host intents converge before transport-aware feasibility."""
        mock_llm = AsyncMock()
        messages = [
            HumanMessage(content="对主机 host-1 注入 CPU 压力", id="human_host"),
            AIMessage(
                content="",
                tool_calls=[_submit_fault_tc(
                    fault_type="host-cpu-fullload",
                    scope="host",
                    target="cpu",
                    action="fullload",
                    namespace="",
                    names=["host-1"],
                )],
                id="ai_host_submit",
            ),
            ToolMessage(
                content="✓ 故障注入意图已提交",
                name="submit_fault_intent",
                tool_call_id="call_submit_1",
            ),
        ]

        result = await make_intent_clarification(llm=mock_llm)({
            "confirmed_intent": None,
            "messages": messages,
            "clarification_round": 0,
            "dialogue_round": 1,
            "fault_intent": {},
        })

        assert result["confirmed_intent"] == "inject"
        assert intent_dict_from_result(result)["scope"] == "host"
        assert intent_dict_from_result(result)["namespace"] == ""
        mock_llm.bind_tools.assert_not_called()

    @pytest.mark.asyncio
    async def test_intent_binding_keeps_semantics_global_and_discovery_transport_aware(self):
        """Transport selects read-only probes, not the fault catalog or semantic tools."""
        response = _make_llm_response(content="请补充故障强度。")
        bound_llm = MagicMock()
        bound_llm.ainvoke = AsyncMock(return_value=response)
        llm = MagicMock()
        llm.bind_tools = MagicMock(return_value=bound_llm)
        tools = [
            SimpleNamespace(name="kubectl_read"),
            SimpleNamespace(name="host_read"),
            SimpleNamespace(name="activate_skill"),
            SimpleNamespace(name="read_skill_resource"),
            SimpleNamespace(name="submit_fault_intent"),
        ]
        node = make_intent_clarification(llm=llm, tools=tools)

        for state in (
            {"kube_connection_mode": "ssh", "ssh_host": "host-1"},
            {"kube_connection_mode": "kubeconfig"},
        ):
            await node({
                **state,
                "confirmed_intent": None,
                "messages": [HumanMessage(content="注入 CPU 故障")],
                "clarification_round": 0,
                "dialogue_round": 0,
            })

        bound_names = [
            {tool.name for tool in call.args[0]}
            for call in llm.bind_tools.call_args_list
        ]
        semantic_tools = {"activate_skill", "read_skill_resource", "submit_fault_intent"}
        assert semantic_tools <= bound_names[0]
        assert semantic_tools <= bound_names[1]
        assert "host_read" in bound_names[0]
        assert "kubectl_read" not in bound_names[0]
        assert "kubectl_read" in bound_names[1]
        assert "host_read" not in bound_names[1]

        prompts = [
            call.args[0][0].content
            for call in bound_llm.ainvoke.call_args_list
        ]
        assert prompts[0] == prompts[1]

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
        assert intent_dict_from_result(result)["scope"] == "pod"
        mock_llm.bind_tools.assert_not_called()

    @pytest.mark.asyncio
    async def test_recover_task_tool_message_routes_correctly(self):
        """recover_task ToolMessage should set confirmed_intent='recover'."""
        mock_llm = AsyncMock()
        # LLM won't be called — fast-path detects ToolMessage before LLM invocation
        mock_llm.bind_tools = MagicMock(
            return_value=AsyncMock(ainvoke=AsyncMock(return_value=_make_llm_response())))

        ai_msg = AIMessage(
            content="好的，正在为您恢复实验。",
            tool_calls=[_recover_tc("task-recover-001")],
            id="ai_recover",
        )
        tool_msg = ToolMessage(
            content="Recover request received for task: task-recover-001",
            tool_call_id="call_recover_1",
            name="recover_task",
            id="tool_recover",
        )

        node = make_intent_clarification(llm=mock_llm)
        state = {
            "confirmed_intent": None,
            "messages": [ai_msg, tool_msg],
            "clarification_round": 0,
            "dialogue_round": 0,
            "task_id": "",
            "tui_session_id": "",
        }
        result = await node(state)
        assert result["confirmed_intent"] == "recover"
        assert result["recover_task_id"] == "task-recover-001"
        mock_llm.bind_tools.assert_not_called()

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
            "fault_revision": 0,
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
        fi = intent_dict_from_result(result)
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
        assert intent_dict_from_result(result)["names"] == ["node-1"]

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
        assert intent_dict_from_result(result)["names"] == ["node-7"]

    @pytest.mark.asyncio
    async def test_numeric_param_values_coerced_to_str(self):
        """LLMs occasionally emit numeric params (``80`` / ``true``)
        instead of strings. Downstream code formats params with
        ``%s`` / ``f"{k}={v}"``, which works either way, but we
        normalise to ``str`` so the inject pipeline sees a uniform
        ``dict[str, str]``."""
        mock_llm = AsyncMock()
        messages = self._build_messages({
            "fault_type": "pod-network-drop",
            "scope": "pod",
            "target": "network",
            "action": "drop",
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
        assert intent_dict_from_result(result)["params"] == {
            "percent": "80",
            "timeout": "600",
            "verbose": "True",
        }


class TestFastPathLLMArgsPriority:
    """Pin the fast-path bootstrap contract for structured submits.

    The 089212f refactor removed the regex-based
    ``_merge_known_params_into_fault_intent`` prose fallback entirely.
    When no reviewed ``fault_spec`` exists in state, the fast-path
    bootstraps a contract strictly from the ``submit_fault_intent``
    tool_call args (``_bootstrap_submitted_spec``) — natural-language
    history is never mined. A bootstrap succeeds only if the resulting
    spec ``is_complete``; otherwise the node returns a clarification
    message rather than advancing to ``inject``.
    """

    @pytest.mark.asyncio
    async def test_llm_supplies_full_args(self):
        """Full structured submission — values come from LLM args, not regex."""
        mock_llm = AsyncMock()
        ai_msg = AIMessage(
            content="",
            tool_calls=[_submit_fault_tc(
                fault_type="pod-network-drop",
                scope="pod",
                target="network",
                action="drop",
                namespace="cms-demo",
                names=["nginx-7d4f-abc12"],
                params={"interface": "eth0", "timeout": "600"},
            )],
            id="ai_full_1",
        )
        tool_msg = ToolMessage(
            content="✓ 故障注入意图已提交",
            name="submit_fault_intent",
            tool_call_id="call_submit_1",
        )
        messages = [
            HumanMessage(content="给 cms-demo 注入网络丢包 10 分钟", id="h1"),
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
        fi = intent_dict_from_result(result)
        assert fi["fault_type"] == "pod-network-drop"
        assert fi["scope"] == "pod"
        assert fi["target"] == "network"
        assert fi["action"] == "drop"
        assert fi["namespace"] == "cms-demo"
        assert fi["names"] == ["nginx-7d4f-abc12"]
        # ``params`` values are coerced to str by ``_extract_submit_args``.
        assert fi["params"] == {"interface": "eth0", "timeout": "600"}
        mock_llm.bind_tools.assert_not_called()

    @pytest.mark.asyncio
    async def test_node_scope_omitted_namespace_still_bootstraps(self):
        """node scope is cluster-scoped, so ``is_complete`` does not
        require a namespace. A structured submit that omits it still
        bootstraps a complete contract and advances to inject — the
        namespace simply stays empty (no regex recovery, no default
        fill). This replaces the old ``partial_args_fallback`` test
        whose premise (regex mining the AI summary for
        ``**命名空间**：default``) no longer exists post-089212f."""
        mock_llm = AsyncMock()
        ai_submit = AIMessage(
            content="",
            tool_calls=[_submit_fault_tc(
                fault_type="node-cpu-fullload",
                scope="node",
                target="cpu",
                action="fullload",
                namespace="",  # ← omitted; node scope doesn't require it
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
            ai_submit,
            tool_msg,
        ]
        node = make_intent_clarification(llm=mock_llm)
        result = await node({
            "confirmed_intent": None,
            "messages": messages,
            "clarification_round": 0,
            "dialogue_round": 3,
            "fault_spec": None,  # no reviewed contract → bootstrap path
        })
        assert result["confirmed_intent"] == "inject"
        fi = intent_dict_from_result(result)
        assert fi["scope"] == "node"
        assert fi["target"] == "cpu"
        assert fi["action"] == "fullload"
        # No prose mining and no default fill — namespace stays empty.
        assert fi["namespace"] == ""
        assert fi["names"] == ["cn-hongkong.10.0.1.101"]
        mock_llm.bind_tools.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_args_cannot_bootstrap(self):
        """Older qwen builds that emit ``submit_fault_intent`` with empty
        args used to be rescued by the regex fallback. That fallback is
        gone: an empty-args submit yields an incomplete bootstrap spec,
        so the node refuses to advance and asks the user to re-confirm
        against the reviewed contract instead of inventing one from
        dialogue prose."""
        mock_llm = AsyncMock()
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
        # History still packed with the signals the old regex would
        # have mined — proving they are NO LONGER consulted.
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
            "fault_spec": None,
        })
        # No bootstrap possible → not advanced to inject.
        assert result.get("confirmed_intent") != "inject"
        assert result["dialogue_round"] == 3
        # A clarification AIMessage is returned rather than a contract.
        assert intent_dict_from_result(result) == {}
        ai_msgs = [m for m in result.get("messages", []) if isinstance(m, AIMessage)]
        assert len(ai_msgs) == 1
        mock_llm.bind_tools.assert_not_called()

    @pytest.mark.asyncio
    async def test_pod_scope_missing_namespace_cannot_bootstrap(self):
        """pod scope requires a namespace for ``is_complete``. A submit
        that omits it (and no reviewed spec exists to inherit from)
        bootstraps an incomplete spec, so the fast-path declines to
        advance — the mirror image of the node-scope case above."""
        mock_llm = AsyncMock()
        ai_submit = AIMessage(
            content="",
            tool_calls=[_submit_fault_tc(
                fault_type="pod-cpu-fullload",
                scope="pod",
                target="cpu",
                action="fullload",
                namespace="",  # ← pod scope MUST have one
                names=["nginx-1"],
            )],
            id="ai_submit_1",
        )
        tool_msg = ToolMessage(
            content="✓ 故障注入意图已提交",
            name="submit_fault_intent",
            tool_call_id="call_submit_1",
        )
        messages = [
            HumanMessage(content="对 pod 注入 cpu 故障", id="h1"),
            ai_submit,
            tool_msg,
        ]
        node = make_intent_clarification(llm=mock_llm)
        result = await node({
            "confirmed_intent": None,
            "messages": messages,
            "clarification_round": 0,
            "dialogue_round": 2,
            "fault_spec": None,
        })
        assert result.get("confirmed_intent") != "inject"
        assert intent_dict_from_result(result) == {}
        mock_llm.bind_tools.assert_not_called()


class TestHookIntegration:
    """Tests for PreReasoningHook integration (merge_hook_updates)."""

    @pytest.mark.asyncio
    async def test_hook_updates_merged_not_overwritten(self):
        """Hook RemoveMessages + LLM response both appear in result messages."""
        from langchain_core.messages import RemoveMessage

        mock_llm = AsyncMock()
        response = _make_llm_response(content="再见！")
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

        assert "confirmed_intent" not in result
        assert result["compressed_summary"] == "摘要内容"
        # Messages: [RemoveMessage x2] + [AIMessage]
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
    async def test_fast_path_does_not_trim_messages(self):
        """Option A invariant: ``intent_clarification``'s inject fast-path
        no longer emits RemoveMessages or the
        ``[Intent Clarification Summary]`` SystemMessage. Both side
        effects move to ``intent_confirm.approved`` so a user-initiated
        rejection preserves the full clarification dialogue.
        """
        from langchain_core.messages import HumanMessage, RemoveMessage, SystemMessage

        mock_llm = AsyncMock()

        # Simulate 5 prior messages + the submit AIMessage/ToolMessage pair.
        # The submit tool_call must replay the reviewed spec exactly
        # (revision + every executable field), otherwise the fast-path
        # rejects the submission instead of advancing to inject.
        old_messages = [
            HumanMessage(content=f"msg-{i}", id=f"msg_id_{i}")
            for i in range(5)
        ]
        ai_submit = AIMessage(
            content="",
            tool_calls=[_submit_fault_tc(
                scope="pod", target="cpu", action="fullload",
                namespace="default", labels={"app": "myapp"},
                fault_revision=0,
            )],
            id="ai_submit_trim",
        )
        old_messages.append(ai_submit)
        tool_msg = ToolMessage(
            content="✓ 故障注入意图已提交",
            name="submit_fault_intent",
            tool_call_id="call_submit_1",
            id="tool_msg_id",
        )
        old_messages.append(tool_msg)

        node = make_intent_clarification(llm=mock_llm)
        from chaos_agent.agent.spec.fault_spec import FaultSpec
        _spec = FaultSpec(
            scope="pod", blade_target="cpu", blade_action="fullload",
            namespace="default", labels={"app": "myapp"},
        )
        state = {
            "confirmed_intent": None,
            "messages": old_messages,
            "clarification_round": 0,
            "dialogue_round": 3,
            "fault_spec": _spec.to_dict(),
        }
        result = await node(state)

        assert result["confirmed_intent"] == "inject"
        # The return value must NOT carry a ``messages`` delta produced
        # by intent_clarification itself. (Hook updates may inject
        # their own RemoveMessages — ``test_hook_compaction_passes_through``
        # covers that case — but absent a hook the inject branch must
        # be empty here.)
        msgs = result.get("messages", []) or []
        clarification_remove = [m for m in msgs if isinstance(m, RemoveMessage)]
        assert clarification_remove == [], (
            "intent_clarification.inject must not emit RemoveMessages — "
            "that side effect is now intent_confirm.approved's job."
        )
        clarification_summary = [
            m for m in msgs
            if isinstance(m, SystemMessage)
            and str(getattr(m, "content", "")).startswith(
                "[Intent Clarification Summary]"
            )
        ]
        assert clarification_summary == [], (
            "intent_clarification.inject must not emit the summary marker."
        )

    @pytest.mark.asyncio
    async def test_hook_compaction_passes_through(self):
        """Option A invariant: hook-emitted RemoveMessages still flow
        through ``intent_clarification.inject`` (PreReasoningHook is
        independent of where the post-confirm trim runs), but the node
        itself contributes nothing to the messages delta — exactly one
        RemoveMessage from the hook, zero from the fast-path.
        """
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
        ai_submit = AIMessage(
            content="",
            tool_calls=[_submit_fault_tc(
                scope="pod", target="cpu", action="fullload",
                namespace="default", labels={"app": "myapp"},
                fault_revision=0,
            )],
            id="ai_submit_hook",
        )
        old_messages.append(ai_submit)
        tool_msg = ToolMessage(
            content="✓ 故障注入意图已提交",
            name="submit_fault_intent",
            tool_call_id="call_submit_1",
            id="tool_msg_id",
        )
        old_messages.append(tool_msg)

        node = make_intent_clarification(llm=mock_llm, hook=mock_hook)
        from chaos_agent.agent.spec.fault_spec import FaultSpec
        _spec_hook = FaultSpec(
            scope="pod", blade_target="cpu", blade_action="fullload",
            namespace="default", labels={"app": "myapp"},
        )
        state = {
            "confirmed_intent": None,
            "messages": old_messages,
            "clarification_round": 0,
            "dialogue_round": 0,
            "fault_spec": _spec_hook.to_dict(),
        }
        result = await node(state)

        assert result["confirmed_intent"] == "inject"
        msgs = result.get("messages", []) or []
        remove_msgs = [m for m in msgs if isinstance(m, RemoveMessage)]
        # Exactly the hook's RemoveMessage — no fast-path additions.
        assert len(remove_msgs) == 1
        assert remove_msgs[0].id == "hook_remove_1"


class TestReviewedFaultSpecSection:
    """``get_intent_completeness_section`` now injects the reviewed FaultSpec
    contract (JSON) rather than a completeness/still-missing checklist.

    ``FaultSpec.from_dict`` reads the state-persistence shape
    (``blade_target`` / ``blade_action``), and the section renders
    ``to_intent_dict`` (``target`` / ``action``) inside a ``faults`` array.
    """

    def _spec(self, **overrides):
        base = {
            "scope": "pod",
            "blade_target": "cpu",
            "blade_action": "fullload",
            "namespace": "default",
            "names": ["nginx"],
            "revision": 2,
        }
        base.update(overrides)
        return base

    def test_no_spec_reports_none_collected(self):
        for section in (
            get_intent_completeness_section(),
            get_intent_completeness_section(None),
            get_intent_completeness_section({}),  # empty dict → from_dict None
        ):
            assert "# Reviewed FaultSpec" in section
            assert "No FaultSpec has been collected yet." in section

    def test_single_spec_rendered_as_contract_json(self):
        section = get_intent_completeness_section(self._spec())
        assert "# Reviewed FaultSpec" in section
        assert "Current contract:" in section
        assert '"faults"' in section
        assert '"scope": "pod"' in section
        assert '"target": "cpu"' in section
        assert '"action": "fullload"' in section

    def test_server_owned_revision_is_surfaced(self):
        section = get_intent_completeness_section(self._spec(revision=7))
        assert '"revision": 7' in section
        # The guidance must instruct the LLM to preserve the revision.
        assert "revision" in section

    def test_batch_faults_render_multiple(self):
        section = get_intent_completeness_section(
            batch_faults=[
                self._spec(scope="pod", blade_target="cpu"),
                self._spec(scope="node", blade_target="disk",
                           blade_action="fill", names=["node-a"]),
            ],
        )
        assert '"scope": "pod"' in section
        assert '"scope": "node"' in section
        assert '"target": "disk"' in section

