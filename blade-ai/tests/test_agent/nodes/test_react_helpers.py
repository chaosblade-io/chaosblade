"""Tests for react_helpers.py — shared helper functions for ReAct loop nodes."""

import logging
from unittest.mock import MagicMock

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from chaos_agent.agent.nodes.react_helpers import (
    detect_action_stagnation,
    emit_debug_tool_messages,
    extract_persistent_hm,
    extract_synthetic_messages,
    extract_tool_call_fields,
    log_reasoning_content,
    record_ai_message,
    record_system_prompt,
    summarize_llm_response,
)


# ---------------------------------------------------------------------------
# record_system_prompt
# ---------------------------------------------------------------------------

class TestRecordSystemPrompt:
    def test_records_prompt_when_hook_has_session_store(self):
        hook = MagicMock()
        hook.session_store = MagicMock()
        state = {"task_id": "task-123"}
        record_system_prompt(hook, state, "test prompt")
        hook.session_store.append_messages.assert_called_once_with(
            "task-123", [SystemMessage(content="test prompt")]
        )

    def test_skips_when_hook_is_none(self):
        state = {"task_id": "task-123"}
        record_system_prompt(None, state, "test prompt")  # no crash

    def test_skips_when_session_store_is_none(self):
        hook = MagicMock()
        hook.session_store = None
        state = {"task_id": "task-123"}
        record_system_prompt(hook, state, "test prompt")  # no crash

    def test_skips_when_task_id_is_empty(self):
        hook = MagicMock()
        hook.session_store = MagicMock()
        state = {"task_id": ""}
        record_system_prompt(hook, state, "test prompt")
        hook.session_store.append_messages.assert_not_called()


# ---------------------------------------------------------------------------
# record_ai_message
# ---------------------------------------------------------------------------

class TestRecordAiMessage:
    def test_records_response_when_hook_has_session_store(self):
        hook = MagicMock()
        hook.session_store = MagicMock()
        state = {"task_id": "task-456"}
        response = AIMessage(content="test response")
        record_ai_message(hook, state, response)
        hook.session_store.append_messages.assert_called_once_with(
            "task-456", [response]
        )

    def test_suppresses_exceptions(self):
        hook = MagicMock()
        hook.session_store = MagicMock()
        hook.session_store.append_messages.side_effect = RuntimeError("db error")
        state = {"task_id": "task-456"}
        response = AIMessage(content="test response")
        record_ai_message(hook, state, response)  # no crash

    def test_skips_when_hook_is_none(self):
        state = {"task_id": "task-456"}
        record_ai_message(None, state, AIMessage(content="x"))  # no crash


# ---------------------------------------------------------------------------
# log_reasoning_content
# ---------------------------------------------------------------------------

class TestLogReasoningContent:
    def test_logs_present_when_debug_on(self, monkeypatch, caplog):
        from chaos_agent.config.settings import settings
        monkeypatch.setattr(settings, "log_level", "DEBUG")
        response = AIMessage(
            content="done",
            additional_kwargs={"reasoning_content": "thinking process here"},
        )
        with caplog.at_level(logging.DEBUG):
            log_reasoning_content(response, "Test node", 3)
        assert "present(" in caplog.text
        assert " chars)" in caplog.text
        assert "Test node 3" in caplog.text

    def test_logs_absent_when_debug_on(self, monkeypatch, caplog):
        from chaos_agent.config.settings import settings
        monkeypatch.setattr(settings, "log_level", "DEBUG")
        response = AIMessage(content="done", additional_kwargs={})
        with caplog.at_level(logging.DEBUG):
            log_reasoning_content(response, "Test node", 5)
        assert "ABSENT" in caplog.text

    def test_silent_when_debug_off(self, monkeypatch, caplog):
        from chaos_agent.config.settings import settings
        monkeypatch.setattr(settings, "log_level", "INFO")
        response = AIMessage(
            content="done",
            additional_kwargs={"reasoning_content": "thinking"},
        )
        with caplog.at_level(logging.DEBUG):
            log_reasoning_content(response, "Test node", 1)
        assert "reasoning_content" not in caplog.text


