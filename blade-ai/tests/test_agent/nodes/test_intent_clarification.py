"""Tests for intent_clarification node — dialogue, routing, and fault convergence."""

import json
from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from tests._helpers import intent_dict_from_result
from chaos_agent.agent.nodes.planning.intent_clarification import (
    _advance_fault_spec,
    _allocate_operation_task_id,
    _clarification_bump,
    _confirmation_refund,
    _extract_recover_task_id,
    _normalise_fault_args,
    _params_carry_timeout,
    submit_fault_intent,
    recover_task,
    MAX_DIALOGUE_ROUNDS,
    make_intent_clarification,
)
from chaos_agent.agent.prompts.sections.intent import (
    get_intent_completeness_section,
)
from chaos_agent.agent.spec.fault_spec import FaultSpec


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

    assert first.startswith("inject-")
    assert second.startswith("inject-")
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


class TestDurationContractHelpers:
    """Unit tests for the duration-contract normalisation helpers."""

    def test_normalise_passes_duration_through(self):
        normalized = _normalise_fault_args({
            "scope": "pod", "target": "cpu", "action": "fullload",
            "duration_seconds": 900,
        })
        assert normalized["duration_seconds"] == 900

    def test_normalise_coerces_str_duration(self):
        normalized = _normalise_fault_args({
            "scope": "pod", "target": "cpu", "action": "fullload",
            "duration_seconds": "300",
        })
        assert normalized["duration_seconds"] == 300

    def test_normalise_absent_duration_stays_absent(self):
        # Presence-only pass-through: ``from_intent_args`` treats a
        # missing key as "inherit from the existing spec". Normalising
        # an absent duration to 0 would wipe a previously reviewed value
        # on every resubmit.
        normalized = _normalise_fault_args({
            "scope": "pod", "target": "cpu", "action": "fullload",
        })
        assert "duration_seconds" not in normalized

    def test_params_carry_timeout_detection(self):
        assert _params_carry_timeout({"params": {"timeout": "600"}})
        assert not _params_carry_timeout({"params": {"percent": "80"}})
        assert not _params_carry_timeout({})
        assert not _params_carry_timeout({"params": None})


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
        assert "intent submitted" in result

    def test_submit_fault_intent_with_optional_args(self):
        # Full structured submission with every optional field — what
        # the prompt now instructs the LLM to do. Duration travels via
        # duration_seconds (params.timeout is rejected downstream).
        result = submit_fault_intent.invoke({
            "fault_type": "pod-network-drop",
            "scope": "pod",
            "target": "network",
            "action": "drop",
            "fault_revision": 0,
            "namespace": "cms-demo",
            "labels": {"app": "nginx"},
            "params": {"interface": "eth0"},
            "duration_seconds": 600,
            "user_description": "给 nginx 注入网络丢包",
        })
        assert "intent submitted" in result

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
        assert "intent submitted" in result

    def test_submit_fault_intent_args_schema_has_required_fields(self):
        # Schema dump sanity: the @lc_tool decorator must surface the
        # five required-or-defaulted fields plus the four optionals so
        # the LLM bound to this tool sees the full structure.
        schema = submit_fault_intent.args_schema.model_json_schema()
        props = set(schema.get("properties", {}).keys())
        required = set(schema.get("required", []))
        assert {"fault_type", "scope", "target", "action"} <= required
        assert {"namespace", "names", "labels", "params", "user_description",
                "duration_seconds"} <= props

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
        assert "intent submitted" in result

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
        assert "intent submitted" in result

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
        assert "goodbye" in result["messages"][0].content

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
            assert op_task_id.startswith("inject-")
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
            # A host drill needs a host transport. Without any connection field
            # the channel resolves to the process-wide ``settings`` default
            # (typically a k8s one), which the submit-time capability gate now
            # refuses — the same verdict ``agent_loop`` has always produced for
            # this state. Real sessions always carry a channel, so the omission
            # was a fixture gap rather than intended coverage.
            "kube_connection_mode": "kubewiz_host",
            "host_name": "host-1",
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
        # The semantic core (role, priorities, full skill catalog) is shared,
        # but the prompts are no longer byte-identical: each now states the
        # KNOWN capability profile as a fact so a host-channel environment
        # stops loading k8s-chaos-skills. This informs, it does not filter —
        # the catalog stays complete either way.
        assert "configured host" in prompts[0]
        assert "Kubernetes environment" in prompts[1]
        # neither path leaks the "unsupported / do not attempt" wording
        for p in prompts:
            assert "Do not attempt injection" not in p

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
    async def test_fast_path_rejects_params_timeout(self):
        """Duration contract gate: a submission smuggling duration as
        ``params.timeout`` is rejected before bootstrap/contract
        construction, with a corrective message for the ReAct loop."""
        mock_llm = AsyncMock()
        mock_llm.bind_tools = MagicMock(
            return_value=AsyncMock(ainvoke=AsyncMock(return_value=_make_llm_response())))

        human_msg = HumanMessage(content="执行", id="human_1")
        ai_msg = AIMessage(
            content="",
            tool_calls=[_submit_fault_tc(
                namespace="production",
                params={"percent": "80", "timeout": "600"},
            )],
            id="ai_submit_timeout",
        )
        submit_tool_msg = ToolMessage(
            content="✓ Fault-injection intent submitted",
            name="submit_fault_intent",
            tool_call_id="call_submit_1",
        )
        messages = [human_msg, ai_msg, submit_tool_msg]

        node = make_intent_clarification(llm=mock_llm)
        state = {
            "confirmed_intent": None,
            "messages": messages,
            "clarification_round": 0,
            "dialogue_round": 2,
            "fault_intent": {},
        }
        result = await node(state)
        # Not injected — the turn ends in a corrective rejection.
        assert result.get("confirmed_intent") != "inject"
        rejection = result["messages"][0]
        assert "duration_seconds" in rejection.content
        assert "params.timeout" in rejection.content

    @pytest.mark.asyncio
    async def test_fast_path_rejects_stringified_params_timeout(self):
        """Gate must also catch ``params`` smuggled as a JSON string —
        qwen-style models emit ``params='{"timeout": "600"}'`` instead of
        a structured dict, and the rejection must not depend on shape."""
        mock_llm = AsyncMock()
        mock_llm.bind_tools = MagicMock(
            return_value=AsyncMock(ainvoke=AsyncMock(return_value=_make_llm_response())))

        human_msg = HumanMessage(content="执行", id="human_1")
        ai_msg = AIMessage(
            content="",
            tool_calls=[_submit_fault_tc(
                namespace="production",
                params='{"percent": "80", "timeout": "600"}',
            )],
            id="ai_submit_timeout_str",
        )
        submit_tool_msg = ToolMessage(
            content="✓ Fault-injection intent submitted",
            name="submit_fault_intent",
            tool_call_id="call_submit_1",
        )
        messages = [human_msg, ai_msg, submit_tool_msg]

        node = make_intent_clarification(llm=mock_llm)
        state = {
            "confirmed_intent": None,
            "messages": messages,
            "clarification_round": 0,
            "dialogue_round": 2,
            "fault_intent": {},
        }
        result = await node(state)
        assert result.get("confirmed_intent") != "inject"
        rejection = result["messages"][0]
        assert "duration_seconds" in rejection.content
        assert "params.timeout" in rejection.content

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
    async def test_recover_tool_message_dry_run_preview_does_not_bootstrap_or_confirm(
        self, tmp_path
    ):
        """Round-64 R64-1: a dry-run turn (/plan preview) carrying a
        recover tool message must NOT bootstrap a recover session nor
        confirm the recover intent. Before the early return the preview
        REALLY RAN the recovery: the branch bootstrapped and confirmed
        unconditionally, and the turn stream's ``_run_recover`` gated
        only on the confirmed intent — a /plan over a recovery request
        executed a REAL recovery (the preview-safety defect the inject
        preview's route_after_confirmation "end" already legislates for
        its own side). The preview answers with an announcement instead;
        ``recover_task_id`` is still recorded (a dialogue FACT that keeps
        the previewed target on record — the next real turn re-derives it
        from its own recover_task tool message, so the state field is a
        fallback for recover_handler's pass-through), and the graph ends
        the turn: no handler runs, no session exists, nothing to close or
        leak.
        """
        from chaos_agent.memory.session_store import (
            SessionStore,
            set_global_session_store,
        )
        store = SessionStore(task_dir=tmp_path / "tasks")
        set_global_session_store(store)
        try:
            mock_llm = AsyncMock()
            mock_llm.bind_tools = MagicMock(
                return_value=AsyncMock(
                    ainvoke=AsyncMock(return_value=_make_llm_response())))

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
                "dry_run": True,
            }
            result = await node(state)

            # No confirmation, no session id allocation — the graph ends
            # this turn (should_continue_intent_clarification reads the
            # unset confirmed_intent and routes to END).
            assert result.get("confirmed_intent") is None
            assert not result.get("task_id"), (
                "the dry-run preview must not allocate an operation task id"
            )
            # The extracted FACT survives for the next real turn.
            assert result["recover_task_id"] == "task-recover-001"
            # The preview answers with the announcement, not silence.
            assert "NOT executed" in result["messages"][0].content
            # And NOTHING was bootstrapped: no active session, no task
            # file on disk.
            assert not store.has_active("task-recover-001")
            assert list((tmp_path / "tasks").glob("*.json")) == [], (
                "a dry-run preview must not bootstrap a recover session"
            )
        finally:
            set_global_session_store(None)

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
        # New semantics: counts user turns that REVISED the proposal —
        # no proposal under review here, so nothing is counted.
        assert result["clarification_round"] == 0
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
        assert "intent submitted" in result

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
        # No proposal under review at entry → not a clarification round.
        assert result["clarification_round"] == 0
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
        # No proposal under review at entry → not a clarification round.
        assert result["clarification_round"] == 0
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
        # Cluster probing is not a user clarification round.
        assert result["clarification_round"] == 0
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
        # Capability probing is not a user clarification round.
        assert result["clarification_round"] == 0
        msg = result["messages"][0]
        assert any(tc["name"] == "read_skill_resource" for tc in msg.tool_calls)


