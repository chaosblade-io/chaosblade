"""finish_execution — the clean terminal exit for a completed Phase 2.

#39 third-retest tail-tension root fix, full chain: the tool writes the
ledger's terminal phase, the stall-nudge gate and the router read it,
and the prompt teaches the STOP action. Before this, the model's only
exits were a bare text turn (answered by EXECUTION REQUIRED up to the
budget) or ``request_replan`` (whose semantics are "goal unreachable" —
a successful task using it would report a failure that never happened).
"""

from __future__ import annotations

from typing import Annotated

import pytest
from langchain_core.messages import AIMessage
from typing_extensions import TypedDict
from langgraph.graph.message import add_messages

from chaos_agent.agent.progress_ledger import (
    freeze_anchor,
    merge_ledger_channel,
    merge_progress_ledger,
)
from chaos_agent.tools.progress import (
    finish_execution,
    ledger_declares_execution_complete,
)


_LEDGER_COMPLETE = {
    "anchor": {"goal": "g"},
    "state": {"phase": "execution-complete"},
    "log": [{"event": "done", "status": "observed"}],
}


# ── Predicate ──────────────────────────────────────────────────────────

class TestLedgerDeclaresExecutionComplete:
    def test_true_on_tool_written_phase(self):
        assert ledger_declares_execution_complete(
            {"progress_ledger": _LEDGER_COMPLETE},
        ) is True

    def test_true_on_canonical_case_variant(self):
        # The gate reads the LEDGER and normalises case/spelling variants of
        # the canonical marker. NOTE (C1 knife-1): the update_progress TOOL
        # path for the canonical marker is validator-REJECTED below — the
        # only legal writer is finish_execution; this asserts the predicate's
        # tolerance, not an open write path.
        led = merge_progress_ledger(
            None, state_update={"phase": "Execution-Complete"},
        )
        assert ledger_declares_execution_complete(
            {"progress_ledger": led},
        ) is True

    def test_false_on_bare_word_complete(self):
        """C1 knife-1: the bare word is a DIFFERENT fact — a verifier or
        clarification model writing ``phase=complete`` means "MY phase is
        done", which must never satisfy the execute-side gates. The
        terminal marker has one writer (finish_execution) and one
        vocabulary: if a bare word re-enters the set, the gates open to
        every phase's exit word."""
        for phase in ("complete", "Complete", "COMPLETED", "completed", "done"):
            led = merge_progress_ledger(None, state_update={"phase": phase})
            assert not ledger_declares_execution_complete(
                {"progress_ledger": led},
            ), phase

    def test_false_on_incomplete_phases(self):
        for phase in ("executing", "await-settle", "", None, "verify"):
            led = merge_progress_ledger(None, state_update={"phase": phase})
            assert not ledger_declares_execution_complete(
                {"progress_ledger": led},
            ), phase

    def test_false_on_missing_shapes(self):
        assert not ledger_declares_execution_complete({})
        assert not ledger_declares_execution_complete(
            {"progress_ledger": None},
        )
        assert not ledger_declares_execution_complete(
            {"progress_ledger": {"state": {}}},
        )
        assert not ledger_declares_execution_complete(None)


# ── Tool write path ────────────────────────────────────────────────────

class _FinishState(TypedDict):
    # Module-level so langgraph can resolve the annotation lazily.
    messages: Annotated[list, add_messages]
    # Same channel wiring as the fixed AgentState/IntentState: the ledger is a
    # reducer channel, not a bare LastValue field. Type ``dict`` (not Optional)
    # so the channel seeds empty and the FIRST write runs the reducer too.
    progress_ledger: Annotated[dict, merge_ledger_channel]