# ---------------------------------------------------------------------------
# extract_tool_call_fields
# ---------------------------------------------------------------------------

class TestExtractToolCallFields:
    def test_dict_tool_call(self):
        tc = {"name": "kubectl", "args": {"subcommand": "get"}}
        name, args = extract_tool_call_fields(tc)
        assert name == "kubectl"
        assert args == {"subcommand": "get"}

    def test_object_tool_call(self):
        tc = MagicMock()
        tc.name = "blade_create"
        tc.args = {"scope": "pod"}
        name, args = extract_tool_call_fields(tc)
        assert name == "blade_create"
        assert args == {"scope": "pod"}

    def test_empty_dict(self):
        tc = {}
        name, args = extract_tool_call_fields(tc)
        assert name == ""
        assert args == {}

    def test_object_with_no_attrs(self):
        tc = MagicMock(spec=[])  # no attributes
        name, args = extract_tool_call_fields(tc)
        assert name == ""
        assert args == {}


# ---------------------------------------------------------------------------
# extract_synthetic_messages
# ---------------------------------------------------------------------------

class TestExtractSyntheticMessages:
    def test_extracts_matching_aimessage(self):
        synthetic_ids = frozenset({"synth_tc_1"})
        ai_msg = AIMessage(
            content="",
            tool_calls=[{"name": "baseline_check", "args": {}, "id": "synth_tc_1"}],
        )
        messages = [ai_msg]
        result = extract_synthetic_messages(messages, synthetic_ids)
        assert result == [ai_msg]

    def test_extracts_matching_tool_message(self):
        synthetic_ids = frozenset({"synth_tc_1"})
        tool_msg = ToolMessage(content="result", tool_call_id="synth_tc_1")
        messages = [tool_msg]
        result = extract_synthetic_messages(messages, synthetic_ids)
        assert result == [tool_msg]

    def test_extracts_both_aimessage_and_tool_message(self):
        synthetic_ids = frozenset({"synth_tc_1"})
        ai_msg = AIMessage(
            content="",
            tool_calls=[{"name": "baseline_check", "args": {}, "id": "synth_tc_1"}],
        )
        tool_msg = ToolMessage(content="result", tool_call_id="synth_tc_1")
        messages = [ai_msg, tool_msg]
        result = extract_synthetic_messages(messages, synthetic_ids)
        assert len(result) == 2
        assert result[0] == ai_msg
        assert result[1] == tool_msg

    def test_skips_non_matching_messages(self):
        synthetic_ids = frozenset({"synth_tc_1"})
        ai_msg = AIMessage(
            content="real call",
            tool_calls=[{"name": "kubectl", "args": {}, "id": "real_tc_1"}],
        )
        human_msg = HumanMessage(content="context")
        messages = [ai_msg, human_msg]
        result = extract_synthetic_messages(messages, synthetic_ids)
        assert result == []

    def test_aimessage_with_no_tool_calls_is_skipped(self):
        synthetic_ids = frozenset({"synth_tc_1"})
        ai_msg = AIMessage(content="text only")
        messages = [ai_msg]
        result = extract_synthetic_messages(messages, synthetic_ids)
        assert result == []

    def test_empty_messages(self):
        synthetic_ids = frozenset({"synth_tc_1"})
        result = extract_synthetic_messages([], synthetic_ids)
        assert result == []


# ---------------------------------------------------------------------------
# extract_persistent_hm
# ---------------------------------------------------------------------------

