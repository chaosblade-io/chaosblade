"""Tests for E2 Phase 2B — observation timeline accumulation.

After ``_extract_tool_metrics`` populates ToolMessage additional_kwargs,
``PreReasoningHook._build_observation_update`` walks the messages and
returns a state update that appends new entries to
``state.metric_observations``. The list must:
  - Be idempotent across hook re-entry (same tool_call_id never
    duplicates).
  - Survive ``[Compressed History]`` compaction (lives on state,
    not on the message list).
  - Carry iteration + timestamp + tool_call_id + metrics on each entry.
"""
from __future__ import annotations

from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.memory.hook import PreReasoningHook


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hook() -> PreReasoningHook:
    """Build a minimal hook — only the method under test is exercised
    here, so the heavy collaborators get None."""
    return PreReasoningHook(
        context_manager=None,
        tool_compactor=None,
        session_store=None,
        llm=None,
        tui_session_store=None,
    )


def _tool_with_metrics(
    tool_call_id: str,
    metrics: dict[str, str],
    *,
    content: str = "raw",
    name: str = "kubectl",
) -> ToolMessage:
    """A ToolMessage already 'processed' by _extract_tool_metrics —
    additional_kwargs['extracted_metrics'] is the contract Phase 2B
    consumes."""
    tm = ToolMessage(content=content, tool_call_id=tool_call_id, name=name)
    tm.additional_kwargs = {"extracted_metrics": dict(metrics)}
    return tm


# ---------------------------------------------------------------------------
# _build_observation_update
# ---------------------------------------------------------------------------


