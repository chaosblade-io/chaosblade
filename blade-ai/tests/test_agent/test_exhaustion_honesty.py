"""task-ff057e7f regressions: exhaustion must not fake success, and a model
that ignores warnings must be stopped rather than warned again.

The run this file is named after produced four separate defects at once:

  1. ``execute_loop`` burned exactly 100/100 iterations polling pod status;
  2. the router sent that straight to ``save_memory``, so the verifier never ran;
  3. ``failure_reason`` stayed empty, because the node only stamped a cause on
     ``count > MAX`` while the router terminated on ``count >= MAX``;
  4. the envelope reported ``status=success / task_state=injected`` with
     ``verification=null`` — in the same payload as a postmortem saying the
     experiment stalled without injecting.

Underlying all of it: ``reasoning_content`` was present on 2 of 100 executor
turns. The ~20 stagnation notices could not work, because a model that is not
reasoning cannot be reached by text.
"""
import pytest
from unittest.mock import patch

from langchain_core.messages import AIMessage
from langgraph.graph.message import add_messages

from chaos_agent.agent.nodes.execute.llm_step_helpers import hint_count_key
from chaos_agent.agent.router import (
    mark_loop_exhausted,
    should_continue_execute_loop,
    should_continue_verifier,
)


class TestExhaustionRoutesToVerification:
    @patch("chaos_agent.agent.router.settings")
    def test_budget_exhaustion_goes_to_verifier(self, mock_settings):
        mock_settings.max_execute_loop = 100
        mock_settings.max_inject_seconds = 0
        state = {"execute_loop_count": 100, "blade_uid": "uid-1", "error": None}
        assert should_continue_execute_loop(state) == "verifier"

    @patch("chaos_agent.agent.router.settings")
    def test_exhaustion_and_error_agree_now(self, mock_settings):
        """The two branches applied opposite policies to the same situation.

        ``error`` routed to the verifier ("a signal, not a verdict") while
        exhaustion routed to the end — the opposite treatment for the case with
        LESS information, since on exhaustion the model said nothing at all.
        """
        mock_settings.max_execute_loop = 100
        mock_settings.max_inject_seconds = 0
        exhausted = {"execute_loop_count": 100, "blade_uid": "u", "error": None}
        errored = {"execute_loop_count": 1, "blade_uid": "u", "error": "boom"}
        assert should_continue_execute_loop(exhausted) == \
            should_continue_execute_loop(errored) == "verifier"


class TestExhaustionRecordsItsCause:
    """The off-by-one that left ``failure_reason`` empty."""

    def test_exact_budget_use_is_recorded(self):
        # count == max is the NORMAL way to exhaust a budget, and it was the one
        # case neither check covered: the node wanted count > max, the router
        # stopped at count >= max.
        result: dict = {}
        mark_loop_exhausted(result, 100, 100)
        assert result["failure_reason"] == "execution_timeout"
        assert "100/100" in result["error"]
        assert result["failure_detail"]

    def test_below_budget_records_nothing(self):
        result: dict = {}
        mark_loop_exhausted(result, 99, 100)
        assert result == {}

    def test_existing_error_wins(self):
        # A cause the executor diagnosed is more specific than "out of budget".
        result = {"error": "blade create failed: image pull"}
        mark_loop_exhausted(result, 100, 100)
        assert result["error"] == "blade create failed: image pull"
        # The reason field is still filled — the run DID exhaust its budget.
        assert result["failure_reason"] == "execution_timeout"

    def test_disabled_budget_is_a_no_op(self):
        result: dict = {}
        mark_loop_exhausted(result, 100, 0)
        assert result == {}


class TestExecuteLoopHasNoBypassLeft:
    """The router must not be able to end the loop without verification.

    Found while reviewing the routing change: after redirecting both exhaustion
    and wall-clock expiry to the verifier, ``"end"`` became unreachable — but the
    docstring still advertised it and the graph still mapped it to save_memory. A
    dead edge is not harmless here: it is a loaded gun. Any future ``return
    "end"`` would silently restore the exact bypass that produced task-ff057e7f.
    Removing the mapping turns that mistake into a KeyError at runtime, and this
    test turns it into a failure at development time.
    """

    def test_router_never_returns_end(self):
        import ast
        import inspect

        from chaos_agent.agent import router

        tree = ast.parse(inspect.getsource(router))
        for fn in tree.body:
            if not (isinstance(fn, ast.FunctionDef)
                    and fn.name == "should_continue_execute_loop"):
                continue
            returns = {
                ast.unparse(n.value) for n in ast.walk(fn)
                if isinstance(n, ast.Return) and n.value
            }
            assert "'end'" not in returns, (
                "execute_loop routing regained an 'end' exit — every stop must "
                "go through verification"
            )
            assert "'verifier'" in returns
            return
        raise AssertionError("should_continue_execute_loop not found")

    def test_graph_does_not_map_end_for_execute_loop(self):
        import inspect

        from chaos_agent.agent import graph

        src = inspect.getsource(graph)
        idx = src.find("should_continue_execute_loop")
        assert idx > 0
        mapping = src[idx:idx + 500]
        assert '"end": "save_memory"' not in mapping, (
            "the dead 'end' edge is back; it would let a stray return value "
            "bypass the verifier silently instead of raising"
        )


