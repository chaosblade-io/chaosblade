"""Tests for the PR-D4 locator allocator + /show / /copy / /rerun handlers.

Six orthogonal pieces of behavior are pinned so a regression in any one
of them surfaces as a focused failure:

1. Allocator hands out monotonic sequences per kind (E1, E2 / T1, T2)
   that don't interleave — readers should never have to compute "T#
   relative to E#".
2. ``get`` is case-insensitive (``e1`` matches ``E1``) so users typing
   either capitalisation hit.
3. Lookup misses return ``None`` instead of raising — ``/show E99``
   prints a helpful message instead of crashing the REPL.
4. The experiment card title renders ``[E#]`` in working/dense and
   suppresses it in calm; bookkeeping happens regardless of mode.
5. ``/show`` re-renders an experiment without allocating a *second*
   locator (would otherwise pollute the index).
6. ``/copy`` prints the payload as plain text; ``/rerun`` echoes the
   prior NL description rather than auto-executing.
"""

from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace

from rich.console import Console

from chaos_agent.tui.controllers.commands import CommandDispatcher
from chaos_agent.tui.locators import LocatorAllocator
from chaos_agent.tui.renderers import experiment_card
from chaos_agent.tui.state import DisplayMode, SessionState


# -------------------- Allocator unit tests ----------------------------


class TestAllocatorSequences:
    def test_experiment_counter_starts_at_one(self):
        a = LocatorAllocator()
        assert a.allocate_experiment() == "E1"
        assert a.allocate_experiment() == "E2"

    def test_tool_counter_independent_of_experiment(self):
        # Interleave E and T allocations; the counters must not cross-pollinate.
        a = LocatorAllocator()
        assert a.allocate_experiment() == "E1"
        assert a.allocate_tool() == "T1"
        assert a.allocate_experiment() == "E2"
        assert a.allocate_tool() == "T2"

    def test_payload_is_stored_at_allocation(self):
        a = LocatorAllocator()
        loc = a.allocate_experiment({"fault_intent": {"target": "pod"}})
        rec = a.get(loc)
        assert rec is not None
        assert rec.kind == "experiment"
        assert rec.payload["fault_intent"]["target"] == "pod"


class TestAllocatorLookup:
    def test_get_is_case_insensitive(self):
        a = LocatorAllocator()
        a.allocate_experiment({"x": 1})
        assert a.get("E1") is not None
        assert a.get("e1") is not None

    def test_unknown_locator_returns_none(self):
        a = LocatorAllocator()
        assert a.get("E99") is None
        assert a.get("") is None

    def test_update_payload_merges_into_existing_record(self):
        a = LocatorAllocator()
        loc = a.allocate_tool({"tool_name": "kubectl"})
        a.update_payload(loc, output="ns/default ok")
        rec = a.get(loc)
        assert rec.payload["tool_name"] == "kubectl"
        assert rec.payload["output"] == "ns/default ok"

    def test_update_payload_on_unknown_is_silent(self):
        # Defensive — a tool_end without a matching tool_start (rare,
        # but possible during a streaming abort) shouldn't crash.
        a = LocatorAllocator()
        a.update_payload("T99", output="late")  # no error


class TestAllocatorReset:
    def test_reset_wipes_counters_and_records(self):
        a = LocatorAllocator()
        a.allocate_experiment({"x": 1})
        a.allocate_tool({"y": 2})
        a.reset()
        assert a.list_experiments() == []
        assert a.list_tools() == []
        # Counters restart from 1.
        assert a.allocate_experiment() == "E1"
        assert a.allocate_tool() == "T1"


# -------------------- Renderer integration tests ----------------------


def _render_to_string(call) -> str:
    buf = io.StringIO()

    class _Stub:
        def __init__(self) -> None:
            self.console = Console(file=buf, color_system=None, width=120)

        def print(self, *args, **kwargs):
            self.console.print(*args, **kwargs)

        def print_text(self, *args, **kwargs):
            self.console.print(*args, **kwargs)

        def bell(self) -> None:
            pass

    call(_Stub())
    return buf.getvalue()


