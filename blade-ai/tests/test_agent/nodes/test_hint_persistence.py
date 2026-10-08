"""Corrective hints must survive the turn; per-iteration hints must not.

The distinction is the whole point of ``persist_corrective_hint``. A hint that
only reaches the LLM through a node-local message copy is gone when the turn
ends, so the next iteration re-derives it and the model reads a first-time
warning every time. task-e9ee12d6 measured the cost: the verify-phase stagnation
notice fired from turn 11 onward and the model still issued the same
``kubectl_read top`` call 31 more times — 42 identical calls in one phase.

Per-iteration budget hints are the opposite case and must stay turn-local: their
text names the current iteration ("iteration 3 of 15"), so a stale persisted copy
would tell turn 12 it still has plenty of budget.
"""
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph.message import add_messages

from chaos_agent.agent.nodes.execute.llm_step_helpers import (
    count_prior_hints,
    fold_hint_state,
    persist_corrective_hint,
)


class TestPersistedHintAccumulation:
    def test_first_occurrence_records_one_message(self):
        injections: list = []
        returned = persist_corrective_hint(
            injections, [], "stagnation", "kubectl_read:top", "You are repeating.",
        )
        assert len(injections) == 1
        assert injections[0].id == "hint:stagnation:kubectl_read:top"
        # The turn-local copy must NOT carry the id: a node folding both into the
        # same update would otherwise have the reducer collapse them into one.
        assert returned.id != injections[0].id
        assert "You are repeating." in returned.content

    def test_repeat_overwrites_instead_of_piling_up(self):
        """Within the escalation threshold, repeats overwrite instead of piling up.

        Bounded by ``settings.hint_escalate_after`` rather than a literal 5: past
        the threshold the mode deliberately switches to accumulating entries, so
        a fixed count above it would be asserting that escalation never happens.
        That assertion is what this test used to make by omitting the kwarg —
        i.e. it pinned the opt-out-by-omission bug as a contract. See
        :class:`TestEscalationLegislationAppliesByConstruction`.
        """
        from chaos_agent.config.settings import settings

        triggers = settings.hint_escalate_after
        history: list = []
        for _ in range(triggers):
            injections: list = []
            persist_corrective_hint(
                injections, history, "stagnation", "kubectl_read:top", "Repeating.",
            )
            history = add_messages(history, injections)
        # Every trigger at or below the threshold, one entry: the reducer replaced
        # by id each time.
        assert len(history) == 1
        assert count_prior_hints(history, "stagnation", "kubectl_read:top") == triggers

    def test_text_states_the_running_count_and_that_prior_ones_failed(self):
        history: list = []
        for _ in range(3):
            injections: list = []
            msg = persist_corrective_hint(
                injections, history, "stagnation", "kubectl_read:top", "Repeating.",
            )
            history = add_messages(history, injections)
        # A weak model that ignores one notice needs to see that it ignored the
        # earlier ones too — that is the whole reason for persisting.
        assert "reminder #3" in msg.content
        assert "did not change the outcome" in msg.content

    def test_escalation_switches_to_accumulating_entries(self):
        history: list = []
        for _ in range(13):
            injections: list = []
            persist_corrective_hint(
                injections, history, "stagnation", "kubectl_read:top", "Repeating.",
                escalate_after=10,
            )
            history = add_messages(history, injections)
        # Occurrences 1-10 collapse onto the stable id; 11, 12, 13 each get their
        # own, so the visible weight grows once overwriting has demonstrably
        # failed to change behaviour.
        assert len(history) == 4, [m.id for m in history]
        assert history[0].id == "hint:stagnation:kubectl_read:top"
        assert {m.id for m in history[1:]} == {
            "hint:stagnation:kubectl_read:top#11",
            "hint:stagnation:kubectl_read:top#12",
            "hint:stagnation:kubectl_read:top#13",
        }

    def test_distinct_keys_are_counted_separately(self):
        history: list = []
        for key in ("kubectl_read:top", "kubectl_read:describe"):
            injections: list = []
            persist_corrective_hint(injections, history, "stagnation", key, "x")
            history = add_messages(history, injections)
        assert len(history) == 2
        assert count_prior_hints(history, "stagnation", "kubectl_read:top") == 1
        assert count_prior_hints(history, "stagnation", "kubectl_read:describe") == 1

    def test_unrelated_history_is_not_counted(self):
        history = [
            HumanMessage(content="用户请求"),
            AIMessage(content="动作"),
            HumanMessage(content="**ACTION_STAGNATION**: looks similar", id="other"),
        ]
        assert count_prior_hints(history, "stagnation", "kubectl_read:top") == 0


