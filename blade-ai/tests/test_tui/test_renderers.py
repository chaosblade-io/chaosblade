"""Smoke tests for renderer functions — each writes non-empty output."""

import io

from rich.console import Console

from chaos_agent.tui.console import ChaosConsole
from chaos_agent.tui.renderers import header as header_renderer
from chaos_agent.tui.renderers import intent_confirm, messages, result
from chaos_agent.tui.renderers import welcome as welcome_renderer
from chaos_agent.tui.renderers.phase_timeline import PhaseTimelineRenderer
from chaos_agent.tui.state import SessionState


def _capture_console() -> ChaosConsole:
    cc = ChaosConsole()
    cc._console = Console(file=io.StringIO(), force_terminal=False, width=80)
    return cc


def _render_plain(renderable) -> str:
    """Render any Rich renderable (Text / Group / Table) to plain text.

    Replaces ``body.plain`` calls from before PR-C2: ``build_body`` now
    returns a Group so the field rows go through Rich's column-width
    logic instead of CJK-broken ``ljust``. This helper just renders the
    Group through a captured Console — same effect for the test
    assertions, which only care about substring presence.
    """
    cc = _capture_console()
    cc._console.print(renderable)
    return cc._console.file.getvalue()


class TestMessageRenderers:
    def test_render_user(self):
        cc = _capture_console()
        messages.render_user(cc, "hello")
        assert "hello" in cc._console.file.getvalue()

    def test_render_system(self):
        cc = _capture_console()
        messages.render_system(cc, "ready")
        assert "ready" in cc._console.file.getvalue()

    def test_render_error_includes_task_id(self):
        cc = _capture_console()
        messages.render_error(cc, "boom", task_id="t-1")
        out = cc._console.file.getvalue()
        assert "boom" in out and "t-1" in out

    def test_no_vline_rail_in_user_system_error(self):
        """PR-B1 — the colored ┃ left rail was the loudest visual artifact
        of the pre-B1 TUI. It's been removed across renderers; this test
        locks that contract for messages.py specifically because that file
        kept the rail after streaming.py was already cleaned up. If a
        future renderer brings it back, this assertion catches it.
        """
        cc = _capture_console()
        messages.render_user(cc, "hi")
        messages.render_system(cc, "ready")
        messages.render_error(cc, "boom", task_id="t-1")
        out = cc._console.file.getvalue()
        assert "\u2503" not in out  # ┃ U+2503 must not appear anywhere
        # And we still surface a role glyph at column 1 — `>` for user,
        # `ℹ` for system, `✗` for error — so role attribution is preserved.
        assert ">" in out
        assert "\u2139" in out  # ℹ
        assert "\u2717" in out  # ✗

    def test_vline_constants_deleted_from_icons(self):
        """B1 cleanup — ``Icons.VLINE`` / ``Icons.VLINE_THIN`` are gone.
        Re-adding them needs a deliberate design decision (the docstring
        spells out why they were removed), so guarding with hasattr."""
        from chaos_agent.tui.theme import Icons

        assert not hasattr(Icons, "VLINE")
        assert not hasattr(Icons, "VLINE_THIN")

    def test_spacing_role_indent_deleted(self):
        """B1 cleanup — ``Spacing.ROLE_INDENT`` was the "spaces after ┃"
        constant. With no ┃ left, the constant is dead code; locking its
        absence so it doesn't drift back as a misleading magic number."""
        from chaos_agent.tui.theme import Spacing

        assert not hasattr(Spacing, "ROLE_INDENT")


