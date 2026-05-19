"""Tests for IntentRouter — minimal TUI-level classification."""

import pytest

from chaos_agent.tui.intent import IntentRouter, IntentType


class TestIntentRouter:
    def setup_method(self):
        self.router = IntentRouter()

    # Slash commands
    def test_slash_help(self):
        assert self.router.classify("/help") == IntentType.SLASH_COMMAND

    def test_slash_clear(self):
        assert self.router.classify("/clear") == IntentType.SLASH_COMMAND

    def test_slash_inject(self):
        assert self.router.classify("/inject pod cpu") == IntentType.SLASH_COMMAND

    # Exit
    def test_exit(self):
        assert self.router.classify("exit") == IntentType.EXIT

    def test_quit(self):
        assert self.router.classify("quit") == IntentType.EXIT

    def test_exit_chinese(self):
        assert self.router.classify("退出") == IntentType.EXIT

    def test_bye(self):
        assert self.router.classify("bye") == IntentType.EXIT

    # Agent input — everything else goes to LangGraph
    def test_recover_is_agent_input(self):
        assert self.router.classify("恢复故障") == IntentType.AGENT_INPUT

    def test_inject_is_agent_input(self):
        assert self.router.classify("注入CPU故障") == IntentType.AGENT_INPUT

    def test_inject_english_is_agent_input(self):
        assert self.router.classify("inject pod CPU fullload") == IntentType.AGENT_INPUT

    def test_query_is_agent_input(self):
        assert self.router.classify("你能做什么") == IntentType.AGENT_INPUT

    def test_greeting_is_agent_input(self):
        assert self.router.classify("你好") == IntentType.AGENT_INPUT

    def test_help_text_is_agent_input(self):
        assert self.router.classify("help me with something") == IntentType.AGENT_INPUT

    # Edge: "exit" embedded in longer text should NOT be EXIT
    def test_exit_in_sentence_is_agent_input(self):
        assert self.router.classify("how to exit the pod") == IntentType.AGENT_INPUT
