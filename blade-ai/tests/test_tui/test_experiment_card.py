"""Tests for the PR-D2 experiment card renderer.

The card synthesizes a chaos-experiment header from the same data the
intent_confirm panel already showed — no new state fields, no LLM call,
no cluster probe. We pin five orthogonal pieces of behavior so any one
regression surfaces as a focused failure:

1. ``calm`` mode and empty intent both yield ``None`` from
   ``build_card`` so the renderer is a no-op.
2. Working mode renders the hypothesis sentence + blast radius without
   the dense-only sparkline / params / rollback rows.
3. Dense mode adds the sparkline and the params + rollback rows when
   the data exists; absence of data must not synthesise filler.
4. Hypothesis composition is deterministic — same intent dict always
   yields the same sentence (snapshot-safe).
5. The rollback table only emits hints we can name; unknown actions
   produce no row rather than a fabricated ``stop-foo``.
"""

from __future__ import annotations

import io

from rich.console import Console

from chaos_agent.tui.renderers.experiment_card import (
    _action_label,
    _hypothesis_sentence,
    _rollback_hint,
    build_card,
)
from chaos_agent.tui.state import DisplayMode


def _render_to_string(renderable) -> str:
    """Render a Rich renderable to plain text for assertions."""
    buf = io.StringIO()
    Console(file=buf, color_system=None, width=120).print(renderable)
    return buf.getvalue()


def _intent_concrete() -> dict:
    """Two named pods — exercises the concrete risk path."""
    return {
        "fault_type": "cpu",
        "scope": "pod",
        "target": "pod",
        "action": "fullload",
        "namespace": "payment",
        "names": ["api-1", "api-2"],
        "params": {"cpu-percent": 80},
    }


def _intent_unbounded_label() -> dict:
    return {
        "target": "pod",
        "action": "delay",
        "namespace": "default",
        "labels": "app=worker",
    }


class TestBuildCardSuppression:
    def test_calm_mode_returns_none(self):
        # Calm explicitly hides the card — the user opted out of the
        # differentiating UI.
        assert build_card(_intent_concrete(), DisplayMode.CALM) is None

    def test_empty_intent_returns_none(self):
        # Defensive — an upstream node failing to populate fault_intent
        # shouldn't paint a card with "unknown" everywhere.
        assert build_card({}, DisplayMode.WORKING) is None
        assert build_card(None, DisplayMode.WORKING) is None  # type: ignore[arg-type]


class TestBuildCardWorking:
    def test_renders_hypothesis_and_blast_radius(self):
        body = build_card(_intent_concrete(), DisplayMode.WORKING)
        assert body is not None
        out = _render_to_string(body)
        assert "\u5047\u8bbe" in out         # 假设
        assert "\u7206\u70b8\u534a\u5f84" in out  # 爆炸半径
        assert "2 pod" in out
        # Working stays compact: no dense-only rows.
        assert "\u53c2\u6570" not in out     # 参数
        assert "\u56de\u6eda" not in out     # 回滚

    def test_working_omits_sparkline(self):
        # The sparkline is reserved for dense — keeping working at one
        # visual mass tier below dense is the whole point of the mode.
        body = build_card(_intent_concrete(), DisplayMode.WORKING)
        out = _render_to_string(body)
        assert "\u2581\u2581\u2581" not in out  # ▁▁▁ low sparkline


class TestBuildCardDense:
    def test_includes_params_and_rollback_and_sparkline(self):
        body = build_card(_intent_concrete(), DisplayMode.DENSE)
        assert body is not None
        out = _render_to_string(body)
        assert "\u53c2\u6570" in out                   # 参数
        assert "cpu-percent=80" in out
        assert "\u56de\u6eda" in out                   # 回滚
        assert "stop-cpu-fullload" in out
        # Concrete count of 2 → low tier sparkline.
        assert "\u2581\u2581\u2581" in out             # ▁▁▁

    def test_dense_omits_unnamed_rollback(self):
        # An unknown action shouldn't produce a fabricated rollback row
        # — silence is better than a wrong hint the user might trust.
        body = build_card(
            {"target": "pod", "action": "totally-unknown",
             "namespace": "default", "names": ["p-1"]},
            DisplayMode.DENSE,
        )
        out = _render_to_string(body)
        assert "\u56de\u6eda" not in out

    def test_dense_omits_params_when_empty(self):
        intent = _intent_unbounded_label()
        intent.pop("params", None)
        body = build_card(intent, DisplayMode.DENSE)
        out = _render_to_string(body)
        assert "\u53c2\u6570" not in out


class TestHypothesisComposition:
    def test_sentence_includes_namespace_and_target_and_action_label(self):
        s = _hypothesis_sentence(_intent_concrete())
        assert "payment" in s
        assert "pod" in s
        assert "CPU \u6ee1\u8f7d" in s  # CPU 满载

    def test_unknown_action_falls_back_to_raw(self):
        # Unknown actions still render the raw chaos-blade verb so a
        # reviewer can see exactly what was approved.
        s = _hypothesis_sentence({"action": "weirdaction", "target": "pod",
                                  "namespace": "ns"})
        assert "weirdaction" in s

    def test_sentence_is_deterministic(self):
        intent = _intent_concrete()
        assert _hypothesis_sentence(intent) == _hypothesis_sentence(intent)


