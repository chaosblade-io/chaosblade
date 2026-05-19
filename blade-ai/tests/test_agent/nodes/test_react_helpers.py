"""Tests for react_helpers.py — shared helper functions for ReAct loop nodes."""

import logging
from unittest.mock import MagicMock

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from chaos_agent.agent.nodes.react_helpers import (
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