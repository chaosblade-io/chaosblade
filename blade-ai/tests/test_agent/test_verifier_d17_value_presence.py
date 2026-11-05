"""D17 — value-presence oracle for absent-key metric claims.

``cross_check_evidence`` cross-checks the LLM's structured
``metric_claims`` against ``state.metric_observations`` (the key-space
timeline ``extract_metrics`` parses from a FIXED vocabulary). A free-form
exec read (``ping``, ``tc -s qdisc``, ``ss``, ``iptables -L``,
``/proc/net/*``) yields ``extract_metrics() == {}`` so it NEVER enters the
timeline — its metric keys are structurally absent. Before D17, absence was
symmetric between an extractor coverage gap and a genuine hallucination, so
every netem-class claim (#3-R3) landed on the warn-only unknown-key branch
with a misleading "hallucinated metric or extractor coverage gap" note.

D17 breaks that symmetry in the VALUE space: the hook harvests a
format-agnostic fingerprint of every raw observation output (full text, before
the 1KB compactor truncation) into ``state.evidence_fingerprints`` =
``{tool_call_id: {tokens, numeric_fields, complete, tool_name, command}}``.
ONE harvest carries TWO views because the two inferences need OPPOSITE biases:
``tokens`` is deliberately over-inclusive and serves the ABSENCE proof (T3) —
narrowing it would manufacture false accusations (D12); ``numeric_fields`` is
the subset the output delimits as standalone numbers and is the ONLY corpus
that may certify PRESENCE (T2) — narrowing it can only move a claim
corroborated→abstain, never →fabricated. Conflating them was the defect: a hex
UID fragment (``932`` of ``932eaa6bd493bf54``) or a date artifact (``-10`` of
``2026-10-27``) satisfied the same membership test a real metric satisfies, so
an incidental collision certified a hallucinated value as REAL and suppressed
T3. An absent-key claim is resolved by ``_classify_unknown_claim``:

  - T2 corroborated: the post value is present in SOME harvested output AS A
    STANDALONE NUMBER → the number is REAL (coverage gap, not hallucination).
    Positive note, NEVER downgrades.
  - T2-weak abstain: the value's digits occur ONLY inside a longer non-numeric
    token (UID / timestamp fragment), or the fingerprint predates
    field-grading → too weak to certify reality, and it defeats the absence
    proof too. Coverage-gap warning, NEVER downgrades.
  - T3 fabricated: the claim declares ``kind=raw`` + a ``source`` anchoring
    to EXACTLY ONE complete harvest, and the value is in NO output's
    ``tokens`` → deterministic fabrication. MAY downgrade.
  - abstain: everything else (no fingerprint, derived/unmarked, unmapped or
    ambiguous source, incomplete harvest) → honest coverage-gap warning,
    never downgrades (D12 fail-open danger asymmetry).

Fixtures use the real #3-R3 netem-duplicate values (``tc -s qdisc`` output)
plus a chaosblade-log shape carrying a hex UID and ISO timestamps.
"""
from __future__ import annotations

import pytest

from chaos_agent.agent.nodes.verify._metric_extractor import (
    _FINGERPRINT_TOKEN_CAP,
    canonicalize_number,
    harvest_evidence_fingerprint,
)
from chaos_agent.agent.nodes.verify._verifier_layer2_parse import (
    _classify_unknown_claim,
    _norm_source,
    _parse_claim_number,
    cross_check_evidence,
)

# Real #3-R3 netem-duplicate ``tc -s qdisc show`` output (kubectl exec).
# extract_metrics returns {} for this — no netem branch — so its numbers
# only reach cross_check via the D17 fingerprint.
_NETEM_CMD = "kubectl exec drill-c3-dup-0 -n default -- tc -s qdisc show dev eth0"
_NETEM_OUT = (
    "qdisc netem 8001: dev eth0 root refcnt 2 limit 1000 duplicate 30%\n"
    " Sent 369 bytes 3 pkt (errored 0, dropped 10, overlimits 0, requeues 0)\n"
    " backlog 0b 0p requeues 0\n"
)

# A chaosblade log read back over host_read — the real-world shape that exposed
# the conflation: a hex experiment UID and ISO timestamps whose digit runs are
# NOT metrics, yet satisfied the same membership test a real metric satisfies.
_UID_CMD = "cat /var/log/chaosblade.log"
_UID_OUT = (
    "2026-10-27 07:34:40 INFO create experiment uid: 932eaa6bd493bf54\n"
    "2026-10-27 07:34:41 INFO destroy ok\n"
)


def _fp(content=_NETEM_OUT, tool="kubectl", cmd=_NETEM_CMD, complete=True) -> dict:
    fp = harvest_evidence_fingerprint(content, tool, cmd)
    if not complete:  # simulate a token-capped (unprovable) harvest
        fp["complete"] = False
    return fp


def _fps(*entries: dict) -> dict:
    return {f"tc-{i}": fp for i, fp in enumerate(entries, start=1)}