class TestErrorWithSuggestions:
    """§8.4 — actionable error recovery routing.

    ``render_error_with_suggestions`` is the public entry the dispatch
    path uses. Six pieces of behavior pinned:

      1. Unknown / generic message → falls back to plain one-line
         ``render_error`` (no panel, no scrollback noise for benign
         errors).
      2. Recognised pattern → grows into the recovery panel via
         ``render_error_recovery`` with a labelled title and a list
         of slash-command next steps.
      3. The "下一步:" header is the §8.4 mockup's signal — we pin it
         appears so the panel doesn't silently revert to plain.
      4. The matched suggestion list contains commands the dispatcher
         actually serves (no ``/switch-context`` / ``/retry`` / etc.
         that doc §8.4 invented but we never implemented).
      5. ``task_id`` appears as a dim subordinate line under the
         panel for postmortem correlation.
      6. Order matters: ``"failed to initialize"`` must trip INIT
         before ``"connection refused"`` could trip CLUSTER.
    """

    def test_generic_error_falls_back_to_plain_render(self):
        cc = _capture_console()
        messages.render_error_with_suggestions(
            cc, "some unrelated bug", task_id="t-9"
        )
        out = cc._console.file.getvalue()
        # No box drawing → no panel.
        assert "╭" not in out  # ╭
        assert "╰" not in out  # ╰
        # Original content preserved.
        assert "some unrelated bug" in out
        # Plain render still includes task_id on the same line.
        assert "t-9" in out
        # And no "下一步:" section.
        assert "下一步" not in out  # 下一步

    def test_init_error_routes_to_init_panel(self):
        cc = _capture_console()
        messages.render_error_with_suggestions(
            cc,
            "Failed to initialize agent runner: missing model_name",
            task_id="t-1",
        )
        out = cc._console.file.getvalue()
        # Panel is drawn → corner glyphs present.
        assert "╭" in out  # ╭
        # Title carries the matched label.
        assert "INIT FAILED" in out
        # Suggestion section header.
        assert "下一步" in out  # 下一步
        # Specific commands surface.
        assert "/doctor" in out
        assert "/config" in out

    def test_kubeconfig_error_routes_to_cluster_panel(self):
        cc = _capture_console()
        messages.render_error_with_suggestions(
            cc, "kubectl get nodes: connection refused"
        )
        out = cc._console.file.getvalue()
        assert "CLUSTER UNREACHABLE" in out
        assert "/doctor" in out

    def test_stream_error_routes_to_stream_panel(self):
        cc = _capture_console()
        messages.render_error_with_suggestions(cc, "Stream error: timeout")
        out = cc._console.file.getvalue()
        assert "STREAM ERROR" in out
        # Stream panel suggests retry-by-resending.
        assert "重新发送" in out  # 重新发送

    def test_replay_error_routes_to_replay_panel(self):
        cc = _capture_console()
        messages.render_error_with_suggestions(
            cc, "Replay failed: cannot rehydrate event"
        )
        out = cc._console.file.getvalue()
        assert "REPLAY FAILED" in out
        assert "/recordings" in out

    def test_command_error_routes_to_command_panel(self):
        cc = _capture_console()
        messages.render_error_with_suggestions(cc, "Command failed: invalid arg")
        out = cc._console.file.getvalue()
        assert "COMMAND FAILED" in out
        assert "/help" in out

    def test_init_error_with_connection_keyword_still_picks_init(self):
        # Order check: a wrapped exception "Failed to initialize agent
        # runner: connection refused" matches BOTH "failed to initialize"
        # AND "connection refused". The init pattern is registered first
        # (it's the user's primary signal), so the init label should win.
        cc = _capture_console()
        messages.render_error_with_suggestions(
            cc, "Failed to initialize agent runner: connection refused"
        )
        out = cc._console.file.getvalue()
        assert "INIT FAILED" in out
        assert "CLUSTER UNREACHABLE" not in out

    def test_task_id_appears_as_subordinate_line_for_panel_path(self):
        # The panel path emits the task_id on a separate dim line so
        # postmortem readers can correlate the panel with a recording.
        cc = _capture_console()
        messages.render_error_with_suggestions(
            cc, "Stream error: lost connection", task_id="t-42"
        )
        out = cc._console.file.getvalue()
        # task_id is in the output but on its own line under the panel
        # (not inside the panel title or body).
        lines = out.splitlines()
        task_line = next(i for i, ln in enumerate(lines) if "t-42" in ln)
        # The task line should NOT contain panel border glyphs.
        assert "╭" not in lines[task_line]
        assert "│" not in lines[task_line]

    def test_suggestions_only_reference_real_commands(self):
        # Pin that no entry in the suggestion table promises a command
        # the dispatcher doesn't actually serve. doc §8.4 mocked
        # /switch-context, /retry, /abort which we deliberately don't
        # implement; suggesting them would be a worse UX than offering
        # none. This test catches a future contributor adding one
        # without also adding the command.
        from chaos_agent.tui.renderers.messages import _ERROR_SUGGESTIONS

        # The "valid" set is whatever the registry actually serves.
        # We approximate it by reading the strings module's CMD_*_DESC
        # constants (every command has one) plus the always-present
        # built-ins.

        valid = {
            "/help", "/doctor", "/clear", "/exit", "/config",
            "/model", "/mode", "/compact", "/memory",
            "/plan", "/run", "/recover", "/review", "/tasks",
            "/experiments", "/show", "/copy", "/rerun", "/expand",
            "/replay", "/recordings", "/skills",
        }
        for _kw, _label, sugg_list in _ERROR_SUGGESTIONS:
            for line in sugg_list:
                # Each suggestion line is "<command> — <description>"
                # or pure prose advice (no leading "/"). Skip non-/
                # advice; verify any leading slash command exists.
                line = line.strip()
                if not line.startswith("/"):
                    continue
                cmd = line.split()[0].split("—")[0].strip()
                # Take just the first token (no args).
                cmd = cmd.split()[0]
                assert cmd in valid, (
                    f"Suggestion references '{cmd}' but dispatcher does "
                    f"not serve it. Add the command or remove the line."
                )