class TestFinishExecutionTool:
    @pytest.mark.asyncio
    async def test_writes_terminal_phase_and_log(self):
        from langgraph.checkpoint.memory import MemorySaver
        from langgraph.graph import END, START, StateGraph
        from langgraph.prebuilt import ToolNode

        builder = StateGraph(_FinishState)
        builder.add_node("t", ToolNode([finish_execution]))
        builder.add_edge(START, "t")
        builder.add_edge("t", END)
        app = builder.compile(checkpointer=MemorySaver())

        led0 = freeze_anchor({"scope": "pod"}, goal="占用者冲突演练")
        call = AIMessage(content="", tool_calls=[{
            "name": "finish_execution", "id": "c1",
            "args": {
                "summary": "occupant applied; victim deleted; conflict live",
            },
        }])
        out = await app.ainvoke(
            {"messages": [call], "progress_ledger": led0},
            {"configurable": {"thread_id": "t1"}},
        )
        led = out["progress_ledger"]
        assert led["state"]["phase"] == "execution-complete"
        assert any(
            "execution declared complete" in e.get("event", "")
            for e in led["log"]
        )
        # Anchor untouched — the terminal write is a state/log write.
        assert led["anchor"]["goal"] == "占用者冲突演练"
        # The ToolMessage teaches the follow-up: no more tool calls.
        reply = out["messages"][-1]
        assert "do not call more tools" in reply.content

    def test_written_ledger_opens_the_gates(self):
        """The chain closes: what the tool writes is what the gates read."""
        led = merge_progress_ledger(
            freeze_anchor({"scope": "pod"}),
            state_update={"phase": "execution-complete"},
        )
        assert ledger_declares_execution_complete(
            {"progress_ledger": led},
        ) is True


# ── Single-writer discipline (C1 knife-1) ─────────────────────────────

class TestUpdateProgressTerminalWriteRejected:
    """update_progress is bound in ALL five ReAct phases — a verifier or
    clarification model writing a generic terminal marker (meaning ITS
    phase is done) must not poison the execute-side gates for the rest
    of the task. The terminal phase has exactly one legal writer:
    finish_execution (phase2-only binding)."""

    @staticmethod
    def _app():
        from langgraph.checkpoint.memory import MemorySaver
        from langgraph.graph import END, START, StateGraph
        from langgraph.prebuilt import ToolNode

        from chaos_agent.tools.progress import update_progress

        builder = StateGraph(_FinishState)
        builder.add_node("t", ToolNode([update_progress]))
        builder.add_edge(START, "t")
        builder.add_edge("t", END)
        return builder.compile(checkpointer=MemorySaver())

    @pytest.mark.asyncio
    async def test_rejects_canonical_marker(self):
        """The rejected write must leave the ledger UNTOUCHED (the tool
        body never runs) and answer with an error ToolMessage that TEACHES
        the legal writer — the model learns to use finish_execution
        instead of silently retrying the same poisoned write."""
        app = self._app()
        ledger = {
            "anchor": {"goal": "g"},
            "state": {"phase": "await-settle"},
            "log": [],
        }
        call = AIMessage(content="", tool_calls=[{
            "name": "update_progress", "id": "c1",
            "args": {"state_update": {"phase": "execution-complete"}},
        }])
        out = await app.ainvoke(
            {"messages": [call], "progress_ledger": ledger},
            {"configurable": {"thread_id": "t1"}},
        )
        # The gate fact survives the attack: tool body never executed.
        assert out["progress_ledger"]["state"]["phase"] == "await-settle"
        reply = out["messages"][-1]
        assert "finish_execution" in reply.content

    @pytest.mark.asyncio
    async def test_rejects_underscore_variant(self):
        app = self._app()
        ledger = {
            "anchor": {"goal": "g"},
            "state": {"phase": "await-settle"},
            "log": [],
        }
        call = AIMessage(content="", tool_calls=[{
            "name": "update_progress", "id": "c1",
            "args": {"state_update": {"phase": "Execution_Complete"}},
        }])
        out = await app.ainvoke(
            {"messages": [call], "progress_ledger": ledger},
            {"configurable": {"thread_id": "t1"}},
        )
        assert out["progress_ledger"]["state"]["phase"] == "await-settle"
        assert "finish_execution" in out["messages"][-1].content

    @pytest.mark.asyncio
    async def test_accepts_bare_complete_word(self):
        """The bare word is NOT the terminal marker — recording it is legal
        and harmless: the write lands, but the narrowed predicate (knife-1)
        reads False, so the execute-side gates stay armed."""
        app = self._app()
        call = AIMessage(content="", tool_calls=[{
            "name": "update_progress", "id": "c1",
            "args": {"state_update": {"phase": "complete"}},
        }])
        out = await app.ainvoke(
            {"messages": [call], "progress_ledger": dict(_LEDGER_COMPLETE)},
            {"configurable": {"thread_id": "t2"}},
        )
        led = out["progress_ledger"]
        assert led["state"]["phase"] == "complete"
        assert not ledger_declares_execution_complete(
            {"progress_ledger": led},
        )