def _intent() -> dict:
    return {
        "fault_type": "cpu",
        "scope": "pod",
        "target": "pod",
        "action": "fullload",
        "namespace": "default",
        "names": ["w-1"],
    }


class TestExperimentCardLocatorRendering:
    def test_working_renders_locator_in_title(self):
        state = SessionState()
        out = _render_to_string(
            lambda c: experiment_card.render(
                c, _intent(), display_mode=DisplayMode.WORKING, state=state,
            )
        )
        assert "[E1]" in out
        # Allocator advanced.
        assert state.locators.get("E1") is not None

    def test_dense_renders_locator_in_title(self):
        state = SessionState()
        out = _render_to_string(
            lambda c: experiment_card.render(
                c, _intent(), display_mode=DisplayMode.DENSE, state=state,
            )
        )
        assert "[E1]" in out

    def test_calm_suppresses_locator_label_but_still_allocates(self):
        # Calm hides the visible label but MUST still record the snapshot
        # — otherwise switching calm → dense mid-session would leave the
        # earlier experiments invisible to /show. This was an actual bug
        # at one point: the renderer early-returned on body-is-None
        # before the allocator ran.
        state = SessionState()
        state.display_mode = DisplayMode.CALM
        out = _render_to_string(
            lambda c: experiment_card.render(
                c, _intent(), display_mode=DisplayMode.CALM, state=state,
            )
        )
        # The visible card is suppressed in calm.
        assert "[E" not in out
        # …but the allocator DID run, and the snapshot is queryable.
        rec = state.locators.get("E1")
        assert rec is not None
        assert rec.payload["fault_intent"]["action"] == "fullload"

    def test_render_without_state_skips_allocation(self):
        # The renderer must not blow up when called without state (older
        # callers or unit tests that drive it directly).
        out = _render_to_string(
            lambda c: experiment_card.render(
                c, _intent(), display_mode=DisplayMode.WORKING,
            )
        )
        # No state → no locator label.
        assert "[E" not in out


# -------------------- Slash-command tests ----------------------------


class _CapturingRenderer:
    """Thin renderer stub that captures system messages.

    The locator commands call ``renderer.system(...)`` for "no such
    locator", "/show output", "/copy text", "/rerun description", and
    ``renderer.thinking.finalize()`` / ``streamer.finalize()`` /
    ``console`` for the experiment-card re-render. We provide just
    enough scaffolding so each handler can run without a real Renderer.
    """

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.console = SimpleNamespace(
            print=lambda *a, **kw: self.messages.append(
                "RENDER:" + " ".join(str(x) for x in a)
            )
        )
        self.thinking = SimpleNamespace(finalize=lambda: None)
        self.streamer = SimpleNamespace(finalize=lambda: None)

    def system(self, msg: str) -> None:
        self.messages.append(msg)


def _make_dispatcher(state: SessionState) -> tuple[CommandDispatcher, _CapturingRenderer]:
    renderer = _CapturingRenderer()
    conversation = SimpleNamespace(in_conversation=False, is_streaming=False)
    config_store = SimpleNamespace()
    dispatcher = CommandDispatcher(
        state=state,
        conversation=conversation,
        config_store=config_store,
        renderer=renderer,
    )
    return dispatcher, renderer


