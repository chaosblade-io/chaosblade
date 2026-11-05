"""D17 — hook-side value-fingerprint harvest and provenance isolation.

The fingerprint harvest rides on ``_extract_tool_metrics`` and MUST obey the
same provenance gate as ``extracted_metrics``: only cluster-observation tools
(``_OBSERVATION_TOOLS``) produce independent facts. An agent-internal tool
(e.g. ``blade_create``) carries the LLM's own narrative as args; harvesting
its numbers would seed the cross-check value space from the thing it is
supposed to check (self-referential pollution). ``_build_fingerprint_update``
then promotes per-observation fingerprints into ``state.evidence_fingerprints``
— DELIBERATELY INDEPENDENT of ``_build_observation_update`` (a free-form exec
read yields ``extract_metrics() == {}`` so it never enters metric_observations,
yet its fingerprint is exactly what corroborates the netem-class claim).
"""
from __future__ import annotations

from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.memory.hook import (
    _OBSERVATION_TOOLS,
    PreReasoningHook,
    _extract_tool_metrics,
)


def _hook() -> PreReasoningHook:
    return PreReasoningHook(
        context_manager=None,
        tool_compactor=None,
        session_store=None,
        llm=None,
        tui_session_store=None,
    )


def _ai_tool_call(tool_call_id: str, name: str, args: dict) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": tool_call_id,
                     "type": "tool_call"}],
    )


def _tool(tool_call_id: str, content: str, name: str) -> ToolMessage:
    return ToolMessage(content=content, tool_call_id=tool_call_id, name=name)


_NETEM_CMD_ARGS = {
    "subcommand": "exec",
    "v_args": "drill-c3-dup-0 -n default -- tc -s qdisc show dev eth0",
}
_NETEM_OUT = (
    "qdisc netem 8001: dev eth0 root refcnt 2 limit 1000 duplicate 30%\n"
    " Sent 369 bytes 3 pkt (errored 0, dropped 10, overlimits 0, requeues 0)\n"
)


# ---------------------------------------------------------------------------
# _extract_tool_metrics — provenance gate on the fingerprint harvest
# ---------------------------------------------------------------------------


class TestFingerprintProvenance:
    def test_observation_tool_harvests_fingerprint(self):
        ai = _ai_tool_call("tc-1", "kubectl", _NETEM_CMD_ARGS)
        tm = _tool("tc-1", _NETEM_OUT, "kubectl")
        _extract_tool_metrics([ai, tm])
        fp = tm.additional_kwargs.get("evidence_fingerprint")
        assert isinstance(fp, dict)
        assert "369" in fp["tokens"]
        assert "tc -s qdisc" in fp["command"]
        assert fp["complete"] is True

    def test_non_observation_tool_harvests_no_fingerprint(self):
        # blade_create is NOT in _OBSERVATION_TOOLS → the provenance gate
        # `continue`s before the harvest, so no evidence_fingerprint is set
        # even though the content is full of numbers. No self-pollution.
        assert "blade_create" not in _OBSERVATION_TOOLS
        ai = _ai_tool_call("tc-1", "blade_create", {"x": "1"})
        tm = _tool("tc-1", "Created experiment 369 with uid 416 dropped 10",
                   "blade_create")
        _extract_tool_metrics([ai, tm])
        assert "evidence_fingerprint" not in tm.additional_kwargs
        # The extracted_metrics flag is still set to {} (never re-walked).
        assert tm.additional_kwargs.get("extracted_metrics") == {}

    def test_harvest_independent_of_empty_extract_metrics(self):
        # The whole D17 point: extract_metrics returns {} for netem output, yet
        # the fingerprint is STILL harvested (not gated on non-empty metrics).
        ai = _ai_tool_call("tc-1", "kubectl", _NETEM_CMD_ARGS)
        tm = _tool("tc-1", _NETEM_OUT, "kubectl")
        _extract_tool_metrics([ai, tm])
        assert tm.additional_kwargs.get("extracted_metrics") == {}
        assert tm.additional_kwargs.get("evidence_fingerprint", {}).get("tokens")

    def test_idempotent_no_double_harvest(self):
        ai = _ai_tool_call("tc-1", "kubectl", _NETEM_CMD_ARGS)
        tm = _tool("tc-1", _NETEM_OUT, "kubectl")
        _extract_tool_metrics([ai, tm])
        first = tm.additional_kwargs["evidence_fingerprint"]
        _extract_tool_metrics([ai, tm])  # second pass must skip
        assert tm.additional_kwargs["evidence_fingerprint"] is first