class TestVerifierPersistsCorrectiveHints:
    """The node that produced task-e9ee12d6 must now fold hints into its update."""

    def test_verifier_result_includes_persisted_hints(self):
        import inspect

        from chaos_agent.agent.nodes.verify import verifier

        src = inspect.getsource(verifier)
        assert "_hints_for_state" in src
        # The hints must reach the returned update, not just the local list.
        assert "_hints_for_state + [response]" in src

    @pytest.mark.parametrize("module_path,expect", [
        ("chaos_agent.agent.nodes.execute.agent_loop", "_injections_for_state"),
        ("chaos_agent.agent.nodes.execute.execute_loop", "_hints_for_state"),
        ("chaos_agent.agent.nodes.planning.intent_clarification", "_hints_for_state"),
    ])
    def test_every_loop_node_persists_its_corrective_hints(self, module_path, expect):
        import importlib
        import inspect

        src = inspect.getsource(importlib.import_module(module_path))
        assert "persist_corrective_hint" in src, (
            f"{module_path} still injects corrective hints turn-locally"
        )
        assert expect in src


class TestPerIterationHintsStayTurnLocal:
    """Budget countdowns must NOT persist — a stale one misstates the budget."""

    @pytest.mark.parametrize("module_path", [
        "chaos_agent.agent.nodes.execute.agent_loop",
        "chaos_agent.agent.nodes.execute.execute_loop",
    ])
    def test_iteration_countdown_is_not_routed_through_persist(self, module_path):
        import importlib
        import inspect
        import re

        src = inspect.getsource(importlib.import_module(module_path))
        # Find each countdown injection and assert it is a plain append, not a
        # persisted one. "iteration N of max M" text in history would tell a
        # later turn it has more budget than it does.
        for marker in ("Iteration Progress", "FINAL ITERATION"):
            for m in re.finditer(re.escape(marker), src):
                window = src[max(0, m.start() - 400):m.start()]
                # The nearest preceding append call must not be the persisting one.
                appends = re.findall(r"(\w+)\.append\(|persist_corrective_hint\(", window)
                if appends:
                    assert appends[-1] != "", f"unexpected parse near {marker}"
                assert "persist_corrective_hint(\n" not in window[-120:], (
                    f"{module_path}: {marker} appears to be persisted; its text "
                    "names the current iteration and must stay turn-local"
                )


