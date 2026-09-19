"""Tests for the drill-failure issue-report subsystem (builder + publisher).

Covers the plan's test matrix:
- should_publish_issue three-state gating (switch / intent / failure)
- redact regex shapes (Authorization / GH token / password / CLI / kubeconfig)
- 60K body truncation
- publisher with mocked httpx (201 → success, 401 / timeout → failed)
- daily cap skip + local archive on every outcome
- wiring: result envelope carries ``issue_report``
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from chaos_agent.agent.issue_report.builder import (
    MAX_BODY_CHARS,
    build_issue_body,
    build_issue_title,
    redact,
    should_publish_issue,
)
from chaos_agent.agent.issue_report.publisher import publish_issue_report
from chaos_agent.config.settings import settings
from chaos_agent.utils.time import now_iso


# ─── fixtures / helpers ─────────────────────────────────────────────


class _GateSettings:
    """Minimal settings stand-in for gate testing. The token doubles
    as the on/off switch — configured = on, empty = off."""

    def __init__(self, *, token="ghp_xxxxxxxxxxxxxxxxxxxx"):
        self.github_token = token


def _failed_inject_state(**extra) -> dict:
    state = {
        "confirmed_intent": "inject",
        "fault_spec": {"scope": "pod", "fault_target": "cpu", "fault_action": "fullload"},
        "failure_detail": {"category": "execution_failed"},
    }
    state.update(extra)
    return state


class _FakeResponse:
    def __init__(self, status_code=201, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self):
        return self._json


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient inside publish_issue_report."""

    response: _FakeResponse | None = None
    exc: Exception | None = None
    last_call: dict | None = None

    def __init__(self, timeout=None):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        _FakeAsyncClient.last_call = {"url": url, "json": json, "headers": headers}
        if _FakeAsyncClient.exc is not None:
            raise _FakeAsyncClient.exc
        return _FakeAsyncClient.response


@pytest.fixture
def fake_httpx(monkeypatch):
    _FakeAsyncClient.response = None
    _FakeAsyncClient.exc = None
    _FakeAsyncClient.last_call = None
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    return _FakeAsyncClient


@pytest.fixture
def token_settings(monkeypatch):
    monkeypatch.setattr(settings, "github_token", "ghp_xxxxxxxxxxxxxxxxxxxx")
    monkeypatch.setattr(settings, "issue_report_repo", "chaosblade-io/chaosblade")
    monkeypatch.setattr(settings, "issue_report_daily_cap", 5)


# ─── should_publish_issue gating ────────────────────────────────────