def _raw_fault(names: list) -> dict:
    """Minimal proposal payload accepted by ``FaultSpec.from_intent_args``."""
    return {
        "fault_type": "node-mem-load",
        "scope": "node",
        "target": "mem",
        "action": "load",
        "namespace": "",
        "names": names,
        "duration_seconds": 600,
        "params": {"mode": "ram", "mem-percent": 80},
    }


def _proposal_text(reply: str, names: list) -> str:
    payload = json.dumps({"faults": [_raw_fault(names)]}, ensure_ascii=False)
    return f"{reply}<blade-fault-proposal>{payload}</blade-fault-proposal>"


class TestClarificationBumpHelper:
    """Unit rules for ``_clarification_bump`` / ``_confirmation_refund``."""

    def test_fresh_turn_after_opening_counts_once(self):
        assert _clarification_bump(True, 2, 0) == 1

    def test_fresh_turn_accumulates(self):
        assert _clarification_bump(True, 5, 3) == 4

    def test_phase_opening_turn_does_not_count(self):
        # dialogue_round == 0 at entry: the first utterance IS the intent.
        assert _clarification_bump(True, 0, 0) == 0

    def test_tool_loop_reentry_does_not_count(self):
        assert _clarification_bump(False, 2, 1) == 1

    def test_refund_gives_back_the_confirmation_bump(self):
        assert _confirmation_refund(
            2, reviewed_contract_existed=True,
        ) == 1

    def test_refund_never_goes_negative(self):
        # A one-shot session can reach submission without any bump firing.
        assert _confirmation_refund(
            0, reviewed_contract_existed=True,
        ) == 0

    def test_no_refund_without_a_reviewed_contract(self):
        # sess_5bb60326518c: a submit that had to bootstrap the contract
        # from its own args sat in the turn that made the decision —
        # refunding it would erase a real clarification round.
        assert _confirmation_refund(
            1, reviewed_contract_existed=False,
        ) == 1


