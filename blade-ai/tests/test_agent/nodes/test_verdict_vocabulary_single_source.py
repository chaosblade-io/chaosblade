"""Vocabulary single-source pinning (B76 round-14 root-cause fix).

Round-14 found three mutually contradictory hand-copied vocabularies live
at once: the RecoverVerdict enum legislated {recovered, partial, failed}
while the recover clamp enforced {recovered, partial, unverified,
unrecovered} and the submit prompt taught a fourth set — plus twelve
hand-copies of the non-passed checklist triple across two files, and
teach/parse drift on the recover side (prompt taught 4 item words, the
regex parsed a different 5).

The fix made every enforcement point DERIVE from the verdict.py enums.
These tests pin the derivation from all four faces so drift becomes
structurally impossible again:

  1. legislation  — closed sets and counting subsets are well-formed
  2. enforcement  — clamps accept exactly the enum members
  3. teaching     — submit prompts / JSON reminder teach exactly the
                    enum members (no more, no less)
  4. parsing      — the derived regexes parse every enum member
  5. source level — the hand-copied tuple literals are GONE from the
                    enforcement files (the acceptance criterion of the
                    round, mechanized)
  6. round-15 ext  — the task_state domain's slice/mapping/boundary hand
                    copies are gone from every consumer wired in round-15
                    (task_state legislation lives in state.py)

If someone adds an enum member without teaching/parsing/clamping it, or
re-introduces a hand-copied word list, one of these tests fails.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from chaos_agent.agent.nodes.recover._recover_finalize import (
    _recover_verification_from_submit_args,
)
from chaos_agent.agent.nodes.recover._recover_layer2_parse import (
    _parse_recovery_checklist_items,
)
from chaos_agent.agent.nodes.verify._verifier_finalize import (
    _overall_to_level,
    _verification_from_submit_args,
)
from chaos_agent.agent.nodes.verify._verifier_layer2_parse import (
    _parse_checklist_items,
    _try_parse_json,
)
from chaos_agent.agent.nodes.verify._verifier_messages import _ITEM_STATUS_PROSE
from chaos_agent.agent.nodes.verify._verifier_submit import (
    submit_recover_verification,
    submit_verification,
)
from chaos_agent.agent.prompts.sections.verification import (
    _CHECKLIST_STATUS_DEFINITIONS,
    STATUS_NOT_APPLICABLE_DEFINITION,
    get_verifier_output_format_section,
)
from chaos_agent.agent.prompts.sections.recovery import (
    _RECOVER_CHECKLIST_STATUS_DEFINITIONS,
    get_recover_output_format_section,
)
from chaos_agent.agent.nodes.verify import verifier as verifier_module
from chaos_agent.agent.result.verdict import (
    CHECKLIST_BENIGN_STATUSES,
    CHECKLIST_NON_PASSED_STATUSES,
    CHECKLIST_STATUS_VALUES,
    ChecklistItemStatus,
    INJECT_VERDICT_VALUES,
    InjectVerdict,
    LAYER2_PARSE_KEYWORDS,
    LAYER2_STATUS_VALUES,
    Layer2Status,
    RECOVER_VERDICT_VALUES,
    RecoverVerdict,
    ResidualAttribution,
    RESIDUAL_ATTRIBUTION_VALUES,
)

SRC = Path(__file__).resolve().parents[3] / "src" / "chaos_agent"


# ---------------------------------------------------------------------------
# 1. Legislation: closed sets and counting subsets are well-formed
# ---------------------------------------------------------------------------


class TestClosedSetLegislation:
    def test_recover_verdict_is_the_pipeline_four_word_set(self):
        # F1 pin: "failed" is NOT a recovery level (unreachable — the clamp
        # maps it to "unrecovered"); "unverified" != "unrecovered" is the
        # anti-conflation contract.
        assert {m.value for m in RecoverVerdict} == {
            "recovered", "partial", "unverified", "unrecovered",
        }

    def test_inject_verdict_closure(self):
        assert INJECT_VERDICT_VALUES == {"verified", "partial", "unverified"}

    def test_layer2_status_closure(self):
        assert LAYER2_STATUS_VALUES == {
            "passed", "partial", "failed", "skipped",
            "recovered_before_observation", "unknown",
        }

    def test_checklist_status_closure(self):
        assert CHECKLIST_STATUS_VALUES == {
            "passed", "partial", "failed", "skipped",
            "recovered_before_observation", "expected", "not_applicable",
        }

    def test_counting_subsets_partition_the_closed_set(self):
        # NON_PASSED and BENIGN are disjoint and together cover every
        # legislated word — an item can never fall outside both rules.
        assert not (CHECKLIST_NON_PASSED_STATUSES & CHECKLIST_BENIGN_STATUSES)
        assert (
            CHECKLIST_NON_PASSED_STATUSES | CHECKLIST_BENIGN_STATUSES
            == CHECKLIST_STATUS_VALUES
        )

    def test_residual_attribution_closure(self):
        assert RESIDUAL_ATTRIBUTION_VALUES == {
            "none", "recovery_process", "fault_residual", "mixed",
        }


# ---------------------------------------------------------------------------
# 2. Enforcement: clamps accept exactly the enum members
# ---------------------------------------------------------------------------


class TestClampDerivation:
    @pytest.mark.parametrize("word", sorted(RECOVER_VERDICT_VALUES))
    def test_recover_clamp_passes_every_member(self, word):
        result = _recover_verification_from_submit_args(
            {"overall": word, "layer2_status": "passed"}
        )
        assert result["level"] == word

    @pytest.mark.parametrize(
        "word", ["failed", "success", "bogus", "", "RECOVERED", "recovered!"]
    )
    def test_recover_clamp_rejects_out_of_set(self, word):
        # "failed" included deliberately: it is the round-14 fossil word
        # the old enum legislated — the pipeline maps it to "unrecovered".
        result = _recover_verification_from_submit_args(
            {"overall": word, "layer2_status": "passed"}
        )
        assert result["level"] == "unrecovered"

    @pytest.mark.parametrize("word", sorted(INJECT_VERDICT_VALUES))
    def test_inject_clamp_passes_every_member(self, word):
        assert _overall_to_level(word) == word

    @pytest.mark.parametrize("word", ["bogus", "success", "verified ", "unverified!"])
    def test_inject_clamp_rejects_out_of_set(self, word):
        assert _overall_to_level(word) == "unverified"

    @pytest.mark.parametrize("word", sorted(LAYER2_STATUS_VALUES))
    def test_layer2_clamp_passes_every_member_both_sides(self, word):
        inject = _verification_from_submit_args(
            {"overall": "unverified", "layer2_status": word}
        )
        recover = _recover_verification_from_submit_args(
            {"overall": "unrecovered", "layer2_status": word}
        )
        assert inject["layer2"]["status"] == word
        assert recover["layer2"]["status"] == word
        assert not any("closed vocabulary" in w for w in inject["warnings"])
        assert not any("closed vocabulary" in w for w in recover["warnings"])

    @pytest.mark.parametrize("word", ["success", "ok", "maybe", "done"])
    def test_layer2_clamp_rejects_out_of_set_both_sides(self, word):
        inject = _verification_from_submit_args(
            {"overall": "unverified", "layer2_status": word}
        )
        recover = _recover_verification_from_submit_args(
            {"overall": "unrecovered", "layer2_status": word}
        )
        assert inject["layer2"]["status"] == "unknown"
        assert recover["layer2"]["status"] == "unknown"
        assert any("closed vocabulary" in w for w in inject["warnings"])
        assert any("closed vocabulary" in w for w in recover["warnings"])

    def test_json_path_accepts_members_rejects_out_of_set(self):
        for l2 in LAYER2_STATUS_VALUES:
            payload = '{"layer1":"passed","layer2":"%s","overall":"verified"}' % l2
            assert _try_parse_json(payload) is not None
        for bad in ("maybe", "success"):
            payload = '{"layer1":"passed","layer2":"%s","overall":"verified"}' % bad
            assert _try_parse_json(payload) is None
        for bad_overall in ("success", "failed"):
            payload = '{"layer1":"passed","layer2":"passed","overall":"%s"}' % bad_overall
            assert _try_parse_json(payload) is None

    def test_json_path_missing_required_fields_rejected(self):
        # Schema violation (missing layer2/overall) rejects the JSON — the
        # default-fill "unknown"/"unverified" must not silently pass the
        # closed-set gate just because those words are enum members.
        for payload in (
            '{"foo":"bar"}',
            '{"layer1":"passed","layer2":"passed"}',
            '{"layer1":"passed","overall":"verified"}',
        ):
            assert _try_parse_json(payload) is None


class TestCountingPolicy:
    def _inject_result(self, statuses):
        return _verification_from_submit_args({
            "overall": "unverified",
            "layer2_status": "passed",
            "checklist": [
                {"step": i, "status": s, "evidence": "e"}
                for i, s in enumerate(statuses, start=1)
            ],
        })

    @pytest.mark.parametrize(
        "statuses,expected",
        [
            (["passed", "skipped", "expected", "not_applicable"], 0),
            (["failed"], 1),
            (["partial"], 1),
            (["recovered_before_observation"], 1),
            (["failed", "partial", "recovered_before_observation"], 3),
        ],
    )
    def test_known_words_count_as_before(self, statuses, expected):
        result = self._inject_result(statuses)
        assert result["checklist"]["non_passed_count"] == expected

    @pytest.mark.parametrize(
        "statuses", [["success"], ["ok"], ["observed"], [None], ["passed", "impact"]]
    )
    def test_out_of_set_counts_non_passed_fail_closed(self, statuses):
        # F3 pin: a closed-set-outside word (or a missing status key) is
        # not a pass claim — it used to silently count as passed.
        result = self._inject_result(statuses)
        assert result["checklist"]["non_passed_count"] == 1
        assert any("closed vocabulary" in w for w in result["warnings"])

    def test_benign_words_do_not_warn(self):
        result = self._inject_result(["passed", "skipped", "not_applicable"])
        assert not any("closed vocabulary" in w for w in result["warnings"])

    def test_json_path_counts_fail_closed(self):
        payload = (
            '{"layer1":"passed","layer2":"passed","overall":"unverified",'
            '"verification_checklist":['
            '{"step":1,"status":"passed","evidence":"e"},'
            '{"step":2,"status":"impact","evidence":"e"}]}'
        )
        result = _try_parse_json(payload)
        assert result["checklist"]["non_passed_count"] == 1
        assert any("closed vocabulary" in w for w in result["warnings"])

    def test_text_path_checklist_counts_fail_closed(self):
        text = (
            "VERIFICATION_CHECKLIST:\n"
            "Step 1: passed — cpu high\n"
            "VERIFICATION_RESULT:\nLayer1: passed\nLayer2: passed"
        )
        from chaos_agent.agent.nodes.verify._verifier_layer2_parse import (
            _parse_verification_result,
        )

        result = _parse_verification_result(text)
        # Regex-derived vocabulary means every parsed item is in-set:
        # no silent garbage inflow on the text path.
        for item in result["checklist"]["items"]:
            assert item["status"] in CHECKLIST_STATUS_VALUES


# ---------------------------------------------------------------------------
# 3. Teaching: prompts teach exactly the enum members
# ---------------------------------------------------------------------------


def _segment(doc: str, marker: str) -> str:
    """Text from ``marker`` to the next ``\\n  - `` input line."""
    return doc.split(marker, 1)[1].split("\n  - ", 1)[0]


class TestPromptTeachingDerivation:
    def test_inject_overall_line_teaches_exactly_the_enum(self):
        seg = _segment(submit_verification.description, "- overall: ")
        words = set(re.findall(r'"([a-z_]+)"', seg))
        assert words == INJECT_VERDICT_VALUES

    def test_recover_overall_line_teaches_exactly_the_enum(self):
        seg = _segment(submit_recover_verification.description, "- overall: ")
        words = set(re.findall(r'"([a-z_]+)"', seg))
        assert words == RECOVER_VERDICT_VALUES
        # The fossil word must never be taught as a recovery level again.
        assert "failed" not in words

    def test_layer2_lines_teach_exactly_the_enum_both_sides(self):
        inject_seg = _segment(submit_verification.description, "- layer2_status: ")
        recover_seg = _segment(submit_recover_verification.description, "- layer2_status: ")
        inject_words = set(inject_seg.split("\n")[0].strip('"').split("|"))
        recover_words = set(re.findall(r'"([a-z_]+)"', recover_seg.split("\n")[0]))
        assert inject_words == LAYER2_STATUS_VALUES
        assert recover_words == LAYER2_STATUS_VALUES

    def test_checklist_lines_teach_exactly_the_enum_both_sides(self):
        for doc in (submit_verification.description, submit_recover_verification.description):
            seg = _segment(doc, "- checklist: ")
            # The status vocabulary is the quoted pipe-list after "status":
            # — the first quoted token in the segment is "step".
            vocab = re.search(r'"status":\s*"([a-z_|]+)"', seg).group(1)
            assert set(vocab.split("|")) == CHECKLIST_STATUS_VALUES

    def test_residual_attribution_line_teaches_exactly_the_enum(self):
        seg = _segment(submit_recover_verification.description, "- residual_attribution: ")
        words = set(re.findall(r'"([a-z_]+)"', seg.split("\n")[0]))
        assert words == RESIDUAL_ATTRIBUTION_VALUES

    def test_json_mode_reminder_derives_from_enums(self):
        # The verifier.py module-level schema strings derive from the
        # legislation (source-level: no hand-copied vocabulary literal).
        assert verifier_module._JSON_OVERALL_VOCAB == "|".join(
            m.value for m in InjectVerdict
        )
        assert verifier_module._JSON_LAYER2_VOCAB == "|".join(
            m.value for m in Layer2Status
        )
        assert verifier_module._JSON_ITEM_STATUS_VOCAB == "|".join(
            m.value for m in ChecklistItemStatus
        )

    def test_messages_prose_teaches_exactly_the_enum(self):
        assert _ITEM_STATUS_PROSE == ", ".join(
            m.value for m in ChecklistItemStatus
        )
        assert set(_ITEM_STATUS_PROSE.split(", ")) == CHECKLIST_STATUS_VALUES

    def test_status_definitions_cover_every_enum_member(self):
        # B76 completion (2026-10-29 python Case #5 inject-e0b3109a): round-14
        # pinned the VALUE LISTS (the tests above) but never the DEFINITIONS —
        # the inject-face prose hand-defined only 5 of 7 members and the
        # recover-face only 4 of 7, leaving `partial` / `not_applicable` /
        # `expected` / `recovered_before_observation` exposed as enum values
        # with no taught meaning. The model then invented a definition for
        # not_applicable and flip-flopped skipped/not_applicable/expected across
        # structurally identical conditional steps. This pins the guarantee
        # round-14 only claimed for words, on BOTH consumption faces: every
        # legislated member has a DEFINITION, derived from an enum-keyed map (a
        # member without one raises KeyError at import, before this can run).
        for defs, render, face in (
            (_CHECKLIST_STATUS_DEFINITIONS,
             get_verifier_output_format_section, "inject"),
            (_RECOVER_CHECKLIST_STATUS_DEFINITIONS,
             get_recover_output_format_section, "recover"),
        ):
            assert set(defs) == set(ChecklistItemStatus), face
            assert {m.value for m in defs} == CHECKLIST_STATUS_VALUES, face
            prose = render()
            for member in ChecklistItemStatus:
                assert f"- **{member.value}**:" in prose, (
                    f"[{face}] checklist status {member.value!r} is legislated "
                    "but its definition is missing from the output-format prose"
                )

    def test_not_applicable_is_single_sourced_across_both_faces(self):
        # `not_applicable` is CONTEXT-INVARIANT (a step that does not apply to
        # the actual situation means the same thing whether judging injection or
        # recovery), so it is defined ONCE and imported by the recover face — a
        # second hand-copy is exactly the drift this fix removes. Pin identity,
        # not just equality, so a future re-copy (equal today, drifts tomorrow)
        # is caught.
        assert (
            _CHECKLIST_STATUS_DEFINITIONS[ChecklistItemStatus.NOT_APPLICABLE]
            is STATUS_NOT_APPLICABLE_DEFINITION
        )
        assert (
            _RECOVER_CHECKLIST_STATUS_DEFINITIONS[ChecklistItemStatus.NOT_APPLICABLE]
            is STATUS_NOT_APPLICABLE_DEFINITION
        )

    def test_conditional_step_semantics_have_a_single_owner(self):
        # The flip-flop root cause: "a conditional/diagnostic step whose
        # trigger precondition did not fire, so it was correctly NOT run" had
        # no exclusive owner, so the model spread it across skipped and
        # not_applicable. Pin the disambiguation so it cannot silently regress:
        # not_applicable OWNS the precondition-not-fired case; skipped is a
        # genuine coverage gap that explicitly redirects there — on both faces.
        assert "precondition" in STATUS_NOT_APPLICABLE_DEFINITION
        assert "NOT a coverage gap" in STATUS_NOT_APPLICABLE_DEFINITION
        for defs, face in (
            (_CHECKLIST_STATUS_DEFINITIONS, "inject"),
            (_RECOVER_CHECKLIST_STATUS_DEFINITIONS, "recover"),
        ):
            sk = defs[ChecklistItemStatus.SKIPPED]
            assert "not_applicable" in sk and "coverage gap" in sk, face

    def test_submit_doc_teaches_the_consistency_contract(self):
        # B2 / Phase 2c (D14): the downgrade rules the enforcer actually applies
        # are TAUGHT at submit time — the model sees the contract while filling
        # the args, not only in the post-hoc warning after a mismatch cost a
        # re-verify round. The checklist rule is now purely structural: a
        # 'failed' item under a 'passed' conclusion downgrades; the program
        # never re-derives absence semantics from evidence text.
        doc = submit_verification.description
        assert "Consistency contract" in doc
        assert "DOWNGRADED" in doc
        # Each taught rule mirrors a real enforcement branch in
        # _verification_from_submit_args (order: the doc's bullet list).
        # Arrow form ("+ →") keeps the contract inside the schema budget
        # cap (610, see test_tool_description_budget.py).
        assert '"verified" + primary_evidence_observed=false' in doc
        assert '"verified" + layer2_status="failed"' in doc
        assert 'layer2_status="partial" → overall "partial"' in doc
        # Structural checklist rule + the benign-status guidance that replaces
        # the retired free-text "absence" escape hatch.
        assert 'layer2_status="passed" + a "failed" item → DOWNGRADED to "partial"' in doc
        assert '"recovered_before_observation"' in doc
        # The retired free-text path must NOT be taught any more.
        assert "absence" not in doc.lower()

    def test_taught_contract_matches_the_enforcer(self):
        # Behavioural anchor: for every taught rule, the enforcer really
        # downgrades exactly as the doc says — the contract can never
        # drift into teaching a rule code does not apply.
        # Rule 1: verified + no primary evidence -> partial
        r = _verification_from_submit_args({
            "overall": "verified", "layer2_status": "passed",
            "primary_evidence_observed": False,
        })
        assert r["level"] == "partial"
        # Rule 2: verified + layer2 failed -> unverified
        r = _verification_from_submit_args({
            "overall": "verified", "layer2_status": "failed",
            "primary_evidence_observed": True,
        })
        assert r["level"] == "unverified"
        # Rule 3: layer2 partial forces level partial
        r = _verification_from_submit_args({
            "overall": "verified", "layer2_status": "partial",
            "primary_evidence_observed": True,
        })
        assert r["level"] == "partial"
        # Rule 4 (structural, Phase 2c / D14): layer2 passed + ANY 'failed'
        # checklist item -> layer2 partial. The downgrade is enum-driven; the
        # evidence text is irrelevant (absence semantics are no longer parsed).
        r = _verification_from_submit_args({
            "overall": "verified", "layer2_status": "passed",
            "primary_evidence_observed": True,
            "checklist": [{"step": 1, "status": "failed",
                           "evidence": "cpu far below threshold"}],
        })
        assert r["layer2"]["status"] == "partial"
        assert any("inconsistency" in w for w in r["warnings"])
        # A 'failed' item WITHOUT any absence phrase downgrades identically —
        # pinning that the decision is structural, not free-text driven.
        r_no_absence = _verification_from_submit_args({
            "overall": "verified", "layer2_status": "passed",
            "primary_evidence_observed": True,
            "checklist": [{"step": 1, "status": "failed",
                           "evidence": "timing delay"}],
        })
        assert r_no_absence["layer2"]["status"] == "partial"
        assert any("inconsistency" in w for w in r_no_absence["warnings"])
        # A benign non-'failed' status expresses "non-passed but not a real
        # failure" -> no structural contradiction -> stays passed.
        r_benign = _verification_from_submit_args({
            "overall": "verified", "layer2_status": "passed",
            "primary_evidence_observed": True,
            "checklist": [{"step": 1, "status": "recovered_before_observation",
                           "evidence": "effect dissipated before check"}],
        })
        assert r_benign["layer2"]["status"] == "passed"
        assert not any("inconsistency" in w for w in r_benign["warnings"])


# ---------------------------------------------------------------------------
# 4. Parsing: the derived regexes parse every enum member
# ---------------------------------------------------------------------------


class TestParseTeachingAlignment:
    @pytest.mark.parametrize("word", sorted(CHECKLIST_STATUS_VALUES))
    def test_inject_regex_parses_every_member(self, word):
        items = _parse_checklist_items(
            f"VERIFICATION_CHECKLIST:\nStep 1: {word} — evidence\n"
            "VERIFICATION_RESULT:\nLayer1: passed\nLayer2: passed"
        )
        assert [i["status"] for i in items] == [word]

    @pytest.mark.parametrize("word", sorted(CHECKLIST_STATUS_VALUES))
    def test_recover_regex_parses_every_member(self, word):
        items = _parse_recovery_checklist_items(f"Step 1: {word} — evidence")
        assert [i["status"] for i in items] == [word]


# ---------------------------------------------------------------------------
# 5. Source level: the hand-copied vocabularies are gone
# ---------------------------------------------------------------------------


class TestNoHandCopiedVocabularies:
    """Mechanized acceptance criterion: no enforcement file may carry a
    hand-copied closed-set literal — the enum in verdict.py is the only
    place a vocabulary literal may live."""

    ENFORCEMENT_FILES = [
        "agent/nodes/verify/_verifier_finalize.py",
        "agent/nodes/verify/_verifier_layer2_parse.py",
        "agent/nodes/verify/_verifier_shared.py",
        "agent/nodes/verify/_verifier_submit.py",
        "agent/nodes/verify/_verifier_messages.py",
        "agent/nodes/verify/verifier.py",
        "agent/nodes/recover/_recover_finalize.py",
        "agent/nodes/recover/_recover_layer2_parse.py",
    ]

    HAND_COPY_FORMS = [
        # the non-passed triple, both historical word orders
        '("failed", "partial", "recovered_before_observation")',
        '("failed", "recovered_before_observation", "partial")',
        # the layer2 ordered-parse keyword tuple, both drifted orders
        # (the shared parser carried skipped-before-partial while the
        # layer2 details extractor carried partial-before-skipped)
        '("recovered_before_observation", "passed", "failed", "skipped", "partial")',
        '("recovered_before_observation", "passed", "failed", "partial", "skipped")',
        # the recover-level four-word hand copy (round-14 F1)
        '"recovered", "partial", "unverified", "unrecovered"',
        # the inject-level hand copy
        '"verified", "partial", "unverified"',
    ]

    def test_enforcement_files_carry_no_hand_copied_closed_sets(self):
        violations = []
        for rel in self.ENFORCEMENT_FILES:
            text = (SRC / rel).read_text(encoding="utf-8")
            for form in self.HAND_COPY_FORMS:
                if form in text:
                    violations.append((rel, form))
        assert not violations, (
            f"hand-copied closed sets re-introduced: {violations}. The "
            "vocabulary literal may live ONLY in "
            "src/chaos_agent/agent/result/verdict.py — derive from the "
            "enum/derived constants instead (B76 round-14)."
        )

    def test_verdict_py_is_the_only_vocabulary_legislation(self):
        # The derived constants + subset constants exist and equal their
        # enums — the legislation is load-bearing, not decorative.
        from chaos_agent.agent.result import verdict as verdict_module

        for enum_cls, const in (
            (InjectVerdict, "INJECT_VERDICT_VALUES"),
            (RecoverVerdict, "RECOVER_VERDICT_VALUES"),
            (Layer2Status, "LAYER2_STATUS_VALUES"),
            (ChecklistItemStatus, "CHECKLIST_STATUS_VALUES"),
            (ResidualAttribution, "RESIDUAL_ATTRIBUTION_VALUES"),
        ):
            assert getattr(verdict_module, const) == frozenset(
                m.value for m in enum_cls
            )

    def test_layer2_parse_keywords_legislated_in_verdict_py(self):
        # The ordered parse tuple exists, equals the Layer2Status closed set
        # minus "unknown", and starts with the longest keyword — order is
        # load-bearing (the parser scans in order, first keyword present in
        # the text wins), so the leading longest-keyword pin keeps a future
        # edit from silently reordering the scan.
        assert set(LAYER2_PARSE_KEYWORDS) == LAYER2_STATUS_VALUES - {"unknown"}
        assert LAYER2_PARSE_KEYWORDS[0] == "recovered_before_observation"
        assert len(LAYER2_PARSE_KEYWORDS) == len(set(LAYER2_PARSE_KEYWORDS))


class TestNoHandCopiedTaskStateVocabularies:
    """Round-15 extension (task_state domain): the slice/mapping/boundary
    hand copies are gone from every consumer wired this round.

    state.py is the LEGISLATION file for this domain — its internal subset
    literals are legal (mirroring verdict.py's subsets) — so it is
    deliberately NOT in the governance set. The historical hand-copy forms
    banned here are the exact shapes round-15 removed; if one reappears in
    a consumer, derive from TaskState / TASK_STATE_TERMINAL_VALUES /
    recovery_task_state_from_level instead."""

    GOVERNANCE_FILES = [
        "agent/nodes/recover/_recover_finalize.py",
        "agent/result/operation_result.py",
        "agent/result/operation_outcome.py",
        "persistence/task_store.py",
        "server/routes/turn_event_stream.py",
        "agent/postmortem/builder.py",
        "agent/nodes/store/memory_nodes.py",
        "l4/execution.py",
        "l4/recovery.py",
        "l4/adapter.py",
        "cli/client.py",
        "agent/streaming.py",
    ]

    HAND_COPY_FORMS = [
        # slice: recover-success pair
        'in ("recovered", "partial")',
        # slice: task_state success pair
        'in ("recovered", "partial_recovered")',
        # slice: degraded pair
        'in ("partial_recovered", "unverified")',
        # slice: inject verdict FULL set (postmortem gate historical form)
        'in ("verified", "unverified", "partial")',
        # slice: inject veto singleton
        'in ("unverified",)',
        # slice: terminal-set frozenset (task_store historical form)
        '"injected", "recovered", "partial_recovered",',
        # slice: session-close four-word tuple (turn_event_stream)
        'in ("recovered", "partial_recovered", "completed", "unverified")',
        # slice: streaming success triple
        'in ("injected", "recovered", "partial_recovered")',
        # slice: infer_inject_status success tuple — legal ONLY in state.py
        'in ("injected", "recovering", "recovered", "partial_recovered")',
    ]

    def test_governance_files_carry_no_task_state_hand_copies(self):
        violations = []
        for rel in self.GOVERNANCE_FILES:
            text = (SRC / rel).read_text(encoding="utf-8")
            for form in self.HAND_COPY_FORMS:
                if form in text:
                    violations.append((rel, form))
        assert not violations, (
            f"hand-copied task_state slices re-introduced: {violations}. "
            "The task_state vocabulary literal may live ONLY in "
            "src/chaos_agent/agent/state.py — derive from TaskState / "
            "TASK_STATE_TERMINAL_VALUES / recovery_task_state_from_level "
            "instead (B76 round-15)."
        )

    def test_task_state_legislation_is_load_bearing(self):
        # The derived constants exist and equal the enum — plus every
        # task_state-word map key the round wired (label map) stays inside
        # the legislated closed set.
        from chaos_agent.agent.state import (
            TASK_STATE_TERMINAL_VALUES,
            TASK_STATE_VALUES,
            TaskState,
        )
        from chaos_agent.agent.result.operation_result import (
            _RECOVER_LABEL_BY_TASK_STATE,
        )

        assert TASK_STATE_VALUES == frozenset(m.value for m in TaskState)
        assert TASK_STATE_TERMINAL_VALUES < TASK_STATE_VALUES
        assert set(_RECOVER_LABEL_BY_TASK_STATE) <= TASK_STATE_VALUES

    def test_subset_constants_are_legislated_in_verdict_py(self):
        # RECOVER_SUCCESS_VALUES / INJECT_VETO_VALUES exist and equal the
        # semantic subsets they name (round-15 slice legislation).
        from chaos_agent.agent.result import verdict as verdict_module

        assert verdict_module.RECOVER_SUCCESS_VALUES == frozenset({
            RecoverVerdict.RECOVERED.value, RecoverVerdict.PARTIAL.value,
        })
        assert verdict_module.INJECT_VETO_VALUES == frozenset({
            InjectVerdict.UNVERIFIED.value,
        })