class TestShouldPublish:
    def test_off_when_no_token(self):
        """Token IS the switch: empty / whitespace-only = feature off."""
        for token in ("", "   ", None):
            s = _GateSettings(token=token)
            assert should_publish_issue(_failed_inject_state(), s) is False, token

    def test_off_for_non_inject_intent(self):
        s = _GateSettings()
        for intent in ("chat", "recover", None, ""):
            state = _failed_inject_state(confirmed_intent=intent)
            assert should_publish_issue(state, s) is False, intent

    def test_off_when_no_failure(self):
        """Card-badge parity: a verified clean inject never publishes."""
        s = _GateSettings()
        state = {
            "confirmed_intent": "inject",
            "fault_spec": {"scope": "pod", "fault_target": "cpu", "fault_action": "fullload"},
            "experiment_uid": "uid-clean",
            "verification": {
                "level": "verified",
                "layer1": {"status": "passed"},
                "layer2": {"status": "passed"},
            },
            "result": {"success": True},
        }
        assert should_publish_issue(state, s) is False

    def test_on_with_failure_detail(self):
        s = _GateSettings()
        assert should_publish_issue(_failed_inject_state(), s) is True

    def test_on_with_error_only(self):
        s = _GateSettings()
        state = _failed_inject_state(failure_detail=None, error="blade crashed")
        assert should_publish_issue(state, s) is True

    def test_off_when_error_but_verification_passed(self):
        """Card-badge parity regression: a lingering error must not
        publish when the verifier confirmed the fault took effect —
        the card shows INJECTED there, so no report is attempted.
        (Old ``failure_detail or error`` gate would have published.)"""
        s = _GateSettings()
        state = _failed_inject_state(
            failure_detail=None,
            error="earlier attempt timed out",
            experiment_uid="uid-x",
            verification={
                "level": "verified",
                "layer1": {"status": "passed"},
                "layer2": {"status": "passed"},
            },
        )
        assert should_publish_issue(state, s) is False

    def test_off_for_safety_rejection(self):
        """A rejection is the guardrail working as intended, not a drill
        failure — the card shows REJECTED, never FAILED, so never publish."""
        s = _GateSettings()
        state = _failed_inject_state(
            failure_detail=None, error="blocked", safety_status="rejected",
        )
        assert should_publish_issue(state, s) is False

    def test_off_when_unverified(self):
        """Card-badge parity: L1 passed but level=unverified is no longer
        FAILED on the card (it is its own "unverified" knowledge claim —
        observation unavailable, no counter-evidence), so no issue is
        attempted. The local postmortem still records the run; the GitHub
        issue is reserved for actual drill failures."""
        s = _GateSettings()
        state = _failed_inject_state(
            failure_detail=None,
            experiment_uid="uid-y",
            verification={
                "level": "unverified",
                "layer1": {"status": "passed"},
                "layer2": {"status": "unknown"},
            },
        )
        assert should_publish_issue(state, s) is False

    def test_off_for_user_rejected_category(self):
        """Pre-execution rejection — the user said no at the confirm
        gate. That is the guardrail working as intended, not a drill
        failure; same skip list as the postmortem gate keeps it out of
        the failure-report pool (task-349ccf5d funnel: reject now flows
        through terminal_reports, so this category filter is what stops
        the upload)."""
        s = _GateSettings()
        state = _failed_inject_state(
            failure_detail={
                "category": "user_rejected",
                "context": "user said no at confirm gate",
            },
        )
        assert should_publish_issue(state, s) is False

    def test_off_for_safety_rejected_category(self):
        s = _GateSettings()
        state = _failed_inject_state(
            failure_detail={
                "category": "safety_rejected",
                "context": "namespace in blacklist",
            },
        )
        assert should_publish_issue(state, s) is False

    def test_on_for_execution_failure_category(self):
        """Execution-class failures still publish — the category filter
        only removes the pre-execution rejections and planning_timeout."""
        s = _GateSettings()
        state = _failed_inject_state(
            failure_detail={
                "category": "execution_failed",
                "context": "blade create crashed",
            },
        )
        assert should_publish_issue(state, s) is True

    def test_off_for_planning_timeout_category(self):
        """Budget/config exhaustion (max planning iterations hit before
        any plan existed) is a resource issue, not a drill finding —
        deliberate asymmetry: it still gets a LOCAL postmortem (the
        postmortem skip list does NOT include it), it just never
        publishes to GitHub."""
        s = _GateSettings()
        state = _failed_inject_state(
            failure_detail={
                "category": "planning_timeout",
                "context": "max_iterations exceeded",
            },
        )
        assert should_publish_issue(state, s) is False

    def test_on_for_planning_rejected_category(self):
        """planning_rejected is a systemic failure mode (the agent could
        not produce an acceptable plan — task-349ccf5d itself) and stays
        in the failure-report pool: postmortem AND issue."""
        s = _GateSettings()
        state = _failed_inject_state(
            failure_detail={
                "category": "planning_rejected",
                "context": "plan rejected by safety review twice",
            },
        )
        assert should_publish_issue(state, s) is True


# ─── redact ─────────────────────────────────────────────────────────


class TestRedact:
    def test_authorization_header(self):
        out = redact("curl -H 'Authorization: Bearer abcdef1234567890' https://x")
        assert "abcdef1234567890" not in out
        assert "[REDACTED]" in out

    def test_github_pat(self):
        tok = "ghp_" + "A" * 36
        out = redact(f"token leaked in log: {tok}")
        assert tok not in out
        assert "REDACTED" in out

    def test_key_value_secrets(self):
        out = redact('{"password": "hunter2secret", "api_key": "sk-1234567890"}')
        assert "hunter2secret" not in out
        assert "sk-1234567890" not in out

    def test_cli_flag_secrets(self):
        out = redact("blade ssh --password s3cr3tpass user@host")
        assert "s3cr3tpass" not in out

    def test_kubeconfig_credential_blobs(self):
        blob = "client-key-data: " + "QUJDREVGRw==" * 4
        out = redact(blob)
        assert "QUJDREVGRw==" not in out

    def test_plain_text_passes_through(self):
        text = "pod cpu fullload failed: kubectl timed out"
        assert redact(text) == text

    def test_empty(self):
        assert redact("") == ""