class TestTerminalStateIsSharedAcrossPaths:
    """Single-injection and batch must not describe the same run differently."""

    def test_both_paths_use_terminal_task_state(self):
        import inspect

        from chaos_agent.agent.nodes.batch import batch_next
        from chaos_agent.agent.result import operation_result

        for module in (operation_result, batch_next):
            src = inspect.getsource(module)
            assert "terminal_task_state(" in src, (
                f"{module.__name__} derives task_state on its own; the "
                "unverified-means-failed rule then applies in one place and not "
                "the other"
            )

    def test_unverified_is_failed_not_in_progress(self):
        from chaos_agent.agent.state import infer_task_state, terminal_task_state

        unverified = {"confirmed_intent": "inject", "blade_uid": "u1"}
        # ``infer_task_state`` answers "where is this run" — in progress.
        assert infer_task_state(unverified) == "injecting"
        # At a terminal point that answer is unavailable, so it must not leak out.
        assert terminal_task_state(unverified) == "failed"

    def test_a_real_verdict_is_preserved(self):
        from chaos_agent.agent.state import terminal_task_state

        verified = {
            "confirmed_intent": "inject", "blade_uid": "u1",
            "verification": {"layer1": {"status": "passed"},
                             "layer2": {"status": "passed"}},
        }
        assert terminal_task_state(verified) == "injected"


class TestUnverifiedIsNotSuccess:
    """A creation handle is not evidence the fault took effect."""

    @staticmethod
    def _build(**overrides):
        from chaos_agent.agent.result.operation_result import (
            build_inject_data_from_state,
        )

        state = {
            "confirmed_intent": "inject",
            "blade_uid": "40d9b8e9ec1a1552",
            "fault_spec": {"scope": "pod", "blade_target": "mem",
                           "blade_action": "load", "namespace": "arms-prom"},
        }
        state.update(overrides)
        return build_inject_data_from_state(state, "task-1")

    def test_blade_uid_without_verification_is_failed(self):
        data = self._build()
        assert data["task_state"] == "failed", (
            "blade_uid alone used to be reported as injected/success — that is "
            "the injection ACTION succeeding, not the fault taking effect"
        )

    def test_recovery_handle_survives_the_failed_verdict(self):
        """An unverified experiment may still be live; it must stay recoverable."""
        data = self._build()
        assert data["recovery_handle"] == {
            "kind": "blade_uid", "value": "40d9b8e9ec1a1552",
        }

    def test_verified_injection_is_still_injected(self):
        data = self._build(verification={
            "level": "verified",
            "layer1": {"status": "passed"},
            "layer2": {"status": "passed"},
        })
        assert data["task_state"] == "injected"


