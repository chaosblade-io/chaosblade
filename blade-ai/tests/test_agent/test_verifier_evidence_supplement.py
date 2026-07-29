"""Tests for the verify-side deterministic host evidence supplement (Q2).

A fast, strong host fault (e.g. CPU fullload) legitimately concludes on the
first observation, but the LLM's metric probes rarely include host identity —
leaving target_identity uncovered. finalize_verification runs cheap read-only
host probes to close that gap without forcing a re-verify loop.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from chaos_agent.agent.evidence import EvidenceProfile
from chaos_agent.agent.nodes.verify._verifier_finalize import (
    _record_evidence_text,
    _supplement_host_verification_evidence,
)
from chaos_agent.agent.spec.fault_spec import FaultSpec


def _result(exit_code: int = 0, stdout: str = "out") -> SimpleNamespace:
    return SimpleNamespace(exit_code=exit_code, stdout=stdout, stderr="")


def test_record_evidence_text_flattens_dict():
    text = _record_evidence_text(
        {"description": "Host CPU", "command": "top -bn1", "stdout": "us 80"}
    )
    assert "Host CPU" in text
    assert "top -bn1" in text
    assert _record_evidence_text("raw string") == "raw string"
    assert _record_evidence_text(None) == ""


def test_supplement_adds_identity_and_cross_for_cpu():
    spec = FaultSpec(scope="host", blade_target="cpu")
    existing = [{"description": "CPU", "command": "vmstat 1 2", "stdout": "us 80 cpu"}]
    calls: list[list[str]] = []

    async def fake_exec(cmd, target, **kw):
        calls.append(cmd)
        return _result(
            stdout={"hostname": "host-01", "uptime": "load average: 12"}.get(cmd[0], "out")
        )

    with patch("chaos_agent.transports.execute_via_transport", side_effect=fake_exec):
        records = asyncio.run(
            _supplement_host_verification_evidence(
                spec,
                {"target_identity", "independent_cross_metric"},
                existing,
                {"host_name": "host-01"},
            )
        )

    probed = [c[0] for c in calls]
    assert "hostname" in probed
    assert "uptime" in probed
    descriptions = {r["description"] for r in records}
    assert descriptions == {"Host identity", "Host cross-check"}


def test_supplement_skips_when_evidence_already_present():
    spec = FaultSpec(scope="host", blade_target="cpu")
    existing = [
        {"description": "Host identity", "command": "hostname", "stdout": "host-01"},
        {"description": "load", "command": "uptime", "stdout": "load average"},
    ]

    async def fake_exec(cmd, target, **kw):
        raise AssertionError("must not probe when evidence already present")

    with patch("chaos_agent.transports.execute_via_transport", side_effect=fake_exec):
        records = asyncio.run(
            _supplement_host_verification_evidence(
                spec,
                {"target_identity", "independent_cross_metric"},
                existing,
                {},
            )
        )
    assert records == []


def test_supplement_records_close_coverage():
    spec = FaultSpec(scope="host", blade_target="cpu")
    profile = EvidenceProfile.for_fault(spec, "host")
    existing = [{"description": "CPU", "command": "vmstat 1 2", "stdout": "us 80 cpu"}]
    pre = profile.coverage(existing)
    assert "target_identity" in pre.missing

    async def fake_exec(cmd, target, **kw):
        return _result(
            stdout={"hostname": "host-01", "uptime": "load average: 12"}.get(cmd[0], "out")
        )

    with patch("chaos_agent.transports.execute_via_transport", side_effect=fake_exec):
        records = asyncio.run(
            _supplement_host_verification_evidence(
                spec, set(pre.missing), existing, {"host_name": "host-01"},
            )
        )

    post = profile.coverage(existing + records)
    assert post.complete is True


def test_supplement_best_effort_on_probe_failure():
    spec = FaultSpec(scope="host", blade_target="cpu")
    existing = [{"description": "CPU", "command": "vmstat 1 2", "stdout": "us 80 cpu"}]

    async def fake_exec(cmd, target, **kw):
        return _result(exit_code=1, stdout="")

    with patch("chaos_agent.transports.execute_via_transport", side_effect=fake_exec):
        records = asyncio.run(
            _supplement_host_verification_evidence(
                spec,
                {"target_identity", "independent_cross_metric"},
                existing,
                {"host_name": "host-01"},
            )
        )
    # Failed probes yield no records; the advisory coverage warning still fires.
    assert records == []