class TestResultRenderer:
    def test_success(self):
        cc = _capture_console()
        result.render_result(cc, {"status": "success", "task_state": "injected"}, "t-1")
        out = cc._console.file.getvalue()
        assert "SUCCESS" in out and "t-1" in out

    def test_failure(self):
        cc = _capture_console()
        result.render_result(cc, {"status": "failed"}, "")
        assert "FAILED" in cc._console.file.getvalue()

    def test_failure_renders_cause_and_hint_from_failure_reason(self):
        """failure_reason carries '<base> | llm_analysis: <hint>' — the renderer
        must split it into Cause / Hint lines instead of dumping it under Error."""
        cc = _capture_console()
        result.render_result(
            cc,
            {
                "status": "failed",
                "failure_reason": (
                    "safety_rejected: Namespace 'kube-system' is in the safety blacklist"
                    " | llm_analysis: try a non-system namespace such as cms-demo"
                ),
            },
            "t-9",
        )
        out = cc._console.file.getvalue()
        assert "Cause:" in out
        assert "kube-system" in out
        assert "Hint:" in out
        assert "cms-demo" in out
        assert "Error:" not in out

    def test_failure_falls_back_to_error_when_no_failure_reason(self):
        cc = _capture_console()
        result.render_result(
            cc,
            {"status": "failed", "error": "blade-uid not found"},
            "t-10",
        )
        out = cc._console.file.getvalue()
        assert "Error:" in out
        assert "blade-uid not found" in out
        assert "Cause:" not in out

    def test_failure_reason_without_separator_renders_cause_only(self):
        cc = _capture_console()
        result.render_result(
            cc,
            {"status": "failed", "failure_reason": "unexpected_blade_state"},
            "t-11",
        )
        out = cc._console.file.getvalue()
        assert "Cause:" in out
        assert "unexpected_blade_state" in out
        assert "Hint:" not in out

    def test_replan_history_renders_versioned_timeline_on_success(self):
        """PR-A3 — when the agent shrank a too-big plan and succeeded on
        attempt 2, surface that convergence as a v1 → v2 timeline. Without
        this, a successful run looks identical to a single-shot success and
        we lose the agent's value pitch."""
        cc = _capture_console()
        result.render_result(
            cc,
            {
                "status": "success",
                "task_state": "injected",
                "replan_count": 1,
                "replan_history": [
                    {
                        "attempt": 1,
                        "original_error": "blast radius too large (>30% of namespace)",
                        "action_taken": "shrink scope",
                    },
                ],
            },
            "t-rp1",
        )
        out = cc._console.file.getvalue()
        assert "Replan History" in out
        assert "v1" in out
        assert "blast radius too large" in out
        assert "v2" in out  # Final attempt that succeeded
        assert "agent improved the plan" in out

    def test_replan_history_marks_final_failure_when_overall_failed(self):
        """If the agent exhausted replan budget and still failed, the final
        line should read as a failure marker, not a success — otherwise we'd
        misrepresent retried-but-still-broken runs."""
        cc = _capture_console()
        result.render_result(
            cc,
            {
                "status": "failed",
                "task_state": "failed",
                "replan_count": 2,
                "replan_history": [
                    {"attempt": 1, "original_error": "err A"},
                    {"attempt": 2, "original_error": "err B"},
                ],
                "error": "exhausted",
            },
            "t-rp2",
        )
        out = cc._console.file.getvalue()
        assert "v1" in out and "err A" in out
        assert "v2" in out and "err B" in out
        assert "v3" in out  # The final attempt
        assert "final attempt also failed" in out

    def test_replan_history_empty_skips_section(self):
        """No replan happened → no Replan History header. Single-shot
        successes shouldn't grow a stub timeline section."""
        cc = _capture_console()
        result.render_result(
            cc,
            {
                "status": "success",
                "task_state": "injected",
                "replan_history": [],
            },
            "t-rp3",
        )
        out = cc._console.file.getvalue()
        assert "Replan History" not in out

    def test_replan_history_truncates_long_reason(self):
        """A 1000-char error_summary must not blow out the panel width.
        Renderer caps each line; full value is still in build_status_data."""
        cc = _capture_console()
        long_reason = "x" * 200
        result.render_result(
            cc,
            {
                "status": "failed",
                "replan_history": [{"attempt": 1, "original_error": long_reason}],
            },
            "t-rp4",
        )
        out = cc._console.file.getvalue()
        assert "\u2026" in out  # ellipsis marker
        assert "x" * 200 not in out

    def test_side_effects_renders_container_restart_line(self):
        """PR-A4 — when the fault triggered a real container restart,
        surface it under a Side Effects header so operators see the
        most valuable signal of an injection: it actually crashed something."""
        cc = _capture_console()
        result.render_result(
            cc,
            {
                "status": "success",
                "task_state": "injected",
                "side_effects": {
                    "container_restarts": [
                        {"pod": "web-1", "restart_count": 1, "reason": "OOMKilled"},
                    ]
                },
            },
            "t-se1",
        )
        out = cc._console.file.getvalue()
        assert "Side Effects" in out
        assert "web-1" in out
        assert "1\u00d7" in out  # the multiplier (e.g., "1×")
        assert "OOMKilled" in out

    def test_side_effects_skipped_when_empty(self):
        """No restart events → no header. We don't want a stub section
        polluting the most common path (single-shot success without
        side effects)."""
        cc = _capture_console()
        result.render_result(
            cc,
            {
                "status": "success",
                "task_state": "injected",
                "side_effects": {},
            },
            "t-se2",
        )
        out = cc._console.file.getvalue()
        assert "Side Effects" not in out

    def test_side_effects_handles_multiple_pods(self):
        """The verifier can record restarts on multiple pods — each gets
        its own line so operators can see the full blast radius."""
        cc = _capture_console()
        result.render_result(
            cc,
            {
                "status": "success",
                "task_state": "injected",
                "side_effects": {
                    "container_restarts": [
                        {"pod": "web-1", "restart_count": 2, "reason": "OOMKilled"},
                        {"pod": "web-2", "restart_count": 1, "reason": "CrashLoopBackOff"},
                    ]
                },
            },
            "t-se3",
        )
        out = cc._console.file.getvalue()
        assert "web-1" in out
        assert "2\u00d7" in out
        assert "web-2" in out
        assert "1\u00d7" in out
        assert "CrashLoopBackOff" in out

    def test_envelope_shape_surfaces_nested_diagnostics(self):
        """Production result events arrive as JSONEnvelope:
        ``{status, code, message, data: {...inner...}}``. The diagnostic
        fields (failure_reason, replan_history, side_effects) live under
        ``data`` — never at the top level. Renderer must surface them
        anyway, otherwise A1/A3/A4 are invisible in production despite
        the in-test flat-dict path passing.
        """
        cc = _capture_console()
        envelope = {
            "status": "fail",
            "code": "INJECT_FAILED",
            "message": "blade_create timed out",
            "data": {
                "task_id": "t-env",
                "result": "failed",
                "failure_reason": (
                    "blade_create_timeout: exceeded 60s"
                    " | llm_analysis: try smaller blast radius"
                ),
                "replan_count": 1,
                "replan_history": [
                    {"attempt": 1, "original_error": "scope too broad"},
                ],
                "side_effects": {
                    "container_restarts": [
                        {"pod": "api-1", "restart_count": 1, "reason": "OOMKilled"},
                    ]
                },
            },
        }
        result.render_result(cc, envelope, "t-env")
        out = cc._console.file.getvalue()
        assert "Cause:" in out
        assert "blade_create_timeout" in out
        assert "Hint:" in out
        assert "smaller blast radius" in out
        assert "Replan History" in out
        assert "scope too broad" in out
        assert "Side Effects" in out
        assert "api-1" in out