# ---------------------------------------------------------------------------
# canonicalize_number — shared by harvest (hook) and compare (cross_check)
# ---------------------------------------------------------------------------


class TestCanonicalizeNumber:
    def test_int_and_equivalent_forms_agree(self):
        # The whole point: a claim's post (int 369) must match a harvested
        # text token ('369' / '369.0' / '369.00').
        assert canonicalize_number(369) == "369"
        assert canonicalize_number(369.0) == "369"
        assert canonicalize_number("369") == "369"
        assert canonicalize_number("369.0") == "369"
        assert canonicalize_number("369.00") == "369"

    def test_fraction_strips_trailing_zeros(self):
        assert canonicalize_number("0.053") == "0.053"
        assert canonicalize_number("0.0530") == "0.053"
        assert canonicalize_number(0.053) == "0.053"

    def test_negative_and_signed(self):
        assert canonicalize_number(-10) == "-10"
        assert canonicalize_number("-10") == "-10"

    def test_non_numeric_returns_none(self):
        assert canonicalize_number("OOMKilled") is None
        assert canonicalize_number("x") is None
        assert canonicalize_number(None) is None
        assert canonicalize_number("") is None

    def test_nan_and_inf_return_none(self):
        assert canonicalize_number(float("nan")) is None
        assert canonicalize_number(float("inf")) is None
        assert canonicalize_number(float("-inf")) is None


# ---------------------------------------------------------------------------
# _parse_claim_number — unit-tolerant numeric parse of a claim base/post (fix b)
# ---------------------------------------------------------------------------


class TestParseClaimNumber:
    """Fix (b): the D17 cross-check must key on a claim's numeric VALUE, not on
    whether the LLM happened to append a unit. Pre-fix, ``float("1014.755 ms")``
    raised so the whole layer went inert for unit-suffixed claims (R2 live) — the
    identical drill cross-checked in one run, silently skipped in the next on pure
    output-formatting variance. A genuinely non-numeric base/post must still yield
    ``None`` so the claim is skipped exactly as before."""

    @pytest.mark.parametrize(("value", "expected"), [
        (1014.755, 1014.755),        # bare float passthrough
        (369, 369.0),                # bare int passthrough
        ("1012.525", 1012.525),      # numeric string
        ("1014.755 ms", 1014.755),   # unit-suffixed (the R2 shape)
        ("0%", 0.0),                 # percent unit
        ("6.433 ms", 6.433),
        ("-5.2 ms", -5.2),           # signed with unit
        ("+3 dB", 3.0),
        ("1e3 bytes", 1000.0),       # scientific notation with unit
        ("  42  ", 42.0),            # surrounding whitespace
    ])
    def test_numeric_and_unit_suffixed_parse(self, value, expected):
        assert _parse_claim_number(value) == pytest.approx(expected)

    @pytest.mark.parametrize("value", [
        "OOMKilled",                  # no leading digit
        "sub-ms healthy expectation", # prose, not a measurement
        "",                           # empty
        None,                         # absent
        True,                         # bool rejected (isinstance(True, int))
        False,
        float("nan"),
        float("inf"),
        float("-inf"),
        ["1"],                        # wrong type
        {"v": 1},
    ])
    def test_non_numeric_returns_none(self, value):
        assert _parse_claim_number(value) is None


# ---------------------------------------------------------------------------
# harvest_evidence_fingerprint — format-agnostic value harvest
# ---------------------------------------------------------------------------