class TestHardStagnationBlock:
    """When notices demonstrably do not work, refuse the call."""

    @staticmethod
    def _block(issued, tool="kubectl", args=None, enforcing=True):
        from chaos_agent.agent.nodes.planning.tool_screener import (
            _hard_stagnation_block,
        )
        from chaos_agent.config.settings import settings

        orig = settings.target_guard_enforcing
        settings.target_guard_enforcing = enforcing
        try:
            state = {"hint_repeat_counts": {
                hint_count_key("stagnation", "kubectl:get"): issued,
            }}
            return _hard_stagnation_block(
                state, tool, args if args is not None else
                {"subcommand": "get", "v_args": "pod p -n ns"},
            )
        finally:
            settings.target_guard_enforcing = orig

    def test_allows_until_notices_are_proven_useless(self):
        from chaos_agent.config.settings import settings

        # Up to and including the escalation point the soft path still owns it.
        for issued in range(1, settings.hint_escalate_after + 1):
            reason, _ = self._block(issued)
            assert reason == "", f"blocked too early at {issued}"

    def test_blocks_past_the_escalation_point(self):
        from chaos_agent.config.settings import settings

        reason, fix = self._block(settings.hint_escalate_after + 1)
        assert "refused as stagnant" in reason
        assert "subcommand 'get'" in reason
        # The fix must say it is a refusal, not a suggestion — advice is exactly
        # what had already failed ~20 times.
        assert "refusal, not advice" in fix
        assert "state your conclusion" in fix
        # And it must disclose that it alternates, so the model can use the
        # opening deliberately instead of treating the call as gone for good.
        assert "alternates" in fix

    def test_the_block_alternates_rather_than_latching(self):
        """Block, allow, block — never a permanent wall.

        A latched block turns "stop repeating this" into "you may never look at
        this again", which the model cannot satisfy: the same subcommand is often
        legitimately needed once it changes angle. Alternating still breaks the
        streak — every other attempt fails — while keeping the call reachable.
        """
        from langchain_core.messages import ToolMessage

        from chaos_agent.agent.nodes.planning.tool_screener import (
            _hard_stagnation_block,
        )
        from chaos_agent.config.settings import settings

        orig = settings.target_guard_enforcing
        settings.target_guard_enforcing = True
        try:
            counts = {hint_count_key("stagnation", "kubectl:get"): 20}
            history: list = []
            verdicts = []
            for attempt in range(6):
                state = {"messages": list(history), "hint_repeat_counts": counts}
                reason, _ = _hard_stagnation_block(
                    state, "kubectl", {"subcommand": "get", "v_args": "pod p"},
                )
                verdicts.append("block" if reason else "allow")
                # Feed back whichever result that decision would produce.
                history.append(ToolMessage(
                    content=(f"[target_guard] REJECT — {reason}" if reason
                             else "NAME CPU MEM\npod-x 34m 110Mi"),
                    tool_call_id=f"c{attempt}", name="kubectl",
                ))
        finally:
            settings.target_guard_enforcing = orig

        assert verdicts == ["block", "allow"] * 3, verdicts

    def test_an_older_block_behind_successful_calls_grants_no_pass(self):
        """Only the IMMEDIATELY preceding attempt matters.

        Otherwise a block from early in the drill would keep excusing repeats
        much later, after the model had gone off and done other things.
        """
        from langchain_core.messages import ToolMessage

        from chaos_agent.agent.nodes.planning.tool_screener import (
            _hard_stagnation_block,
        )
        from chaos_agent.config.settings import settings

        orig = settings.target_guard_enforcing
        settings.target_guard_enforcing = True
        try:
            state = {
                "hint_repeat_counts": {
                    hint_count_key("stagnation", "kubectl:get"): 20,
                },
                "messages": [
                    ToolMessage(content="[target_guard] REJECT — was refused as "
                                        "stagnant: ...",
                                tool_call_id="c1", name="kubectl"),
                    ToolMessage(content="NAME CPU\npod-x 34m",
                                tool_call_id="c2", name="kubectl"),
                ],
            }
            reason, _ = _hard_stagnation_block(
                state, "kubectl", {"subcommand": "get", "v_args": "pod p"},
            )
        finally:
            settings.target_guard_enforcing = orig
        assert reason, "a stale block must not excuse the current repeat"

    @pytest.mark.asyncio
    async def test_stagnation_refusal_is_labelled_differently_from_a_real_ban(self):
        """The model must be able to tell "not now" from "not allowed".

        Both used ``REJECT_UNKNOWN``, so the ToolMessage prefix was identical and
        a repetition block read as "this tool is unavailable" — a lesson the model
        would carry for the rest of the drill even though the call is admissible
        and the refusal alternates. Worse, ``REJECT_UNKNOWN`` additionally
        attracts the "no approved target on record; the screener default-denies"
        note, which has nothing to do with stagnation.
        """
        from langchain_core.messages import AIMessage

        from chaos_agent.agent.nodes.planning.tool_screener import tool_screener
        from chaos_agent.agent.target_guard import freeze_approved_target
        from chaos_agent.config.settings import settings

        approved = freeze_approved_target(
            target={"namespace": "arms-prom", "names": ["pod-a"]},
            params={"scope": "pod"},
            blade_scope="pod", blade_target="mem", blade_action="load",
        )
        base = {
            "approved_target": approved, "execution_artifacts": [],
            "task_id": "t1", "kubeconfig": "/tmp/kc",
        }
        orig = settings.target_guard_enforcing
        settings.target_guard_enforcing = True
        try:
            stagnant = await tool_screener({
                **base,
                "hint_repeat_counts": {
                    hint_count_key("stagnation", "kubectl:get"): 20,
                },
                "messages": [AIMessage(content="", tool_calls=[{
                    "name": "kubectl",
                    "args": {"subcommand": "get", "v_args": "pod pod-a -n arms-prom"},
                    "id": "c1",
                }])],
            })
            unavailable = await tool_screener({
                **base,
                "messages": [AIMessage(content="", tool_calls=[{
                    "name": "host_inject", "args": {"command": "x"}, "id": "c2",
                }])],
            })
        finally:
            settings.target_guard_enforcing = orig

        stag_text = str(stagnant["messages"][0].content)
        unavail_text = str(unavailable["messages"][0].content)

        assert "REJECT_STAGNANT" in stag_text
        assert "REJECT_STAGNANT" not in unavail_text
        # The stagnation refusal must not borrow the vocabulary of a real ban.
        assert "REJECT_UNKNOWN" not in stag_text
        assert "cannot make it reachable" not in stag_text
        assert "default-denies" not in stag_text
        # And it must state the repetition as the cause, plus the alternation.
        assert "kept repeating" in stag_text
        assert "alternates" in stag_text

    def test_a_different_subcommand_is_not_blocked(self):
        reason, _ = self._block(20, args={"subcommand": "describe", "v_args": "pod p"})
        assert reason == "", "the block must be per-subcommand, not per-tool"

    def test_a_different_tool_is_not_blocked(self):
        reason, _ = self._block(20, tool="blade_status", args={})
        assert reason == ""

    def test_log_only_mode_does_not_block(self):
        reason, _ = self._block(20, enforcing=False)
        assert reason == "", (
            "the guard's global switch governs refusals; in log-only mode a "
            "stuck model is a diagnosis, not something to block"
        )

    def test_uses_the_detectors_own_key_function(self):
        """Independent key derivation is how a block misses its target."""
        import inspect

        from chaos_agent.agent.nodes.planning import tool_screener

        src = inspect.getsource(tool_screener._hard_stagnation_block)
        assert "_stagnation_key(" in src, (
            "must key through the detector's own _stagnation_key, or the block "
            "can refuse a call nobody warned about"
        )

    @pytest.mark.parametrize("bad", ["abc", None, [], {}])
    def test_malformed_count_does_not_raise(self, bad):
        from chaos_agent.agent.nodes.planning.tool_screener import (
            _hard_stagnation_block,
        )

        state = {"hint_repeat_counts": {
            hint_count_key("stagnation", "kubectl:get"): bad,
        }}
        reason, _ = _hard_stagnation_block(
            state, "kubectl", {"subcommand": "get"},
        )
        assert reason == ""