class TestHostInjectObservationHarvest:
    """cfd89b32 方向A cascade completion — host_inject is host_read's SUPERSET:
    its read-only channel is the ONLY in-window observation channel for a host
    drill (host_read is not bound to the EXECUTE phase). Its output MUST be
    harvested like host_read's, or D17 T3 anchors a self-recovering fault's
    in-window value to the post-recovery re-read and false-flags the genuinely
    observed number as a fabrication (procload inject-9cc36680: in-window
    ``pgrep -c stress-ng``→206 was invisible, so a correct ``verified`` was
    downgraded to ``partial``). The fault-command channel (``blade`` /
    ``stress-ng --fork``) is a MUTATION whose receipt is not an observation and
    stays excluded — same provenance isolation as ``blade_create``.
    """

    def test_readonly_channel_harvests_fingerprint(self):
        # The in-window observation D17 was blind to before the fix.
        ai = _ai_tool_call("tc-1", "host_inject",
                           {"command": "pgrep -c stress-ng", "task_id": "inject-x"})
        tm = _tool("tc-1", "206", "host_inject")
        _extract_tool_metrics([ai, tm])
        fp = tm.additional_kwargs.get("evidence_fingerprint")
        assert isinstance(fp, dict)
        assert "206" in fp["tokens"]
        assert fp["tool_name"] == "host_inject"

    def test_fault_command_channel_harvests_no_fingerprint(self):
        # Channel 1 (mutation) is NOT an observation — its numbers must not seed
        # the corpus, exactly like blade_create.
        ai = _ai_tool_call("tc-1", "host_inject",
                           {"command": "setsid stress-ng --fork 150 --timeout 180s",
                            "task_id": "inject-x"})
        tm = _tool("tc-1", "206 369 416", "host_inject")
        _extract_tool_metrics([ai, tm])
        assert "evidence_fingerprint" not in tm.additional_kwargs
        assert tm.additional_kwargs.get("extracted_metrics") == {}

    def test_readonly_gate_judges_command_arg_not_value_join(self):
        # The classifier runs on args['command'] (via _parent_tool_arg), NOT the
        # value-joined command+task_id _command_from_parent produces — a
        # read-only probe stays read-only regardless of the routing task_id.
        ai = _ai_tool_call("tc-1", "host_inject",
                           {"command": "cat /proc/loadavg", "task_id": "inject-9cc"})
        tm = _tool("tc-1", "139.46 41.25 15.00 136/673", "host_inject")
        _extract_tool_metrics([ai, tm])
        fp = tm.additional_kwargs.get("evidence_fingerprint")
        assert isinstance(fp, dict)
        assert "139.46" in fp["tokens"]

    def test_readonly_fingerprint_promotes_to_state(self):
        # End-to-end at the state seam: the harvested host_inject fingerprint is
        # promoted into evidence_fingerprints (the corpus cross_check reads).
        ai = _ai_tool_call("tc-1", "host_inject",
                           {"command": "pgrep -c stress-ng", "task_id": "inject-x"})
        tm = _tool("tc-1", "206", "host_inject")
        _extract_tool_metrics([ai, tm])
        update = _hook()._build_fingerprint_update([ai, tm], {})
        assert "tc-1" in update["evidence_fingerprints"]
        assert "206" in update["evidence_fingerprints"]["tc-1"]["tokens"]


# ---------------------------------------------------------------------------
# _build_fingerprint_update — promotion into state.evidence_fingerprints
# ---------------------------------------------------------------------------


class TestBuildFingerprintUpdate:
    def _processed(self, tool_call_id: str, content: str, name: str = "kubectl",
                   args: dict | None = None) -> tuple[AIMessage, ToolMessage]:
        ai = _ai_tool_call(tool_call_id, name, args or _NETEM_CMD_ARGS)
        tm = _tool(tool_call_id, content, name)
        _extract_tool_metrics([ai, tm])
        return ai, tm

    def test_promotes_new_fingerprint(self):
        ai, tm = self._processed("tc-1", _NETEM_OUT)
        update = _hook()._build_fingerprint_update([ai, tm], {})
        assert "evidence_fingerprints" in update
        fps = update["evidence_fingerprints"]
        assert "tc-1" in fps
        assert "369" in fps["tc-1"]["tokens"]

    def test_skips_non_observation_tool(self):
        # blade_create carries no evidence_fingerprint → nothing to promote.
        ai, tm = self._processed("tc-1", "Sent 369 bytes", name="blade_create",
                                 args={"x": "1"})
        assert _hook()._build_fingerprint_update([ai, tm], {}) == {}

    def test_idempotent_returns_empty_when_all_seen(self):
        ai, tm = self._processed("tc-1", _NETEM_OUT)
        state = {"evidence_fingerprints": {"tc-1": {"tokens": ["369"],
                                                    "complete": True,
                                                    "tool_name": "kubectl",
                                                    "command": "x"}}}
        assert _hook()._build_fingerprint_update([ai, tm], state) == {}

    def test_merges_with_existing_full_view(self):
        # No reducer (last-write-wins) → the write must be the FULL merged view,
        # not just the delta.
        ai, tm = self._processed("tc-2", "Sent 416 bytes 5 pkt dropped 10")
        existing = {"tc-1": {"tokens": ["369"], "complete": True,
                             "tool_name": "kubectl", "command": "c1"}}
        update = _hook()._build_fingerprint_update(
            [ai, tm], {"evidence_fingerprints": existing},
        )
        fps = update["evidence_fingerprints"]
        assert set(fps) == {"tc-1", "tc-2"}
        assert fps["tc-1"] is existing["tc-1"]  # preserved
        assert "416" in fps["tc-2"]["tokens"]

    def test_independent_of_metric_observations(self):
        # netem yields extracted_metrics == {} so _build_observation_update
        # finds nothing, but the fingerprint update is non-empty — proving the
        # two accumulators are decoupled.
        ai, tm = self._processed("tc-1", _NETEM_OUT)
        hook = _hook()
        assert hook._build_observation_update([ai, tm], {}) == {}
        assert hook._build_fingerprint_update([ai, tm], {}) != {}

    def test_no_tool_messages_returns_empty(self):
        assert _hook()._build_fingerprint_update([], {}) == {}
