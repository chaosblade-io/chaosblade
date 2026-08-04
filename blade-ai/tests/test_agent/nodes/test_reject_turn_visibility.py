"""Node-authored rejections must reach all three of their audiences.

A rejection built inside ``intent_clarification`` — not by an LLM call — used to
return only an ``AIMessage`` appended to graph state. That reaches the platform
(which reads it out as ``summary``) but nobody else:

- The TUI renders only ``on_chat_model_stream`` / ``on_tool_*`` /
  ``on_custom_event``. A message merely added to state emits no event, so a real
  session ended with ``submit_fault_intent`` marked ✓, no intent card, no reason
  shown, and ``status: aborted`` — from the user's side the turn just stopped.
- The session transcript missed it too: every terminal path calls
  ``_persist_dialogue``, but the early returns did not, so the rejection could
  not be found afterwards either.

``_reject_turn`` closes both gaps while keeping the ``AIMessage``. Two of the
five call sites predate the channel gate (submission/plan mismatch) and had the
same defect.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from unittest.mock import patch

import pytest

from chaos_agent.agent.nodes.planning import intent_clarification as ic


async def _run_reject(**over):
    kwargs = dict(
        messages=[],
        human_msg=None,
        dialogue_round=3,
        tui_session_id="sess-1",
        hook_updates={},
    )
    kwargs.update(over)
    return await ic._reject_turn("channel mismatch: host vs k8s", **kwargs)


async def test_rejection_reaches_tui_session_and_state():
    """All three outlets fire from a single call."""
    dispatched: list[tuple[str, str]] = []
    persisted: list[tuple[str, list]] = []

    async def fake_dispatch(node, content):
        dispatched.append((node, content))

    def fake_persist(sid, lst):
        persisted.append((sid, list(lst)))

    with patch.object(ic, "dispatch_node_message", fake_dispatch), \
            patch.object(ic, "_persist_dialogue", fake_persist):
        out = await _run_reject()

    # 1) TUI
    assert dispatched == [("intent_clarification", "channel mismatch: host vs k8s")]
    # 2) session transcript — the reason itself, not merely that persistence ran.
    #    Asserting only the call is what let the defect through: ``_reject_turn``
    #    did call ``_persist_dialogue``, but without ``response=`` the built list
    #    held just the human turn and trailing ToolMessages, so a real drill's
    #    transcript showed submit succeeding and jumped to the next round with no
    #    reason on disk.
    assert persisted and persisted[0][0] == "sess-1"
    assert any(
        "channel mismatch: host vs k8s" in str(getattr(m, "content", ""))
        for m in persisted[0][1]
    ), "the rejection reason must be in the persisted list, not just the call"
    # 3) platform summary + next-turn model context
    assert len(out["messages"]) == 1
    assert out["messages"][0].content == "channel mismatch: host vs k8s"
    assert out["dialogue_round"] == 4
    # One object for both outlets: two would differ in id, and the session store
    # dedups by id, so re-persisting the same turn would append a duplicate.
    assert persisted[0][1][-1] is out["messages"][0]


async def test_rejection_never_promotes_the_turn_to_inject():
    """A rejection must not set ``confirmed_intent`` — that would run the drill."""
    with patch.object(ic, "dispatch_node_message"), \
            patch.object(ic, "_persist_dialogue"):
        out = await _run_reject()
    assert "confirmed_intent" not in out


async def test_side_effects_cannot_break_the_rejection():
    """Display and persistence are cosmetic; the verdict must survive both failing.

    ``dispatch_node_message`` itself only catches ``RuntimeError`` (missing run
    context), so the broader guard has to live in ``_reject_turn``.
    """
    async def boom_dispatch(node, content):
        raise RuntimeError("no run context")

    def boom_persist(sid, lst):
        raise OSError("disk full")

    with patch.object(ic, "dispatch_node_message", boom_dispatch), \
            patch.object(ic, "_persist_dialogue", boom_persist):
        out = await _run_reject()

    assert out["messages"][0].content == "channel mismatch: host vs k8s"
    assert out["dialogue_round"] == 4


async def test_works_without_a_run_context():
    """Called directly (no LangGraph run) the real dispatch must stay silent."""
    with patch.object(ic, "_persist_dialogue"):
        out = await _run_reject()          # real dispatch_node_message
    assert len(out["messages"]) == 1


def test_every_rejection_path_uses_the_helper():
    """No early return may hand back a bare ``AIMessage`` rejection again.

    Scoped to returns whose ``messages`` is a literal ``[AIMessage(content=...)]``
    — that is the node-authored shape which emitted no TUI event and skipped the
    transcript. LLM-reply paths in the same function also return
    ``messages`` + ``dialogue_round``, but they carry a streamed ``response``
    object (already visible as tokens) and each persists on its own, so keying on
    the dict shape alone would flag them wrongly.
    """
    src = textwrap.dedent(inspect.getsource(ic.make_intent_clarification))
    tree = ast.parse(src)

    def _is_ai_message_literal(value) -> bool:
        return (
            isinstance(value, ast.List)
            and len(value.elts) == 1
            and isinstance(value.elts[0], ast.Call)
            and getattr(value.elts[0].func, "id", None) == "AIMessage"
        )

    bare = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (
                isinstance(key, ast.Constant)
                and key.value == "messages"
                and _is_ai_message_literal(value)
            ):
                bare.append(key.lineno)

    # The goodbye and generic-error returns are not rejections of a submission:
    # they end the conversation rather than asking the user to fix and retry, and
    # each already handles its own persistence.
    assert len(bare) <= 2, (
        f"found {len(bare)} node-authored AIMessage returns at lines {bare}; "
        "submission rejections must go through _reject_turn"
    )


def test_helper_is_the_single_source_for_all_five_sites():
    src = textwrap.dedent(inspect.getsource(ic.make_intent_clarification))
    assert src.count("await _reject_turn(") == 5, (
        "expected all five rejection paths (2 pre-existing mismatch checks, "
        "the single-fault gate, the batch mismatch check and the batch gate) "
        "to share the helper"
    )


def test_helper_emits_a_node_message_not_a_token():
    """``node_message`` is the channel for non-LLM text; ``token`` breaks the TUI.

    Recorded in ``streaming.py``: when this content was emitted as ``token`` the
    TUI treated it as accumulating LLM text, every message ended with ``\\n\\n``
    so the mid-stream split never fired, and a whole run piled into one growing
    pending item — visible flicker on each Ink re-render.
    """
    src = inspect.getsource(ic._reject_turn)
    assert "dispatch_node_message" in src
    assert "adispatch_custom_event" not in src, (
        "call the dispatch helper, not the raw event API — the helper carries "
        "the missing-run-context guard"
    )