class TestHeaderAndWelcome:
    def test_header_banner(self):
        cc = _capture_console()
        state = SessionState()
        state.cluster_name = "kind"
        header_renderer.print_banner(cc, state)
        out = cc._console.file.getvalue()
        assert "blade-ai" in out

    def test_welcome_card(self):
        cc = _capture_console()
        state = SessionState()
        welcome_renderer.print_card(cc, state)
        out = cc._console.file.getvalue()
        assert "blade-ai" in out


class TestIntentConfirmBody:
    """build_body is the pure function that constructs the panel body —
    we test it directly so we don't need to drive a PromptSession."""

    BASE_INTENT = {
        "fault_type": "cpu-fullload",
        "scope": "pod",
        "target": "cpu",
        "action": "fullload",
        "namespace": "cms-demo",
    }

    def test_no_confidence_row_when_zero(self):
        """confidence=0.0 means the LLM did not report one — keep the panel
        compact rather than showing a misleading ``Confidence: 0.00`` line."""
        body = intent_confirm.build_body({"fault_intent": self.BASE_INTENT})
        assert "Confidence:" not in _render_plain(body)

    def test_high_confidence_renders_value_without_warning(self):
        body = intent_confirm.build_body(
            {"fault_intent": self.BASE_INTENT, "intent_confidence": 0.92}
        )
        text = _render_plain(body)
        assert "Confidence:" in text
        assert "0.92" in text
        # Post-PR-A2: the warning row is gated by the threshold; high
        # confidence must NOT trip either lead phrase.
        assert "建议逐项核对" not in text
        assert "强烈建议" not in text

    def test_low_confidence_surfaces_warning(self):
        body = intent_confirm.build_body(
            {"fault_intent": self.BASE_INTENT, "intent_confidence": 0.55}
        )
        text = _render_plain(body)
        assert "Confidence:" in text
        assert "0.55" in text
        # Post-PR-A2: the warning is now a field-aware subordinate
        # row, not the old generic "请仔细核对字段后再批准" filler.
        assert "建议逐项核对" in text

    def test_confidence_style_thresholds(self):
        from chaos_agent.tui.theme import Colors
        assert intent_confirm._confidence_style(0.95) == Colors.SUCCESS
        assert intent_confirm._confidence_style(0.65) == Colors.WARNING
        assert intent_confirm._confidence_style(0.30) == Colors.ERROR