class TestExtractPersistentHm:
    def test_extracts_tagged_hm_when_not_in_state(self):
        kwargs_key = "_verifier_main_context"
        hm = HumanMessage(
            content="verification context",
            additional_kwargs={kwargs_key: True},
        )
        messages = [hm]
        state = {"messages": []}
        result = extract_persistent_hm(messages, state, kwargs_key)
        assert result == [hm]

    def test_skips_when_already_in_state(self):
        kwargs_key = "_verifier_main_context"
        hm_in_state = HumanMessage(
            content="verification context",
            additional_kwargs={kwargs_key: True},
        )
        hm_local = HumanMessage(
            content="verification context",
            additional_kwargs={kwargs_key: True},
        )
        messages = [hm_local]
        state = {"messages": [hm_in_state]}
        result = extract_persistent_hm(messages, state, kwargs_key)
        assert result == []

    def test_skips_non_tagged_human_messages(self):
        kwargs_key = "_verifier_main_context"
        hm = HumanMessage(content="regular message")
        messages = [hm]
        state = {"messages": []}
        result = extract_persistent_hm(messages, state, kwargs_key)
        assert result == []

    def test_different_kwargs_key_is_isolated(self):
        kwargs_key_v = "_verifier_main_context"
        kwargs_key_r = "_recover_main_context"
        hm = HumanMessage(
            content="verification context",
            additional_kwargs={kwargs_key_v: True},
        )
        messages = [hm]
        state = {"messages": []}
        # Using recover key should NOT find verifier HM
        result = extract_persistent_hm(messages, state, kwargs_key_r)
        assert result == []


# ---------------------------------------------------------------------------
# summarize_llm_response
# ---------------------------------------------------------------------------

class TestSummarizeLlmResponse:
    def test_tool_calls_only(self):
        response = AIMessage(
            content="",
            tool_calls=[{"name": "kubectl", "args": {"subcommand": "get"}, "id": "tc1"}],
        )
        summary, tool_names = summarize_llm_response(response)
        assert "kubectl" in tool_names
        assert "🔧 tool: kubectl" in summary

    def test_reasoning_content_only(self):
        response = AIMessage(
            content="done",
            additional_kwargs={"reasoning_content": "thinking process"},
        )
        summary, tool_names = summarize_llm_response(response)
        assert "💭 thinking" in summary
        assert "💬 response" in summary

    def test_empty_response(self):
        response = AIMessage(content="")
        summary, tool_names = summarize_llm_response(response)
        assert summary == "(empty response)"
        assert tool_names == []


# ---------------------------------------------------------------------------
# emit_debug_tool_messages
# ---------------------------------------------------------------------------

