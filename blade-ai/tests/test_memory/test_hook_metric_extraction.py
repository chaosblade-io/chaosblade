"""Tests for E2 Phase 2A — metric extraction in PreReasoningHook.

The extraction step runs BEFORE ``tool_compactor.compact()`` and must:
  1. Set ``additional_kwargs['extracted_metrics']`` on each new
     ToolMessage (even when extractor finds nothing → set to ``{}``).
  2. Prepend a ``[Auto-extracted: …]`` summary to the content HEAD
     (so the summary survives the 1KB head-only truncation) — but
     only when at least one metric was extracted.
  3. Be idempotent: a second call must not re-prepend.
  4. Survive missing parent AIMessage / multi-modal content / empty
     content without raising.
"""
from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chaos_agent.memory.hook import (
    _AUTO_EXTRACTED_MARKER,
    _extract_tool_metrics,
    _find_tool_command,
    _format_metric_summary,
    _is_json_shaped,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ai_with_tool_call(tool_call_id: str, tool_name: str, args: dict) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{
            "name": tool_name,
            "args": args,
            "id": tool_call_id,
            "type": "tool_call",
        }],
    )


def _tool_msg(tool_call_id: str, content: str, name: str = "kubectl") -> ToolMessage:
    return ToolMessage(content=content, tool_call_id=tool_call_id, name=name)


# ---------------------------------------------------------------------------
# _find_tool_command — walking back to parent AIMessage
# ---------------------------------------------------------------------------


class TestFindToolCommand:
    def test_finds_parent_args(self):
        ai = _ai_with_tool_call("tc-1", "kubectl", {
            "subcommand": "exec",
            "v_args": "my-pod -n default -- df -h",
        })
        tm = _tool_msg("tc-1", "Filesystem 50G ...")
        cmd = _find_tool_command(tm, [ai, tm])
        # Args stringified — extractor's substring dispatch will find "df -h".
        assert "df -h" in cmd
        assert "exec" in cmd

    def test_get_pod_stays_contiguous(self):
        # Regression: ``kubectl(subcommand="get", v_args="pod my-pod -o json")``
        # must flatten to a string where ``"get pod"`` is contiguous.
        # A previous ``k=v k=v`` join broke this with ``" v_args="`` in
        # between, silently disabling the get-pod-json dispatch.
        ai = _ai_with_tool_call("tc-1", "kubectl", {
            "subcommand": "get",
            "v_args": "pod my-pod -o json",
        })
        tm = _tool_msg("tc-1", "{}")
        cmd = _find_tool_command(tm, [ai, tm])
        assert "get pod" in cmd
        assert "-o json" in cmd
        # And NO accidental field-name leakage that could derail
        # downstream parsers expecting kubectl-cli-shaped strings.
        assert "=" not in cmd

    def test_empty_values_dropped(self):
        # Defaults like ``kubeconfig=""`` shouldn't pollute the join
        # with bare spaces.
        ai = _ai_with_tool_call("tc-1", "kubectl", {
            "subcommand": "get",
            "v_args": "pods",
            "kubeconfig": "",  # default — should be filtered
            "context": None,    # also filtered
        })
        tm = _tool_msg("tc-1", "x")
        cmd = _find_tool_command(tm, [ai, tm])
        assert cmd == "get pods"

    def test_no_match_returns_empty(self):
        # ToolMessage referencing an id no AIMessage carries.
        tm = _tool_msg("orphan-id", "data")
        assert _find_tool_command(tm, [tm]) == ""

    def test_no_tool_call_id_returns_empty(self):
        # Defensive: malformed ToolMessage with empty tool_call_id.
        tm = ToolMessage(content="x", tool_call_id="", name="kubectl")
        ai = _ai_with_tool_call("tc-1", "kubectl", {"cmd": "df -h"})
        assert _find_tool_command(tm, [ai, tm]) == ""


# ---------------------------------------------------------------------------
# _format_metric_summary — wire format Phase 3 will parse
# ---------------------------------------------------------------------------


class TestFormatMetricSummary:
    def test_pipe_separated_bracketed(self):
        s = _format_metric_summary({"RestartCount": "8", "Pod Ready": "True"})
        assert s.startswith(_AUTO_EXTRACTED_MARKER)
        assert s.endswith("]\n")
        # Order preserved — dict insertion order matches test fixture.
        assert "RestartCount=8" in s
        assert "Pod Ready=True" in s
        # Single line — no embedded newlines that could break truncation
        # head retention.
        assert "\n" not in s[:-1]


# ---------------------------------------------------------------------------
# _extract_tool_metrics — the main hook step
# ---------------------------------------------------------------------------