class TestActionLabelAndRollback:
    def test_known_action_label(self):
        assert _action_label("fullload") == "CPU \u6ee1\u8f7d"
        assert _action_label("delay") == "\u7f51\u7edc\u5ef6\u8fdf"

    def test_unknown_action_label_falls_through(self):
        assert _action_label("weird") == "weird"

    def test_known_rollback_hint(self):
        assert _rollback_hint("delay") == "stop-delay"
        assert _rollback_hint("kill")

    def test_unknown_rollback_hint_is_empty(self):
        # Empty string lets the caller skip the row entirely; None would
        # require an extra type guard in every consumer.
        assert _rollback_hint("weirdaction") == ""
        assert _rollback_hint("") == ""


class TestUnboundedRendering:
    def test_label_scope_surfaces_runtime_qualifier(self):
        body = build_card(_intent_unbounded_label(), DisplayMode.WORKING)
        out = _render_to_string(body)
        # Unbounded scope: the count is determined at injection time;
        # the qualifier text must appear so the user doesn't read a
        # missing number as "0".
        assert "\u8fd0\u884c\u65f6\u786e\u5b9a" in out  # 运行时确定
        assert "\u6807\u7b7e\u5339\u914d" in out         # 标签匹配

    def test_namespace_scope_qualifier(self):
        intent = {
            "target": "pod",
            "action": "kill",
            "namespace": "default",
            "scope": "namespace",
        }
        body = build_card(intent, DisplayMode.WORKING)
        out = _render_to_string(body)
        assert "\u6574\u4e2a namespace" in out  # 整个 namespace


class TestPRE6CustomHypothesis:
    """PR-E6 — when intent_clarification populates ``hypothesis`` and
    ``success_criteria``, the card must surface those instead of (or in
    addition to) the synth fallback. The whole point of E6 is that
    every experiment looks distinct on the transcript; if the LLM gives
    a real prediction, we MUST show it.
    """

    def test_custom_hypothesis_replaces_synth(self):
        intent = _intent_concrete()
        intent["hypothesis"] = "HPA 应在 60s 内扩到 \u22653 副本"
        s = _hypothesis_sentence(intent)
        assert s == "HPA 应在 60s 内扩到 \u22653 副本"
        # Synth template tokens must be absent — we replaced, not appended.
        assert "\u4fdd\u6301\u57fa\u7ebf\u8868\u73b0" not in s

    def test_blank_hypothesis_falls_back_to_synth(self):
        # An empty / whitespace string is treated as "not provided" so
        # the user doesn't see a stub like "假设：" with nothing after.
        for blank in ("", "   ", "\n"):
            intent = _intent_concrete()
            intent["hypothesis"] = blank
            s = _hypothesis_sentence(intent)
            assert "CPU \u6ee1\u8f7d" in s
            assert "\u4fdd\u6301\u57fa\u7ebf\u8868\u73b0" in s

    def test_success_criteria_render_in_working(self):
        intent = _intent_concrete()
        intent["success_criteria"] = [
            "kubectl get pod 显示 Running 副本 \u2265 3",
            "p99 latency < 500ms",
        ]
        body = build_card(intent, DisplayMode.WORKING)
        out = _render_to_string(body)
        assert "\u9a8c\u6536" in out  # 验收
        assert "kubectl get pod" in out
        assert "p99 latency < 500ms" in out

    def test_success_criteria_render_in_dense_alongside_params(self):
        intent = _intent_concrete()
        intent["success_criteria"] = ["5xx 比例 < 1%"]
        body = build_card(intent, DisplayMode.DENSE)
        out = _render_to_string(body)
        # Both criteria and params/rollback show in dense.
        assert "\u9a8c\u6536" in out  # 验收
        assert "5xx" in out
        assert "\u53c2\u6570" in out  # 参数
        assert "\u56de\u6eda" in out  # 回滚

    def test_empty_criteria_omits_row_entirely(self):
        # A "no criteria found" stub would lie about analysis depth;
        # silence is honest. Mirrors the rollback-hint discipline.
        intent = _intent_concrete()
        intent["success_criteria"] = []
        body = build_card(intent, DisplayMode.WORKING)
        out = _render_to_string(body)
        assert "\u9a8c\u6536" not in out

    def test_non_list_criteria_treated_as_empty(self):
        # Defensive: a buggy LLM might send a string. Don't crash and
        # don't try to display it — just skip the row.
        intent = _intent_concrete()
        intent["success_criteria"] = "p99 < 500ms"  # type: ignore[assignment]
        body = build_card(intent, DisplayMode.WORKING)
        out = _render_to_string(body)
        assert "\u9a8c\u6536" not in out

    def test_criteria_strip_whitespace_and_drop_blanks(self):
        intent = _intent_concrete()
        intent["success_criteria"] = ["  p99 < 500ms  ", "", "   "]
        body = build_card(intent, DisplayMode.WORKING)
        out = _render_to_string(body)
        # Survivor only — no double-space artifacts.
        assert "p99 < 500ms" in out
        # Single criterion → single 验收 row.
        assert out.count("\u9a8c\u6536") == 1

    def test_calm_still_suppresses_card_with_custom_hypothesis(self):
        # PR-E6 doesn't change the calm-mode opt-out — even a real
        # hypothesis stays hidden when the user picked calm.
        intent = _intent_concrete()
        intent["hypothesis"] = "HPA 应在 60s 内扩到 \u22653"
        intent["success_criteria"] = ["kubectl 显示副本 \u2265 3"]
        assert build_card(intent, DisplayMode.CALM) is None
