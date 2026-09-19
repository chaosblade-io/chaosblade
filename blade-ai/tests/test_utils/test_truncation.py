"""Shared truncation contract tests (chaos_agent.utils.truncation).

Covers the three-layer contract:
* invariant layer — three fields (marker + original size + retrieval path)
  present on every notice, and the constructor/parser duality: any notice
  built with a retrieve_path must yield that path back through
  TRUNCATION_CACHE_RE;
* guidance layer — kind variants differ where they must (strategy hints vs
  NEVER warning vs TAIL verdict vs state.baseline_data) and never bleed
  into each other;
* morphology layer — truncate_head_tail keeps both ends under a UTF-8
  byte budget; elided_preview (relocated from status_tracker) keeps its
  exact historical behavior.
"""

import pytest

from chaos_agent.utils.truncation import (
    TRUNCATION_CACHE_RE,
    TRUNCATION_MARKERS,
    apply_output_safety_valve,
    build_truncation_notice,
    elided_preview,
    truncate_head_tail,
)


class TestBuildTruncationNoticeInvariants:
    def test_unknown_kind_raises(self):
        with pytest.raises(ValueError, match="Unknown truncation notice kind"):
            build_truncation_notice("no-such-kind", 100)

    def test_three_field_invariant_across_kinds(self):
        """Marker + honestly-reported size on every kind; retrieval path
        whenever one was supplied (file-content is the whitelisted
        omission — it must still carry marker + size)."""
        for kind in ("success-output", "historical", "error", "baseline-evidence"):
            notice = build_truncation_notice(
                kind, 12345, retrieve_path="/tmp/tc/xyz.txt"
            )
            assert any(m in notice for m in TRUNCATION_MARKERS), kind
            assert "12345" in notice, kind
            assert "/tmp/tc/xyz.txt" in notice, kind

    def test_unit_is_reported_as_given(self):
        notice = build_truncation_notice(
            "success-output", 97, unit="KB", retrieve_path="/c/o.txt"
        )
        assert "original 97KB" in notice
        notice = build_truncation_notice("baseline-evidence", 2000, unit="characters")
        assert "original 2000 characters" in notice

    def test_constructor_parser_duality(self):
        """Any notice built with a retrieve_path must let the shared regex
        extract that exact path back — the wording this module emits is
        what the recover-side cache bridge parses."""
        for kind in ("success-output", "historical", "error", "baseline-evidence"):
            notice = build_truncation_notice(
                kind, 5000, retrieve_path="/tmp/tool_cache/ab12cd34.txt"
            )
            m = TRUNCATION_CACHE_RE.search(notice)
            assert m is not None, f"kind={kind}: notice carries no parseable cache wording"
            assert m.group(1) == "/tmp/tool_cache/ab12cd34.txt", kind

    def test_no_retrieve_path_leaves_no_dangling_wording(self):
        for kind in ("success-output", "historical", "error", "baseline-evidence"):
            notice = build_truncation_notice(kind, 500)
            assert TRUNCATION_CACHE_RE.search(notice) is None, kind