class TestEmitDebugToolMessages:
    """Tests for emit_debug_tool_messages — uses a plain object tracker so
    getattr(tracker, '_emitted_tool_ids', set()) returns a real set."""

    def _make_tracker(self):
        """Tracker mock with real ``update`` method and real ``_emitted_tool_ids`` set."""
        tracker = MagicMock()
        tracker._emitted_tool_ids = set()
        return tracker

    def test_emits_new_tool_messages(self, monkeypatch):
        """Simple variant: emit all new ToolMessages."""
        from chaos_agent.config.settings import settings
        monkeypatch.setattr(settings, "log_level", "DEBUG")

        tracker = self._make_tracker()
        tm1 = ToolMessage(content="pod list output", id="tm_1", name="kubectl", tool_call_id="tc_1")
        tm2 = ToolMessage(content="node info", id="tm_2", name="kubectl", tool_call_id="tc_2")
        state = {"messages": [HumanMessage(content="go"), tm1, AIMessage(content="ok"), tm2]}

        emit_debug_tool_messages(tracker, state)

        assert tracker.update.call_count == 2
        assert tracker._emitted_tool_ids == {"tm_1", "tm_2"}

    def test_skips_already_emitted_ids(self, monkeypatch):
        """Second call skips messages already emitted."""
        from chaos_agent.config.settings import settings
        monkeypatch.setattr(settings, "log_level", "DEBUG")

        tracker = self._make_tracker()
        tracker._emitted_tool_ids = {"tm_1"}  # Simulate first call happened

        tm1 = ToolMessage(content="pod list output", id="tm_1", name="kubectl", tool_call_id="tc_1")
        tm2 = ToolMessage(content="node info", id="tm_2", name="kubectl", tool_call_id="tc_2")
        state = {"messages": [tm1, tm2]}

        emit_debug_tool_messages(tracker, state)

        # Only tm_2 should be emitted (tm_1 already in emitted_ids)
        assert tracker.update.call_count == 1
        assert "kubectl" in tracker.update.call_args[0][0]

    def test_silent_when_debug_off(self, monkeypatch):
        """No emission when is_debug is False."""
        from chaos_agent.config.settings import settings
        monkeypatch.setattr(settings, "log_level", "INFO")

        tracker = self._make_tracker()
        tm = ToolMessage(content="output", id="tm_1", name="kubectl", tool_call_id="tc_1")
        state = {"messages": [tm]}

        emit_debug_tool_messages(tracker, state)

        tracker.update.assert_not_called()

    def test_seed_existing_suppresses_preexisting(self, monkeypatch):
        """seed_existing=True on first call: ALL existing ToolMessages are seeded
        and suppressed from emission."""
        from chaos_agent.config.settings import settings
        monkeypatch.setattr(settings, "log_level", "DEBUG")

        tracker = self._make_tracker()
        # tracker._emitted_tool_ids starts as empty set → seed will fire
        tm_preexisting = ToolMessage(content="inject phase output", id="tm_pre", name="blade_create", tool_call_id="tc_pre")
        state = {"messages": [tm_preexisting]}

        emit_debug_tool_messages(tracker, state, seed_existing=True)

        # Pre-existing message seeded into emitted_ids → not emitted
        tracker.update.assert_not_called()
        assert "tm_pre" in tracker._emitted_tool_ids

    def test_seed_existing_no_re_seed_on_second_call(self, monkeypatch):
        """seed_existing=True on 2nd call: no re-seeding, only truly new messages emitted."""
        from chaos_agent.config.settings import settings
        monkeypatch.setattr(settings, "log_level", "DEBUG")

        tracker = self._make_tracker()
        tracker._emitted_tool_ids = {"tm_pre", "tm_new"}  # Already populated from 1st call

        tm_pre = ToolMessage(content="inject output", id="tm_pre", name="blade_create", tool_call_id="tc_pre")
        tm_new = ToolMessage(content="verify output", id="tm_new", name="kubectl", tool_call_id="tc_new")
        tm_later = ToolMessage(content="L2 step output", id="tm_later", name="kubectl", tool_call_id="tc_later")
        state = {"messages": [tm_pre, tm_new, tm_later]}

        emit_debug_tool_messages(tracker, state, seed_existing=True)

        # Only tm_later should be emitted
        assert tracker.update.call_count == 1
        assert "kubectl" in tracker.update.call_args[0][0]

    def test_long_content_preview_truncated(self, monkeypatch):
        """Content > 100 chars gets preview truncation."""
        from chaos_agent.config.settings import settings
        monkeypatch.setattr(settings, "log_level", "DEBUG")

        tracker = self._make_tracker()
        long_content = "x" * 200
        tm = ToolMessage(content=long_content, id="tm_1", name="kubectl", tool_call_id="tc_1")
        state = {"messages": [tm]}

        emit_debug_tool_messages(tracker, state)

        preview = tracker.update.call_args[0][0]
        assert "..." in preview

    def test_non_string_content_handled(self, monkeypatch):
        """Non-string content is converted to str."""
        from chaos_agent.config.settings import settings
        monkeypatch.setattr(settings, "log_level", "DEBUG")

        tracker = self._make_tracker()
        tm = ToolMessage(content="line1\nline2", id="tm_1", name="kubectl", tool_call_id="tc_1")
        state = {"messages": [tm]}

        emit_debug_tool_messages(tracker, state)

        tracker.update.assert_called_once()

    def test_empty_messages(self, monkeypatch):
        """No crash on empty messages list."""
        from chaos_agent.config.settings import settings
        monkeypatch.setattr(settings, "log_level", "DEBUG")

        tracker = self._make_tracker()
        state = {"messages": []}

        emit_debug_tool_messages(tracker, state)

        tracker.update.assert_not_called()

    def test_tracker_emitted_ids_updated_after_emission(self, monkeypatch):
        """_emitted_tool_ids set is persisted on tracker after emission."""
        from chaos_agent.config.settings import settings
        monkeypatch.setattr(settings, "log_level", "DEBUG")

        tracker = self._make_tracker()
        tm = ToolMessage(content="output", id="tm_1", name="kubectl", tool_call_id="tc_1")
        state = {"messages": [tm]}

        emit_debug_tool_messages(tracker, state)

        assert tracker._emitted_tool_ids == {"tm_1"}