class TestHarvestEvidenceFingerprint:
    def test_netem_output_harvests_values(self):
        # #3-R3: the decisive '369' (Sent bytes) and '10' (dropped) must be
        # present even though extract_metrics returns {} for this shape.
        fp = harvest_evidence_fingerprint(_NETEM_OUT, "kubectl", _NETEM_CMD)
        assert "369" in fp["tokens"]
        assert "10" in fp["tokens"]
        assert fp["complete"] is True
        assert fp["tool_name"] == "kubectl"
        assert fp["command"] == _NETEM_CMD

    def test_format_agnostic_ss_output(self):
        # Generality (b5c0af28): a DIFFERENT free-form shape still harvests —
        # no per-format branch, unlike extract_metrics.
        out = (
            "State  Recv-Q Send-Q  Local Address:Port  Peer Address:Port\n"
            "ESTAB  0      0       10.244.1.5:8080     10.244.2.9:41632\n"
        )
        fp = harvest_evidence_fingerprint(out, "kubectl_read", "ss -tan")
        assert "8080" in fp["tokens"]
        assert "41632" in fp["tokens"]
        assert fp["complete"] is True

    def test_format_agnostic_iptables_counters(self):
        out = (
            "Chain INPUT (policy ACCEPT 0 packets, 0 bytes)\n"
            " pkts bytes target prot opt in out source destination\n"
            "  416 297K DROP  all  --  *  *   0.0.0.0/0  0.0.0.0/0\n"
        )
        fp = harvest_evidence_fingerprint(out, "kubectl", "iptables -L -v -n")
        assert "416" in fp["tokens"]
        assert fp["complete"] is True

    def test_empty_content_is_valid_numberless_harvest(self):
        fp = harvest_evidence_fingerprint("", "kubectl", "cmd")
        assert fp == {
            "tokens": [], "numeric_fields": [], "complete": True,
            "tool_name": "kubectl", "command": "cmd",
        }

    def test_json_serializable_tokens_are_list(self):
        # tokens MUST be a list (not a set) to survive checkpoint serialization.
        fp = harvest_evidence_fingerprint(_NETEM_OUT, "kubectl", _NETEM_CMD)
        assert isinstance(fp["tokens"], list)
        assert isinstance(fp["numeric_fields"], list)
        import json

        json.dumps(fp)  # must not raise

    def test_token_cap_marks_incomplete(self):
        # Beyond the cap the output is so number-dense that absence is
        # unprovable → complete=False so T3 abstains.
        big = " ".join(str(i) for i in range(_FINGERPRINT_TOKEN_CAP + 50))
        fp = harvest_evidence_fingerprint(big, "kubectl", "cmd")
        assert fp["complete"] is False
        assert len(fp["tokens"]) == _FINGERPRINT_TOKEN_CAP

    def test_numeric_fields_is_a_subset_of_tokens(self):
        # The strict view may only ever NARROW the over-inclusive one. If a
        # field were not also a token, presence and absence would disagree
        # about the same corpus and T3 could fire on a value T2 just certified.
        for content, cmd in ((_NETEM_OUT, _NETEM_CMD), (_UID_OUT, _UID_CMD)):
            fp = harvest_evidence_fingerprint(content, "t", cmd)
            assert set(fp["numeric_fields"]) <= set(fp["tokens"])

    def test_token_cap_also_caps_numeric_fields(self):
        # Both views ride the same checkpoint-bloat guard.
        big = " ".join(str(i) for i in range(_FINGERPRINT_TOKEN_CAP + 50))
        fp = harvest_evidence_fingerprint(big, "kubectl", "cmd")
        assert len(fp["numeric_fields"]) <= _FINGERPRINT_TOKEN_CAP