class TestClarificationRoundSemantics:
    """clarification_round = user turns spent clarifying before submission."""

    @staticmethod
    def _node_with(response):
        mock_llm = AsyncMock()
        mock_llm.bind_tools = MagicMock(
            return_value=AsyncMock(ainvoke=AsyncMock(return_value=response)))
        return make_intent_clarification(llm=mock_llm)

    @staticmethod
    def _state(messages, spec=None, dialogue_round=2, clarification_round=0):
        state = {
            "confirmed_intent": None,
            "messages": messages,
            "clarification_round": clarification_round,
            "dialogue_round": dialogue_round,
            "task_id": "",
            "tui_session_id": "",
        }
        if spec is not None:
            state["fault_spec"] = spec.to_dict()
        return state

    @pytest.mark.asyncio
    async def test_user_revision_turn_counts_one_round(self):
        """Trace replay: user switches the target node → exactly 1 round."""
        existing = _advance_fault_spec(None, _raw_fault(["node-118"]))
        node = self._node_with(_make_llm_response(
            content=_proposal_text("好的，目标节点已更新。", ["node-119"])))
        result = await node(self._state(
            [HumanMessage(content="换成 119", id="h-turn-2")], spec=existing))
        assert result["clarification_round"] == 1
        assert result["fault_spec"]["revision"] == 2

    @pytest.mark.asyncio
    async def test_question_answer_turn_counts_one_round(self):
        """Answering the agent's A/B question is clarification even though
        no proposal existed at turn entry (task turn-73d42254e303: the old
        revision-based counter showed 0 for exactly this flow)."""
        node = self._node_with(_make_llm_response(
            content=_proposal_text("明白，采用持续杀进程模式。", ["node-118"])))
        result = await node(self._state(
            [HumanMessage(content="进程被反复杀死", id="h-turn-2")]))
        assert result["clarification_round"] == 1

    @pytest.mark.asyncio
    async def test_text_turn_after_opening_counts_even_without_revision(self):
        """Any fresh post-opening turn that gets a substantive reply counts
        — the metric measures turns spent, not contract deltas."""
        existing = _advance_fault_spec(None, _raw_fault(["node-118"]))
        node = self._node_with(_make_llm_response(
            content=_proposal_text("好的，按当前方案提交。", ["node-118"])))
        result = await node(self._state(
            [HumanMessage(content="确认", id="h-turn-2")], spec=existing))
        assert result["clarification_round"] == 1
        assert result["fault_spec"]["revision"] == 1

    @pytest.mark.asyncio
    async def test_phase_opening_turn_counts_nothing(self):
        """The turn that opens the phase is the intent itself."""
        node = self._node_with(_make_llm_response(
            content=_proposal_text("已识别注入意图。", ["node-118"])))
        result = await node(self._state(
            [HumanMessage(content="对一个 node 注入内存故障", id="h-turn-1")],
            dialogue_round=0))
        assert result["clarification_round"] == 0
        assert result["fault_spec"]["revision"] == 1

    @pytest.mark.asyncio
    async def test_tool_loop_reentry_does_not_double_count(self):
        """Only the fresh user turn's entry counts; tool-loop passes never do."""
        existing = _advance_fault_spec(None, _raw_fault(["node-118"]))
        human = HumanMessage(content="换成 119", id="h-turn-2")
        ai = AIMessage(content="", tool_calls=[_ask_human_tc()], id="ai-1")
        tool = ToolMessage(content="ok", tool_call_id="call_ask_1",
                           name="ask_human", id="tool-1")
        node = self._node_with(_make_llm_response(
            content=_proposal_text("已更新。", ["node-119"]),
            tool_calls=[_submit_fault_tc()]))
        result = await node(self._state([human, ai, tool], spec=existing))
        assert result["fault_spec"]["revision"] == 2
        assert result["clarification_round"] == 0

    @pytest.mark.asyncio
    async def test_successful_submit_refunds_the_confirmation_turn(self):
        """The confirming turn's entry bump is given back by the fast path:
        user says 确认 → model calls submit → re-entry commits the intent
        with the count refunded (net 0 for a pure confirmation)."""
        spec = _advance_fault_spec(None, _raw_fault(["node-118"]))
        submit = _submit_fault_tc(
            fault_type="node-mem-load", scope="node", target="mem",
            action="load", namespace="", names=["node-118"],
            duration_seconds=600,
            params={"mode": "ram", "mem-percent": 80},
            user_description="对 node-118 注入内存故障",
            fault_revision=spec.revision,
        )
        messages = [
            HumanMessage(content="确认", id="h-confirm"),
            AIMessage(content="", tool_calls=[submit], id="ai-submit"),
            ToolMessage(content="✓ Fault-injection intent submitted",
                        name="submit_fault_intent",
                        tool_call_id="call_submit_1", id="tool-submit"),
        ]
        node = make_intent_clarification(llm=AsyncMock())
        result = await node(self._state(
            messages, spec=spec, clarification_round=1))
        assert result["confirmed_intent"] == "inject"
        assert result["clarification_round"] == 0

    @pytest.mark.asyncio
    async def test_same_turn_trailer_plus_submit_converges_despite_stale_revision(self):
        """Regression pin for the golden-eval failure mode: the reply that
        carries a submit call may also carry the proposal trailer that just
        advanced the server-owned revision.  The submit args then hold the
        OLD revision (the only one the model could observe at generation
        time).  Replay matching must compare execution fields only — a strict
        revision replay turned this legal same-turn window into a guaranteed
        rejection."""
        prior = _advance_fault_spec(None, _raw_fault(["node-118"]))
        # Same-turn trailer changed the contract → server advanced to rev 2.
        reviewed = _advance_fault_spec(prior, _raw_fault(["node-119"]))
        assert reviewed.revision == 2
        submit = _submit_fault_tc(
            fault_type="node-mem-load", scope="node", target="mem",
            action="load", namespace="", names=["node-119"],
            duration_seconds=600,
            params={"mode": "ram", "mem-percent": 80},
            user_description="对 node-119 注入内存故障",
            fault_revision=1,  # extra unknown arg (resumed-session habit); pydantic drops it silently
        )
        messages = [
            HumanMessage(content="确认", id="h-confirm"),
            AIMessage(content="", tool_calls=[submit], id="ai-submit"),
            ToolMessage(content="✓ Fault-injection intent submitted",
                        name="submit_fault_intent",
                        tool_call_id="call_submit_1", id="tool-submit"),
        ]
        node = make_intent_clarification(llm=AsyncMock())
        result = await node(self._state(messages, spec=reviewed))
        assert result["confirmed_intent"] == "inject"

    @pytest.mark.asyncio
    async def test_field_mismatch_rejection_names_the_differing_fields(self):
        """A genuine replay mismatch (submit args ≠ reviewed contract) is
        still rejected — and the rejection now names the differing fields so
        the model can repair the submission instead of guessing."""
        spec = _advance_fault_spec(None, _raw_fault(["node-118"]))
        submit = _submit_fault_tc(
            fault_type="node-mem-load", scope="node", target="mem",
            action="load", namespace="", names=["node-118"],
            duration_seconds=600,
            params={"mode": "ram", "mem-percent": 90},  # reviewed says 80
            user_description="对 node-118 注入内存故障",
        )
        messages = [
            HumanMessage(content="确认", id="h-confirm"),
            AIMessage(content="", tool_calls=[submit], id="ai-submit"),
            ToolMessage(content="✓ Fault-injection intent submitted",
                        name="submit_fault_intent",
                        tool_call_id="call_submit_1", id="tool-submit"),
        ]
        node = make_intent_clarification(llm=AsyncMock())
        result = await node(self._state(messages, spec=spec))
        assert "confirmed_intent" not in result
        reason = result["messages"][-1].content
        assert "differ from the reviewed contract" in reason
        assert "params" in reason and "mem-percent" in reason


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
            # JSON-stringified — the bug shape. Duration travels via
            # duration_seconds, never params.timeout (rejected upstream).
            "names": '["cn-hongkong.10.0.1.63"]',
            "params": '{"percent": "80"}',
            "duration_seconds": 600,
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
        assert fi["params"] == {"percent": "80"}
        assert fi["duration_seconds"] == 600
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
            # timeout is NOT allowed in params (duration contract) — the
            # numeric-coercion coverage moves to another int param.
            "params": {"percent": 80, "retry": 3, "verbose": True},
            "duration_seconds": 600,
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
            "retry": "3",
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
                params={"interface": "eth0"},
                duration_seconds=600,
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
        assert fi["params"] == {"interface": "eth0"}
        assert fi["duration_seconds"] == 600
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