class TestBudgetHintsPersistByReplacement:
    """The countdown must survive the turn, and must never be stale.

    These were turn-local, so the model saw "iteration 12 of 15" once and the
    next turn had no idea how much budget was left. The reason they were left
    turn-local was a real hazard, just not an unavoidable one: a plain append
    would leave "iteration 3 of 15" in history for turn 12 to read and conclude
    it has room. A STABLE id gets both properties — ``add_messages`` replaces the
    previous copy, so history holds exactly one entry always stating the current
    number.
    """

    def test_replaceable_hint_keeps_one_entry_with_latest_text(self):
        from chaos_agent.agent.nodes.execute.llm_step_helpers import (
            persist_replaceable_hint,
        )

        history: list = []
        for turn in (10, 11, 12):
            injections: list = []
            persist_replaceable_hint(
                injections, "budget", "execute",
                f"**Iteration Progress**: iteration {turn} of max 15",
            )
            history = add_messages(history, injections)
        assert len(history) == 1
        assert "iteration 12 of max 15" in history[0].content
        assert "iteration 10" not in history[0].content

    def test_replaceable_hint_adds_no_repeat_counter(self):
        """It restates a changing fact; it is not a record of ignored warnings."""
        from chaos_agent.agent.nodes.execute.llm_step_helpers import (
            persist_replaceable_hint,
        )

        injections: list = []
        msg = persist_replaceable_hint(injections, "budget", "execute", "iteration 3")
        assert "reminder #" not in msg.content
        assert "hint-repeat" not in msg.content

    def test_turn_local_copy_has_no_id(self):
        from chaos_agent.agent.nodes.execute.llm_step_helpers import (
            persist_replaceable_hint,
        )

        injections: list = []
        returned = persist_replaceable_hint(injections, "budget", "execute", "x")
        # Both copies in one update with the same id would collapse to one,
        # costing the tail position that carries the attention weight.
        assert returned.id != injections[0].id

    @pytest.mark.parametrize("module_path", [
        "chaos_agent.agent.nodes.execute.agent_loop",
        "chaos_agent.agent.nodes.execute.execute_loop",
    ])
    def test_all_three_tiers_are_persisted(self, module_path):
        import importlib
        import inspect
        import re

        src = inspect.getsource(importlib.import_module(module_path))
        for marker in ("Iteration Progress", "CRITICAL WARNING", "FINAL ITERATION"):
            idx = src.find(marker)
            assert idx > 0, f"{marker} not found in {module_path}"
            window = src[max(0, idx - 350):idx]
            # ``execute_loop`` funnels its three tiers through a local ``_emit``
            # wrapper, ``agent_loop`` calls the helper inline — accept either, and
            # separately assert the wrapper itself persists.
            assert ("persist_replaceable_hint(" in window or "_emit(" in window), (
                f"{module_path}: {marker} is still turn-local — the model will "
                "see the countdown once and forget it next turn"
            )
        if "_emit(" in src:
            emit_def = src[src.find("def _emit("):]
            assert "persist_replaceable_hint(" in emit_def[:400], (
                f"{module_path}: the _emit wrapper does not persist"
            )


