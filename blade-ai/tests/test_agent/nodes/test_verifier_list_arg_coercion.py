"""W-81-1 (Case #81, live run inject-6129a6da) — JSON-stringified list args.

Some models JSON-stringify structured tool arguments before serialising the
tool_call (a known qwen-class quirk). The live run's first ``submit_verification``
passed ``checklist`` as a JSON string with a leading newline; pydantic rejected
it at the ``@tool`` boundary ("Input should be a valid list") and the verifier
burned a round-trip re-submitting the identical verdict as a real array.

Two layers must agree:
  1. The ``@tool`` boundary (``BeforeValidator``) — so the call is not rejected.
  2. The CONSUMER (``_*_verification_from_submit_args``) — which reads the RAW
     AIMessage tool_call args, NOT the validated/coerced copy the tool body saw.
     Without consumer-side coercion a stringified checklist silently collapses to
     ``[]`` (losing every per-step evidence item) and a stringified ``warnings``
     explodes into single characters via ``list("...")``.
"""
from __future__ import annotations

from chaos_agent.agent.nodes.recover._recover_finalize import (
    _recover_verification_from_submit_args,
)
from chaos_agent.agent.nodes.verify._verifier_finalize import (
    _verification_from_submit_args,
)
from chaos_agent.agent.nodes.verify._verifier_submit import (
    _coerce_list_arg,
    submit_verification,
)


# ---------------------------------------------------------------------------
# _coerce_list_arg — the shared helper
# ---------------------------------------------------------------------------


class TestCoerceListArg:
    def test_json_string_to_list(self):
        assert _coerce_list_arg('\n[{"step": 1}]\n') == [{"step": 1}]

    def test_already_list_untouched(self):
        val = [{"step": 1}]
        assert _coerce_list_arg(val) is val

    def test_empty_string_to_none(self):
        assert _coerce_list_arg("   ") is None

    def test_none_untouched(self):
        assert _coerce_list_arg(None) is None

    def test_unparseable_string_returned_raw(self):
        # Genuine garbage must surface a real pydantic type error downstream,
        # not be masked into something plausible.
        assert _coerce_list_arg("not json at all") == "not json at all"

    def test_json_object_string_not_a_list_returned_raw(self):
        # Parses as JSON but is a dict, not a list — returned untouched so the
        # type error stays genuine.
        assert _coerce_list_arg('{"a": 1}') == '{"a": 1}'


# ---------------------------------------------------------------------------
# Layer 1 — the @tool boundary no longer rejects a stringified checklist
# ---------------------------------------------------------------------------


class TestToolBoundaryCoercion:
    def test_submit_verification_accepts_stringified_checklist(self):
        schema = submit_verification.args_schema
        model = schema(
            overall="verified",
            layer2_status="passed",
            checklist='[{"step": 1, "status": "passed", "evidence": "x"}]',
        )
        assert model.checklist == [
            {"step": 1, "status": "passed", "evidence": "x"},
        ]

    def test_submit_verification_accepts_native_checklist(self):
        schema = submit_verification.args_schema
        model = schema(
            overall="verified",
            layer2_status="passed",
            checklist=[{"step": 1, "status": "passed", "evidence": "x"}],
        )
        assert model.checklist == [
            {"step": 1, "status": "passed", "evidence": "x"},
        ]


# ---------------------------------------------------------------------------
# Layer 2 — the CONSUMER reads the raw AIMessage args (validator not applied)
# ---------------------------------------------------------------------------


class TestInjectConsumerCoercion:
    def test_stringified_checklist_not_dropped(self):
        args = {
            "overall": "verified",
            "layer2_status": "passed",
            "primary_evidence_observed": True,
            "checklist": (
                '[{"step": 1, "status": "passed", "evidence": "cmd flipped"},'
                ' {"step": 2, "status": "passed", "evidence": "exit 1"}]'
            ),
        }
        result = _verification_from_submit_args(args)
        items = result.get("checklist", {}).get("items", [])
        assert len(items) == 2, "stringified checklist must not collapse to []"
        assert items[0]["evidence"] == "cmd flipped"

    def test_stringified_warnings_not_exploded_into_chars(self):
        args = {
            "overall": "verified",
            "layer2_status": "passed",
            "warnings": '["backoffLimit showed no delta", "jobs gc-ed"]',
        }
        result = _verification_from_submit_args(args)
        assert "backoffLimit showed no delta" in result["warnings"]
        assert "jobs gc-ed" in result["warnings"]
        # Regression guard: list("...") would have produced single-char items.
        assert all(len(w) > 1 for w in result["warnings"])

    def test_native_args_still_work(self):
        args = {
            "overall": "verified",
            "layer2_status": "passed",
            "primary_evidence_observed": True,
            "checklist": [{"step": 1, "status": "passed", "evidence": "ok"}],
        }
        result = _verification_from_submit_args(args)
        assert result["checklist"]["total_count"] == 1


class TestRecoverConsumerCoercion:
    def test_stringified_checklist_not_dropped(self):
        args = {
            "overall": "recovered",
            "layer2_status": "passed",
            "checklist": '[{"step": 1, "status": "passed", "evidence": "cmd restored"}]',
        }
        result = _recover_verification_from_submit_args(args)
        items = result.get("checklist", {}).get("items", [])
        assert len(items) == 1
        assert items[0]["evidence"] == "cmd restored"

    def test_stringified_warnings_not_exploded_into_chars(self):
        args = {
            "overall": "recovered",
            "layer2_status": "passed",
            "warnings": '["failed jobs remain failed by design"]',
        }
        result = _recover_verification_from_submit_args(args)
        assert "failed jobs remain failed by design" in result["warnings"]
        assert all(len(w) > 1 for w in result["warnings"])
