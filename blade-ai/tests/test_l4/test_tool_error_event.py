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
    """A verbose pydantic error must not bloat the wire frame."""
    err = _normalize_langgraph_event({
        "event": "on_tool_error", "name": "t", "run_id": _RID,
        "data": {"error": "x" * 5000}, "metadata": {},
    })[0]
    assert len(err["output"]) <= 2000 + len("...(truncated)")
    assert err["output"].endswith("...(truncated)")


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