# ---------------------------------------------------------------------------
# detect_action_stagnation
# ---------------------------------------------------------------------------

class TestDetectActionStagnation:
    def test_no_stagnation_below_threshold(self):
        messages = [
            AIMessage(content="", tool_calls=[{"name": "kubectl", "args": {"subcommand": "get"}, "id": f"tc_{i}"}])
            for i in range(4)
        ]
        hint, tool = detect_action_stagnation(messages, threshold=5)
        assert hint is None
        assert tool is None

    def test_stagnation_at_threshold(self):
        messages = [
            AIMessage(content="", tool_calls=[{"name": "save_fault_plan", "args": {"plan_content": f"v{i}"}, "id": f"tc_{i}"}])
            for i in range(6)
        ]
        hint, tool = detect_action_stagnation(messages, threshold=5)
        assert hint is not None
        assert tool == "save_fault_plan"
        assert "ACTION_STAGNATION" in hint

    def test_different_tools_no_stagnation(self):
        messages = [
            AIMessage(content="", tool_calls=[{"name": "kubectl", "args": {}, "id": "tc_1"}]),
            AIMessage(content="", tool_calls=[{"name": "activate_skill", "args": {}, "id": "tc_2"}]),
            AIMessage(content="", tool_calls=[{"name": "kubectl", "args": {}, "id": "tc_3"}]),
        ]
        hint, tool = detect_action_stagnation(messages, threshold=2)
        assert hint is None

    def test_pure_text_breaks_streak(self):
        messages = [
            AIMessage(content="", tool_calls=[{"name": "kubectl", "args": {}, "id": "tc_1"}]),
            AIMessage(content="", tool_calls=[{"name": "kubectl", "args": {}, "id": "tc_2"}]),
            AIMessage(content="thinking...", tool_calls=[]),
            AIMessage(content="", tool_calls=[{"name": "kubectl", "args": {}, "id": "tc_3"}]),
            AIMessage(content="", tool_calls=[{"name": "kubectl", "args": {}, "id": "tc_4"}]),
        ]
        hint, tool = detect_action_stagnation(messages, threshold=3)
        assert hint is None

    def test_multi_tool_call_breaks_streak(self):
        messages = [
            AIMessage(content="", tool_calls=[{"name": "kubectl", "args": {}, "id": "tc_1"}]),
            AIMessage(content="", tool_calls=[
                {"name": "kubectl", "args": {}, "id": "tc_2"},
                {"name": "activate_skill", "args": {}, "id": "tc_3"},
            ]),
            AIMessage(content="", tool_calls=[{"name": "kubectl", "args": {}, "id": "tc_4"}]),
        ]
        hint, tool = detect_action_stagnation(messages, threshold=2)
        assert hint is None


# ---------------------------------------------------------------------------
# Tool error introspection (runtime feedback > static docs)
# ---------------------------------------------------------------------------

from chaos_agent.agent.nodes.react_helpers import (
    _should_trigger_introspection,
    suggest_verify_command,
    detect_tool_error_hint,
    extract_rejected_params,
)
from chaos_agent.errors import ErrorClass