class TestCountSurvivesCompaction:
    """Compaction removes messages; the count must not ride on them.

    ``ContextManager`` reserves a tail worth ``reserve_tokens`` and hands the rest
    to the summariser, whose originals are then dropped. A hint is recorded at its
    FIRST occurrence — early in history — so in any drill that outgrows the
    reservation the hint message is exactly the kind that gets summarised away.
    Measured on a 35,461-token synthetic history: the hint landed in
    ``to_compact`` and a message-derived count went 1 → 0, which would tell the
    model "reminder #1" after twenty real occurrences.

    So the number lives in ``state["hint_repeat_counts"]``, which compaction never
    touches, and the messages exist only so the model can SEE the mistake.
    """

    def test_state_count_is_used_when_the_message_is_gone(self):
        from chaos_agent.agent.nodes.execute.llm_step_helpers import (
            hint_count_key,
            resolve_hint_count,
        )

        counts = {hint_count_key("stagnation", "kubectl_read:top"): 20}
        # Post-compaction history: the hint message no longer exists.
        assert resolve_hint_count(counts, [], "stagnation", "kubectl_read:top") == 20

        injections: list = []
        out: dict = {}
        msg = persist_corrective_hint(
            injections, [], "stagnation", "kubectl_read:top", "Repeating.",
            counts=counts, counts_out=out,
        )
        assert "reminder #21" in msg.content
        assert out[hint_count_key("stagnation", "kubectl_read:top")] == 21

    def test_message_scan_still_works_for_pre_upgrade_checkpoints(self):
        """A checkpoint written before the state field existed must not reset."""
        from chaos_agent.agent.nodes.execute.llm_step_helpers import resolve_hint_count

        history: list = []
        injections: list = []
        persist_corrective_hint(
            injections, history, "stagnation", "kubectl_read:top", "x",
        )
        history = add_messages(history, injections)
        # No state dict at all — the fallback reads the marker out of the message.
        assert resolve_hint_count(None, history, "stagnation", "kubectl_read:top") == 1
        assert resolve_hint_count({}, history, "stagnation", "kubectl_read:top") == 1

    def test_state_and_history_disagreeing_takes_the_larger(self):
        from chaos_agent.agent.nodes.execute.llm_step_helpers import (
            hint_count_key,
            resolve_hint_count,
        )

        history: list = []
        injections: list = []
        for _ in range(3):
            injections = []
            persist_corrective_hint(
                injections, history, "stagnation", "k", "x",
            )
            history = add_messages(history, injections)
        # A mid-upgrade run can have history ahead of a freshly-created dict.
        assert resolve_hint_count({}, history, "stagnation", "k") == 3
        # And state ahead of a compacted history.
        assert resolve_hint_count(
            {hint_count_key("stagnation", "k"): 9}, history, "stagnation", "k",
        ) == 9

    def test_malformed_state_value_does_not_raise(self):
        from chaos_agent.agent.nodes.execute.llm_step_helpers import (
            hint_count_key,
            resolve_hint_count,
        )

        # A checkpoint could carry anything; the hint path must not be the thing
        # that breaks a live drill.
        for bad in ("abc", None, [], {}):
            counts = {hint_count_key("stagnation", "k"): bad}
            assert resolve_hint_count(counts, [], "stagnation", "k") == 0

    @pytest.mark.parametrize("module_path", [
        "chaos_agent.agent.nodes.execute.agent_loop",
        "chaos_agent.agent.nodes.execute.execute_loop",
        "chaos_agent.agent.nodes.verify.verifier",
        "chaos_agent.agent.nodes.planning.intent_clarification",
    ])
    def test_every_node_reads_and_writes_the_state_counter(self, module_path):
        import importlib
        import inspect

        src = inspect.getsource(importlib.import_module(module_path))
        assert 'state.get("hint_repeat_counts")' in src, (
            f"{module_path} does not seed counts from state — after compaction it "
            "would restart counting at 1"
        )
        assert '"hint_repeat_counts"' in src
        assert "counts_out=" in src

    def test_state_field_is_declared_on_both_state_classes(self):
        from chaos_agent.agent.state import AgentState, IntentState

        # IntentState is not an AgentState subclass, so the field has to be on
        # both or intent_clarification silently cannot persist its counter.
        assert "hint_repeat_counts" in AgentState.__annotations__
        assert "hint_repeat_counts" in IntentState.__annotations__


class TestEscalationThresholdIsConfigurable:
    """The escalation point is an operator dial, not a constant.

    It was initially borrowed from ``stagnation_frequency_ceiling`` (10), which
    conflated two different questions: how many consecutive turns make a streak
    abnormal, versus how many ignored reminders make the overwrite mode a proven
    failure. They also count different things — the ceiling counts tool calls
    inside the detection window, this counts hints ISSUED, and a hint only starts
    counting once the detector first fires. In task-e9ee12d6 the detector first
    fired on tool call 11, so a value of 3 lands around tool call 14.
    """

    def test_default_is_three(self):
        from chaos_agent.config.settings import Settings

        # 3 hints, not 3 tool calls: the detector has its own threshold, so by
        # the time hint #1 exists the call has already repeated 5 times (output
        # steady) or 11 (output drifting). Escalating after 3 more therefore
        # lands at tool call 8 / 14 — well past the p95 of 3 consecutive turns
        # measured across 14 real drills.
        assert Settings().hint_escalate_after == 3

    def test_nodes_use_the_setting_not_the_frequency_ceiling(self):
        import importlib
        import inspect

        for path in (
            "chaos_agent.agent.nodes.execute.agent_loop",
            "chaos_agent.agent.nodes.execute.execute_loop",
            "chaos_agent.agent.nodes.verify.verifier",
            "chaos_agent.agent.nodes.planning.intent_clarification",
        ):
            src = inspect.getsource(importlib.import_module(path))
            assert "escalate_after=settings.hint_escalate_after" in src, path
            # The ceiling answers a different question and must not be reused
            # as the escalation point.
            assert "escalate_after=settings.stagnation_frequency_ceiling" not in src

    @pytest.mark.parametrize("bad", [0, -1, -10])
    def test_non_positive_is_refused_not_clamped(self, bad):
        from chaos_agent.config.settings import Settings

        # count > escalate_after would be true on occurrence 1, so every hint
        # would get its own id and history would grow every turn from the start.
        with pytest.raises(ValueError, match="hint_escalate_after"):
            Settings(hint_escalate_after=bad)

    def test_a_lower_value_escalates_sooner(self):
        history: list = []
        counts: dict = {}
        entries_at = {}
        for turn in range(1, 9):
            injections: list = []
            persist_corrective_hint(
                injections, history, "stagnation", "k", "x",
                escalate_after=3, counts=counts, counts_out=counts,
            )
            history = add_messages(history, injections)
            entries_at[turn] = sum(
                1 for m in history
                if (getattr(m, "id", "") or "").startswith("hint:stagnation:k")
            )
        # With escalate_after=3: occurrences 1-3 collapse onto one entry, then
        # 4..8 each add one → 1 + 5 = 6.
        assert entries_at[3] == 1
        assert entries_at[4] == 2
        assert entries_at[8] == 6


