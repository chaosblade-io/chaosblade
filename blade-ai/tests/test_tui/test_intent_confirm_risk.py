"""Tests for the PR-D3 intent_confirm risk meter.

Locks four orthogonal pieces of behavior so a regression in any one of
them surfaces as a focused failure rather than a snapshot diff:

1. ``_compute_risk_info`` distinguishes the three risk-derivation kinds
   (concrete/bounded/unbounded) and gracefully returns ``None`` for an
   intent that has nothing useful to surface.
2. ``_risk_tier`` honours the absolute-count breakpoints (≤2 / 3-9 /
   10+). The boundaries matter — a regression that flipped "low" into
   "medium" at count=2 would be silent in any rendering test.
3. ``_render_risk_summary`` respects ``display_mode``: calm hides the
   row, working shows the count + tier label, dense adds the
   box-drawing sparkline. Streaming / non-confirm contexts don't reach
   this path so they're not tested here.
4. ``build_body`` integrates the risk row at the right spot — a
   non-calm rendering should expose ``Risk:`` in the visible text and
   calm should not.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from chaos_agent.tui.renderers.intent_confirm import (
    LOW_CONFIDENCE_THRESHOLD,
    _compute_risk_info,
    _low_confidence_hint,
    _render_risk_summary,
    _risk_tier,
    _RiskInfo,
    build_body,
)
from chaos_agent.tui.state import DisplayMode


def _render_to_string(renderable) -> str:
    """Render a Rich renderable to a plain string (no ANSI) for assertions."""
    buf = io.StringIO()
    Console(file=buf, color_system=None, width=120).print(renderable)
    return buf.getvalue()


class TestComputeRiskInfoConcrete:
    def test_names_yields_concrete_with_count_and_sample(self):
        intent = {
            "target": "pod",
            "names": ["api-1", "api-2", "api-3"],
        }
        risk = _compute_risk_info(intent)
        assert risk is not None
        assert risk.kind == "concrete"
        assert risk.count == 3
        assert risk.target == "pod"
        # Sample lists the first three names verbatim, comma-joined.
        assert "api-1" in risk.sample
        assert "api-3" in risk.sample

    def test_names_with_overflow_appends_plus_marker(self):
        # Five names → first three named + "(+2)" so the user sees the
        # tail isn't missing, just elided.
        intent = {
            "target": "pod",
            "names": [f"p-{i}" for i in range(5)],
        }
        risk = _compute_risk_info(intent)
        assert risk is not None
        assert risk.count == 5
        assert "+2" in risk.sample


class TestComputeRiskInfoBounded:
    def test_params_count_yields_bounded(self):
        intent = {"target": "node", "params": {"count": 3}}
        risk = _compute_risk_info(intent)
        assert risk is not None
        assert risk.kind == "bounded"
        assert risk.count == 3
        assert risk.target == "node"

    def test_params_capital_count_also_recognised(self):
        # chaos-blade sometimes emits Pascal-Case keys.
        intent = {"target": "pod", "params": {"Count": 7}}
        risk = _compute_risk_info(intent)
        assert risk is not None
        assert risk.kind == "bounded"
        assert risk.count == 7

    def test_garbage_count_falls_through(self):
        # Non-numeric count must not crash; falls through to unbounded
        # logic which has no labels/percent/namespace either → None.
        intent = {"target": "pod", "params": {"count": "lots"}}
        risk = _compute_risk_info(intent)
        assert risk is None


class TestComputeRiskInfoUnbounded:
    def test_labels_yields_unbounded_with_descriptor(self):
        intent = {"target": "pod", "labels": "app=api"}
        risk = _compute_risk_info(intent)
        assert risk is not None
        assert risk.kind == "unbounded"
        assert risk.descriptor == "labels"

    def test_percent_yields_unbounded_with_percent_descriptor(self):
        intent = {"target": "pod", "params": {"percent": 50}}
        risk = _compute_risk_info(intent)
        assert risk is not None
        assert risk.kind == "unbounded"
        assert "percent" in risk.descriptor

    def test_namespace_scope_yields_unbounded_namespace(self):
        intent = {"target": "pod", "scope": "namespace"}
        risk = _compute_risk_info(intent)
        assert risk is not None
        assert risk.kind == "unbounded"
        assert risk.descriptor == "namespace"

    def test_empty_intent_returns_none(self):
        assert _compute_risk_info({}) is None
        assert _compute_risk_info(None) is None  # type: ignore[arg-type]


class TestRiskTierBoundaries:
    """The thresholds (≤2 / 3-9 / 10+) are policy decisions; pin them.

    A regression that nudged the medium boundary up by one (say, ≤3 →
    low) would be silent in any rendering test that uses a count of 5.
    """

    @pytest.mark.parametrize("count", [0, 1, 2])
    def test_low_tier(self, count: int):
        label, _color, bar = _risk_tier(count)
        assert label == "low"
        # Sparkline reads "small" — three lowest blocks.
        assert bar == "\u2581\u2581\u2581"

    @pytest.mark.parametrize("count", [3, 5, 9])
    def test_medium_tier(self, count: int):
        label, _color, bar = _risk_tier(count)
        assert label == "medium"
        # Sparkline reads "ramping up".
        assert bar == "\u2581\u2583\u2585"

    @pytest.mark.parametrize("count", [10, 50, 999])
    def test_high_tier(self, count: int):
        label, _color, bar = _risk_tier(count)
        assert label == "high"
        # Sparkline reads "full"  — three highest blocks.
        assert bar == "\u2586\u2587\u2588"


class TestRenderRiskSummaryByMode:
    def test_calm_returns_none(self):
        # Calm explicitly hides the risk meter — that's the differentiator
        # the user opted out of.
        risk = _RiskInfo(kind="concrete", target="pod", count=2, sample="a, b")
        assert _render_risk_summary(risk, DisplayMode.CALM) is None

    def test_working_shows_count_and_tier_no_sparkline(self):
        risk = _RiskInfo(kind="concrete", target="pod", count=2, sample="a, b")
        text = _render_risk_summary(risk, DisplayMode.WORKING)
        assert text is not None
        plain = text.plain
        assert "Risk:" in plain
        assert "2 pod" in plain
        assert "low" in plain
        # Sparkline glyphs are dense-only.
        assert "\u2581\u2581\u2581" not in plain

    def test_dense_includes_sparkline(self):
        risk = _RiskInfo(kind="concrete", target="pod", count=2, sample="a, b")
        text = _render_risk_summary(risk, DisplayMode.DENSE)
        assert text is not None
        plain = text.plain
        assert "\u2581\u2581\u2581" in plain
        assert "low" in plain

    def test_bounded_is_prefixed_with_le(self):
        # ≤ N target reads as an upper bound, not an exact count.
        risk = _RiskInfo(kind="bounded", target="node", count=5)
        text = _render_risk_summary(risk, DisplayMode.WORKING)
        assert text is not None
        assert "\u2264" in text.plain  # ≤
        assert "5 node" in text.plain
        assert "medium" in text.plain

    def test_unbounded_surfaces_runtime_qualifier(self):
        # Unbounded means count is determined at injection time; the
        # qualifier must be visible so the user doesn't read a missing
        # number as "0".
        risk = _RiskInfo(kind="unbounded", target="pod", descriptor="labels")
        text = _render_risk_summary(risk, DisplayMode.WORKING)
        assert text is not None
        assert "\u8fd0\u884c\u65f6\u786e\u5b9a" in text.plain  # 运行时确定


class TestBuildBodyIntegration:
    """End-to-end: the rendered panel body should expose Risk: in
    working/dense and omit it in calm."""

    def _info(self) -> dict:
        return {
            "fault_intent": {
                "fault_type": "cpu",
                "scope": "pod",
                "target": "pod",
                "action": "fullload",
                "namespace": "default",
                "names": ["worker-1", "worker-2"],
            },
            "intent_confidence": 0.9,
        }

    def test_working_renders_risk_row(self):
        body = build_body(self._info(), display_mode=DisplayMode.WORKING)
        out = _render_to_string(body)
        assert "Risk:" in out
        assert "2 pod" in out
        assert "low" in out

    def test_dense_renders_risk_row_with_sparkline(self):
        body = build_body(self._info(), display_mode=DisplayMode.DENSE)
        out = _render_to_string(body)
        assert "Risk:" in out
        assert "\u2581\u2581\u2581" in out

    def test_calm_omits_risk_row(self):
        body = build_body(self._info(), display_mode=DisplayMode.CALM)
        out = _render_to_string(body)
        # Calm strips the differentiator — no "Risk:" prefix anywhere.
        assert "Risk:" not in out
        # But the rest of the panel still renders.
        assert "Confirm & Execute" in out

    def test_default_mode_is_working(self):
        # Callers that don't pass display_mode (e.g. older code paths)
        # should get the daily-driver experience, not the calm one.
        body = build_body(self._info())
        out = _render_to_string(body)
        assert "Risk:" in out

    def test_low_confidence_warning_still_renders_with_risk(self):
        # Pin that the risk row doesn't displace the low-confidence
        # warning — both serve different purposes (count vs. parser
        # uncertainty) and the user needs to see them together.
        info = self._info()
        info["intent_confidence"] = LOW_CONFIDENCE_THRESHOLD - 0.1
        body = build_body(info, display_mode=DisplayMode.WORKING)
        out = _render_to_string(body)
        assert "Risk:" in out
        # Post-PR-A2 fix: the warning is now a subordinate ``└─`` row
        # beneath the Confidence value, with field-aware text. We pin
        # both halves so a regression that drops the leader OR the
        # field hint is caught.
        assert "└─" in out
        assert "建议逐项核对" in out


class TestLowConfidenceHint:
    """Pin the field-aware ``_low_confidence_hint`` behaviour.

    The doc §16.3 mockup envisioned an *ambiguity-candidate* hint
    (``"prod" 可能指 cms-prod 也可能指 prod-payment``). We don't have
    LLM-emitted alternatives today, so the hint instead names the
    specific field values the user should re-verify, with extra
    escalation for the ``prod`` namespace red-flag and very-low
    confidence (< 0.5).
    """

    def _intent(self, **overrides) -> dict:
        base = {
            "fault_type": "cpu-fullload",
            "scope": "pod",
            "target": "cpu",
            "action": "fullload",
            "namespace": "cms-demo",
        }
        base.update(overrides)
        return base

    def test_lists_three_identity_fields(self):
        # The user must see each of namespace / target / action with
        # its actual value — generic "please double-check" was the old
        # broken UX we're replacing.
        msg = _low_confidence_hint(self._intent(), confidence=0.6)
        assert "namespace=cms-demo" in msg
        assert "target=cpu" in msg
        assert "action=fullload" in msg

    def test_default_lead_is_softer(self):
        # 0.5 ≤ confidence < 0.7 → "建议" (suggestion).
        msg = _low_confidence_hint(self._intent(), confidence=0.6)
        assert msg.startswith("建议")
        assert not msg.startswith("强烈建议")

    def test_very_low_confidence_uses_stronger_lead(self):
        # confidence < 0.5 → "强烈建议" so the visual reads as a near-stop.
        msg = _low_confidence_hint(self._intent(), confidence=0.3)
        assert msg.startswith("强烈建议")

    def test_prod_namespace_appends_extra_warning(self):
        # The single highest-stakes signal we can detect from the
        # renderer alone — gets a dedicated tail.
        msg = _low_confidence_hint(
            self._intent(namespace="prod-payment"), confidence=0.6
        )
        assert "namespace 含 'prod' 字样" in msg
        assert "请确认非生产环境" in msg

    def test_production_namespace_also_flagged(self):
        # "production" should match the same heuristic as "prod".
        msg = _low_confidence_hint(
            self._intent(namespace="production-east"), confidence=0.6
        )
        assert "namespace 含 'prod' 字样" in msg

    def test_non_prod_namespace_no_extra_warning(self):
        # A staging / demo namespace must NOT trip the prod flag —
        # otherwise the tail becomes banner blindness.
        msg = _low_confidence_hint(
            self._intent(namespace="cms-demo"), confidence=0.6
        )
        assert "请确认非生产环境" not in msg

    def test_missing_fields_render_as_question_mark(self):
        # An intent missing namespace / target / action shouldn't
        # crash — fall back to "?" so the user still sees the shape.
        msg = _low_confidence_hint({}, confidence=0.6)
        assert "namespace=default" in msg  # default ns
        assert "target=?" in msg
        assert "action=?" in msg


class TestBuildBodyLowConfidenceLayout:
    """End-to-end: build_body composes the warning under Confidence."""

    def _info(self, confidence: float, **intent_overrides) -> dict:
        intent = {
            "fault_type": "cpu-fullload",
            "scope": "pod",
            "target": "cpu",
            "action": "fullload",
            "namespace": "cms-demo",
        }
        intent.update(intent_overrides)
        return {"fault_intent": intent, "intent_confidence": confidence}

    def test_high_confidence_omits_warning_row(self):
        body = build_body(
            self._info(confidence=0.9), display_mode=DisplayMode.WORKING
        )
        out = _render_to_string(body)
        # Confidence row is shown but no warning hint.
        assert "Confidence: 0.90" in out
        # The hint text is the unique signal — the ``└─`` leader on
        # its own would also match the table bottom border, so we
        # match the hint phrase instead.
        assert "建议逐项核对" not in out
        assert "强烈建议" not in out

    def test_low_confidence_uses_branch_leader_on_new_line(self):
        # Doc §16.3 mockup: warning is a subordinate row, not a
        # same-line tail. The hint must appear on its own line right
        # after Confidence. We search for the hint phrase rather than
        # the bare ``└─`` glyph because the table bottom border also
        # uses that glyph.
        body = build_body(
            self._info(confidence=0.6), display_mode=DisplayMode.WORKING
        )
        out = _render_to_string(body)
        lines = out.splitlines()
        conf_line = next(i for i, ln in enumerate(lines) if "Confidence:" in ln)
        warn_line = next(
            i for i, ln in enumerate(lines) if "建议逐项核对" in ln
        )
        assert warn_line == conf_line + 1, (
            "warning row must come on the line right after Confidence"
        )
        # The leader-glyph + warning-glyph combo must be on the warn
        # line itself (not somewhere else from the table border).
        assert "└─" in lines[warn_line]
        # Icons.WARNING is whatever the active palette uses; verify
        # by checking the line contains a warning marker via the
        # imported Icons.
        from chaos_agent.tui.theme import Icons

        assert Icons.WARNING in lines[warn_line]

    def test_very_low_confidence_propagates_to_panel(self):
        body = build_body(
            self._info(confidence=0.3), display_mode=DisplayMode.WORKING
        )
        out = _render_to_string(body)
        assert "强烈建议" in out

    def test_prod_namespace_warning_visible_in_panel(self):
        body = build_body(
            self._info(confidence=0.6, namespace="prod-payment"),
            display_mode=DisplayMode.WORKING,
        )
        out = _render_to_string(body)
        assert "请确认非生产环境" in out

    def test_warning_renders_in_calm_mode_too(self):
        # Calm hides the *risk* meter (the differentiator) but the
        # confidence warning is a safety signal — it must always show
        # when below threshold, regardless of density.
        body = build_body(
            self._info(confidence=0.6), display_mode=DisplayMode.CALM
        )
        out = _render_to_string(body)
        assert "Risk:" not in out
        assert "└─" in out
        assert "建议逐项核对" in out