class TestExtractToolMetrics:
    def test_populates_additional_kwargs_and_prepends_summary(self):
        ai = _ai_with_tool_call("tc-1", "kubectl", {
            "subcommand": "exec",
            "v_args": "my-pod -- df -h",
        })
        tm = _tool_msg("tc-1",
            "Filesystem      Size  Used Avail Use% Mounted on\n"
            "overlay          50G   13G   38G  26% /\n",
        )
        _extract_tool_metrics([ai, tm])

        assert tm.additional_kwargs["extracted_metrics"] == {
            "Disk usage (overlay)": "26% (13G/50G)",
        }
        assert tm.content.startswith(_AUTO_EXTRACTED_MARKER)
        assert "Disk usage (overlay)=26% (13G/50G)" in tm.content
        # Original raw content STILL present after the summary.
        assert "Filesystem" in tm.content

    def test_idempotent_on_repeated_call(self):
        ai = _ai_with_tool_call("tc-1", "kubectl", {"v_args": "exec x -- df -h"})
        tm = _tool_msg("tc-1", "overlay  50G  13G  38G  26% /\n")
        _extract_tool_metrics([ai, tm])
        content_after_first = tm.content
        _extract_tool_metrics([ai, tm])
        # Second call must not re-prepend — content unchanged.
        assert tm.content == content_after_first
        # additional_kwargs flag still set (didn't get clobbered).
        assert "extracted_metrics" in tm.additional_kwargs

    def test_no_match_still_sets_flag_no_prepend(self):
        # Command extractor doesn't recognise → empty dict.
        # Flag is still set so we don't re-walk every turn, but content
        # stays clean (no useless "[Auto-extracted: ]" noise).
        ai = _ai_with_tool_call("tc-1", "kubectl", {
            "subcommand": "version", "v_args": "--client",
        })
        original_content = "Client Version: v1.30.0\n"
        tm = _tool_msg("tc-1", original_content)
        _extract_tool_metrics([ai, tm])
        assert tm.additional_kwargs["extracted_metrics"] == {}
        assert tm.content == original_content

    def test_empty_content_marked_processed(self):
        # Opt #4: empty ToolMessage content gets the flag set to {} so
        # subsequent hook calls don't re-walk it. ToolMessages are
        # atomic — by the time they're in state, content is final.
        # Mid-stream content drips through AIMessageChunk, not
        # ToolMessage, so there's no late-arriving content to wait for.
        ai = _ai_with_tool_call("tc-1", "kubectl", {"v_args": "exec x -- df -h"})
        tm = _tool_msg("tc-1", "")
        _extract_tool_metrics([ai, tm])
        assert tm.additional_kwargs.get("extracted_metrics") == {}
        # Content unchanged — no useless prepend.
        assert tm.content == ""

    def test_non_string_content_marked_processed(self):
        # Multi-modal ToolMessage (list content) is out of scope for
        # text parsers. Opt #4: still mark the flag so we don't
        # re-walk every turn.
        ai = _ai_with_tool_call("tc-1", "kubectl", {"v_args": "describe pod x"})
        tm = ToolMessage(
            content=[{"type": "text", "text": "Restart Count: 8\n"}],
            tool_call_id="tc-1",
            name="kubectl",
        )
        _extract_tool_metrics([ai, tm])
        assert tm.additional_kwargs.get("extracted_metrics") == {}
        # Content unchanged — list content is not text-parser-eligible
        # and we don't transform it.
        assert isinstance(tm.content, list)

    def test_double_prepended_marker_marks_as_processed(self):
        # Content that already carries the marker from a previous session
        # replay — must NOT double-prepend, and must mark as processed
        # so subsequent calls don't re-walk.
        ai = _ai_with_tool_call("tc-1", "kubectl", {"v_args": "exec x -- df -h"})
        pre_extracted = (
            "[Auto-extracted: Disk usage (overlay)=26% (13G/50G)]\n"
            "Filesystem      Size  Used Avail Use% Mounted on\n"
            "overlay          50G   13G   38G  26% /\n"
        )
        tm = _tool_msg("tc-1", pre_extracted)
        _extract_tool_metrics([ai, tm])
        assert tm.content == pre_extracted  # unchanged
        assert tm.additional_kwargs["extracted_metrics"] == {}

    def test_multiple_tool_messages_each_processed(self):
        ai1 = _ai_with_tool_call("tc-1", "kubectl", {"v_args": "describe pod x"})
        tm1 = _tool_msg("tc-1", "Restart Count: 7\nReady             True\n")
        ai2 = _ai_with_tool_call("tc-2", "kubectl", {"v_args": "exec x -- df -h"})
        tm2 = _tool_msg("tc-2",
            "Filesystem      Size  Used Avail Use% Mounted on\n"
            "overlay          50G   20G   30G  40% /\n",
        )
        _extract_tool_metrics([ai1, tm1, ai2, tm2])

        assert tm1.additional_kwargs["extracted_metrics"]["RestartCount"] == "7"
        assert tm1.content.startswith(_AUTO_EXTRACTED_MARKER)
        assert tm2.additional_kwargs["extracted_metrics"][
            "Disk usage (overlay)"
        ] == "40% (20G/50G)"

    def test_skips_non_tool_messages(self):
        hm = HumanMessage(content="hello")
        ai = AIMessage(content="hi there")
        _extract_tool_metrics([hm, ai])
        # No mutation, no exceptions.
        assert hm.content == "hello"
        assert ai.content == "hi there"

    def test_json_content_not_prepended_smart_strip_intact(self):
        """Regression for Bug A — kubectl get -o json output must NOT
        get a non-JSON prefix prepended, or ``tool_compactor``'s
        ``smart_strip_k8s_json`` fails its ``json.loads`` and falls
        back to dumb head-cut, losing the intelligent K8s field
        stripping (and inflating LLM context instead of reducing it)."""
        import json as _json
        from chaos_agent.memory.tool_compactor import smart_strip_k8s_json

        pod_list = {
            "kind": "PodList",
            "items": [{
                "kind": "Pod",
                "metadata": {"name": "my-pod"},
                "status": {
                    "containerStatuses": [{"restartCount": 5, "ready": True}],
                    "phase": "Running",
                },
            }],
        }
        raw_json = _json.dumps(pod_list)
        ai = _ai_with_tool_call("tc-1", "kubectl", {
            "subcommand": "get",
            "v_args": "pod my-pod -o json",
        })
        tm = _tool_msg("tc-1", raw_json)
        _extract_tool_metrics([ai, tm])

        # Metrics MUST still be captured in additional_kwargs even
        # though the head wasn't prepended.
        assert tm.additional_kwargs["extracted_metrics"]["RestartCount"] == "5"
        # Content must remain valid JSON — no [Auto-extracted: ...] prefix.
        assert not tm.content.startswith(_AUTO_EXTRACTED_MARKER)
        # And smart_strip can still parse + reduce it.
        stripped = smart_strip_k8s_json(tm.content, 4096)
        assert stripped is not None, (
            "smart_strip_k8s_json failed — Bug A regression: a non-JSON "
            "prefix snuck onto a JSON-shaped tool result and broke the "
            "intelligent K8s field stripping path."
        )

    def test_jsonarray_content_also_not_prepended(self):
        # Some kubectl outputs are top-level arrays (e.g. ``-o
        # jsonpath`` with array fields). The JSON detector should
        # accept both ``{`` and ``[`` roots.
        ai = _ai_with_tool_call("tc-1", "kubectl", {"v_args": "describe pod x"})
        raw_array = '[{"name": "pod-1"}, {"name": "pod-2"}]'
        tm = _tool_msg("tc-1", raw_array)
        _extract_tool_metrics([ai, tm])
        # `describe pod` extractor doesn't fire on JSON array; metrics={}.
        # The point is that the JSON-shape sniff prevents an accidental
        # prepend even when extractor DID match something.
        assert not tm.content.startswith(_AUTO_EXTRACTED_MARKER)

    def test_is_json_shaped_sniff(self):
        assert _is_json_shaped('{"foo": 1}')
        assert _is_json_shaped('  \n  {"foo": 1}')  # leading whitespace
        assert _is_json_shaped('[1, 2, 3]')
        assert not _is_json_shaped('Restart Count: 8\n')
        assert not _is_json_shaped('')
        assert not _is_json_shaped('overlay 50G ...')

    def test_survives_truncation_simulation(self):
        # The whole point of running BEFORE truncation: head-only
        # ``truncate_text`` (1KB) must not lose the summary. Simulate
        # with a 5KB raw output that gets head-truncated to 1KB.
        ai = _ai_with_tool_call("tc-1", "kubectl", {"v_args": "describe pod x"})
        raw = (
            "Restart Count: 8\n"
            "Ready             True\n"
            + "padding line\n" * 500  # ~5KB of filler
        )
        tm = _tool_msg("tc-1", raw)
        _extract_tool_metrics([ai, tm])

        # Now mimic ``tool_compactor.truncate_text(text, 1024)``:
        truncated = tm.content.encode("utf-8")[:1024].decode(
            "utf-8", errors="replace",
        )
        # Even after 1KB head-only truncation, the summary line is
        # still in the visible window for the LLM.
        assert _AUTO_EXTRACTED_MARKER in truncated
        assert "RestartCount=8" in truncated