class TestEscalationLegislationAppliesByConstruction:
    """``escalate_after=None`` INHERITS the legislation; it does not disable it.

    Opt-out-by-omission was the measured failure: ``tool_error`` and
    ``transient_exhaustion`` never passed the kwarg, so ``escalated`` was
    permanently False for them. inject-6ebf341c ran the planning-phase
    ``tool_error`` count to 6 — the model reported reading reminder #7 — while
    history still held ONE overwritten copy. That is the exact condition the
    escalation mode exists for, with the mode never engaging.

    A source-text assertion cannot catch this, and the four omissions surviving
    beside ten correct call sites is the proof: such an assertion passes as soon
    as ANY call site in the module spells the kwarg. The guarantee has to be
    behavioural — the mechanism defaults to the legislation, so a call site can
    override the threshold but cannot forget to be subject to it.
    """

    @staticmethod
    def _run(turns: int, **kw) -> dict:
        history: list = []
        counts: dict = {}
        entries: dict = {}
        for turn in range(1, turns + 1):
            injections: list = []
            persist_corrective_hint(
                injections, history, "tool_error", "kubectl_read", "x",
                counts=counts, counts_out=counts, **kw,
            )
            history = add_messages(history, injections)
            entries[turn] = sum(
                1 for m in history
                if (getattr(m, "id", "") or "").startswith("hint:tool_error:")
            )
        return entries

    def test_omitting_the_kwarg_matches_passing_the_setting(self):
        from chaos_agent.config.settings import settings

        assert self._run(6) == self._run(
            6, escalate_after=settings.hint_escalate_after,
        )

    def test_omitting_the_kwarg_actually_escalates(self):
        entries = self._run(6)
        # 1..3 collapse onto one entry, 4..6 each add one.
        assert entries[3] == 1
        assert entries[4] == 2
        assert entries[6] == 4

    def test_an_explicit_override_still_wins(self):
        # Inheriting by default must not remove the ability to override.
        assert self._run(6, escalate_after=10)[6] == 1


class TestFoldHintState:
    """One fold for every carrier field.

    Five loop nodes each hand-rolled the compare-then-assign for
    ``hint_repeat_counts``. Adding a second carrier to five sites is how a field
    ends up written by some nodes and dropped by others, so the fold is a
    function and a guard below asserts nobody hand-rolls it again.
    """

    def test_writes_both_carriers(self):
        result: dict = {}
        state = {"hint_repeat_counts": {}, "hinted_error_calls": {}}
        fold_hint_state(
            result, state,
            {"tool_error:kubectl_read": 2},
            {"tc1": "tool_error:kubectl_read"},
        )
        assert result["hint_repeat_counts"] == {"tool_error:kubectl_read": 2}
        assert result["hinted_error_calls"] == {"tc1": "tool_error:kubectl_read"}

    def test_unchanged_state_emits_nothing(self):
        result: dict = {}
        state = {"hint_repeat_counts": {"a": 1}, "hinted_error_calls": {"tc1": "k"}}
        fold_hint_state(result, state, {"a": 1}, {"tc1": "k"})
        assert result == {}

    def test_empty_local_copy_cannot_clobber_state(self):
        result: dict = {}
        state = {"hint_repeat_counts": {"a": 1}, "hinted_error_calls": {"tc1": "k"}}
        fold_hint_state(result, state, {}, {})
        assert result == {}

    def test_no_node_hand_rolls_the_writeback(self):
        import importlib
        import inspect

        for path in (
            "chaos_agent.agent.nodes.execute.agent_loop",
            "chaos_agent.agent.nodes.execute.execute_loop",
            "chaos_agent.agent.nodes.verify.verifier",
        ):
            src = inspect.getsource(importlib.import_module(path))
            assert "fold_hint_state(" in src, path
            # The hand-rolled form, in either variable spelling.
            assert '["hint_repeat_counts"] = _hint_counts' not in src, path