class TestShowCommand:
    def test_show_with_no_arg_prints_usage(self):
        state = SessionState()
        dispatcher, renderer = _make_dispatcher(state)
        asyncio.run(dispatcher.dispatch("/show"))
        assert any("/show <locator>" in m for m in renderer.messages)

    def test_show_unknown_locator_reports_miss(self):
        state = SessionState()
        dispatcher, renderer = _make_dispatcher(state)
        asyncio.run(dispatcher.dispatch("/show E99"))
        assert any("E99" in m and "找不到" in m for m in renderer.messages)

    def test_show_experiment_does_not_allocate_a_second_locator(self):
        # Re-rendering for /show must NOT allocate another [E#] —
        # otherwise scrolling /show E1 a few times would consume the
        # index space and confuse the user.
        state = SessionState()
        state.locators.allocate_experiment({"fault_intent": _intent()})
        dispatcher, _ = _make_dispatcher(state)
        asyncio.run(dispatcher.dispatch("/show E1"))
        # Still exactly one experiment locator — no E2 was minted.
        assert state.locators.get("E2") is None

    def test_show_tool_dumps_output(self):
        state = SessionState()
        state.locators.allocate_tool({"tool_name": "kubectl", "output": "ok\n"})
        dispatcher, renderer = _make_dispatcher(state)
        asyncio.run(dispatcher.dispatch("/show T1"))
        assert any("kubectl" in m and "ok" in m for m in renderer.messages)

    def test_show_in_calm_mode_still_prints_card(self):
        # /show is an explicit user request — calm's "hide cards" rule
        # must NOT silently swallow it. Internally the command forces at
        # least working mode for this one render, so the experiment card
        # actually paints to the console.
        state = SessionState()
        state.display_mode = DisplayMode.CALM
        state.locators.allocate_experiment({"fault_intent": _intent()})
        dispatcher, renderer = _make_dispatcher(state)
        asyncio.run(dispatcher.dispatch("/show E1"))
        # The renderer printed something via console.print (RENDER: prefix
        # in our capturing stub). The card body contains the hypothesis
        # phrase, which is unique enough to pin.
        joined = "\n".join(renderer.messages)
        assert "RENDER:" in joined


class TestCopyCommand:
    def test_copy_experiment_prints_json(self):
        state = SessionState()
        state.locators.allocate_experiment({"fault_intent": _intent()})
        dispatcher, renderer = _make_dispatcher(state)
        asyncio.run(dispatcher.dispatch("/copy E1"))
        joined = "\n".join(renderer.messages)
        assert "E1" in joined
        # JSON serialisation of fault_intent makes the action visible.
        assert "fullload" in joined

    def test_copy_unknown_locator_reports_miss(self):
        state = SessionState()
        dispatcher, renderer = _make_dispatcher(state)
        asyncio.run(dispatcher.dispatch("/copy T42"))
        assert any("T42" in m for m in renderer.messages)


class TestRerunCommand:
    def test_rerun_echoes_user_description(self):
        # When the original intent had a user_description we surface it
        # verbatim — the user is responsible for re-issuing.
        state = SessionState()
        intent = _intent()
        intent["user_description"] = "对 default 的 worker pod 注入 CPU 满载"
        state.locators.allocate_experiment({"fault_intent": intent})
        dispatcher, renderer = _make_dispatcher(state)
        asyncio.run(dispatcher.dispatch("/rerun E1"))
        joined = "\n".join(renderer.messages)
        assert "对 default 的 worker pod 注入 CPU 满载" in joined
        # The hint about re-confirmation is part of the safety story.
        assert "意图确认" in joined

    def test_rerun_synthesises_when_description_missing(self):
        # Without an explicit description, fall back to a composed
        # sentence from the structured fields. Better than printing "".
        state = SessionState()
        state.locators.allocate_experiment({"fault_intent": _intent()})
        dispatcher, renderer = _make_dispatcher(state)
        asyncio.run(dispatcher.dispatch("/rerun E1"))
        joined = "\n".join(renderer.messages)
        assert "default" in joined and "fullload" in joined

    def test_rerun_refuses_tool_locator(self):
        # T# locators don't carry an experiment intent; refusing
        # explicitly is friendlier than fabricating something.
        state = SessionState()
        state.locators.allocate_tool({"tool_name": "kubectl"})
        dispatcher, renderer = _make_dispatcher(state)
        asyncio.run(dispatcher.dispatch("/rerun T1"))
        assert any("[E#]" in m for m in renderer.messages)


