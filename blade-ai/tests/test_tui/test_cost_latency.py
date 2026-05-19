"""Tests for PR-E8 — cost + latency tracking on SessionState.

The footer renders ``$X.XX`` and ``p95 N.Ns`` columns. Behaviour pinned:

1. ``add_tokens`` rolls into ``usd_cost`` using the active model's
   pricing — no separate "remember to also add cost" call.
2. Pricing falls back to a mid-tier rate for unknown models so the
   cost number still moves on long-running sessions.
3. Date-stamped model variants (e.g. ``claude-opus-4-7-20250101``)
   resolve to the base model's price via prefix match.
4. ``latency_p95_ms`` returns 0 with no samples and a sensible
   number with one (no zero-division), and trims to the rolling
   buffer cap so a long session doesn't leak memory.
5. The toolbar suppresses the cost column under one cent and the
   latency column with no samples, so the default-state footer
   stays narrow.
"""

from __future__ import annotations

from chaos_agent.tui.pricing import resolve_pricing, _DEFAULT
from chaos_agent.tui.state import SessionState


class TestPricingTable:
    def test_known_model_returns_table_rate(self):
        # claude-opus-4-7 is in the table; the default would be (.003,.015).
        rate = resolve_pricing("claude-opus-4-7")
        assert rate == (0.015, 0.075)

    def test_unknown_model_falls_back_to_default(self):
        rate = resolve_pricing("model-that-does-not-exist")
        assert rate == _DEFAULT

    def test_dated_variant_matches_via_prefix(self):
        # API providers commonly suffix with a date — the resolver must
        # strip the suffix and find the base rate. Otherwise every model
        # release would silently fall to the default rate.
        rate = resolve_pricing("claude-opus-4-7-20250101")
        assert rate == (0.015, 0.075)

    def test_empty_string_returns_default(self):
        # Cost accounting fires before the model name is known on the
        # very first call — must not blow up.
        rate = resolve_pricing("")
        assert rate == _DEFAULT


class TestCostAccumulation:
    def test_add_tokens_also_rolls_cost(self):
        state = SessionState()
        state.set_model_name("claude-opus-4-7")
        state.add_tokens(input_tokens=1000, output_tokens=500)
        # 1k * 0.015 + 0.5k * 0.075 = 0.015 + 0.0375 = 0.0525
        assert abs(state.usd_cost - 0.0525) < 1e-9

    def test_zero_tokens_does_not_add_cost(self):
        state = SessionState()
        state.set_model_name("claude-opus-4-7")
        state.add_tokens(0, 0)
        assert state.usd_cost == 0.0

    def test_cost_accumulates_across_calls(self):
        state = SessionState()
        state.set_model_name("claude-opus-4-7")
        state.add_tokens(1000, 0)  # 0.015
        state.add_tokens(0, 1000)  # 0.075
        assert abs(state.usd_cost - 0.090) < 1e-9


class TestLatencyP95:
    def test_no_samples_returns_zero(self):
        # Footer relies on this to suppress the column entirely on the
        # very first turn — must not blow up with a IndexError on empty.
        assert SessionState().latency_p95_ms() == 0

    def test_single_sample_is_its_own_p95(self):
        state = SessionState()
        state.record_turn_latency_ms(1234)
        assert state.latency_p95_ms() == 1234

    def test_p95_picks_tail_value(self):
        state = SessionState()
        # 100 samples 1..100 — p95 should be near the top of the range.
        for ms in range(1, 101):
            state.record_turn_latency_ms(ms)
        # Buffer caps at 50, so only the most recent 50 (51..100) survive.
        # p95 over those is the 47th index of sorted (50 elements * .95 - 1).
        result = state.latency_p95_ms()
        assert 90 <= result <= 100

    def test_zero_or_negative_latency_ignored(self):
        state = SessionState()
        state.record_turn_latency_ms(0)
        state.record_turn_latency_ms(-1)
        # Neither was recorded — buffer is empty, p95 is 0.
        assert state.latencies_ms == []
        assert state.latency_p95_ms() == 0

    def test_buffer_caps_at_max_samples(self):
        state = SessionState()
        for ms in range(1, 200):
            state.record_turn_latency_ms(ms)
        # Hard cap so a long session doesn't grow unbounded.
        assert len(state.latencies_ms) == state._latency_max_samples
        # The window slid forward — first sample should be high, not 1.
        assert state.latencies_ms[0] > 100


class TestEndTurnRecordsLatency:
    def test_end_turn_appends_latency_sample(self, monkeypatch):
        state = SessionState()
        # Drive the wall clock manually so the test is deterministic.
        clock = [1000.0]

        def fake_time():
            return clock[0]

        monkeypatch.setattr("chaos_agent.tui.state.time.time", fake_time)
        state.start_turn()
        clock[0] += 1.5  # 1500ms turn
        elapsed = state.end_turn()
        assert abs(elapsed - 1.5) < 1e-9
        assert state.latencies_ms == [1500]
