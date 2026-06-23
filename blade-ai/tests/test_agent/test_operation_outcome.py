"""Tests for operation verification/outcome accessors."""

from pathlib import Path

from chaos_agent.agent.operation_outcome import (
    build_verification_simple,
    read_inject_verification,
    read_merged_error,
    read_operation_outcome,
    read_recover_verification,
    read_verification_side_effects,
    write_inject_verification,
    write_recover_verification,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_inject_and_recover_verification_are_read_from_separate_fields():
    inject = {"level": "verified", "layer2": {"status": "passed"}}
    recover = {"level": "recovered", "layer2": {"status": "passed"}}

    state = {
        "verification": inject,
        "recover_verification": recover,
    }

    assert read_inject_verification(state) == inject
    assert read_recover_verification(state) == recover


def test_verification_reads_return_deep_copies():
    state = {
        "verification": {"level": "verified", "layer2": {"status": "passed"}},
        "recover_verification": {"level": "recovered", "layer2": {"status": "passed"}},
    }

    inject_read = read_inject_verification(state)
    recover_read = read_recover_verification(state)
    assert inject_read is not None
    assert recover_read is not None

    inject_read["layer2"]["status"] = "mutated"
    recover_read["layer2"]["status"] = "mutated"

    assert state["verification"]["layer2"]["status"] == "passed"
    assert state["recover_verification"]["layer2"]["status"] == "passed"


def test_write_inject_verification_does_not_write_recover_field():
    update = write_inject_verification(
        {"existing": True},
        result={"verified": True},
        verification={"level": "verified"},
    )

    assert update["existing"] is True
    assert update["result"] == {"verified": True}
    assert update["verification"] == {"level": "verified"}
    assert "recover_verification" not in update


def test_write_recover_verification_does_not_write_inject_field():
    update = write_recover_verification(
        {"existing": True},
        result={"recovered": True},
        verification={"level": "recovered"},
        finished_at="2026-06-18T00:00:00+08:00",
    )

    assert update["existing"] is True
    assert update["result"] == {"recovered": True}
    assert update["recover_verification"] == {"level": "recovered"}
    assert update["finished_at"] == "2026-06-18T00:00:00+08:00"
    assert "verification" not in update


def test_write_helpers_deep_copy_values():
    result = {"nested": {"ok": True}}
    verification = {"layer2": {"status": "passed"}}

    update = write_inject_verification(result=result, verification=verification)
    result["nested"]["ok"] = False
    verification["layer2"]["status"] = "mutated"

    assert update["result"]["nested"]["ok"] is True
    assert update["verification"]["layer2"]["status"] == "passed"


def test_side_effects_read_from_inject_verification():
    verification = {"side_effects": {"container_restarts": [{"pod": "p1"}]}}
    side_effects = read_verification_side_effects(verification)
    assert side_effects == {"container_restarts": [{"pod": "p1"}]}

    side_effects["container_restarts"][0]["pod"] = "mutated"
    assert verification["side_effects"]["container_restarts"][0]["pod"] == "p1"


def test_build_verification_simple_flattens_evidence_and_details():
    verification = {
        "level": "strong",
        "layer1": {"status": "passed"},
        "layer2": {"status": "passed", "details": "all checks passed"},
        "baseline_confidence": "high",
        "baseline_used": True,
        "warnings": ["late sample"],
        "checklist": {
            "items": [
                {"step": "kubectl get pod", "status": "passed", "evidence": "Running"},
                {"ignored": "malformed but still a dict"},
                "bad-item",
            ]
        },
    }

    assert build_verification_simple(verification) == {
        "level": "strong",
        "layer1": {"status": "passed"},
        "layer2": {"status": "passed"},
        "baseline_confidence": "high",
        "baseline_used": True,
        "warnings": ["late sample"],
        "evidence": [
            {"step": "kubectl get pod", "status": "passed", "detail": "Running"},
            {"step": None, "status": None, "detail": ""},
        ],
        "evidence_summary": "all checks passed",
    }


def test_build_verification_simple_tolerates_malformed_layers():
    assert build_verification_simple({}) is None
    assert build_verification_simple(None) is None
    assert build_verification_simple(
        {
            "level": "weak",
            "layer1": "bad-layer",
            "layer2": None,
            "checklist": "bad-checklist",
        }
    ) == {
        "level": "weak",
        "layer1": {"status": "unknown"},
        "layer2": {"status": "unknown"},
        "baseline_confidence": "none",
        "baseline_used": None,
    }


def test_operation_outcome_prefers_failure_reason_over_error():
    state = {
        "result": {"verified": False},
        "failure_reason": "safety_rejected: namespace denied",
        "error": "raw error",
        "failure_detail": {"category": "safety_rejected"},
        "postmortem": {"summary": "failure summary"},
        "finished_at": "done",
    }

    outcome = read_operation_outcome(state)

    assert outcome.result == {"verified": False}
    assert outcome.error == "safety_rejected: namespace denied"
    assert outcome.failure_reason == "safety_rejected: namespace denied"
    assert outcome.failure_detail == {"category": "safety_rejected"}
    assert outcome.postmortem == {"summary": "failure summary"}
    assert outcome.finished_at == "done"


def test_merged_error_falls_back_to_error():
    assert read_merged_error({"error": "raw error"}) == "raw error"


def test_raw_verification_field_reads_are_limited_to_boundaries_and_payloads():
    """Graph state verification reads should pass through operation_outcome."""

    allowed = {
        (
            "src/chaos_agent/agent/operation_outcome.py",
            'return _copy_dict(state.get("verification"))',
        ),
        (
            "src/chaos_agent/agent/operation_outcome.py",
            'return _copy_dict(state.get("recover_verification"))',
        ),
        (
            "src/chaos_agent/agent/task_snapshot.py",
            'verification = result_data.get("verification") or record.get("verification")',
        ),
        (
            "src/chaos_agent/agent/task_snapshot.py",
            'verification = record.get("verification") or result_data.get("verification")',
        ),
        (
            "src/chaos_agent/server/routes/turn_event_stream.py",
            'verification = data.get("verification")',
        ),
        (
            "src/chaos_agent/agent/operation_summary.py",
            'verification = data.get("verification")',
        ),
        (
            "src/chaos_agent/server/routes/inject.py",
            '"verification": data.get("verification"),',
        ),
        (
            "src/chaos_agent/cli/client.py",
            '"verification": data.get("verification"),',
        ),
    }

    search_roots = [
        PROJECT_ROOT / "src/chaos_agent/agent",
        PROJECT_ROOT / "src/chaos_agent/server/routes",
        PROJECT_ROOT / "src/chaos_agent/cli",
        PROJECT_ROOT / "src/chaos_agent/l4",
    ]
    findings = set()
    for root in search_roots:
        for path in root.rglob("*.py"):
            rel = path.relative_to(PROJECT_ROOT).as_posix()
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if '.get("verification")' in stripped or '.get("recover_verification")' in stripped:
                    findings.add((rel, stripped))

    unexpected = findings - allowed
    assert unexpected == set()


def test_verification_projection_owner_is_operation_outcome():
    """Agent/CLI code should not depend on memory.session_store for projection."""

    checked_files = [
        "src/chaos_agent/agent/operation_summary.py",
        "src/chaos_agent/cli/runner.py",
    ]
    violations = []
    for rel in checked_files:
        text = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
        if "from chaos_agent.memory.session_store import build_verification_simple" in text:
            violations.append(rel)

    assert violations == []


def test_known_state_consumers_do_not_use_legacy_outcome_reads():
    """Lock CLI/Server summary/finalize paths onto operation_outcome helpers."""

    forbidden_by_file = {
        "src/chaos_agent/server/routes/inject.py": [
            'values_fin.get("verification")',
            'values_fin.get("failure_reason")',
            'values_fin.get("error")',
        ],
        "src/chaos_agent/server/routes/turn_event_stream.py": [
            '_psv.get("verification")',
            '_vals.get("error")',
            '_vals.get("failure_reason")',
        ],
        "src/chaos_agent/cli/runner.py": [
            'pv.get("verification")',
            'pv.get("failure_reason")',
            'pv.get("error")',
            '_vals.get("failure_reason")',
            '_vals.get("error")',
            'result.get("recover_verification")',
            'result.get("failure_reason")',
            'result.get("error")',
            'values_fin.get("recover_verification")',
            'values_fin.get("failure_reason")',
            'values_fin.get("error")',
        ],
        "src/chaos_agent/cli/result_builder.py": [
            'values.get("failure_reason")',
            'values.get("error")',
        ],
        "src/chaos_agent/l4/agent.py": [
            'recover_result.get("recover_verification")',
        ],
    }

    violations = []
    for rel, forbidden_snippets in forbidden_by_file.items():
        text = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
        for snippet in forbidden_snippets:
            if snippet in text:
                violations.append(f"{rel}: {snippet}")

    assert violations == []