class TestSoftConstraintsAskForReasoning:
    """Every stagnation notice must ask for explicit reasoning first.

    task-ff057e7f had ``reasoning_content`` on 2 of 100 executor turns, and the
    ~20 notices it received changed nothing. A notice that assumes reflection is
    happening cannot reach a model that is not reflecting; asking for the
    reflection explicitly is the one lever the text still has.
    """

    def test_subcommand_level_hint_asks_for_reasoning(self):
        from chaos_agent.agent.nodes.execute.llm_step_helpers import (
            build_stagnation_hint,
        )

        hint = build_stagnation_hint("kubectl:get", colon_suffix="to gather evidence")
        assert "reason it through explicitly" in hint
        # The three questions are what makes it actionable rather than a scold.
        assert "still genuinely" in hint
        assert "the last one did not" in hint

    def test_tool_level_hint_asks_for_reasoning(self):
        from chaos_agent.agent.nodes.execute.llm_step_helpers import (
            build_stagnation_hint,
        )

        hint = build_stagnation_hint("blade_status", else_actions=["Use another tool."])
        assert "Reason the choice through before acting" in hint
        assert "valid conclusion" in hint

    @pytest.mark.parametrize("marker", [
        "Iteration Progress", "CRITICAL WARNING", "FINAL ITERATION",
    ])
    def test_budget_tiers_ask_for_reasoning(self, marker):
        import inspect

        from chaos_agent.agent.nodes.execute import agent_loop, execute_loop

        for module in (agent_loop, execute_loop):
            src = inspect.getsource(module)
            idx = src.find(marker)
            if idx < 0:
                continue
            block = src[idx:idx + 900]
            assert any(cue in block for cue in (
                "Think the next step through", "Reason it through",
                "Think through what the evidence",
            )), f"{module.__name__}: {marker} does not ask for reasoning"


class TestEveryLoopRecordsExhaustion:
    """No loop may end on budget exhaustion without a recorded cause."""

    @pytest.mark.parametrize("module_path,const", [
        ("chaos_agent.agent.nodes.execute.agent_loop", "MAX_AGENT_LOOP"),
        ("chaos_agent.agent.nodes.execute.execute_loop", "MAX_EXECUTE_LOOP"),
        ("chaos_agent.agent.nodes.verify.verifier", "max_verifier_loop"),
    ])
    def test_loop_node_calls_the_backstop(self, module_path, const):
        import importlib
        import inspect

        src = inspect.getsource(importlib.import_module(module_path))
        assert "mark_loop_exhausted(" in src, (
            f"{module_path} has no exhaustion backstop: a branch that forgets to "
            f"stamp a cause ends the run with failure_reason='' (task-ff057e7f)"
        )
        assert const in src

    def test_planning_exhaustion_uses_the_planning_category(self):
        from chaos_agent.agent.result.verdict import FailureCategory

        result: dict = {}
        mark_loop_exhausted(
            result, 40, 40,
            category=FailureCategory.PLANNING_TIMEOUT, label="planning loop",
        )
        assert result["failure_reason"] == "planning_timeout"
        assert "planning loop budget exhausted" in result["error"]


