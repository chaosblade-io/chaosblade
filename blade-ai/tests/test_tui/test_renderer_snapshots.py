"""Snapshot tests for the steady-state renderers (PR-B5 / §17.3).

Each test renders a renderer in isolation against the ``captured_console``
fixture (width=80, ANSI-off) and compares the captured stdout against a
golden file. The intent is to lock the *whole shape* of the output —
column counts, blank-line rhythm, label alignment — so a future cosmetic
change is a deliberate snapshot bump rather than a silent regression.

Coverage is deliberately narrow: the renderers picked here are the ones
users see most often (chat-line attribution and the result panel) plus
the ones that just changed (B1 messages, B2 thinking — though B2 is
transient and can't be cleanly snapshotted, so we skip it).

Updating after an intentional layout change:

    UPDATE_SNAPSHOTS=1 uv run pytest tests/test_tui/test_renderer_snapshots.py

Then review the resulting ``snapshots/*.txt`` diff before committing.
"""

from __future__ import annotations

import pytest

from chaos_agent.tui.renderers import intent_confirm, messages, result

pytestmark = pytest.mark.usefixtures("require_unicode_locale")


def _render_to_text(captured_console, renderable) -> str:
    """Render a Rich renderable to the captured console and return output.

    Used by snapshot tests after PR-C2 switched ``build_body`` from a
    plain ``Text`` (which had a ``.plain`` attribute) to a ``Group``
    composing Text + Table — Group has no ``.plain``, so we round-trip
    through the console to get a comparable string.
    """
    captured_console._console.print(renderable)
    return captured_console._console.file.getvalue()


# ── Message renderers (locks B1 cleanup) ─────────────────────────────────


class TestMessageSnapshots:
    """Lock the post-B1 layout: glyph at column 1, no ┃ rail, no
    quote-frame indent. If a future renderer brings any of those back,
    the snapshot diff makes it impossible to miss in review."""

    def test_render_user_snapshot(self, snapshot, captured_console):
        messages.render_user(captured_console, "对 default 命名空间注入 cpu 满载")
        snapshot.assert_match(
            "messages-user", captured_console._console.file.getvalue()
        )

    def test_render_system_snapshot(self, snapshot, captured_console):
        messages.render_system(captured_console, "已连接到集群 kind-blade")
        snapshot.assert_match(
            "messages-system", captured_console._console.file.getvalue()
        )

    def test_render_error_with_task_id_snapshot(self, snapshot, captured_console):
        messages.render_error(
            captured_console, "blade-uid not found", task_id="t-42"
        )
        snapshot.assert_match(
            "messages-error", captured_console._console.file.getvalue()
        )


# ── Result panel (the highest-information renderer) ──────────────────────


class TestResultPanelSnapshots:
    """Lock the result panel layouts. ``render_result`` is the renderer
    with the most knobs (status colors, optional sections for replan
    history / side effects / verification / cause + hint), and the one
    users stare at the longest after each run. Substring asserts already
    cover the conditional sections individually; the snapshots here lock
    the *interaction* between sections — what happens when several light
    up at once."""

    def test_success_minimal_snapshot(self, snapshot, captured_console):
        result.render_result(
            captured_console,
            {"status": "success", "task_state": "injected", "fault_type": "cpu-fullload"},
            "t-success",
        )
        snapshot.assert_match(
            "result-success-minimal", captured_console._console.file.getvalue()
        )

    def test_failure_with_cause_and_hint_snapshot(self, snapshot, captured_console):
        result.render_result(
            captured_console,
            {
                "status": "failed",
                "failure_reason": (
                    "safety_rejected: Namespace 'kube-system' is in the safety blacklist"
                    " | llm_analysis: try a non-system namespace such as cms-demo"
                ),
            },
            "t-fail",
        )
        snapshot.assert_match(
            "result-failure-cause-hint", captured_console._console.file.getvalue()
        )

    def test_success_with_replan_and_side_effects_snapshot(
        self, snapshot, captured_console
    ):
        # The "kitchen sink" success: replan history *and* container
        # restarts. The interaction of these two sections is the most
        # likely place a future change drops a blank line or shifts the
        # divider width — exactly what snapshot tests catch.
        result.render_result(
            captured_console,
            {
                "status": "success",
                "task_state": "injected",
                "fault_type": "cpu-fullload",
                "replan_count": 1,
                "replan_history": [
                    {
                        "attempt": 1,
                        "original_error": "blast radius too large (>30% of namespace)",
                        "action_taken": "shrink scope",
                    },
                ],
                "side_effects": {
                    "container_restarts": [
                        {"pod": "web-1", "restart_count": 1, "reason": "OOMKilled"},
                    ]
                },
            },
            "t-rich",
        )
        snapshot.assert_match(
            "result-success-rich", captured_console._console.file.getvalue()
        )


# ── Intent confirmation body (the panel users approve from) ──────────────


class TestIntentConfirmBodySnapshots:
    """``build_body`` is the pure function under the confirmation panel.
    Snapshotting it locks the field order, the confidence-row gating,
    and the warning sentence — a regression in any of those changes how
    a user reads safety-critical info before approving a fault."""

    BASE_INTENT = {
        "fault_type": "cpu-fullload",
        "scope": "pod",
        "target": "cpu",
        "action": "fullload",
        "namespace": "cms-demo",
    }

    def test_body_no_confidence_snapshot(self, snapshot, captured_console):
        body = intent_confirm.build_body({"fault_intent": self.BASE_INTENT})
        snapshot.assert_match(
            "intent-confirm-no-confidence", _render_to_text(captured_console, body)
        )

    def test_body_high_confidence_snapshot(self, snapshot, captured_console):
        body = intent_confirm.build_body(
            {"fault_intent": self.BASE_INTENT, "intent_confidence": 0.92}
        )
        snapshot.assert_match(
            "intent-confirm-high-confidence", _render_to_text(captured_console, body)
        )

    def test_body_low_confidence_snapshot(self, snapshot, captured_console):
        body = intent_confirm.build_body(
            {"fault_intent": self.BASE_INTENT, "intent_confidence": 0.55}
        )
        snapshot.assert_match(
            "intent-confirm-low-confidence", _render_to_text(captured_console, body)
        )
