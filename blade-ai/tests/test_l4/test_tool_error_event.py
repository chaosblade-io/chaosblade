"""A tool that raises must still close its card on the platform timeline.

Observed on the platform front-end: ``update_progress`` showed 正在调用 with no
result. It returns ``Command(update=...)`` but that is not the cause — a probe
confirmed Command tools emit ``on_tool_start``/``on_tool_end`` normally with a
shared ``run_id``. The real cause: the model passed ``state_update`` /
``log_append`` as JSON strings while the signature wants ``dict`` / ``list``, so
Pydantic raised a ``ValidationError`` and LangChain fired ``on_tool_error`` —
NOT ``on_tool_end``.

``_normalize_langgraph_event`` handled ``on_tool_end`` but not ``on_tool_error``,
so this channel emitted a ``tool_start`` with no terminal event. The platform
timeline pairs 正在调用/完成 by ``call_id`` (= ``run_id``); with no matching
``tool_end`` the card stays 正在调用 forever. The TUI path (``streaming.py``)
already synthesised a terminal event here; this fix brings the platform channel
to parity.
"""

from __future__ import annotations

from chaos_agent.l4.events import _normalize_langgraph_event

_RID = "run-abc-123"


def test_tool_error_becomes_a_paired_tool_end():
    """The synthesised terminal event must reuse the start's run_id."""
    start = _normalize_langgraph_event({
        "event": "on_tool_start", "name": "update_progress", "run_id": _RID,
        "data": {"input": {"state_update": "{...}"}}, "metadata": {},
    })
    err = _normalize_langgraph_event({
        "event": "on_tool_error", "name": "update_progress", "run_id": _RID,
        "data": {"error": "4 validation errors: state_update Input should be a "
                           "valid dictionary"},
        "metadata": {},
    })

    assert [e["kind"] for e in start] == ["tool_start"]
    assert start[0]["call_id"] == _RID

    assert err, "on_tool_error produced no event — the card would hang"
    ev = err[0]
    assert ev["kind"] == "tool_end", (
        "must be a tool_end so the timeline can close the running card"
    )
    assert ev["call_id"] == _RID, (
        "must reuse the start's run_id, or the pairing misses and the card hangs"
    )


def test_tool_error_is_flagged_and_carries_the_reason():
    err = _normalize_langgraph_event({
        "event": "on_tool_error", "name": "update_progress", "run_id": _RID,
        "data": {"error": "state_update Input should be a valid dictionary"},
        "metadata": {},
    })[0]
    assert err["is_error"] is True
    assert err["level"] == "error"
    assert "valid dictionary" in err["output"]
    # display name path is shared with on_tool_end
    assert "update_progress" in err["message"]


def test_tool_error_output_is_capped():
    """A verbose pydantic error must not bloat the wire frame — and since
    truncation-debt-cleanup (5.3) it speaks the shared dialect: a both-ends
    preview (exception chains name the entrypoint at the head, the
    exception at the tail) with a quantified elision marker, total budget
    2000 unchanged."""
    err = _normalize_langgraph_event({
        "event": "on_tool_error", "name": "t", "run_id": _RID,
        "data": {"error": "HEAD " + "x" * 4980 + " TAIL"}, "metadata": {},
    })[0]
    out = err["output"]
    # Budget ceiling: head 1500 + marker + tail 500 ≈ 2030 — bounded by
    # 2000 + marker width (~30 chars), not the old 2000 + 14.
    assert len(out) <= 2000 + 64
    # Both ends survive; the old head-only "...(truncated)" dialect is gone.
    assert out.startswith("HEAD")
    assert out.endswith("TAIL")
    assert "chars elided" in out
    assert "...(truncated)" not in out


def test_tool_end_output_is_capped_both_ends():
    """The on_tool_end path shares the same dialect: table headers at the
    head, the verdict at the tail — a head-only cut bets on output shape
    (#31 morphology: it once cut "Error from server (NotFound)" and kept
    only the warning banner)."""
    end = _normalize_langgraph_event({
        "event": "on_tool_end", "name": "kubectl", "run_id": _RID,
        "data": {"output": "HEADER " + "x" * 4980 + " VERDICT"}, "metadata": {},
    })[0]
    out = end["output"]
    assert len(out) <= 2000 + 64
    assert out.startswith("HEADER")
    assert out.endswith("VERDICT")
    assert "chars elided" in out
    assert "...(truncated)" not in out


def test_missing_error_payload_still_closes_the_card():
    """Even with no error detail, the terminal event must be emitted."""
    err = _normalize_langgraph_event({
        "event": "on_tool_error", "name": "t", "run_id": _RID,
        "data": {}, "metadata": {},
    })
    assert err and err[0]["kind"] == "tool_end"
    assert err[0]["call_id"] == _RID
    assert err[0]["output"]  # a non-empty fallback string


def test_successful_tool_end_is_unchanged():
    """The pre-existing on_tool_end path must keep its shape."""
    ok = _normalize_langgraph_event({
        "event": "on_tool_end", "name": "kubectl", "run_id": _RID,
        "data": {"output": "node listing"}, "metadata": {},
    })[0]
    assert ok["kind"] == "tool_end"
    assert ok["level"] == "ok"
    assert ok.get("is_error") is None  # success path never sets the flag
    assert ok["call_id"] == _RID


def test_tool_events_carry_the_generic_tool_key():
    """l4-contract-faithfulness: tool_start / tool_end / tool_error payloads
    identify the tool under the single generic ``tool`` key — the key
    external consumers (resiliencebenchmark's _normalize_bladeai_event)
    read via payload.get("tool"). The retired ``tool_name`` key must not
    appear (no dual emission).

    Note the channel boundary: the TUI/server conversation stream
    (agent/streaming.py StreamEvent) keeps its own ``tool_name`` field —
    that is a separate protocol, untouched by this rename.
    """
    start = _normalize_langgraph_event({
        "event": "on_tool_start", "name": "blade_create", "run_id": _RID,
        "data": {"input": {"scope": "pod"}}, "metadata": {},
    })[0]
    assert start["tool"] == "blade_create"
    assert "tool_name" not in start

    end = _normalize_langgraph_event({
        "event": "on_tool_end", "name": "blade_create", "run_id": _RID,
        "data": {"output": "created"}, "metadata": {},
    })[0]
    assert end["tool"] == "blade_create"
    assert "tool_name" not in end

    err = _normalize_langgraph_event({
        "event": "on_tool_error", "name": "blade_create", "run_id": _RID,
        "data": {"error": "boom"}, "metadata": {},
    })[0]
    assert err["tool"] == "blade_create"
    assert "tool_name" not in err