# ── Stall-nudge gate ───────────────────────────────────────────────────

class TestStallGateRespectsLedger:
    def _detect(self, state):
        from chaos_agent.agent.nodes.execute.execute_loop import (
            _detect_terminal_conclusion,
        )
        response = AIMessage(content="All steps executed. Done.")
        result: dict = {}
        _detect_terminal_conclusion(response, state, result)
        return result

    def _nudged(self, result) -> bool:
        return any(
            "EXECUTION REQUIRED" in (getattr(m, "content", "") or "")
            for m in result.get("messages", [])
        )

    def test_declared_completion_skips_nudge(self):
        result = self._detect({"progress_ledger": _LEDGER_COMPLETE})
        assert not self._nudged(result)
        assert not result.get("error")
        assert result.get("_execute_text_stall_count") is None

    def test_undeclared_completion_still_nudges(self):
        # No ledger declaration → old behaviour: the nudge fires. The
        # gate must fail BACK to nudging, never into a silent exit.
        result = self._detect({})
        assert self._nudged(result)


# ── Router fallback ────────────────────────────────────────────────────

class TestRouterRespectsLedger:
    def test_declared_completion_routes_to_verifier(self):
        from unittest.mock import patch
        from chaos_agent.agent.router import should_continue_execute_loop

        with patch("chaos_agent.agent.router.settings") as mock_settings:
            mock_settings.max_execute_loop = 15
            state = {
                "execute_loop_count": 1,
                "experiment_uid": None,
                "error": None,
                "messages": [AIMessage(content="done")],
                "progress_ledger": _LEDGER_COMPLETE,
            }
            assert should_continue_execute_loop(state) == "verifier"

    def test_no_declaration_no_active_fault_continues(self):
        from unittest.mock import patch
        from chaos_agent.agent.router import should_continue_execute_loop

        with patch("chaos_agent.agent.router.settings") as mock_settings:
            mock_settings.max_execute_loop = 15
            state = {
                "execute_loop_count": 1,
                "experiment_uid": None,
                "error": None,
                "messages": [AIMessage(content="done")],
            }
            assert should_continue_execute_loop(state) == "continue"


# ── Binding + prompt ───────────────────────────────────────────────────

class TestBindingAndPrompt:
    def test_phase2_binds_finish_execution(self, mock_registry):
        from chaos_agent.agent.factory import _build_skill_tools, _phase_specs

        skill_tools = _build_skill_tools(mock_registry)
        specs = {s.name: s for s in _phase_specs(skill_tools)}
        phase2_names = {t.name for t in specs["phase2"].static_base}
        assert "finish_execution" in phase2_names
        # Read-only / planning surfaces stay clean — a terminal exit is
        # meaningless before any execution exists.
        for phase in ("clarification", "phase1", "verifier"):
            names = {t.name for t in specs[phase].static_base}
            assert "finish_execution" not in names, phase

    def test_executor_prompt_teaches_stop_action(self):
        from chaos_agent.agent.prompts.sections.workflow import (
            get_executor_core_principles_section,
        )
        section = get_executor_core_principles_section()
        assert "finish_execution" in section
        assert "text-only conclusion is not an exit" in section