class TestNumericFieldGrading:
    """``numeric_fields`` = the numbers the output PRESENTS as numbers.

    This is the presence-certificate corpus (T2). ``tokens`` stays exhaustive
    for the absence proof (T3); the split is what stops an incidental digit
    collision from vouching for a hallucinated value.
    """

    def test_netem_values_are_all_presented_as_numbers(self):
        # D17's raison d'etre must survive the narrowing: every value the netem
        # fix certifies is a delimiter-bounded field, so no legitimate
        # corroboration is lost.
        fp = harvest_evidence_fingerprint(_NETEM_OUT, "kubectl", _NETEM_CMD)
        for value in ("369", "30", "10", "3", "0", "8001", "1000", "2"):
            assert value in fp["numeric_fields"], value

    @pytest.mark.parametrize(("text", "value"), [
        ("Sent 3005ms delay", "3005"),   # multi-letter unit suffix
        ("backlog 0b 0p", "0"),          # single-letter unit
        ("84B icmp_seq", "84"),          # uppercase single-letter unit
        ("4pkts/336bytes", "336"),       # slash-joined counters
        ("4pkts/336bytes", "4"),
        ("usage 95.00%", "95"),          # percent
        ("avail 1.7G", "1.7"),           # fraction + magnitude suffix
        ("Max open files 1024 1048575 files", "1048575"),
        ("rtt 0.053/0.071 ms", "0.053"),
        ("ESTAB 0 0 10.244.1.5:8080", "8080"),
    ])
    def test_delimited_and_unit_suffixed_shapes_are_fields(self, text, value):
        fp = harvest_evidence_fingerprint(text, "t", "c")
        assert value in fp["numeric_fields"]

    @pytest.mark.parametrize(("text", "value"), [
        ("delta: -5 ms", "-5"),
        ("(-3)", "-3"),
        ("offset -1200 sec", "-1200"),
    ])
    def test_genuine_negative_metrics_are_still_fields(self, text, value):
        # Guard against OVER-narrowing: the date-artifact rule keys on the sign
        # being preceded by an identifier character, not on negatives as such.
        # A real signed metric must keep its presence certificate.
        fp = harvest_evidence_fingerprint(text, "t", "c")
        assert value in fp["numeric_fields"]

    def test_hex_uid_fragments_are_not_fields_but_stay_tokens(self):
        fp = harvest_evidence_fingerprint(_UID_OUT, "host_read", _UID_CMD)
        for frag in ("932", "493", "54", "6"):
            assert frag not in fp["numeric_fields"], frag
            assert frag in fp["tokens"], frag  # T3's corpus must NOT narrow

    def test_iso_date_sign_artifacts_are_not_fields(self):
        # ``2026-10-27`` matches ``-10``/``-27``: the sign is a separator, so a
        # negative "metric" that never existed could be certified as REAL.
        fp = harvest_evidence_fingerprint(_UID_OUT, "host_read", _UID_CMD)
        for frag in ("-10", "-27"):
            assert frag not in fp["numeric_fields"], frag
            assert frag in fp["tokens"], frag

    def test_interior_compound_literal_component_is_not_a_field(self):
        # ``34`` of ``07:34:40`` is wedged between two joiners — an interior
        # component of a compound literal, not a reported value. The end
        # components stay fields (conservative direction).
        fp = harvest_evidence_fingerprint("07:34:40 INFO ready 12", "t", "c")
        assert "34" not in fp["numeric_fields"]
        assert "34" in fp["tokens"]
        # End components keep their certificate. (canonicalize_number folds
        # "07" to "7" — the canonical form both harvest and compare agree on.)
        assert "7" in fp["numeric_fields"]
        assert "40" in fp["numeric_fields"]
        assert "12" in fp["numeric_fields"]

    def test_ping_rtt_slash_tuple_avg_and_max_are_fields(self):
        # A1 (netdelay) real shape: the ping summary reports FOUR independent
        # measurements min/avg/max/mdev. avg and max are '/'-flanked interior
        # DECIMALS — genuine reported values, the two most-cited latency stats.
        # Demoting them (the pre-fix behaviour) lost a legitimate corroboration
        # D17 exists to prove; every network drill cites them.
        out = (
            "--- 223.5.5.5 ping statistics ---\n"
            "5 packets transmitted, 5 received, 0% packet loss, time 4006ms\n"
            "rtt min/avg/max/mdev = 1008.584/1012.525/1019.390/4.092 ms\n"
        )
        fp = harvest_evidence_fingerprint(out, "host_read", "ping -c 5 223.5.5.5")
        # avg and max (interior decimals) now keep their presence certificate.
        assert "1012.525" in fp["numeric_fields"]  # avg
        assert "1019.39" in fp["numeric_fields"]   # max (canonicalized)
        # The end components were always fields; the fix must not regress them.
        assert "1008.584" in fp["numeric_fields"]  # min
        assert "4.092" in fp["numeric_fields"]     # mdev
        # tokens stays exhaustive regardless (T3 absence-proof corpus).
        assert "1012.525" in fp["tokens"]

    def test_ping_rtt_claim_is_corroborated_not_t2_weak(self):
        # End-to-end through the D17 oracle: a cited avg rtt whose key never
        # entered the timeline must resolve to T2 corroborated (positive note,
        # NOT downgrading), not the pre-fix T2-weak coverage-gap warning.
        out = "rtt min/avg/max/mdev = 1008.584/1012.525/1019.390/4.092 ms\n"
        fps = _fps(harvest_evidence_fingerprint(out, "host_read", "ping 223.5.5.5"))
        cls, note = _classify_unknown_claim(
            {"metric": "rtt avg to 223.5.5.5 (ms)", "baseline": 14.695,
             "post": 1012.525},
            "rtt avg to 223.5.5.5 (ms)", 14.695, 1012.525, fps,
        )
        assert cls == "corroborated"
        assert "NOT downgrading" in note

    @pytest.mark.parametrize(("text", "value"), [
        ("2026/10/27", "10"),      # slash-joined date, integer component
        ("ratio 3/100/7 total", "100"),  # slash-joined integers, not a value list
    ])
    def test_slash_joined_integer_compound_stays_demoted(self, text, value):
        # Guard against OVER-broadening the carve-out: only a DECIMAL component
        # is a value-list entry. A '/'-joined integer compound literal (a date,
        # an integer ratio run) keeps its interior components demoted, so the
        # fix cannot certify a date/ratio fragment as a reported metric.
        fp = harvest_evidence_fingerprint(text, "t", "c")
        assert value not in fp["numeric_fields"]
        assert value in fp["tokens"]

    @pytest.mark.parametrize(("text", "value"), [
        ("abc123", "123"),
        ("max_open_files_64", "64"),
        ("v1.2", "1.2"),
        ("uid=a1b2c3d4", "1"),
    ])
    def test_identifier_embedded_digits_are_not_fields(self, text, value):
        fp = harvest_evidence_fingerprint(text, "t", "c")
        assert value not in fp["numeric_fields"]
        assert value in fp["tokens"]


# ---------------------------------------------------------------------------
# _norm_source — soft substring anchoring for T3
# ---------------------------------------------------------------------------


class TestNormSource:
    def test_case_folds(self):
        assert _norm_source("TC -S QDISC") == "tc -s qdisc"

    def test_collapses_internal_whitespace(self):
        assert _norm_source("tc   -s\tqdisc") == "tc -s qdisc"

    def test_empty_and_none(self):
        assert _norm_source("") == ""
        assert _norm_source(None) == ""

    def test_llm_echoed_fragment_matches_stored_command(self):
        # The LLM cannot reproduce a full kubectl invocation; a distinctive
        # fragment must still anchor via substring on the normalized form.
        assert _norm_source("tc -s qdisc") in _norm_source(_NETEM_CMD)