class TestBuildTruncationNoticeKindVariants:
    def test_success_output_carries_narrowing_strategies(self):
        notice = build_truncation_notice(
            "success-output", 97000, unit="KB", retrieve_path="/c/out.txt"
        )
        assert "⚠️ OUTPUT_TRUNCATED" in notice
        assert "Full output cached at: /c/out.txt" in notice
        for hint in (
            "--field-selector",
            "-o name",
            "jsonpath",
            "single resource",
        ):
            assert hint in notice

    def test_historical_carries_never_warning(self):
        notice = build_truncation_notice(
            "historical", 100_000, retrieve_path="/tmp/cache/out.txt"
        )
        assert "⚠️ TRUNCATED" in notice
        assert "Cache: /tmp/cache/out.txt" in notice
        assert "NEVER execute a destructive or structural change" in notice
        # Action directive: command-AGNOSTIC narrowing. The producing
        # command is visible to the model in the tool call above, so
        # the notice points at deriving a narrower re-run from it —
        # never a hardcoded tool flag list (#13-R: any fixed example
        # is wrong for some command family).
        assert "re-run the command that produced it" in notice
        assert "--no-headers" not in notice
        # historical keeps the original size (three-field skeleton)
        assert "100000 bytes" in notice

    def test_error_points_at_tail_and_future_compactor_cache(self):
        notice = build_truncation_notice("error", 70_000)
        assert "⚠️ OUTPUT_TRUNCATED" in notice
        assert "TAIL" in notice
        assert "compactor" in notice
        # error guidance never borrows the success re-query strategies
        assert "--field-selector" not in notice

    def test_error_with_retrieve_path_never_contradicts_itself(self):
        """A caller-supplied cache path and the "no cache is written at
        the tool layer" explanation must NEVER co-occur — the notice
        would state a cache exists and deny it in the same breath."""
        notice = build_truncation_notice(
            "error", 70_000, retrieve_path="/tmp/tool_cache/ab12.txt"
        )
        assert "Full output cached at: /tmp/tool_cache/ab12.txt" in notice
        assert "No cache is written" not in notice

    def test_success_output_strategy_hint_replaces_kubectl_strategies(self):
        """The default success strategies are kubectl narrowing flags;
        a non-kubectl caller (skill script executor) swaps them via
        strategy_hint — kubectl jsonpath advice on a Python script's
        stdout would be actively misleading."""
        notice = build_truncation_notice(
            "success-output", 70_000, unit="bytes",
            strategy_hint="narrow the script's output or redirect to a file",
        )
        assert "Do NOT repeat the same query!" in notice  # universal header kept
        assert "narrow the script's output or redirect to a file" in notice
        assert "--field-selector" not in notice
        assert "jsonpath" not in notice

    def test_success_output_never_borrows_error_wording(self):
        notice = build_truncation_notice(
            "success-output", 5000, retrieve_path="/c/o.txt"
        )
        assert "safety valve" not in notice

    def test_baseline_evidence_points_at_state(self):
        notice = build_truncation_notice(
            "baseline-evidence", 2000, unit="characters"
        )
        assert "⚠️ TRUNCATED" in notice
        assert "state.baseline_data" in notice
        assert "compactor" not in notice

    def test_file_content_whitelisted_retrieval_omission(self):
        """file-content omits the retrieval path BY WHITELIST: re-reading
        the same file yields the same capped read — a retrieval path
        would promise what the tool cannot deliver."""
        notice = build_truncation_notice(
            "file-content", 60_000, strategy_hint="showing first 51200 bytes"
        )
        assert "⚠️ TRUNCATED" in notice
        assert "showing first 51200 bytes" in notice
        assert "60000 bytes" in notice
        assert "cached" not in notice
        assert "state.baseline_data" not in notice

    def test_file_content_default_hint_without_strategy(self):
        notice = build_truncation_notice("file-content", 60_000)
        assert "read cap" in notice

    def test_whitelist_omitted_kind_rejects_retrieve_path(self):
        """The omission whitelist is ENFORCED: passing a retrieval path
        for an omitted kind is a contract violation (it would promise
        what the tool cannot deliver) — fail fast, never silently drop."""
        with pytest.raises(ValueError, match="whitelist"):
            build_truncation_notice(
                "file-content", 60_000, retrieve_path="/etc/hosts"
            )