# ─── title / body ───────────────────────────────────────────────────


class TestBuildIssue:
    def test_title_carries_category_and_fault_triple(self):
        title = build_issue_title(_failed_inject_state())
        assert title == (
            "[blade-ai drill failure] execution_failed — pod/cpu/fullload"
        )

    def test_title_falls_back_to_unknown_category(self):
        title = build_issue_title({"confirmed_intent": "inject"})
        assert title.startswith("[blade-ai drill failure] unknown —")

    def test_body_contains_all_sections(self):
        pm = {"markdown": "## Root cause\nblade timeout", "path": "/tmp/pm.md"}
        body = build_issue_body(
            _failed_inject_state(), "task-1", pm, archive_path="/tmp/arch.json",
        )
        assert "## Environment" in body
        assert "## Postmortem Analysis" in body
        assert "blade timeout" in body
        assert "## Local Archive (not uploaded)" in body
        assert "/tmp/arch.json" in body
        # SessionStore convention: <memory_dir>/tasks/<task_id>.json
        assert "tasks/task-1.json" in body
        # the postmortem md is the sole diagnostic payload — the old
        # message-scanned timeline / envelope summary are gone
        assert "## Execution Timeline" not in body
        assert "## Execution Record Summary" not in body
        assert "<details>" not in body

    def test_body_redacts_embedded_secrets(self):
        tok = "ghp_" + "B" * 36
        pm = {"markdown": f"token leaked: {tok}", "path": ""}
        body = build_issue_body(_failed_inject_state(), "task-1", pm)
        assert tok not in body
        assert "[REDACTED_GH_TOKEN]" in body

    def test_huge_postmortem_shrinks_to_own_budget(self):
        """Budget assembly: a 70K postmortem is truncated in place to
        its own budget with an explicit marker — never pushed out."""
        pm = {"markdown": "x" * 70_000, "path": ""}
        body = build_issue_body(_failed_inject_state(), "task-1", pm)
        assert len(body) <= MAX_BODY_CHARS
        assert "postmortem truncated" in body
        # every section still present
        assert "## Postmortem Analysis" in body
        assert "## Local Archive (not uploaded)" in body

    def test_body_without_postmortem(self):
        body = build_issue_body(_failed_inject_state(), "task-1", None)
        assert "_(not generated)_" in body


class TestNoMessageScanning:
    """Conversation messages must never enter a PUBLIC issue.

    The postmortem md's own Timeline / Key Metrics / Verifier Findings
    sections carry the execution evidence, so the message-scanned tool
    timeline was removed; these tests pin the contract so a future
    "let's re-add the timeline" change cannot slip through silently.
    """

    def test_message_content_never_enters_body(self):
        state = _failed_inject_state(messages=[
            {"type": "system", "content": "you are chaos agent"},
            {"type": "human", "content": "inject cpu fault"},
            {
                "type": "ai",
                "content": "injection issued, verifying",
                "tool_calls": [{
                    "name": "blade_create",
                    "args": {"scope": "pod", "target": "cpu"},
                }],
            },
            {"type": "tool", "name": "blade_create", "content": "uid: abc-123"},
        ])
        body = build_issue_body(state, "task-ns", None)
        assert "injection issued, verifying" not in body
        assert "uid: abc-123" not in body
        assert "you are chaos agent" not in body
        assert "inject cpu fault" not in body

    def test_huge_messages_do_not_grow_the_body(self):
        msgs = [
            {"type": "tool", "name": "kubectl", "content": f"e-{i} " + "z" * 440}
            for i in range(400)
        ]
        pm = {"markdown": "## Root cause\nblade create timed out" * 20, "path": ""}
        state = _failed_inject_state(messages=msgs)
        body = build_issue_body(state, "task-ns2", pm)
        assert len(body) <= MAX_BODY_CHARS
        assert "postmortem truncated" not in body  # intact, nothing squeezed it
        assert "## Root cause" in body

    def test_envelope_summary_no_longer_embedded(self):
        """Result-envelope fields (blade_uid etc.) stay out of the body —
        the full record is referenced by local path instead."""
        state = _failed_inject_state(experiment_uid="uid-secret-marker-123")
        body = build_issue_body(state, "task-ns3", None)
        assert "uid-secret-marker-123" not in body
        assert "tasks/task-ns3.json" in body  # local reference survives