# ---------------------------------------------------------------------------
# _classify_unknown_claim — the D17 oracle (unit level)
# ---------------------------------------------------------------------------


class TestClassifyUnknownClaim:
    def test_t2_corroborated_value_present(self):
        # The netem fix: '369' is absent from the key-space timeline but
        # present in the harvested output → corroborated, NOT downgraded.
        cls, note = _classify_unknown_claim(
            {"metric": "Sent bytes", "baseline": 300, "post": 369},
            "Sent bytes", 300, 369, _fps(_fp()),
        )
        assert cls == "corroborated"
        assert "369" in note
        assert "NOT downgrading" in note

    def test_t2_no_fingerprints_abstains(self):
        cls, note = _classify_unknown_claim(
            {"metric": "Sent bytes", "baseline": 300, "post": 369},
            "Sent bytes", 300, 369, None,
        )
        assert cls == "abstain"
        assert "out-of-band" in note  # B-minimal honest coverage-gap text

    def test_t2_non_numeric_post_abstains(self):
        cls, _ = _classify_unknown_claim(
            {"metric": "State", "baseline": "x", "post": "y"},
            "State", "x", "y", _fps(_fp()),
        )
        assert cls == "abstain"

    def test_t2_unit_suffixed_post_corroborates(self):
        # Fix (b) end-to-end at the oracle: a ping avg rtt cited WITH its unit
        # ("1012.525 ms") must resolve identically to the bare number — T2
        # corroborated. Pre-fix, canonicalize_number("1012.525 ms") → float()
        # raised → None → the claim abstained (coverage-gap warning) instead of
        # corroborating, on pure formatting variance.
        out = "rtt min/avg/max/mdev = 1008.584/1012.525/1019.390/4.092 ms\n"
        fps = _fps(harvest_evidence_fingerprint(out, "host_read", "ping 223.5.5.5"))
        cls, note = _classify_unknown_claim(
            {"metric": "rtt avg (ms)", "baseline": "6.433 ms", "post": "1012.525 ms"},
            "rtt avg (ms)", "6.433 ms", "1012.525 ms", fps,
        )
        assert cls == "corroborated"
        assert "NOT downgrading" in note

    def test_t3_fabricated_raw_source_value_absent(self):
        # kind=raw + source anchoring exactly one complete harvest + value in
        # NO output → deterministic fabrication.
        cls, note = _classify_unknown_claim(
            {"metric": "dropped", "baseline": 0, "post": 999,
             "kind": "raw", "source": "tc -s qdisc"},
            "dropped", 0, 999, _fps(_fp()),
        )
        assert cls == "fabricated"
        assert "999" in note
        assert "no observation" in note or "exists in no observation" in note

    def test_fabrication_anchor_is_post_only_not_baseline(self):
        # Contract alignment with verification.py's output contract: the
        # raw+source fabrication anchor inspects ONLY the ``post`` value, never
        # the ``baseline``. A baseline is read in an EARLIER phase (the
        # pre-injection snapshot) and is not fabrication-checked, so a verifier
        # must not drop or demote a claim merely because its baseline is
        # cross-phase. Two directions pin this:
        #   (a) post PRESENT + baseline ABSENT → corroborated (an absent,
        #       cross-phase baseline does not make a raw claim fabricated);
        #   (b) post ABSENT + baseline PRESENT → fabricated (the anchor keys on
        #       post; a present baseline does not rescue an absent post).
        # If the anchor ever widens to the baseline, (a) flips to fabricated.
        fps = _fps(_fp())  # harvested netem tokens include 369; not 12345/999
        cls_a, _ = _classify_unknown_claim(
            {"metric": "Sent bytes", "baseline": 12345, "post": 369,
             "kind": "raw", "source": "tc -s qdisc"},
            "Sent bytes", 12345, 369, fps,
        )
        assert cls_a == "corroborated"
        cls_b, _ = _classify_unknown_claim(
            {"metric": "Sent bytes", "baseline": 369, "post": 999,
             "kind": "raw", "source": "tc -s qdisc"},
            "Sent bytes", 369, 999, fps,
        )
        assert cls_b == "fabricated"

    def test_t3_abstains_without_kind_raw(self):
        # derived / unmarked claim → never downgrade (no raw anchor).
        for kind in ("derived", "", "computed", None):
            cls, _ = _classify_unknown_claim(
                {"metric": "ratio", "baseline": 0, "post": 999,
                 "kind": kind, "source": "tc -s qdisc"},
                "ratio", 0, 999, _fps(_fp()),
            )
            assert cls == "abstain", f"kind={kind!r} must abstain"

    def test_t3_abstains_without_source(self):
        cls, _ = _classify_unknown_claim(
            {"metric": "dropped", "baseline": 0, "post": 999, "kind": "raw"},
            "dropped", 0, 999, _fps(_fp()),
        )
        assert cls == "abstain"

    def test_t3_abstains_unmapped_source(self):
        # source matches NO harvested command → cannot prove which observation.
        cls, _ = _classify_unknown_claim(
            {"metric": "dropped", "baseline": 0, "post": 999,
             "kind": "raw", "source": "netstat -s"},
            "dropped", 0, 999, _fps(_fp()),
        )
        assert cls == "abstain"

    def test_t3_abstains_ambiguous_source(self):
        # source matches TWO harvested commands → ambiguous → abstain.
        fps = _fps(_fp(), _fp())  # both carry the same tc -s qdisc command
        cls, _ = _classify_unknown_claim(
            {"metric": "dropped", "baseline": 0, "post": 999,
             "kind": "raw", "source": "tc -s qdisc"},
            "dropped", 0, 999, fps,
        )
        assert cls == "abstain"

    def test_t3_abstains_incomplete_harvest(self):
        # A token-capped harvest cannot prove absence → abstain.
        cls, _ = _classify_unknown_claim(
            {"metric": "dropped", "baseline": 0, "post": 999,
             "kind": "raw", "source": "tc -s qdisc"},
            "dropped", 0, 999, _fps(_fp(complete=False)),
        )
        assert cls == "abstain"

    def test_t3_defensive_abstains_when_value_actually_present(self):
        # If the value IS in the anchored output, T2 union already caught it —
        # but re-check the defensive guard: never fabricate a present value.
        cls, _ = _classify_unknown_claim(
            {"metric": "dropped", "baseline": 0, "post": 10,
             "kind": "raw", "source": "tc -s qdisc"},
            "dropped", 0, 10, _fps(_fp()),
        )
        assert cls == "corroborated"  # 10 IS in the netem output → T2 wins

    def test_t2_hits_report_tool_names(self):
        cls, note = _classify_unknown_claim(
            {"metric": "Sent bytes", "baseline": 300, "post": 369},
            "Sent bytes", 300, 369, _fps(_fp(tool="kubectl_read")),
        )
        assert cls == "corroborated"
        assert "kubectl_read" in note

    def test_inwindow_host_inject_value_corroborated_not_fabricated(self):
        # procload inject-9cc36680 regression (D17 framework false-positive).
        # A self-recovering host drill observes its in-window value ONLY via
        # the read-only host_inject channel (host_read is not bound to the
        # EXECUTE phase): host_inject(``pgrep -c stress-ng``)→206 during the
        # fault, then host_read(``pgrep -c stress-ng``)→0 after auto-recovery.
        # Both share the same source string. Once the in-window harvest is in
        # the corpus, T2's union finds 206 → corroborated, NOT downgraded.
        inwindow = _fp(content="206", tool="host_inject", cmd="pgrep -c stress-ng")
        postrec = _fp(content="0", tool="host_read", cmd="pgrep -c stress-ng")
        cls, note = _classify_unknown_claim(
            {"metric": "stress-ng live process count (pgrep -c)", "baseline": 0,
             "post": 206, "kind": "raw", "source": "pgrep -c stress-ng"},
            "stress-ng live process count (pgrep -c)", 0, 206,
            _fps(inwindow, postrec),
        )
        assert cls == "corroborated"  # T2: 206 present in the in-window harvest
        assert "206" in note

    def test_pre_fix_corpus_would_have_fabricated(self):
        # Companion anchor pinning the BUG this fix removes: with ONLY the
        # post-recovery host_read(``pgrep -c stress-ng``)→0 in the corpus (the
        # pre-fix state, where the in-window host_inject observation was never
        # harvested), the SAME genuine claim anchors to exactly one complete
        # harvest whose tokens are {0}; 206 is absent → T3 false-flags it as a
        # fabrication and a correct ``verified`` is wrongly downgraded. This is
        # the exact misclassification the harvest-gate fix eliminates.
        postrec = _fp(content="0", tool="host_read", cmd="pgrep -c stress-ng")
        cls, _ = _classify_unknown_claim(
            {"metric": "stress-ng live process count (pgrep -c)", "baseline": 0,
             "post": 206, "kind": "raw", "source": "pgrep -c stress-ng"},
            "stress-ng live process count (pgrep -c)", 0, 206,
            _fps(postrec),
        )
        assert cls == "fabricated"