class TestTruncateHeadTail:
    def test_within_budget_passthrough(self):
        assert truncate_head_tail("short text", 1024) == "short text"
        assert truncate_head_tail("", 100) == ""

    def test_runaway_output_cut_keeps_both_ends(self):
        head = "HEAD" * 50
        tail = "TAIL" * 50
        text = head + "MIDDLE" * 5000 + tail
        out = truncate_head_tail(text, 2000)
        assert out.startswith("HEAD")
        assert out.endswith("TAIL" * 50)
        assert "bytes elided" in out
        # budget contract: returned text stays within the ceiling
        assert len(out.encode("utf-8")) <= 2000

    def test_multibyte_utf8_cuts_are_character_safe(self):
        text = "故" * 10_000  # 3 bytes per char
        out = truncate_head_tail(text, 900)
        assert len(out.encode("utf-8")) <= 900
        assert "bytes elided" in out
        # no mojibake: the head/tail segments decode as whole characters
        # (a byte cut inside a multi-byte char must drop it, not mangle it)
        lines = out.split("\n")
        assert set(lines[0]) == {"故"}
        assert set(lines[-1]) == {"故"}

    def test_head_ratio_controls_split(self):
        text = "A" * 100_000
        out = truncate_head_tail(text, 10_000, head_ratio=0.5)
        lines = out.split("\n")
        head_part, marker, tail_part = lines[0], lines[1], lines[2]
        assert "bytes elided" in marker
        # 50/50 split within the marker reserve slack
        assert abs(len(head_part) - len(tail_part)) <= 64

    def test_degenerate_ceiling_falls_back_to_head_cut(self):
        text = "abcdefgh" * 10
        out = truncate_head_tail(text, 10)
        # budget ≤ 0 after marker reserve: honest head cut, no fake marker
        assert out == text[:10]


class TestApplyOutputSafetyValve:
    """Budget contract: the notice counts against the ceiling — the same
    semantics the compactor enforces (truncate_budget = max_bytes -
    notice_bytes), so the RETURNED message stays within max_bytes."""

    def test_within_budget_byte_identical_passthrough(self):
        text = "x" * 1000
        out = apply_output_safety_valve(text, 4096, kind="error")
        assert out is text  # untouched object, zero overhead

    def test_empty_text_passthrough(self):
        assert apply_output_safety_valve("", 4096, kind="error") == ""

    def test_over_budget_notice_counts_against_ceiling(self):
        """The returned message (cut + notice) must stay within the
        ceiling for both kinds — an appended-on-top notice would return
        max_bytes + ~0.7KB and make the ceiling lie by 1%."""
        for kind in ("error", "success-output"):
            text = "H" * 500 + "M" * (300 * 1024) + "T" * 500
            out = apply_output_safety_valve(text, 64 * 1024, kind=kind)
            assert len(out.encode("utf-8")) <= 64 * 1024, kind
            assert out.startswith("H" * 10), kind      # head kept
            # the kept TAIL segment sits between the elided middle and
            # the appended notice (the notice is what ends the message)
            assert "T" * 100 in out, kind              # tail segment kept
            assert "bytes elided" in out, kind         # quantified middle

    def test_kind_routes_guidance(self):
        text = "m" * (100 * 1024)
        err = apply_output_safety_valve(text, kind="error")
        ok = apply_output_safety_valve(text, kind="success-output")
        assert "TAIL" in err and "--field-selector" not in err
        assert "--field-selector" in ok and "TAIL" not in ok

    def test_degenerate_tiny_ceiling_keeps_visible_cut(self):
        """Notice larger than half the budget: the half-budget floor
        keeps an honest visible cut (compactor semantics) instead of a
        zero-length body; the overrun is bounded by the notice."""
        text = "a" * 5000
        out = apply_output_safety_valve(text, 600, kind="error")
        assert out.startswith("a")
        assert any(m in out for m in TRUNCATION_MARKERS)
        # The "overrun is bounded by the notice" promise, ENFORCED: in
        # the floor regime the returned message may exceed max_bytes,
        # but by strictly less than the notice's own byte length — the
        # ceiling degrades gracefully, never unboundedly. The TINY
        # ceiling (notice 353B > the whole 200B budget) is the load-
        # bearing case: without the half-budget floor, budget would go
        # NEGATIVE (200-353=-153) and the byte-slice would take it as a
        # negative index — cutting 153B off the TAIL and returning a
        # ~5200B "truncated" message (overrun 5000, unbounded).
        notice_alone = build_truncation_notice("error", 5000)
        notice_len = len(notice_alone.encode("utf-8"))
        for ceiling in (600, 200):
            out = apply_output_safety_valve(text, ceiling, kind="error")
            overrun = len(out.encode("utf-8")) - ceiling
            assert overrun < notice_len, ceiling