class TestExpandCommand:
    """``/expand`` re-prints a tool locator's full cached output.

    Pairs with the inline two-line tool result hint
    (``· /expand T1 查看全部 (X 行)``) which is what tells the user
    this command exists. Six pieces of behavior pinned:

    1. No-arg → usage line.
    2. Unknown locator → "找不到" miss.
    3. Wrong-kind locator (E#) → routed to /show with the right id.
    4. Bare digit ``"1"`` is normalised to ``T1`` so the hint's
       short form works.
    5. Lower-case ``"t1"`` and stray-space ``"T 1"`` also resolve.
    6. Tool with cached output → header line + body lines printed
       through ``console.print`` (the locator label is in the
       header so the user can correlate scrollback hits with the
       inline reference).
    """

    def test_expand_with_no_arg_prints_usage(self):
        state = SessionState()
        dispatcher, renderer = _make_dispatcher(state)
        asyncio.run(dispatcher.dispatch("/expand"))
        assert any("用法" in m and "/expand" in m for m in renderer.messages)

    def test_expand_unknown_locator_reports_miss(self):
        state = SessionState()
        dispatcher, renderer = _make_dispatcher(state)
        asyncio.run(dispatcher.dispatch("/expand T42"))
        assert any("T42" in m and "找不到" in m for m in renderer.messages)

    def test_expand_rejects_experiment_locator_with_hint(self):
        # /expand on an E# is a misroute — point the user at /show E#
        # instead of silently rendering whatever's in the payload.
        state = SessionState()
        state.locators.allocate_experiment({"fault_intent": _intent()})
        dispatcher, renderer = _make_dispatcher(state)
        asyncio.run(dispatcher.dispatch("/expand E1"))
        assert any(
            "/show" in m and "E1" in m
            for m in renderer.messages
        )

    def test_expand_accepts_bare_digit(self):
        # The hint reads "/expand T1" but users typing from the older
        # "/expand 1" muscle memory must still hit the right record.
        state = SessionState()
        state.locators.allocate_tool(
            {"tool_name": "kubectl", "output": "line1\nline2\nline3", "elapsed": 0.5}
        )
        dispatcher, renderer = _make_dispatcher(state)
        asyncio.run(dispatcher.dispatch("/expand 1"))
        joined = "\n".join(renderer.messages)
        # The body lands via console.print (RENDER: stub prefix).
        assert "RENDER:" in joined
        # Each cached line is in the output.
        assert "line1" in joined
        assert "line3" in joined

    def test_expand_normalises_case_and_whitespace(self):
        state = SessionState()
        state.locators.allocate_tool(
            {"tool_name": "kubectl", "output": "ok"}
        )
        dispatcher, renderer = _make_dispatcher(state)
        # Lower-case
        asyncio.run(dispatcher.dispatch("/expand t1"))
        # Stray space
        asyncio.run(dispatcher.dispatch("/expand T 1"))
        # Both should have rendered the body, not "找不到".
        miss_count = sum(
            1 for m in renderer.messages if "找不到" in m
        )
        assert miss_count == 0
        joined = "\n".join(renderer.messages)
        assert "ok" in joined

    def test_expand_renders_header_with_locator_label(self):
        # The header line carries the locator label so the user can
        # match a scrollback expand back to the inline reference.
        state = SessionState()
        state.locators.allocate_tool(
            {
                "tool_name": "kubectl",
                "output": "NAMESPACE STATUS AGE\nkube-system Active 30d",
                "elapsed": 1.2,
            }
        )
        dispatcher, renderer = _make_dispatcher(state)
        asyncio.run(dispatcher.dispatch("/expand T1"))
        joined = "\n".join(renderer.messages)
        # Header has tool name + locator label + line count.
        assert "kubectl" in joined
        assert "[T1]" in joined
        # Body preserves the original text.
        assert "kube-system" in joined

    def test_expand_handles_empty_output_gracefully(self):
        # A tool that completed with an empty string shouldn't crash
        # /expand — print a short note instead.
        state = SessionState()
        state.locators.allocate_tool(
            {"tool_name": "kubectl", "output": "", "elapsed": 0.1}
        )
        dispatcher, renderer = _make_dispatcher(state)
        asyncio.run(dispatcher.dispatch("/expand T1"))
        joined = "\n".join(renderer.messages)
        assert "无缓存输出" in joined

    def test_expand_rejects_garbage_arg(self):
        # Anything that's not digit / T# is rejected explicitly.
        state = SessionState()
        dispatcher, renderer = _make_dispatcher(state)
        asyncio.run(dispatcher.dispatch("/expand foobar"))
        assert any("无法识别" in m for m in renderer.messages)