class TestT2WeakEmbeddedCollision:
    """A digit collision with a NON-number must not certify reality.

    Regression for the conflation defect: T2 keyed on the over-inclusive
    ``tokens`` set, so a fabricated value that happened to coincide with a hex
    UID fragment or an ISO-date component came back "corroborated — the number
    is REAL, NOT a hallucination", which simultaneously suppressed T3. Measured
    against a real chaosblade-log shape: ``post=-10`` — a date-separator
    artifact that is never a metric — was certified from
    ``2026-10-27 07:34:40 ... uid: 932eaa6bd493bf54``.
    """

    def _uid_fps(self) -> dict:
        return _fps(_fp(content=_UID_OUT, tool="host_read", cmd=_UID_CMD))

    def test_date_artifact_value_abstains_not_corroborated(self):
        cls, note = _classify_unknown_claim(
            {"metric": "fd_count", "baseline": 0, "post": -10,
             "kind": "raw", "source": "chaosblade.log"},
            "fd_count", 0, -10, self._uid_fps(),
        )
        assert cls == "abstain"
        assert "NOT downgrading" in note
        assert "standalone number" in note

    def test_uid_fragment_value_abstains_not_corroborated(self):
        cls, _ = _classify_unknown_claim(
            {"metric": "fd_count", "baseline": 0, "post": 932,
             "kind": "raw", "source": "chaosblade.log"},
            "fd_count", 0, 932, self._uid_fps(),
        )
        assert cls == "abstain"

    @pytest.mark.parametrize("value", [932, 493, 54, 6, -10, -27, 34])
    def test_monotonicity_embedded_collision_never_becomes_fabricated(self, value):
        # THE safety invariant of the two-view split. Every T3 precondition is
        # met — kind=raw, a source anchoring EXACTLY ONE complete harvest — and
        # the value is absent from every ``numeric_fields``. It must STILL
        # abstain, because the digits are in the over-inclusive ``tokens``
        # corpus T3's absence proof keys on. Narrowing the presence certificate
        # may move a claim corroborated→abstain, never →fabricated, so this
        # redesign cannot manufacture a false accusation (D12's failure mode).
        cls, _ = _classify_unknown_claim(
            {"metric": "m", "baseline": 0, "post": value,
             "kind": "raw", "source": "chaosblade.log"},
            "m", 0, value, self._uid_fps(),
        )
        assert cls == "abstain", value

    def test_t3_teeth_unchanged_for_genuinely_absent_value(self):
        # The narrowing must not blunt T3: a value in NEITHER corpus still
        # fabricates under the identical strict anchor.
        cls, _ = _classify_unknown_claim(
            {"metric": "m", "baseline": 0, "post": 123456789,
             "kind": "raw", "source": "chaosblade.log"},
            "m", 0, 123456789, self._uid_fps(),
        )
        assert cls == "fabricated"

    def test_genuine_number_in_same_output_still_corroborated(self):
        # Nor blunt T2: ``2026`` sits in the very same log line and IS
        # presented as a number, so it keeps its certificate. The fix is
        # positional, not a blanket distrust of number-dense output.
        cls, _ = _classify_unknown_claim(
            {"metric": "m", "baseline": 0, "post": 2026,
             "kind": "raw", "source": "chaosblade.log"},
            "m", 0, 2026, self._uid_fps(),
        )
        assert cls == "corroborated"

    def test_legacy_fingerprint_without_field_grading_fails_open(self):
        # A checkpoint persisted before field-grading carries no
        # ``numeric_fields``. It must abstain rather than re-certify reality
        # from an ungraded corpus; abstain never downgrades, so replaying an
        # old session loses at most a positive note, never a verdict.
        legacy = {"l-1": {"tokens": ["369"], "complete": True,
                          "tool_name": "kubectl", "command": "tc -s qdisc show"}}
        cls, _ = _classify_unknown_claim(
            {"metric": "m", "baseline": 0, "post": 369,
             "kind": "raw", "source": "tc -s qdisc"},
            "m", 0, 369, legacy,
        )
        assert cls == "abstain"

    def test_legacy_fingerprint_still_proves_absence(self):
        # ...and T3 is untouched by a missing strict view: absence is proved
        # from ``tokens`` alone.
        legacy = {"l-1": {"tokens": ["369"], "complete": True,
                          "tool_name": "kubectl", "command": "tc -s qdisc show"}}
        cls, _ = _classify_unknown_claim(
            {"metric": "m", "baseline": 0, "post": 999,
             "kind": "raw", "source": "tc -s qdisc"},
            "m", 0, 999, legacy,
        )
        assert cls == "fabricated"

    def test_non_dict_fingerprint_entries_are_ignored(self):
        # Robustness: malformed entries must not crash either union scan.
        fps = {"bad": "not-a-dict", "none": None}
        fps.update(self._uid_fps())
        cls, _ = _classify_unknown_claim(
            {"metric": "m", "baseline": 0, "post": 2026,
             "kind": "raw", "source": "chaosblade.log"},
            "m", 0, 2026, fps,
        )
        assert cls == "corroborated"