class TestElidedPreviewRelocated:
    """Regression for the relocation from status_tracker: semantics,
    marker format, and signature are unchanged."""

    def test_short_text_passthrough(self):
        assert elided_preview("hello world", 10, 10) == "hello world"

    def test_empty_string(self):
        assert elided_preview("", 10, 10) == ""

    def test_exact_boundary_passthrough(self):
        text = "x" * 30
        assert elided_preview(text, 15, 15) == text

    def test_marker_format_and_both_ends(self):
        text = "H" * 10 + "M" * 100 + "T" * 10
        out = elided_preview(text, 10, 10)
        assert out == "H" * 10 + "\n...[100 chars elided]...\n" + "T" * 10

    def test_reexport_from_status_tracker(self):
        """The 8 existing call sites import elided_preview from
        status_tracker — the thin re-export must keep working."""
        from chaos_agent.observability.status_tracker import elided_preview as reexported

        assert reexported is elided_preview


class TestStateEvidenceKind:
    """truncation-debt-cleanup (task 1.2): the state-evidence kind serves
    state-side evidence injections (side_effects, experience docs) whose
    full content lives somewhere the caller can describe. Pins the
    three-field invariant, the mandatory state_hint enforcement, and the
    separation from both the cache parse surface and baseline-evidence."""

    def test_three_field_invariant(self):
        """Marker from the family + honest original size in the caller's
        unit + the state_hint retrieval guidance — all three present."""
        notice = build_truncation_notice(
            "state-evidence", 4321,
            state_hint="Full side-effect records preserved in state.side_effects",
            unit="characters",
        )
        # Field 1 — machine-parseable marker from the family.
        assert "⚠️ TRUNCATED (state evidence)" in notice
        # Field 2 — honest original size in the caller's unit.
        assert "(original 4321 characters)" in notice
        # Field 3 — the retrieval guidance the caller promised.
        assert (
            "Full side-effect records preserved in state.side_effects"
            in notice
        )

    def test_missing_state_hint_raises(self):
        """state_hint IS the third field for this kind: an omitted hint
        must fail fast, not silently ship a no-way-back notice."""
        with pytest.raises(ValueError, match="state_hint"):
            build_truncation_notice("state-evidence", 100, unit="bytes")

    def test_stray_state_hint_on_other_kind_raises(self):
        """Symmetric enforcement (same style as the retrieve_path
        whitelist): only state-evidence may carry state_hint."""
        with pytest.raises(ValueError, match="does not take state_hint"):
            build_truncation_notice(
                "historical", 100, state_hint="somewhere", unit="bytes",
            )

    def test_not_on_cache_parse_surface(self):
        """state-evidence has NO cache path — the notice must not match
        TRUNCATION_CACHE_RE. Contract self-consistency: the notice kinds
        are type-separated by their retrieval home, and a state hint
        worded like a cache-file reference would make the notice family
        ambiguous for any future consumer of the shared pattern (the
        recover-side parser was retired in round-41; the round-trip
        duality tests keep wording and pattern locked as a pair)."""
        notice = build_truncation_notice(
            "state-evidence", 999,
            state_hint="Full side-effect records preserved in state.side_effects",
            unit="characters",
        )
        assert TRUNCATION_CACHE_RE.search(notice) is None

    def test_wording_mutually_exclusive_with_baseline_evidence(self):
        """A state-evidence notice must never claim baseline_data (and
        vice versa): the two kinds describe DIFFERENT retrieval homes,
        and a cross-leak would misdirect the model to the wrong store."""
        se = build_truncation_notice(
            "state-evidence", 999,
            state_hint="Full side-effect records preserved in state.side_effects",
            unit="characters",
        )
        assert "baseline_data" not in se
        be = build_truncation_notice("baseline-evidence", 999, unit="characters")
        assert "side_effects" not in be
        assert "state evidence" not in be  # marker annotations stay distinct