class TestShouldTriggerIntrospection:
    def test_interface_mismatch_triggers(self):
        assert _should_trigger_introspection(ErrorClass.INTERFACE_MISMATCH) is True

    def test_user_config_triggers(self):
        assert _should_trigger_introspection(ErrorClass.USER_CONFIG) is True

    def test_unknown_triggers(self):
        assert _should_trigger_introspection(ErrorClass.UNKNOWN) is True

    def test_transient_does_not_trigger(self):
        assert _should_trigger_introspection(ErrorClass.INFRA_TRANSIENT) is False

    def test_persistent_does_not_trigger(self):
        assert _should_trigger_introspection(ErrorClass.INFRA_PERSISTENT) is False

    def test_auth_denied_does_not_trigger(self):
        assert _should_trigger_introspection(ErrorClass.AUTH_DENIED) is False

    def test_target_gone_does_not_trigger(self):
        assert _should_trigger_introspection(ErrorClass.TARGET_GONE) is False

    def test_quota_exceeded_does_not_trigger(self):
        assert _should_trigger_introspection(ErrorClass.QUOTA_EXCEEDED) is False


class TestExtractRejectedParams:
    def test_unknown_flag(self):
        assert extract_rejected_params("unknown flag: --percent") == ["--percent"]

    def test_unknown_shorthand(self):
        assert extract_rejected_params("unknown shorthand flag: '-p' in -p") == ["-p"]

    def test_flag_provided_but_not_defined(self):
        r = extract_rejected_params("flag provided but not defined: --foo")
        assert "--foo" in r

    def test_invalid_option_posix(self):
        r = extract_rejected_params("invalid option: --bar")
        assert "bar" in r

    def test_argparse_unrecognized(self):
        r = extract_rejected_params("unrecognized arguments: --baz")
        assert "--baz" in r

    def test_generic_unsupported_parameter(self):
        r = extract_rejected_params("unsupported parameter: --qux")
        assert "--qux" in r

    def test_empty_input(self):
        assert extract_rejected_params("") == []

    def test_no_match(self):
        assert extract_rejected_params("some random error") == []

    def test_dedup_within_single_message(self):
        r = extract_rejected_params(
            "unknown flag: --foo and also unknown flag: --foo"
        )
        assert r == ["--foo"]

    def test_strips_trailing_punctuation(self):
        assert extract_rejected_params("unknown flag: --percent.") == ["--percent"]
        assert extract_rejected_params("unknown flag: --foo,") == ["--foo"]
        assert extract_rejected_params("unknown flag: --bar;") == ["--bar"]
        assert extract_rejected_params("unknown flag: --baz)") == ["--baz"]


class TestSuggestVerifyCommand:
    def test_blade_tool(self):
        s = suggest_verify_command("blade_create")
        assert "blade" in s
        assert "-h" in s

    def test_kubectl_tool(self):
        s = suggest_verify_command("kubectl")
        assert "kubectl" in s
        assert "--help" in s

    def test_kubectl_ro(self):
        s = suggest_verify_command("kubectl_ro")
        assert "kubectl" in s

    def test_unknown_tool_generic(self):
        s = suggest_verify_command("some_new_tool")
        assert "some_new_tool" in s
        assert "error message" in s