class TestEmptyTurnIsNotAConclusion:
    """An empty AI turn must not be read as "the model is done".

    task-a8ad1602 produced this twice, and both times the state machine advanced:

      * execute_loop [500] — ``content=""`` with ``<function=finish_execution>``
        stranded in ``reasoning_content``. That tool does not exist; the model
        copied planning's ``finish_planning`` pattern into a phase whose exit
        protocol is plain text. The router saw "no tool_calls + blade_uid" and
        went to the verifier.
      * verify [731] — ``content=""`` with a COMPLETE and correct
        ``submit_verification`` payload (``overall: verified``) stranded the same
        way. The router took the text-fallback branch, finalize parsed the empty
        string into ``level=partial``, and that shipped as ``status=success``
        while the model's own verdict said verified.

    Both branches only asked "were there tool_calls?", which cannot tell a text
    conclusion from silence. Continuing costs one iteration; concluding costs the
    correctness of the entire result.
    """

    EMPTY = AIMessage(content="")
    BLANK = AIMessage(content="   \n\t ")
    REAL = AIMessage(content="注入完成，内存达到 80%")

    @pytest.mark.parametrize("msg", [EMPTY, BLANK])
    @patch("chaos_agent.agent.router.settings")
    def test_execute_loop_keeps_going_on_an_empty_turn(self, mock_settings, msg):
        mock_settings.max_execute_loop = 100
        mock_settings.max_inject_seconds = 0
        state = {"execute_loop_count": 50, "blade_uid": "uid-1", "messages": [msg]}
        assert should_continue_execute_loop(state) == "continue"

    @patch("chaos_agent.agent.router.settings")
    def test_execute_loop_still_finishes_on_real_text(self, mock_settings):
        """The fix must not break the normal exit — text IS how execute ends."""
        mock_settings.max_execute_loop = 100
        mock_settings.max_inject_seconds = 0
        state = {
            "execute_loop_count": 50, "blade_uid": "uid-1", "messages": [self.REAL],
        }
        assert should_continue_execute_loop(state) == "verifier"

    @pytest.mark.parametrize("msg", [EMPTY, BLANK])
    @patch("chaos_agent.agent.router.settings")
    def test_verifier_does_not_finalize_an_empty_turn(self, mock_settings, msg):
        mock_settings.max_verifier_loop = 15
        mock_settings.max_inject_seconds = 0
        state = {"verifier_loop_count": 3, "messages": [msg]}
        assert should_continue_verifier(state) == "continue", (
            "the text fallback needs text; finalize would parse '' into a verdict"
        )

    @patch("chaos_agent.agent.router.settings")
    def test_verifier_still_finalizes_real_text(self, mock_settings):
        mock_settings.max_verifier_loop = 15
        mock_settings.max_inject_seconds = 0
        state = {"verifier_loop_count": 3, "messages": [self.REAL]}
        assert should_continue_verifier(state) == "finalize"

    @patch("chaos_agent.agent.router.settings")
    def test_tool_calls_win_even_with_empty_content(self, mock_settings):
        """Empty content plus tool_calls is a normal ReAct turn, not silence."""
        mock_settings.max_execute_loop = 100
        mock_settings.max_inject_seconds = 0
        msg = AIMessage(content="", tool_calls=[
            {"name": "kubectl", "args": {"subcommand": "get"}, "id": "c1"},
        ])
        state = {"execute_loop_count": 50, "blade_uid": "uid-1", "messages": [msg]}
        assert should_continue_execute_loop(state) == "continue"

    def test_empty_turn_predicate_ignores_non_ai_messages(self):
        """Only an AI turn can be "the model said nothing"."""
        from langchain_core.messages import HumanMessage, ToolMessage

        from chaos_agent.agent.router import _is_empty_ai_turn

        assert _is_empty_ai_turn(AIMessage(content="")) is True
        assert _is_empty_ai_turn(HumanMessage(content="")) is False
        assert _is_empty_ai_turn(
            ToolMessage(content="", tool_call_id="c1", name="kubectl"),
        ) is False

    def test_multipart_content_is_handled(self):
        """Providers may return content as a list of blocks."""
        from chaos_agent.agent.router import _is_empty_ai_turn

        assert _is_empty_ai_turn(AIMessage(content=[])) is True
        assert _is_empty_ai_turn(
            AIMessage(content=[{"type": "text", "text": "done"}]),
        ) is False
