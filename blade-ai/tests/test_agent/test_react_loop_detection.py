"""ReAct loop-detection guards, shaped by the real task-c758cdbdb incident.

Both detectors were structurally blind there and never fired across 89 tool
calls, because their windows/guards did not match the real message shape:

* ``detect_repeated_tool_calls`` counted a window of MESSAGES (10) while the
  repeats sat 5-8 messages apart, so the window could never hold ``threshold``
  occurrences.
* ``detect_action_stagnation`` aborted on any turn with != 1 tool call, and the
  stalled turns each carried 3 calls.

The false-positive tests matter at least as much as the true positives: telling
a model that is reasoning correctly "you are looping" pushes it to discard a
valid conclusion. Across the drill history, repeats whose OUTPUT was changing
(legitimate polling) outnumbered genuine stalls 5:1.
"""

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from chaos_agent.agent.nodes.execute.react_helpers import (
    detect_action_stagnation,
    detect_repeated_tool_calls,
)
from chaos_agent.config.settings import settings

NODE = "cn-shanghai-cloudspe.25.209.71.165"


def _incident_turn(idx: int, taints: str, label: str, sts: str) -> list:
    """One turn in the shape task-c758cdbdb actually produced.

    1 AIMessage carrying 3 batched reads + 3 ToolMessages + the per-turn
    ``[Memory Compression]`` system message → repeats land 5 messages apart.
    """
    return [
        AIMessage(content="继续执行计划步骤。", tool_calls=[
            {"name": "kubectl", "args": {"subcommand": "get", "v_args": f"node {NODE} -o jsonpath={{.spec.taints}}"}, "id": f"a{idx}"},
            {"name": "kubectl", "args": {"subcommand": "get", "v_args": f"node {NODE} -o jsonpath={{.metadata.labels.ops-maintenance}}"}, "id": f"b{idx}"},
            {"name": "kubectl", "args": {"subcommand": "get", "v_args": "sts reg-center-0 -n reg-center -o jsonpath={.spec.template.spec.nodeSelector}"}, "id": f"c{idx}"},
        ]),
        ToolMessage(content=taints, tool_call_id=f"a{idx}"),
        ToolMessage(content=label, tool_call_id=f"b{idx}"),
        ToolMessage(content=sts, tool_call_id=f"c{idx}"),
        SystemMessage(content="[Memory Compression] Memory OK: 64 messages, ~12712 tokens"),
    ]


def _incident_history(turns: int, progressing: bool = False) -> list:
    msgs: list = []
    for i in range(turns):
        suffix = f" t={i}" if progressing else ""
        msgs.extend(_incident_turn(
            i,
            '[{"effect":"NoSchedule","key":"ops-maintenance","value":"scheduled"}]' + suffix,
            "" if not progressing else f"scheduled{i}",
            'Warning: short name "sts" could also match lower priority resource statefulsets.apps.kruise.io' + suffix,
        ))
    return msgs


class TestIncidentShapeIsDetected:
    """True positives: the exact shape that previously went unnoticed."""

    def test_repeated_calls_fire_on_incident_shape(self):
        hint = detect_repeated_tool_calls(_incident_history(4), phase="execute")
        assert hint is not None
        assert "LOOP DETECTED" in hint

    def test_message_count_window_alone_would_miss_it(self, monkeypatch):
        """Regression witness: disabling the turn window restores the blindness."""
        monkeypatch.setattr(settings, "loop_detection_turns", 0)
        monkeypatch.setattr(settings, "loop_detection_window", 10)
        assert detect_repeated_tool_calls(_incident_history(4), phase="execute") is None

    def test_stagnation_fires_on_batched_turns(self):
        """Batches must accumulate; the old != 1 guard aborted on the first one."""
        hint, tool = detect_action_stagnation(_incident_history(5), phase="execute", threshold=5)
        assert hint is not None
        assert tool == "kubectl:get"