class TestBuildObservationUpdate:
    def test_first_observation_creates_list(self):
        hook = _hook()
        msgs = [_tool_with_metrics("tc-1", {"RestartCount": "7"})]
        state = {"verifier_loop_count": 1}

        update = hook._build_observation_update(msgs, state)
        obs_list = update["metric_observations"]
        assert len(obs_list) == 1
        entry = obs_list[0]
        assert entry["tool_call_id"] == "tc-1"
        assert entry["iteration"] == 1
        assert entry["tool_name"] == "kubectl"
        assert entry["metrics"] == {"RestartCount": "7"}
        assert entry["timestamp"]  # non-empty

    def test_appends_to_existing_observations(self):
        hook = _hook()
        existing = [{
            "iteration": 1, "timestamp": "2026-05-25T00:00:00Z",
            "tool_call_id": "tc-1", "tool_name": "kubectl",
            "metrics": {"RestartCount": "7"},
        }]
        msgs = [_tool_with_metrics("tc-2", {"RestartCount": "8"})]
        state = {
            "verifier_loop_count": 2,
            "metric_observations": existing,
        }

        update = hook._build_observation_update(msgs, state)
        obs_list = update["metric_observations"]
        assert len(obs_list) == 2
        # Order: existing first, then new
        assert obs_list[0]["tool_call_id"] == "tc-1"
        assert obs_list[1]["tool_call_id"] == "tc-2"
        assert obs_list[1]["iteration"] == 2

    def test_idempotent_on_repeated_tool_call_id(self):
        # Same ToolMessage walked twice (hook fires once per LLM step;
        # earlier ToolMessages remain in messages list and would be
        # re-walked) must not duplicate.
        hook = _hook()
        existing = [{
            "iteration": 1, "timestamp": "2026-05-25T00:00:00Z",
            "tool_call_id": "tc-1", "tool_name": "kubectl",
            "metrics": {"RestartCount": "7"},
        }]
        msgs = [_tool_with_metrics("tc-1", {"RestartCount": "7"})]
        state = {
            "verifier_loop_count": 2,
            "metric_observations": existing,
        }

        update = hook._build_observation_update(msgs, state)
        # Nothing new → return {} so caller doesn't bump state version.
        assert update == {}

    def test_empty_extracted_metrics_skipped(self):
        # ToolMessage with extracted_metrics={} (parser found nothing)
        # must NOT pollute the timeline with empty rows.
        hook = _hook()
        msgs = [_tool_with_metrics("tc-1", {})]
        state = {"verifier_loop_count": 1}

        assert hook._build_observation_update(msgs, state) == {}

    def test_no_extracted_metrics_key_skipped(self):
        # ToolMessage that hasn't been through Phase 2A yet (no
        # additional_kwargs flag) — must not crash, just skip.
        hook = _hook()
        tm = ToolMessage(content="x", tool_call_id="tc-1", name="kubectl")
        # explicitly no additional_kwargs set beyond default
        state = {"verifier_loop_count": 1}

        assert hook._build_observation_update([tm], state) == {}

    def test_iteration_is_sum_of_loop_counters(self):
        # Single phase (inject) with only agent_loop_count set →
        # iteration = agent_loop_count.
        hook = _hook()
        msgs = [_tool_with_metrics("tc-1", {"Disk usage (overlay)": "26%"})]
        state = {"agent_loop_count": 5}  # verifier_loop_count missing

        update = hook._build_observation_update(msgs, state)
        assert update["metric_observations"][0]["iteration"] == 5

    def test_iteration_monotonic_across_phases(self):
        # Phase 1 (inject): agent=10, verifier=0 → iteration=10.
        # Phase 2 (verify): agent=10 (frozen), verifier=3 → iteration=13.
        # The sum guarantees monotonicity even when the verifier loop
        # restarts at 0 — replaces the previous OR fallback which
        # could regress from 10 to 3 at the phase boundary.
        hook = _hook()

        msgs1 = [_tool_with_metrics("tc-1", {"RestartCount": "7"})]
        state1 = {"agent_loop_count": 10, "verifier_loop_count": 0}
        update1 = hook._build_observation_update(msgs1, state1)
        iter1 = update1["metric_observations"][0]["iteration"]

        msgs2 = [_tool_with_metrics("tc-2", {"RestartCount": "8"})]
        state2 = {
            "agent_loop_count": 10,  # frozen — execution done
            "verifier_loop_count": 3,
            "metric_observations": update1["metric_observations"],
        }
        update2 = hook._build_observation_update(msgs2, state2)
        iter2 = update2["metric_observations"][-1]["iteration"]

        assert iter1 == 10
        assert iter2 == 13
        assert iter2 > iter1, "iteration must be monotonic across phases"

    def test_multiple_new_observations_in_one_pass(self):
        hook = _hook()
        msgs = [
            _tool_with_metrics("tc-1", {"RestartCount": "7"}),
            _tool_with_metrics("tc-2", {"Disk usage (overlay)": "26%"}),
            _tool_with_metrics("tc-3", {"CPU usage": "100m"}),
        ]
        state = {"verifier_loop_count": 3}

        update = hook._build_observation_update(msgs, state)
        obs_list = update["metric_observations"]
        assert len(obs_list) == 3
        assert [o["tool_call_id"] for o in obs_list] == ["tc-1", "tc-2", "tc-3"]
        # All entries share the SAME timestamp (built once per hook
        # call) — guarantees ordering by tool_call_id within a turn
        # rather than wall-clock noise.
        ts_set = {o["timestamp"] for o in obs_list}
        assert len(ts_set) == 1

    def test_partial_new_obs_with_partial_existing(self):
        # Two existing observations, three messages — one of the three
        # has a matching id (skip), two are new (append). Final list
        # length is 4.
        hook = _hook()
        existing = [
            {"iteration": 1, "timestamp": "t0", "tool_call_id": "tc-1",
             "tool_name": "kubectl", "metrics": {"x": "1"}},
            {"iteration": 1, "timestamp": "t0", "tool_call_id": "tc-2",
             "tool_name": "kubectl", "metrics": {"y": "2"}},
        ]
        msgs = [
            _tool_with_metrics("tc-1", {"x": "1"}),  # duplicate → skip
            _tool_with_metrics("tc-3", {"z": "3"}),  # new
            _tool_with_metrics("tc-4", {"w": "4"}),  # new
        ]
        state = {
            "verifier_loop_count": 2,
            "metric_observations": existing,
        }

        update = hook._build_observation_update(msgs, state)
        obs_list = update["metric_observations"]
        assert len(obs_list) == 4
        assert [o["tool_call_id"] for o in obs_list] == [
            "tc-1", "tc-2", "tc-3", "tc-4",
        ]

    def test_non_tool_messages_ignored(self):
        # AIMessages / HumanMessages don't carry extracted_metrics.
        hook = _hook()
        msgs = [
            AIMessage(content="thinking"),
            _tool_with_metrics("tc-1", {"RestartCount": "7"}),
        ]
        state = {"verifier_loop_count": 1}

        update = hook._build_observation_update(msgs, state)
        # Only one observation, from the tool message.
        assert len(update["metric_observations"]) == 1


# ---------------------------------------------------------------------------
# State field declaration
# ---------------------------------------------------------------------------


class TestStateFieldDeclared:
    """The new state field must be declared on AgentState so LangGraph
    persists it across nodes."""

    def test_metric_observations_in_annotations(self):
        from chaos_agent.agent.state import AgentState
        annotations = AgentState.__annotations__
        assert "metric_observations" in annotations