class TestFastPathPlaceholderBootstrap:
    """Pin the bootstrap contract for placeholder-obscured submits.

    Regression for sess_c0402de35872: the TUI NL entry writes a
    ``FaultSpec.placeholder_nl`` stub into state from the first turn, so the
    bootstrap fallback's old ``existing_spec is None`` condition never fired
    there — a model that rendered a complete reviewed plan but omitted the
    private proposal trailer was rejected ("cannot form a contract yet")
    and the drill only proceeded after a full rejection→resubmit
    round-trip.  The gate now bootstraps from the structured submit
    whenever the state holds no COMPLETE contract (None or placeholder),
    restoring the submit as a contract source while the anti-smuggling
    replay check stays intact for complete contracts.
    """

    @staticmethod
    def _placeholder_state(messages, dialogue_round=5):
        from chaos_agent.agent.spec.fault_spec import FaultSpec
        return {
            "confirmed_intent": None,
            "messages": messages,
            "clarification_round": 0,
            "dialogue_round": dialogue_round,
            # The TUI NL entry's placeholder stub — not None.
            "fault_spec": FaultSpec.placeholder_nl(
                user_description="选一个node注入内存故障",
                source="tui",
            ).to_dict(),
        }

    @pytest.mark.asyncio
    async def test_placeholder_with_complete_submit_bootstraps(self):
        """sess_c0402de35872 scenario: complete structured args against a
        placeholder stub (the model rendered the plan in prose and never
        wrote a trailer) must advance to inject on the FIRST submit."""
        mock_llm = AsyncMock()
        ai_submit = AIMessage(
            content="已确定目标节点与完整演练方案，现在提交注入意图：",
            tool_calls=[_submit_fault_tc(
                fault_type="node-mem-load",
                scope="node",
                target="mem",
                action="load",
                namespace="",
                names=["cn-shanghai.10.0.0.126"],
                params={"mode": "ram", "mem-percent": "80"},
                duration_seconds=0,
                user_description="选一个node注入内存故障",
            )],
            id="ai_submit_placeholder",
        )
        tool_msg = ToolMessage(
            content="✓ Fault-injection intent submitted",
            name="submit_fault_intent",
            tool_call_id="call_submit_1",
        )
        messages = [
            HumanMessage(content="选一个node注入内存故障", id="h1"),
            ai_submit,
            tool_msg,
        ]
        node = make_intent_clarification(llm=mock_llm)
        result = await node(self._placeholder_state(messages))
        assert result["confirmed_intent"] == "inject"
        fi = intent_dict_from_result(result)
        assert fi["fault_type"] == "node-mem-load"
        assert fi["scope"] == "node"
        assert fi["target"] == "mem"
        assert fi["action"] == "load"
        assert fi["names"] == ["cn-shanghai.10.0.0.126"]
        assert fi["params"] == {"mode": "ram", "mem-percent": "80"}
        # duration_seconds=0 is the "system recommended" sentinel: the
        # bootstrap lifts it to a positive default so is_complete holds.
        assert fi["duration_seconds"] > 0
        # The placeholder's identity field survives via inheritance.
        assert fi["user_description"] == "选一个node注入内存故障"
        mock_llm.bind_tools.assert_not_called()

    @pytest.mark.asyncio
    async def test_placeholder_with_incomplete_submit_still_rejected(self):
        """An incomplete submission cannot bootstrap a complete contract:
        the placeholder stays and the node rejects with the
        incomplete-contract wording — a legitimate refusal (the submit
        itself lacks contract fields), not a trailer accident."""
        mock_llm = AsyncMock()
        ai_submit = AIMessage(
            content="",
            tool_calls=[_submit_fault_tc(
                fault_type="pod-cpu-fullload",
                scope="pod",
                target="cpu",
                action="fullload",
                namespace="",  # pod scope requires one — submit is incomplete
            )],
            id="ai_submit_bad",
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
        result = await node(self._placeholder_state(messages))
        assert result.get("confirmed_intent") != "inject"
        assert intent_dict_from_result(result) == {}
        ai_msgs = [m for m in result.get("messages", []) if isinstance(m, AIMessage)]
        assert len(ai_msgs) == 1
        assert "form a contract yet" in ai_msgs[0].content
        mock_llm.bind_tools.assert_not_called()

    @pytest.mark.asyncio
    async def test_complete_contract_still_gates_mismatched_submit(self):
        """Anti-smuggling regression guard: with a COMPLETE reviewed contract
        in state, bootstrap never fires and a deviating submit is rejected
        with the mismatch wording."""
        from chaos_agent.agent.spec.fault_spec import FaultSpec
        mock_llm = AsyncMock()
        ai_submit = AIMessage(
            content="",
            tool_calls=[_submit_fault_tc(
                fault_type="pod-cpu-fullload",
                scope="pod",
                target="cpu",
                action="fullload",
                namespace="production",  # deviates from the reviewed default
            )],
            id="ai_submit_smuggle",
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
        reviewed = FaultSpec(
            scope="pod", fault_target="cpu", fault_action="fullload",
            namespace="default", names=["nginx-1"], duration_seconds=600,
        )
        node = make_intent_clarification(llm=mock_llm)
        result = await node({
            "confirmed_intent": None,
            "messages": messages,
            "clarification_round": 0,
            "dialogue_round": 3,
            "fault_spec": reviewed.to_dict(),
        })
        assert result.get("confirmed_intent") != "inject"
        ai_msgs = [m for m in result.get("messages", []) if isinstance(m, AIMessage)]
        assert len(ai_msgs) == 1
        assert "differ from the reviewed contract" in ai_msgs[0].content
        mock_llm.bind_tools.assert_not_called()

    @pytest.mark.asyncio
    async def test_fast_path_keeps_round_when_contract_bootstrapped_from_submit(self):
        """sess_5bb60326518c regression: the user's clarifying answer
        (「你帮我选一个合适的」 — delegating the target choice) landed in the
        SAME turn where the model decided and called submit_fault_intent,
        with no reviewed FaultSpec in state (the TUI wipes fault_spec at
        every turn entry, and the model omitted the proposal trailer). The
        submit had to bootstrap the contract from its own args, so the turn
        carried the substantive decision — its entry bump must NOT be
        refunded, or the confirm card reports "无需澄清" (one-shot
        convergence) for a session that actually took a Q&A round."""
        mock_llm = AsyncMock()
        ai_msg = AIMessage(
            content="好，我来定。我选这个目标。",
            tool_calls=[_submit_fault_tc(
                fault_type="pod-cpu-fullload",
                scope="pod",
                target="cpu",
                action="fullload",
                namespace="arms-prom",
                labels={"app": "kube-state-metrics"},
            )],
            id="ai_submit_boot",
        )
        tool_msg = ToolMessage(
            content="✓ 故障注入意图已提交，正在进入执行确认阶段。",
            name="submit_fault_intent",
            tool_call_id="call_submit_1",
        )
        human_msg = HumanMessage(content="你帮我选一个合适的", id="human_delegate")
        node = make_intent_clarification(llm=mock_llm)
        state = {
            "confirmed_intent": None,
            "messages": [human_msg, ai_msg, tool_msg],
            "clarification_round": 1,  # bumped at this turn's entry
            "dialogue_round": 2,
            "fault_intent": {},
            # fault_spec deliberately absent: the TUI turn entry wipes it.
        }
        result = await node(state)
        assert result["confirmed_intent"] == "inject"
        # The bootstrapped submit turn WAS the clarification round.
        assert result["clarification_round"] == 1

    @pytest.mark.asyncio
    async def test_fast_path_still_refunds_replay_of_reviewed_contract(self):
        """The classic confirm turn — a COMPLETE reviewed contract sits in
        state and the submission replays it exactly — still gets its entry
        bump refunded: the turn added no clarification."""
        mock_llm = AsyncMock()
        submit_tc = _submit_fault_tc(
            fault_type="pod-cpu-fullload",
            scope="pod",
            target="cpu",
            action="fullload",
            namespace="production",
            labels={"app": "account"},
        )
        reviewed = FaultSpec.from_intent_args(submit_tc["args"])
        # Guard: the fixture must be reviewable, i.e. reach the replay path.
        assert reviewed.is_complete
        ai_msg = AIMessage(
            content="确认提交。",
            tool_calls=[submit_tc],
            id="ai_submit_replay",
        )
        tool_msg = ToolMessage(
            content="✓ 故障注入意图已提交，正在进入执行确认阶段。",
            name="submit_fault_intent",
            tool_call_id="call_submit_1",
        )
        human_msg = HumanMessage(content="确认", id="human_confirm")
        node = make_intent_clarification(llm=mock_llm)
        state = {
            "confirmed_intent": None,
            "messages": [human_msg, ai_msg, tool_msg],
            "clarification_round": 2,  # one substantive round + this confirm turn
            "dialogue_round": 3,
            "fault_intent": {},
            "fault_spec": reviewed.to_dict(),
        }
        result = await node(state)
        assert result["confirmed_intent"] == "inject"
        # The pure-confirmation turn's bump is given back.
        assert result["clarification_round"] == 1

    @pytest.mark.asyncio
    async def test_opening_turn_submit_stays_zero(self):
        """One-shot convergence is unchanged: the opening turn never bumps,
        and a bootstrapped submit there keeps the count at zero."""
        mock_llm = AsyncMock()
        ai_msg = AIMessage(
            content="好的，直接注入。",
            tool_calls=[_submit_fault_tc(
                fault_type="pod-cpu-fullload",
                scope="pod",
                target="cpu",
                action="fullload",
                namespace="production",
                labels={"app": "account"},
            )],
            id="ai_submit_oneshot",
        )
        tool_msg = ToolMessage(
            content="✓ 故障注入意图已提交，正在进入执行确认阶段。",
            name="submit_fault_intent",
            tool_call_id="call_submit_1",
        )
        human_msg = HumanMessage(
            content="对 production 的 account pod 注入 CPU 满载", id="human_open",
        )
        node = make_intent_clarification(llm=mock_llm)
        state = {
            "confirmed_intent": None,
            "messages": [human_msg, ai_msg, tool_msg],
            "clarification_round": 0,  # opening turn: no bump ever fired
            "dialogue_round": 1,
            "fault_intent": {},
        }
        result = await node(state)
        assert result["confirmed_intent"] == "inject"
        assert result["clarification_round"] == 0


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
            scope="pod", fault_target="cpu", fault_action="fullload",
            namespace="default", labels={"app": "myapp"},
            duration_seconds=600,
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
            scope="pod", fault_target="cpu", fault_action="fullload",
            namespace="default", labels={"app": "myapp"},
            duration_seconds=600,
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
            "fault_target": "cpu",
            "fault_action": "fullload",
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

    def test_server_owned_revision_is_hidden_from_intent_view(self):
        """The intent surface never shows or instructs about the revision:
        the submit replay compares execution fields only, and the same-turn
        trailer can advance the revision beyond what the model observed —
        a value it cannot carry correctly must not exist in its world."""
        section = get_intent_completeness_section(self._spec(revision=7))
        assert '"revision"' not in section
        assert "revision" not in section

    def test_batch_faults_render_multiple(self):
        section = get_intent_completeness_section(
            batch_faults=[
                self._spec(scope="pod", fault_target="cpu"),
                self._spec(scope="node", fault_target="disk",
                           fault_action="fill", names=["node-a"]),
            ],
        )
        assert '"scope": "pod"' in section
        assert '"scope": "node"' in section
        assert '"target": "disk"' in section


class TestBatchFastPathDurationContract:
    """Node-level end-to-end for the submit_batch_intent fast path.

    Guards the batch half of the duration contract: a clean replay is
    accepted with durations carried into the stored batch args, while any
    entry smuggling duration via params.timeout — dict or JSON-stringified
    — is rejected per-entry before any state transition.
    """

    @staticmethod
    def _spec(name: str) -> FaultSpec:
        return FaultSpec(
            scope="pod", fault_target="cpu", fault_action="fullload",
            namespace="ns", names=(name,), duration_seconds=600, revision=1,
        )

    @staticmethod
    def _replay(spec: FaultSpec) -> dict:
        return {
            "scope": "pod", "target": "cpu", "action": "fullload",
            "namespace": spec.namespace, "names": list(spec.names),
            "params": {}, "duration_seconds": spec.duration_seconds,
        }

    @staticmethod
    def _state(specs: list, tc_faults: list, revision: int) -> dict:
        messages = [
            HumanMessage(content="执行", id="h1"),
            AIMessage(content="", tool_calls=[{
                "name": "submit_batch_intent", "id": "call_b1",
                "args": {"faults": tc_faults, "fault_revision": revision,
                         "execution_order": "serial"},
            }], id="ai_b"),
            ToolMessage(content="ok", name="submit_batch_intent",
                        tool_call_id="call_b1"),
        ]
        return {
            "confirmed_intent": None, "messages": messages,
            "clarification_round": 0, "dialogue_round": 2,
            "fault_intent": {},
            "batch_submit_args": {"faults": [s.to_dict() for s in specs]},
        }

    @pytest.mark.asyncio
    async def test_clean_replay_accepted_with_durations(self):
        s1, s2 = self._spec("p1"), self._spec("p2")
        node = make_intent_clarification(llm=AsyncMock())
        result = await node(self._state(
            [s1, s2], [self._replay(s1), self._replay(s2)], s1.revision,
        ))
        assert result.get("confirmed_intent") == "batch_inject"
        faults = result["batch_submit_args"]["faults"]
        assert [f.get("duration_seconds") for f in faults] == [600, 600]

    @pytest.mark.asyncio
    async def test_dict_params_timeout_rejected(self):
        s1, s2 = self._spec("p1"), self._spec("p2")
        bad = self._replay(s1)
        bad["params"] = {"timeout": "600"}
        node = make_intent_clarification(llm=AsyncMock())
        result = await node(self._state(
            [s1, s2], [bad, self._replay(s2)], s1.revision,
        ))
        assert result.get("confirmed_intent") != "batch_inject"
        assert "duration_seconds" in result["messages"][0].content

    @pytest.mark.asyncio
    async def test_stringified_params_timeout_rejected(self):
        s1, s2 = self._spec("p1"), self._spec("p2")
        bad = self._replay(s2)
        bad["params"] = '{"timeout": "600"}'
        node = make_intent_clarification(llm=AsyncMock())
        result = await node(self._state(
            [s1, s2], [self._replay(s1), bad], s1.revision,
        ))
        assert result.get("confirmed_intent") != "batch_inject"
        assert "duration_seconds" in result["messages"][0].content

    @pytest.mark.asyncio
    async def test_field_mismatch_rejection_names_the_differing_fault_field(self):
        """Batch replay diffs are indexed per fault so the model can repair
        exactly the entry that strayed from the reviewed contract."""
        s1, s2 = self._spec("p1"), self._spec("p2")
        bad = self._replay(s2)
        bad["params"] = {"cpu-percent": "80"}
        node = make_intent_clarification(llm=AsyncMock())
        result = await node(self._state(
            [s1, s2], [self._replay(s1), bad], s1.revision,
        ))
        assert result.get("confirmed_intent") != "batch_inject"
        reason = result["messages"][0].content
        assert "differs from the reviewed contract" in reason
        assert "fault[2].params" in reason and "cpu-percent" in reason

    @pytest.mark.asyncio
    async def test_bootstrap_converges_when_no_reviewed_batch_exists(self):
        """Regression pin for the trailer-less batch submission: without a
        reviewed contract the replay gate used to diff the submission against
        an empty review — a guaranteed rejection the model could never
        repair.  The structured submission bootstraps the contract, exactly
        like the single-fault path."""
        s1, s2 = self._spec("p1"), self._spec("p2")
        state = self._state(
            [], [self._replay(s1), self._replay(s2)], 0,
        )
        state["batch_submit_args"] = None
        node = make_intent_clarification(llm=AsyncMock())
        result = await node(state)
        assert result.get("confirmed_intent") == "batch_inject"
        assert len(result["batch_submit_args"]["faults"]) == 2

    @pytest.mark.asyncio
    async def test_incomplete_bootstrapped_batch_rejected_actionably(self):
        """A bootstrapped batch missing a locator/namespace must be rejected
        with the incomplete wording — never the misleading 'differs from the
        reviewed contract' phrasing when no review existed."""
        good = self._replay(self._spec("p1"))
        bad = self._replay(self._spec("p2"))
        bad["namespace"] = ""
        state = self._state([], [good, bad], 0)
        state["batch_submit_args"] = None
        node = make_intent_clarification(llm=AsyncMock())
        result = await node(state)
        assert result.get("confirmed_intent") != "batch_inject"
        reason = result["messages"][0].content
        assert "incomplete" in reason
        assert "differs from the reviewed contract" not in reason


class TestRealGraphDryRunRecoverPreview:
    """Execution-level channel pinning for the dry-run × recover preview
    (Round-64 R64-1).

    Function-level tests construct the state dict directly and are blind
    to LangGraph channel filtering (the 2026-09-01 planning_mode lesson:
    an undeclared channel key silently vanishes from both input and node
    updates, with no error). These tests run the real compiled
    StateGraph(IntentState) with the real node factory, real router, real
    ToolNode + recover_task tool, and a real SessionStore, so the
    zero-side-effect guarantee is measured end-to-end: if ``dry_run`` ever
    loses its IntentState channel, the graph takes the real bootstrap path
    and these assertions fail loudly instead of regressing silently.

    intent_screener / recover_handler / save_dialogue are recording stubs —
    their internals are unit-test territory; everything on the channel-
    verification path is real.
    """

    def _compile(self, llm, reached):
        """Compile a real StateGraph(IntentState) mirroring graph.py's
        intent sub-graph topology (graph.py:369-391)."""
        from langgraph.graph import END, StateGraph
        from langgraph.prebuilt import ToolNode

        from chaos_agent.agent.nodes.planning.intent_screener import (
            INTENT_SCREENER_PASS,
        )
        from chaos_agent.agent.router import (
            should_continue_intent_clarification,
        )
        from chaos_agent.agent.state import IntentState

        async def intent_screener_stub(state):
            return {}

        async def recover_handler_stub(state):
            reached["recover_handler"] = True
            return {"operation": "recover"}

        async def save_dialogue_stub(state):
            reached["save_dialogue"] = True
            return {}

        async def unreachable_intent_confirm(state):
            # Fail-closed: no scenario in this probe may route through
            # intent_confirm (dry-run exits early; real recover routes via
            # RECOVER_HANDLER). Mapping INTENT_CONFIRM to a raising node —
            # instead of to a recording stub — keeps the B test honest if
            # should_continue_intent_clarification ever regresses and
            # returns INTENT_CONFIRM for a recover intent: the graph blows
            # up loudly instead of silently reaching the recover stub.
            raise AssertionError(
                "unreachable: a recover-intent scenario routed through "
                "intent_confirm"
            )

        def screener_route(state):
            return state.get("intent_screener_route", INTENT_SCREENER_PASS)

        tools = [recover_task]
        graph = StateGraph(IntentState)
        graph.add_node(
            "intent_clarification",
            make_intent_clarification(llm=llm, tools=tools),
        )
        graph.add_node("intent_screener", intent_screener_stub)
        graph.add_node("clarification_tools", ToolNode(tools))
        graph.add_node("recover_handler", recover_handler_stub)
        graph.add_node("save_dialogue", save_dialogue_stub)
        graph.add_node("unreachable_intent_confirm", unreachable_intent_confirm)
        graph.set_entry_point("intent_clarification")
        graph.add_conditional_edges(
            "intent_clarification",
            should_continue_intent_clarification,
            {
                "continue": "intent_screener",
                "intent_confirm": "unreachable_intent_confirm",
                "recover_handler": "recover_handler",
                "save_memory": "save_dialogue",
                END: END,
            },
        )
        graph.add_conditional_edges(
            "intent_screener",
            screener_route,
            {INTENT_SCREENER_PASS: "clarification_tools", "retry": "intent_clarification"},
        )
        graph.add_edge("clarification_tools", "intent_clarification")
        graph.add_edge("recover_handler", END)
        graph.add_edge("save_dialogue", END)
        return graph.compile()

    @staticmethod
    def _fake_llm():
        from langchain_core.language_models.fake_chat_models import (
            FakeMessagesListChatModel,
        )

        class _ToolCapableFakeLLM(FakeMessagesListChatModel):
            # BaseChatModel.bind_tools raises NotImplementedError; the real
            # node does ``llm.bind_tools(tools_this_iter)``. Responses are
            # pre-scripted, so ignoring the bound tools keeps the call shape
            # real without a provider.
            def bind_tools(self, tools, **kwargs):
                return self

        return _ToolCapableFakeLLM(responses=[AIMessage(
            content="",
            id="ai_1",
            tool_calls=[{
                "name": "recover_task",
                "id": "call_recover_1",
                "args": {"task_id": "task-abc123"},
            }],
        )])

    @pytest.mark.asyncio
    async def test_dry_run_recover_preview_zero_side_effects_on_real_graph(
        self, tmp_path
    ):
        """dry_run=True + recover intent on the REAL compiled graph: the
        preview answers with the announcement, reaches no handler, and
        leaves the SessionStore untouched — and the round-trip proves the
        ``dry_run`` channel (input) and ``recover_task_id`` channel (update)
        both survive LangGraph filtering."""
        from chaos_agent.memory.session_store import (
            SessionStore,
            set_global_session_store,
        )

        store = SessionStore(task_dir=tmp_path / "tasks")
        set_global_session_store(store)
        try:
            reached = {"recover_handler": False, "save_dialogue": False}
            app = self._compile(self._fake_llm(), reached)
            result = await app.ainvoke({
                "messages": [HumanMessage(content="恢复任务 task-abc123", id="h_1")],
                "confirmed_intent": "unset",
                "task_id": "",
                "tui_session_id": "",
                "dialogue_round": 0,
                "dry_run": True,
            })
        finally:
            set_global_session_store(None)  # type: ignore[arg-type]

        assert reached["recover_handler"] is False
        assert list((tmp_path / "tasks").glob("*.json")) == []
        assert not store._active_sessions
        assert result.get("confirmed_intent") != "recover"
        # Channel round-trip: the early-exit update survives filtering.
        assert result.get("recover_task_id") == "task-abc123"
        last = result["messages"][-1]
        assert isinstance(last, AIMessage)
        assert "NOT executed" in last.content
        assert not last.tool_calls

    @pytest.mark.asyncio
    async def test_non_dry_run_recover_bootstraps_and_reaches_handler(
        self, tmp_path
    ):
        """Single-variable control (only dry_run flips to False): bootstrap
        and recover_handler routing genuinely happen — so the zero-side-
        effect assertions above fail for the right reason when the dry_run
        leg regresses, not because the graph never took the recover path."""
        from chaos_agent.memory.session_store import (
            SessionStore,
            set_global_session_store,
        )

        store = SessionStore(task_dir=tmp_path / "tasks")
        set_global_session_store(store)
        try:
            reached = {"recover_handler": False, "save_dialogue": False}
            app = self._compile(self._fake_llm(), reached)
            result = await app.ainvoke({
                "messages": [HumanMessage(content="恢复任务 task-abc123", id="h_1")],
                "confirmed_intent": "unset",
                "task_id": "",
                "tui_session_id": "",
                "dialogue_round": 0,
                "dry_run": False,
            })
        finally:
            set_global_session_store(None)  # type: ignore[arg-type]

        assert reached["recover_handler"] is True
        task_jsons = list((tmp_path / "tasks").glob("*.json"))
        assert len(task_jsons) == 1
        assert result.get("confirmed_intent") == "recover"
        assert result.get("task_id")