# ---------------------------------------------------------------------------
# cross_check_evidence with fingerprints — integration (the #3-R3 replay)
# ---------------------------------------------------------------------------


class TestCrossCheckValuePresence:
    def _result(self, claims, level="verified", l2="passed") -> dict:
        return {
            "level": level,
            "layer2": {"status": l2, "details": "netem duplicate confirmed"},
            "metric_claims": claims,
            "warnings": [],
        }

    def test_t2_pure_exec_no_timeline_corroborates_no_downgrade(self):
        # #3-R3: NO structured read ever parsed (truth empty) but the netem
        # value is in the fingerprint → verified preserved, positive note,
        # NOT the misleading "hallucinated"/"cannot be cross-checked" text.
        result = self._result([
            {"metric": "Sent bytes", "baseline": 300, "post": 369},
        ])
        out = cross_check_evidence(result, [], _fps(_fp()))
        assert out["level"] == "verified"
        assert out["layer2"]["status"] == "passed"
        assert any("369" in w and "corroborat" in w.lower() for w in out["warnings"])
        assert not any("downgraded" in w.lower() for w in out["warnings"])
        assert not any("out-of-band" in w for w in out["warnings"])

    def test_t3_fabrication_downgrades_verified_to_partial(self):
        result = self._result([
            {"metric": "dropped", "baseline": 0, "post": 999,
             "kind": "raw", "source": "tc -s qdisc"},
        ])
        out = cross_check_evidence(result, [], _fps(_fp()))
        assert out["level"] == "partial"
        assert out["layer2"]["status"] == "partial"
        assert any("999" in w for w in out["warnings"])
        assert any("downgraded" in w.lower() for w in out["warnings"])

    def test_fingerprints_none_is_byte_identical_to_pre_d17(self):
        # Legacy caller (fingerprints=None): an absent-key claim against a
        # NON-empty timeline abstains exactly as before D17 — warn-only
        # out-of-band note, never downgrade.
        timeline = [
            {"iteration": 1, "timestamp": "t1", "tool_call_id": "a",
             "tool_name": "kubectl", "metrics": {"RestartCount": "7"}},
            {"iteration": 2, "timestamp": "t2", "tool_call_id": "b",
             "tool_name": "kubectl", "metrics": {"RestartCount": "7"}},
        ]
        result = self._result([
            {"metric": "Sent bytes", "baseline": 300, "post": 369},
        ])
        out = cross_check_evidence(result, timeline, None)
        assert out["level"] == "verified"
        assert any("out-of-band" in w for w in out["warnings"])
        assert not any("downgraded" in w.lower() for w in out["warnings"])

    def test_both_spaces_empty_no_mutation(self):
        result = self._result([
            {"metric": "Sent bytes", "baseline": 300, "post": 369},
        ])
        out = cross_check_evidence(result, [], {})
        assert out["level"] == "verified"
        assert out["warnings"] == []

    def test_t2_corroboration_does_not_neuter_timeline_contradiction(self):
        # A claim whose key IS in the timeline and provably never moved still
        # downgrades (D15 key-space detection intact) even when fingerprints
        # corroborate an UNRELATED absent-key claim.
        timeline = [
            {"iteration": 1, "timestamp": "t1", "tool_call_id": "a",
             "tool_name": "kubectl", "metrics": {"RestartCount": "7"}},
            {"iteration": 2, "timestamp": "t2", "tool_call_id": "b",
             "tool_name": "kubectl", "metrics": {"RestartCount": "7"}},
        ]
        result = self._result([
            {"metric": "RestartCount", "baseline": 7, "post": 8},   # contradicted
            {"metric": "Sent bytes", "baseline": 300, "post": 369},  # corroborated
        ])
        out = cross_check_evidence(result, timeline, _fps(_fp()))
        assert out["level"] == "partial"
        assert any("RestartCount" in w for w in out["warnings"])
        assert any("369" in w for w in out["warnings"])

    def test_unit_suffixed_claim_reaches_corroboration_not_inert(self):
        # THE R2 regression. A1's live re-run submitted every claim post WITH a
        # unit ("1014.755 ms"), float() raised, and _check_metric_claims skipped
        # all four — the entire D17 layer went inert, zero notes. The same drill
        # must now cross-check: a unit-suffixed absent-key claim whose value is
        # present in the harvest yields a T2 corroborated note, no downgrade.
        out = "rtt min/avg/max/mdev = 1008.584/1012.525/1019.390/4.092 ms\n"
        fps = _fps(harvest_evidence_fingerprint(out, "host_read", "ping 223.5.5.5"))
        result = self._result([
            {"metric": "rtt avg (ms)", "baseline": "6.433 ms", "post": "1012.525 ms"},
        ])
        res = cross_check_evidence(result, [], fps)
        assert res["level"] == "verified"           # never downgraded
        assert any("corroborat" in w.lower() for w in res["warnings"])
        assert not any("downgraded" in w.lower() for w in res["warnings"])

    def test_genuinely_non_numeric_claim_is_still_skipped(self):
        # Fix (b) must not widen the parse to non-measurements: a claim whose
        # post has no leading number ("OOMKilled") is skipped exactly as before —
        # no note, no downgrade, no crash — preserving the pre-existing behaviour
        # for a claim with nothing numeric to cross-check.
        result = self._result([
            {"metric": "container state", "baseline": "Running", "post": "OOMKilled"},
        ])
        res = cross_check_evidence(result, [], _fps(_fp()))
        assert res["level"] == "verified"
        assert res["warnings"] == []