class TestThemeDedupeAndSemanticTokens:
    """PR-B3 — locks the icon dedupe + semantic-token routing.

    Why these are worth testing instead of "obvious from reading theme.py":
    a future contributor who needs ``✅`` could re-add it without touching
    a renderer; these assertions catch that. Same for the alias layer —
    if someone hardcodes a hex into ``Colors.SUCCESS``, ``Theme`` is no
    longer the single source of truth.
    """

    def test_duplicate_icon_attrs_are_gone(self):
        from chaos_agent.tui.theme import Icons
        for attr in ("RESULT_OK", "RESULT_FAIL", "RESULT_WARN", "DONE"):
            assert not hasattr(Icons, attr), (
                f"Icons.{attr} should have been merged into the canonical glyph"
            )

    def test_canonical_glyphs(self):
        from chaos_agent.tui.theme import Icons
        assert Icons.SUCCESS == "\u2713"
        assert Icons.FAIL == "\u2717"
        assert Icons.WARNING == "\u26a0"
        # The warning must NOT carry the VS-16 emoji-style selector — that
        # forces double-width rendering on iTerm2 and Alacritty.
        assert "\ufe0f" not in Icons.WARNING
        assert Icons.MARKER == Icons.AGENT == "\u23fa"
        assert Icons.THINKING == "\u273b"
        assert Icons.USER == ">"

    def test_legacy_color_aliases_resolve_through_theme(self):
        from chaos_agent.tui.theme import Colors, Theme
        assert Colors.SUCCESS == Theme.state_ok
        assert Colors.WARNING == Theme.state_warn
        assert Colors.ERROR == Theme.state_err
        assert Colors.BRAND == Theme.text_accent
        assert Colors.MUTED == Theme.text_muted

    def test_legacy_borders_resolve_through_theme(self):
        from chaos_agent.tui.theme import Borders, Theme
        assert Borders.RESULT_SUCCESS == Theme.border_ok
        assert Borders.RESULT_PARTIAL == Theme.border_warn
        assert Borders.RESULT_FAIL == Theme.border_err


class TestPhaseTimelineIdempotent:
    def test_double_start_replaces_live_without_leaking(self):
        cc = _capture_console()
        # Force an 80+ wide console so start() doesn't bail early.
        cc._console = Console(file=io.StringIO(), force_terminal=False, width=120)
        timeline = PhaseTimelineRenderer(cc)
        timeline.start()
        first = timeline._live
        assert first is not None
        timeline.start()
        second = timeline._live
        assert second is not None
        assert first is not second  # old Live was replaced, not appended
        timeline.stop()
        assert timeline._live is None