class TestFalsePositiveTolerance:
    """Silence unless there is positive evidence of a stall."""

    def test_progressing_outputs_suppress_both_detectors(self):
        """Waiting for a Pod to go Pending is legitimate, not a loop."""
        history = _incident_history(5, progressing=True)
        assert detect_repeated_tool_calls(history, phase="execute") is None
        hint, tool = detect_action_stagnation(history, phase="execute", threshold=5)
        assert hint is None and tool is None

    def test_pod_status_transition_is_not_a_loop(self):
        msgs = []
        for i, status in enumerate(["Running", "Running", "Terminating", "Pending"]):
            msgs.append(AIMessage(content="", tool_calls=[
                {"name": "kubectl", "args": {"subcommand": "get", "v_args": "pods -n reg-center"}, "id": f"p{i}"},
            ]))
            msgs.append(ToolMessage(content=f"reg-center-0 {status}", tool_call_id=f"p{i}"))
        assert detect_repeated_tool_calls(msgs, phase="verify") is None
        hint, _ = detect_action_stagnation(msgs, phase="verify", threshold=3)
        assert hint is None

    def test_missing_tool_results_stay_silent(self):
        """Unpaired tool calls give no evidence either way → no warning."""
        msgs = []
        for i in range(6):
            msgs.append(AIMessage(content="", tool_calls=[
                {"name": "kubectl", "args": {"subcommand": "get", "v_args": "nodes"}, "id": f"x{i}"},
            ]))
        assert detect_repeated_tool_calls(msgs, phase="execute") is None
        hint, tool = detect_action_stagnation(msgs, phase="execute", threshold=3)
        assert hint is None and tool is None

    def test_read_write_read_cycle_is_not_stagnation(self):
        """get → patch → get → describe is normal progress, not thrashing."""
        msgs = []
        for i, sub in enumerate(["get", "patch", "get", "describe", "get"]):
            msgs.append(AIMessage(content="", tool_calls=[
                {"name": "kubectl", "args": {"subcommand": sub}, "id": f"rw{i}"},
            ]))
            msgs.append(ToolMessage(content="same output", tool_call_id=f"rw{i}"))
        hint, _ = detect_action_stagnation(msgs, phase="execute", threshold=3)
        assert hint is None

    def test_pure_text_turn_breaks_the_streak(self):
        msgs = []
        for i in range(3):
            msgs.append(AIMessage(content="", tool_calls=[
                {"name": "kubectl", "args": {"subcommand": "get"}, "id": f"s{i}"},
            ]))
            msgs.append(ToolMessage(content="same", tool_call_id=f"s{i}"))
        msgs.append(AIMessage(content="让我重新审视计划", tool_calls=[]))
        msgs.append(AIMessage(content="", tool_calls=[
            {"name": "kubectl", "args": {"subcommand": "get"}, "id": "s9"},
        ]))
        msgs.append(ToolMessage(content="same", tool_call_id="s9"))
        hint, _ = detect_action_stagnation(msgs, phase="execute", threshold=3)
        assert hint is None

    def test_only_tools_in_the_latest_turn_can_stagnate(self):
        """A tool dropped from the newest batch is no longer stagnating."""
        msgs = []
        for i in range(4):
            calls = [{"name": "kubectl", "args": {"subcommand": "get"}, "id": f"k{i}"}]
            if i < 3:
                calls.append({"name": "blade_status", "args": {}, "id": f"bs{i}"})
            msgs.append(AIMessage(content="", tool_calls=calls))
            msgs.append(ToolMessage(content="same", tool_call_id=f"k{i}"))
            if i < 3:
                msgs.append(ToolMessage(content="same", tool_call_id=f"bs{i}"))
        hint, tool = detect_action_stagnation(msgs, phase="execute", threshold=4)
        assert tool != "blade_status"


class TestHintIsRebuttable:
    """Detection can be wrong, so the hint must not be an order."""

    def _stalled(self) -> list:
        msgs = []
        for i in range(5):
            msgs.append(AIMessage(content="", tool_calls=[
                {"name": "kubectl", "args": {"subcommand": "get"}, "id": f"h{i}"},
            ]))
            msgs.append(ToolMessage(content="unchanged", tool_call_id=f"h{i}"))
        return msgs

    def test_no_hard_prohibition(self):
        hint, _ = detect_action_stagnation(self._stalled(), phase="execute", threshold=5)
        assert hint is not None
        assert "Do NOT call" not in hint

    def test_offers_a_way_to_justify_continuing(self):
        hint, _ = detect_action_stagnation(self._stalled(), phase="execute", threshold=5)
        assert "say why and continue" in hint

    def test_states_the_observation_not_a_verdict(self):
        hint, _ = detect_action_stagnation(self._stalled(), phase="execute", threshold=5)
        assert "results came back unchanged" in hint


class TestWindowBounds:
    def test_message_cap_still_applies(self, monkeypatch):
        """The cap is the tighter of the two bounds when set low."""
        monkeypatch.setattr(settings, "loop_detection_window", 4)
        monkeypatch.setattr(settings, "loop_detection_threshold", 3)
        msgs = []
        for i in range(3):
            msgs.append(AIMessage(content="", tool_calls=[
                {"name": "kubectl", "args": {"subcommand": "top", "v_args": "pods"}, "id": f"w{i}"},
            ]))
            msgs.append(ToolMessage(content="CPU: 3m", tool_call_id=f"w{i}"))
        assert detect_repeated_tool_calls(msgs) is None

    def test_turn_window_excludes_older_turns(self, monkeypatch):
        """Repeats spread beyond the turn window do not accumulate."""
        monkeypatch.setattr(settings, "loop_detection_turns", 2)
        monkeypatch.setattr(settings, "loop_detection_threshold", 3)
        msgs = []
        for i in range(4):
            msgs.append(AIMessage(content="", tool_calls=[
                {"name": "kubectl", "args": {"subcommand": "top", "v_args": "pods"}, "id": f"t{i}"},
            ]))
            msgs.append(ToolMessage(content="CPU: 3m", tool_call_id=f"t{i}"))
        assert detect_repeated_tool_calls(msgs) is None

    def test_non_ai_messages_do_not_consume_the_turn_budget(self):
        """Injected hints/system notes must not shrink the effective window."""
        msgs = []
        for i in range(3):
            msgs.append(AIMessage(content="", tool_calls=[
                {"name": "read_skill_resource", "args": {"resource_path": "catalogue/Node_维护"}, "id": f"r{i}"},
            ]))
            msgs.append(ToolMessage(content="skill body", tool_call_id=f"r{i}"))
            msgs.append(SystemMessage(content="[Memory Compression] Memory OK"))
            msgs.append(HumanMessage(content="[hint] keep going"))
        hint = detect_repeated_tool_calls(msgs, phase="planning")
        assert hint is not None
        assert "LOOP DETECTED" in hint