# ─── publisher ──────────────────────────────────────────────────────


class TestPublisher:
    async def test_no_token_fails_fast_without_archive(
        self, monkeypatch, tmp_path,
    ):
        """Defensive path only — the gate requires a token, so normal
        runs never reach this. Direct calls fail fast and write NO
        archive: nothing was attempted."""
        monkeypatch.setattr(settings, "github_token", "")
        payload = await publish_issue_report(
            _failed_inject_state(), "task-nt", None, root=tmp_path,
        )
        assert payload["status"] == "failed"
        assert "no GitHub token" in payload["error"]
        assert not (tmp_path / "task-nt.json").exists()

    async def test_daily_cap_skips(self, tmp_path, token_settings):
        today = now_iso()[:10]
        (tmp_path / "_ledger.json").write_text(
            json.dumps({"date": today, "count": 5}),
        )
        payload = await publish_issue_report(
            _failed_inject_state(), "task-cap", None, root=tmp_path,
        )
        assert payload["status"] == "skipped_cap"
        record = json.loads((tmp_path / "task-cap.json").read_text())
        assert record["status"] == "skipped_cap"

    async def test_201_success_bumps_ledger_and_patches_archive(
        self, tmp_path, token_settings, fake_httpx,
    ):
        fake_httpx.response = _FakeResponse(
            201, {"html_url": "https://github.com/chaosblade-io/chaosblade/issues/9"},
        )
        payload = await publish_issue_report(
            _failed_inject_state(), "task-ok", {"markdown": "pm", "path": ""},
            root=tmp_path,
        )
        assert payload["status"] == "success"
        assert payload["issue_url"].endswith("/issues/9")
        # Ledger counted one successful publish.
        ledger = json.loads((tmp_path / "_ledger.json").read_text())
        assert ledger == {"date": now_iso()[:10], "count": 1}
        # Archive record patched to final outcome — and the FULL body
        # must survive the patch (durable-record contract).
        record = json.loads((tmp_path / "task-ok.json").read_text())
        assert record["status"] == "success"
        assert record["issue_url"].endswith("/issues/9")
        assert "body" in record and "## Environment" in record["body"]
        assert record["repo"] == "chaosblade-io/chaosblade"
        assert record["created_at"]
        # Request shape: right URL + Bearer token.
        call = fake_httpx.last_call
        assert call["url"] == (
            "https://api.github.com/repos/chaosblade-io/chaosblade/issues"
        )
        assert call["headers"]["Authorization"].startswith("Bearer ")
        assert call["json"]["title"].startswith("[blade-ai drill failure]")

    async def test_401_fails_and_archives(
        self, tmp_path, token_settings, fake_httpx,
    ):
        fake_httpx.response = _FakeResponse(401, text='{"message": "Bad credentials"}')
        payload = await publish_issue_report(
            _failed_inject_state(), "task-401", None, root=tmp_path,
        )
        assert payload["status"] == "failed"
        assert "HTTP 401" in payload["error"]
        assert payload["archive_path"]
        record = json.loads((tmp_path / "task-401.json").read_text())
        assert record["status"] == "failed"
        assert "HTTP 401" in record["error"]
        assert "body" in record  # full report survives the failure patch
        # Failed posts must NOT count toward the daily cap.
        assert not (tmp_path / "_ledger.json").exists()

    async def test_timeout_fails_without_retry(
        self, tmp_path, token_settings, fake_httpx,
    ):
        fake_httpx.exc = httpx.TimeoutException("connect timed out")
        payload = await publish_issue_report(
            _failed_inject_state(), "task-to", None, root=tmp_path,
        )
        assert payload["status"] == "failed"
        assert "timeout" in payload["error"]
        assert Path(payload["archive_path"]).exists()
        assert not (tmp_path / "_ledger.json").exists()

    async def test_duplicate_finalize_does_not_repost(
        self, tmp_path, token_settings, fake_httpx,
    ):
        """Archive receipt dedups: an already-successful task never posts twice."""
        (tmp_path / "task-dup.json").write_text(json.dumps({
            "task_id": "task-dup",
            "status": "success",
            "issue_url": "https://github.com/x/y/issues/7",
        }))
        fake_httpx.response = _FakeResponse(201, {"html_url": "NEW"})
        payload = await publish_issue_report(
            _failed_inject_state(), "task-dup", None, root=tmp_path,
        )
        assert payload["status"] == "success"
        assert payload["issue_url"].endswith("/issues/7")  # original, not NEW
        assert fake_httpx.last_call is None  # no second POST

    async def test_failed_archive_allows_retry(
        self, tmp_path, token_settings, fake_httpx,
    ):
        """A prior FAILED attempt is not terminal — retry may publish."""
        (tmp_path / "task-retry.json").write_text(json.dumps({
            "task_id": "task-retry", "status": "failed",
        }))
        fake_httpx.response = _FakeResponse(
            201, {"html_url": "https://github.com/x/y/issues/8"},
        )
        payload = await publish_issue_report(
            _failed_inject_state(), "task-retry", None, root=tmp_path,
        )
        assert payload["status"] == "success"
        assert fake_httpx.last_call is not None

    async def test_cap_zero_is_hard_shut_not_unlimited(
        self, monkeypatch, tmp_path, token_settings, fake_httpx,
    ):
        """daily_cap=0 means 'publish nothing', never 'no limit'."""
        monkeypatch.setattr(settings, "issue_report_daily_cap", 0)
        fake_httpx.response = _FakeResponse(201, {"html_url": "x"})
        payload = await publish_issue_report(
            _failed_inject_state(), "task-zero", None, root=tmp_path,
        )
        assert payload["status"] == "skipped_cap"
        assert fake_httpx.last_call is None  # no POST attempted

    async def test_archive_written_before_post_even_on_body_error(
        self, monkeypatch, tmp_path, token_settings, fake_httpx,
    ):
        """publish_issue_report never raises, even on internal errors."""
        monkeypatch.setattr(
            "chaos_agent.agent.issue_report.publisher.build_issue_body",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        payload = await publish_issue_report(
            _failed_inject_state(), "task-err", None, root=tmp_path,
        )
        assert payload["status"] == "failed"
        assert "RuntimeError" in payload["error"]


# ─── body hygiene: stale same-thread payloads never embedded ──────


class TestBodyHygiene:
    def test_stale_issue_report_not_embedded_in_new_body(self):
        state = _failed_inject_state(
            issue_report={"status": "success", "issue_url": "https://stale/1"},
        )
        body = build_issue_body(state, "task-h", None)
        assert "https://stale/1" not in body


# ─── config surface: PAT must be masked in `blade-ai config` ──────


class TestConfigMasking:
    def test_github_token_is_sensitive(self):
        from chaos_agent.cli.config_manager import SENSITIVE_KEYS

        assert "github_token" in SENSITIVE_KEYS


# ─── wiring: result envelope carries issue_report ───────────────────


class TestEnvelopeWiring:
    def test_inject_envelope_includes_issue_report(self):
        from chaos_agent.agent.result.operation_result import (
            build_inject_data_from_state,
        )

        report = {"status": "success", "issue_url": "https://github.com/x/issues/1"}
        state = _failed_inject_state(issue_report=report)
        data = build_inject_data_from_state(state, "task-w")
        assert data["issue_report"] == report

    def test_inject_envelope_issue_report_none_when_absent(self):
        from chaos_agent.agent.result.operation_result import (
            build_inject_data_from_state,
        )

        data = build_inject_data_from_state(_failed_inject_state(), "task-w2")
        assert "issue_report" in data
        assert data["issue_report"] is None

    def test_unknown_inject_envelope_has_null_issue_report(self):
        from chaos_agent.agent.result.operation_result import build_unknown_inject_data

        data = build_unknown_inject_data("task-u")
        assert data["issue_report"] is None