class TestDetectToolErrorHint:
    def test_blade_unknown_flag(self):
        msgs = [
            ToolMessage(
                content="Error: unknown flag: --percent",
                name="blade_create",
                tool_call_id="tc1",
            )
        ]
        hint = detect_tool_error_hint(msgs)
        assert hint is not None
        assert "TOOL ERROR" in hint
        assert "`--percent`" in hint
        assert "blade" in hint

    def test_blade_generic_error(self):
        msgs = [
            ToolMessage(
                content="Error: blade create failed (exit 1): invalid argument",
                name="blade_create",
                tool_call_id="tc1",
            )
        ]
        hint = detect_tool_error_hint(msgs)
        assert hint is not None
        assert "TOOL ERROR" in hint

    def test_kubectl_error(self):
        msgs = [
            ToolMessage(
                content="Error: invalid option: --foo",
                name="kubectl",
                tool_call_id="tc1",
            )
        ]
        hint = detect_tool_error_hint(msgs)
        assert hint is not None
        assert "kubectl" in hint

    def test_unknown_tool(self):
        msgs = [
            ToolMessage(
                content="Error: validation error: bad input",
                name="some_new_tool",
                tool_call_id="tc1",
            )
        ]
        hint = detect_tool_error_hint(msgs)
        assert hint is not None
        assert "some_new_tool" in hint

    def test_skips_transient(self):
        msgs = [
            ToolMessage(
                content="Error: connection refused",
                name="blade_create",
                tool_call_id="tc1",
            )
        ]
        hint = detect_tool_error_hint(msgs)
        assert hint is None

    def test_skips_non_error_content(self):
        msgs = [
            ToolMessage(
                content='{"success": true, "result": "uid-123"}',
                name="blade_create",
                tool_call_id="tc1",
            )
        ]
        hint = detect_tool_error_hint(msgs)
        assert hint is None

    def test_dedup_same_tool_error(self):
        msgs = [
            ToolMessage(
                content="Error: unknown flag: --percent",
                name="blade_create",
                tool_call_id="tc1",
            ),
            HumanMessage(
                content="**TOOL ERROR — VERIFY BEFORE RETRY**: `blade_create` returned an error."
            ),
            ToolMessage(
                content="Error: unknown flag: --percent",
                name="blade_create",
                tool_call_id="tc2",
            ),
        ]
        hint = detect_tool_error_hint(msgs)
        assert hint is None

    def test_dedup_allows_different_tool(self):
        msgs = [
            ToolMessage(
                content="Error: unknown flag: --percent",
                name="blade_create",
                tool_call_id="tc1",
            ),
            HumanMessage(
                content="**TOOL ERROR — VERIFY BEFORE RETRY**: `blade_create` returned an error."
            ),
            ToolMessage(
                content="Error: unknown flag: --foo",
                name="kubectl",
                tool_call_id="tc2",
            ),
        ]
        hint = detect_tool_error_hint(msgs)
        assert hint is not None
        assert "kubectl" in hint


class TestClassifyErrorInterfaceMismatch:
    def test_unknown_flag(self):
        from chaos_agent.errors import classify_error
        r = classify_error("Error: unknown flag: --percent")
        assert r.error_class == ErrorClass.INTERFACE_MISMATCH

    def test_unknown_command(self):
        from chaos_agent.errors import classify_error
        r = classify_error('Error: unknown command "loss"')
        assert r.error_class == ErrorClass.INTERFACE_MISMATCH

    def test_unknown_shorthand(self):
        from chaos_agent.errors import classify_error
        r = classify_error("unknown shorthand flag: '-p' in -p")
        assert r.error_class == ErrorClass.INTERFACE_MISMATCH

    def test_flag_not_defined(self):
        from chaos_agent.errors import classify_error
        r = classify_error("flag provided but not defined: --bar")
        assert r.error_class == ErrorClass.INTERFACE_MISMATCH

    def test_invalid_parameter_still_user_config(self):
        from chaos_agent.errors import classify_error
        r = classify_error("invalid parameter: bad value")
        assert r.error_class == ErrorClass.USER_CONFIG

    def test_invalid_argument_still_user_config(self):
        from chaos_agent.errors import classify_error
        r = classify_error("invalid argument: --mem-size 0")
        assert r.error_class == ErrorClass.USER_CONFIG


class TestPhaseSpecificLoopHints:
    def test_build_loop_hint_intent(self):
        from chaos_agent.agent.nodes.react_helpers import _build_loop_hint
        hint = _build_loop_hint("kubectl_ro(subcommand=get)", 3, "intent")
        assert "LOOP DETECTED" in hint
        assert "REFLECT" in hint
        assert "Simplify" in hint
        assert "Escalate" in hint

    def test_build_loop_hint_unknown_phase_falls_back_to_intent(self):
        from chaos_agent.agent.nodes.react_helpers import _build_loop_hint
        hint = _build_loop_hint("some_tool()", 3, "unknown_phase")
        assert "REFLECT" in hint
        assert "discovery method" in hint