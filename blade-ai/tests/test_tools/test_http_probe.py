"""Tests for ``chaos_agent.tools.http_probe_cli`` — the typed L7 HTTP probe
(L1 root-cause cure for Case #84).

The tool is the verify / recover-verify phase's replacement for an
LLM-hand-written ``kubectl_read exec ... curl``. Three contracts are pinned
here, each mapping to one leg of the root cause:

  1. FAIL-CLOSED INPUT VALIDATION is the real safety boundary. The classifier
     trusts ``http_probe`` by NAME (it can only run the framework-built GET),
     so injection safety lives entirely in the url / name / max_time
     allowlists. Every shell metacharacter path must be rejected BEFORE the
     exec payload is built.
  2. FRAMEWORK-OWNED COMMAND SHAPE + DELEGATION. The tool resolves the curl
     through the SAME typed-probe registry the execute phase uses and
     delegates to ``_kubectl_impl(..., probe_mode=True)`` so a non-zero curl
     exit (the probe's answer) is not dressed as a channel error.
  3. BORN-STRUCTURED OUTPUT. The result is JSON with the canonical metric
     keys the extractor and the cross-check consume — no reverse-parsing.
"""

from __future__ import annotations

import json

import pytest

from chaos_agent.agent.nodes.execute._effect_probes import HTTP_BODY_SENTINEL
from chaos_agent.tools import http_probe_cli
from chaos_agent.tools.http_probe_cli import _parse_curl_w, http_probe


# ---------------------------------------------------------------------------
# _parse_curl_w — the write-out line parser
# ---------------------------------------------------------------------------


class TestParseCurlW:
    def test_parses_canonical_write_out_line(self):
        raw = (
            "connect=0.004 starttransfer=0.011 http=503 exit=0 len=0\n"
        )
        assert _parse_curl_w(raw) == {
            "time_connect": "0.004",
            "time_starttransfer": "0.011",
            "http_code": "503",
            "exit_code": "0",
            "size_download": "0",
        }

    def test_ignores_unrecognised_tokens(self):
        # A stray curl notice / unknown name=value on the same stream must not
        # invent a metric.
        raw = "curl: (7) failed http=000 exit=7 whatever=9 note=hi"
        assert _parse_curl_w(raw) == {"http_code": "000", "exit_code": "7"}

    def test_token_without_equals_skipped(self):
        assert _parse_curl_w("garbage http=200") == {"http_code": "200"}

    def test_empty_and_none_return_empty(self):
        assert _parse_curl_w("") == {}
        assert _parse_curl_w("   \n ") == {}


# ---------------------------------------------------------------------------
# Fail-closed input validation (the real safety boundary)
# ---------------------------------------------------------------------------


class _Recorder:
    """Async stand-in for ``_kubectl_impl`` that records its call args."""

    def __init__(self, canned: str = ""):
        self.canned = canned
        self.calls: list[tuple] = []

    async def __call__(self, subcommand, v_args, kubeconfig, **kwargs):
        self.calls.append((subcommand, v_args, kubeconfig, kwargs))
        return self.canned


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder(
        canned="connect=0.004 starttransfer=0.011 http=200 exit=0 len=42\n"
    )
    monkeypatch.setattr(http_probe_cli, "_kubectl_impl", rec)
    return rec


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
    async def test_bad_url_rejected_before_exec(self, recorder, bad_url):
        result = await http_probe.ainvoke({
            "origin_pod": "client", "url": bad_url,
            "namespace": "default", "kubeconfig": "",
        })
        assert result.startswith("Error:"), result
        # the exec payload must never be built for a rejected url
        assert recorder.calls == []

    @pytest.mark.parametrize("good_url", [
        "http://svc.default.svc.cluster.local/health",
        "http://backend:8080/live",
        "https://api.example.com/v1/status",
        "http://10.0.0.5:8080/",
    ])
    async def test_clean_url_accepted(self, recorder, good_url):
        result = await http_probe.ainvoke({
            "origin_pod": "client", "url": good_url,
            "namespace": "default", "kubeconfig": "",
        })
        assert not result.startswith("Error:"), result
        assert len(recorder.calls) == 1


class TestNameValidationFailClosed:
    @pytest.mark.parametrize("bad_pod", [
        "Client",                 # uppercase
        "cli ent",                # whitespace
        "client;rm",              # metacharacter
        "client$(id)",            # substitution
        "",                       # empty
    ])
    async def test_bad_origin_pod_rejected(self, recorder, bad_pod):
        result = await http_probe.ainvoke({
            "origin_pod": bad_pod, "url": "http://svc/health",
            "namespace": "default", "kubeconfig": "",
        })
        assert result.startswith("Error:"), result
        assert recorder.calls == []

    async def test_bad_namespace_rejected(self, recorder):
        result = await http_probe.ainvoke({
            "origin_pod": "client", "url": "http://svc/health",
            "namespace": "Default;rm", "kubeconfig": "",
        })
        assert result.startswith("Error:"), result
        assert recorder.calls == []


class TestMaxTimeBounds:
    # Two fail-closed layers: the tool signature types ``max_time`` as ``int``,
    # so a non-int is rejected by the pydantic schema BEFORE the body runs; an
    # in-type but out-of-range int reaches the body's explicit 1-60 bound.
    @pytest.mark.parametrize("bad_t", [0, -1, 61, 999])
    async def test_out_of_range_int_max_time_rejected_in_body(self, recorder, bad_t):
        result = await http_probe.ainvoke({
            "origin_pod": "client", "url": "http://svc/health",
            "namespace": "default", "max_time": bad_t, "kubeconfig": "",
        })
        assert result.startswith("Error:"), result
        assert recorder.calls == []

    @pytest.mark.parametrize("bad_t", ["abc", None, 3.5])
    async def test_non_int_max_time_rejected_by_schema(self, recorder, bad_t):
        from pydantic_core import ValidationError

        with pytest.raises(ValidationError):
            await http_probe.ainvoke({
                "origin_pod": "client", "url": "http://svc/health",
                "namespace": "default", "max_time": bad_t, "kubeconfig": "",
            })
        assert recorder.calls == []

    @pytest.mark.parametrize("good_t", [1, 10, 60])
    async def test_in_range_max_time_accepted(self, recorder, good_t):
        result = await http_probe.ainvoke({
            "origin_pod": "client", "url": "http://svc/health",
            "namespace": "default", "max_time": good_t, "kubeconfig": "",
        })
        assert not result.startswith("Error:"), result


# ---------------------------------------------------------------------------
# Delegation + born-structured output
# ---------------------------------------------------------------------------


class TestDelegationAndOutput:
    async def test_delegates_exec_with_probe_mode(self, recorder):
        await http_probe.ainvoke({
            "origin_pod": "drill-http-client", "url": "http://svc/health",
            "namespace": "chaos", "max_time": 5, "kubeconfig": "/kc",
        })
        assert len(recorder.calls) == 1
        subcommand, v_args, kubeconfig, kwargs = recorder.calls[0]
        assert subcommand == "exec"
        assert kubeconfig == "/kc"
        # probe_mode is the opt-in flag that makes a non-zero curl exit an
        # answer rather than a channel error (the C1 contract).
        assert kwargs.get("probe_mode") is True
        # v_args = "<pod> -n <ns> -- <framework-built curl>"
        assert v_args.startswith("drill-http-client -n chaos -- curl ")
        assert "--max-time 5 http://svc/health" in v_args

    async def test_output_is_born_structured_json(self, recorder):
        result = await http_probe.ainvoke({
            "origin_pod": "client", "url": "http://svc/health",
            "namespace": "default", "kubeconfig": "",
        })
        data = json.loads(result)
        # provenance + the 5 canonical metric keys the extractor consumes
        assert data["tool"] == "http_probe"
        assert data["origin_pod"] == "client"
        assert data["namespace"] == "default"
        assert data["url"] == "http://svc/health"
        assert data["http_code"] == "200"
        assert data["time_connect"] == "0.004"
        assert data["time_starttransfer"] == "0.011"
        assert data["exit_code"] == "0"
        assert data["size_download"] == "42"

    async def test_faulted_target_reported_as_data_not_error(self, monkeypatch):
        # A refused connection (exit 7 / http 000) is the OBSERVATION that the
        # fault is live — it must come back as structured data, never as an
        # "Error:" that would read as a channel fault.
        monkeypatch.setattr(
            http_probe_cli, "_kubectl_impl",
            _Recorder(canned="connect=0.000 starttransfer=0.000 http=000 exit=7 len=0\n"),
        )
        result = await http_probe.ainvoke({
            "origin_pod": "client", "url": "http://svc/health",
            "namespace": "default", "kubeconfig": "",
        })
        assert not result.startswith("Error:"), result
        data = json.loads(result)
        assert data["http_code"] == "000"
        assert data["exit_code"] == "7"


class TestErrorPassthrough:
    async def test_kubectl_client_error_passed_through(self, monkeypatch):
        # A real kubectl failure (pod not found / exec refused) carries no
        # probe stdout and must stay an honest "Error:".
        monkeypatch.setattr(
            http_probe_cli, "_kubectl_impl",
            _Recorder(canned="Error: kubectl exec: pods/client not found"),
        )
        result = await http_probe.ainvoke({
            "origin_pod": "client", "url": "http://svc/health",
            "namespace": "default", "kubeconfig": "",
        })
        assert result.startswith("Error:"), result
        assert "not found" in result

    async def test_no_recognised_write_out_surfaces_raw(self, monkeypatch):
        # curl missing in the client image: exec ran but produced no write-out.
        # Surface the raw output so the model picks another client pod rather
        # than silently reporting an empty probe.
        monkeypatch.setattr(
            http_probe_cli, "_kubectl_impl",
            _Recorder(canned="OCI runtime exec failed: curl: executable not found"),
        )
        result = await http_probe.ainvoke({
            "origin_pod": "client", "url": "http://svc/health",
            "namespace": "default", "kubeconfig": "",
        })
        assert result.startswith("Error:"), result
        assert "executable not found" in result


# ---------------------------------------------------------------------------
# probe_mode contract at the _kubectl_impl layer (C1) — non-zero curl exit is
# the probe's answer, not a channel fault, IFF stdout is non-empty.
# ---------------------------------------------------------------------------


class TestProbeModeContract:
    async def test_nonzero_exit_with_stdout_returns_raw_in_probe_mode(self, mocker):
        from chaos_agent.tools.guard import CommandResult
        from chaos_agent.tools.kubectl_cli import _kubectl_impl

        async def _fake(cmd, *a, **kw):
            return CommandResult(
                exit_code=7,
                stdout="connect=0.000 http=000 exit=7 len=0\n",
                stderr="", duration_ms=10.0,
            )

        import sys
        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        mocker.patch.object(kubectl_mod, "execute_via_transport", _fake)

        out = await _kubectl_impl(
            "exec", "client -n default -- curl -s http://svc/health", "",
            probe_mode=True,
        )
        assert not out.startswith("Error:"), out
        assert "http=000" in out

    async def test_nonzero_exit_empty_stdout_stays_error_in_probe_mode(self, mocker):
        # A kubectl CLIENT-side failure writes stderr only ⇒ empty stdout ⇒
        # still a real error even under probe_mode.
        from chaos_agent.tools.guard import CommandResult
        from chaos_agent.tools.kubectl_cli import _kubectl_impl

        async def _fake(cmd, *a, **kw):
            return CommandResult(
                exit_code=1, stdout="", stderr="pods/client not found",
                duration_ms=10.0,
            )

        import sys
        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        mocker.patch.object(kubectl_mod, "execute_via_transport", _fake)

        out = await _kubectl_impl(
            "exec", "client -n default -- curl -s http://svc/health", "",
            probe_mode=True,
        )
        assert out.startswith("Error:"), out

    async def test_default_probe_mode_false_preserves_error_wrapping(self, mocker):
        # The opt-in default must be byte-identical to the pre-L1 behaviour:
        # a non-zero exit WITH stdout is still wrapped as an error for the
        # ordinary kubectl / kubectl_read callers.
        from chaos_agent.tools.guard import CommandResult
        from chaos_agent.tools.kubectl_cli import _kubectl_impl

        async def _fake(cmd, *a, **kw):
            return CommandResult(
                exit_code=7, stdout="http=000 exit=7\n", stderr="",
                duration_ms=10.0,
            )

        import sys
        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        mocker.patch.object(kubectl_mod, "execute_via_transport", _fake)

        out = await _kubectl_impl(
            "exec", "client -n default -- curl -s http://svc/health", "",
        )
        assert out.startswith("Error:"), out


# ---------------------------------------------------------------------------
# capture modes (A2/A7) — WHICH evidence layers the probe returns. The
# framework-built curl branches on capture; the JSON schema carries headers /
# body only when the mode asked for them; an unknown capture is rejected
# fail-closed BEFORE the exec payload is built.
# ---------------------------------------------------------------------------


class TestCaptureCommandShape:
    """Pin the tool→builder contract from the tool side: the curl in v_args
    must branch on capture exactly as the ``http_response`` builder in
    ``_effect_probes.py`` does (metadata/headers keep the ``-o /dev/null``
    discard sink; body/all drop it and stream stdout behind the sentinel as a
    SINGLE shell-free plain curl — NO ``head -c`` pipe, because the exec-form
    channel runs no shell and body size is governed by the framework output
    safety valve, not a payload-side pipe)."""

    async def test_metadata_discards_body(self, recorder):
        await http_probe.ainvoke({
            "origin_pod": "client", "url": "http://svc/health",
            "namespace": "default", "kubeconfig": "",
        })
        v_args = recorder.calls[0][1]
        assert "-o /dev/null" in v_args           # body discarded
        assert "headers=%{header_json}" not in v_args
        assert "head -c" not in v_args

    async def test_headers_keeps_discard_sink_adds_header_json(self, recorder):
        await http_probe.ainvoke({
            "origin_pod": "client", "url": "http://svc/health",
            "namespace": "default", "capture": "headers", "kubeconfig": "",
        })
        v_args = recorder.calls[0][1]
        assert "-o /dev/null" in v_args           # body STILL discarded
        assert "headers=%{header_json}" in v_args
        assert "head -c" not in v_args            # no pipe in any mode

    async def test_body_streams_stdout_with_sentinel_no_pipe(self, recorder):
        await http_probe.ainvoke({
            "origin_pod": "client", "url": "http://svc/health",
            "namespace": "default", "capture": "body", "kubeconfig": "",
        })
        v_args = recorder.calls[0][1]
        assert "-o /dev/null" not in v_args       # body streamed to stdout
        assert HTTP_BODY_SENTINEL in v_args       # write-out behind the sentinel
        # ROOT-FIX: no shell pipe. The old ``| head -c 4096`` became literal
        # argv tokens after ``--`` that curl mis-parsed as extra URLs (fake DNS
        # exit 6) + a ``-c`` cookie-jar write flag. It must never reappear.
        assert "head -c" not in v_args
        assert "|" not in v_args
        assert v_args.rstrip().endswith("http://svc/health")  # single plain curl
        assert "headers=%{header_json}" not in v_args

    async def test_all_streams_body_and_headers_no_pipe(self, recorder):
        await http_probe.ainvoke({
            "origin_pod": "client", "url": "http://svc/health",
            "namespace": "default", "capture": "all", "kubeconfig": "",
        })
        v_args = recorder.calls[0][1]
        assert "-o /dev/null" not in v_args
        assert HTTP_BODY_SENTINEL in v_args
        assert "head -c" not in v_args            # no shell pipe
        assert "|" not in v_args
        assert "headers=%{header_json}" in v_args


class TestCaptureOutputSchema:
    async def test_metadata_schema_has_no_headers_body(self, recorder):
        result = await http_probe.ainvoke({
            "origin_pod": "client", "url": "http://svc/health",
            "namespace": "default", "kubeconfig": "",
        })
        data = json.loads(result)
        assert data["capture"] == "metadata"
        assert "headers" not in data
        assert "body" not in data

    async def test_headers_recovers_multiline_space_bearing_json(self, monkeypatch):
        # curl 8.7.1's ``%{header_json}`` is a MULTI-LINE, space-bearing JSON
        # object (Date values carry spaces). The whitespace-token
        # ``_parse_curl_w`` would shred it into unrecognised tokens and drop the
        # headers; ``_split_headers`` recovers it by SLICING everything after
        # the trailing ``headers=`` marker, so the blob survives intact.
        monkeypatch.setattr(http_probe_cli, "_kubectl_impl", _Recorder(canned=(
            "connect=0.004 starttransfer=0.011 http=200 exit=0 len=42 "
            "headers={\n"
            '  "Date": ["Tue, 21 Oct 2026 00:00:00 GMT"],\n'
            '  "X-Drill-Injected": ["true"]\n'
            "}\n"
        )))
        result = await http_probe.ainvoke({
            "origin_pod": "client", "url": "http://svc/health",
            "namespace": "default", "capture": "headers", "kubeconfig": "",
        })
        data = json.loads(result)
        assert data["capture"] == "headers"
        assert "body" not in data
        # the 5 metrics are still parsed cleanly (they precede ``headers=``)
        assert data["http_code"] == "200"
        assert data["exit_code"] == "0"
        hdrs = json.loads(data["headers"])
        assert hdrs["X-Drill-Injected"] == ["true"]
        assert hdrs["Date"] == ["Tue, 21 Oct 2026 00:00:00 GMT"]

    async def test_body_splits_verbatim_on_sentinel(self, monkeypatch):
        monkeypatch.setattr(http_probe_cli, "_kubectl_impl", _Recorder(canned=(
            '{"status":"TAMPERED"}\n'
            f"{HTTP_BODY_SENTINEL}\n"
            "connect=0.004 starttransfer=0.011 http=200 exit=0 len=19\n"
        )))
        result = await http_probe.ainvoke({
            "origin_pod": "client", "url": "http://svc/health",
            "namespace": "default", "capture": "body", "kubeconfig": "",
        })
        data = json.loads(result)
        assert data["capture"] == "body"
        assert "headers" not in data
        # body verbatim — the single sentinel-prefix newline is stripped, the
        # body's own content is untouched
        assert data["body"] == '{"status":"TAMPERED"}'
        assert data["http_code"] == "200"
        assert data["exit_code"] == "0"

    async def test_all_carries_both_layers(self, monkeypatch):
        monkeypatch.setattr(http_probe_cli, "_kubectl_impl", _Recorder(canned=(
            "TAMPERED-BODY\n"
            f"{HTTP_BODY_SENTINEL}\n"
            "connect=0.004 starttransfer=0.011 http=200 exit=0 len=13 "
            'headers={"Content-Type":["text/plain"]}\n'
        )))
        result = await http_probe.ainvoke({
            "origin_pod": "client", "url": "http://svc/health",
            "namespace": "default", "capture": "all", "kubeconfig": "",
        })
        data = json.loads(result)
        assert data["capture"] == "all"
        assert data["body"] == "TAMPERED-BODY"
        assert json.loads(data["headers"])["Content-Type"] == ["text/plain"]
        assert data["http_code"] == "200"


class TestCaptureFailClosed:
    @pytest.mark.parametrize("bad_capture", [
        "HEADERS", "Metadata", "everything", "body;rm -rf /", "",
        "-o", "header", "body all", "Metadata\n",
    ])
    async def test_unknown_capture_rejected_before_exec(self, recorder, bad_capture):
        # ``capture`` is a closed enum validated fail-closed: an unknown value
        # is refused BEFORE the exec payload is built, exactly like a bad url /
        # max_time — no shell metacharacter can ride it into the command.
        result = await http_probe.ainvoke({
            "origin_pod": "client", "url": "http://svc/health",
            "namespace": "default", "capture": bad_capture, "kubeconfig": "",
        })
        assert result.startswith("Error:"), result
        assert "capture" in result
        assert recorder.calls == []


# ---------------------------------------------------------------------------
# resolve — the curl ``--resolve host:port:addr`` DNS override that lets the
# typed probe reach a VIRTUAL-HOST-routed endpoint (an Ingress controller
# Service ClusterIP that routes by Host, a TLS gateway by SNI) instead of
# pushing the verifier onto a hand-written ``curl -H 'Host:'`` the metric
# extractor will not parse. Fail-closed allowlist validation is the safety
# boundary; the value threads into the framework-built curl as a single
# ``--resolve`` option placed JUST BEFORE the URL; ABSENT keeps the command and
# the JSON byte-for-byte identical to every pre-resolve caller.
# ---------------------------------------------------------------------------


class TestResolveValidationFailClosed:
    @pytest.mark.parametrize("bad_resolve", [
        "drill-echo.example.com:80:22.10.85.9; rm -rf /",    # command injection
        "drill-echo.example.com:80:22.10.85.9 && curl evil", # shell metachars
        "drill-echo.example.com:80:22.10.85.9|nc x 4444",    # pipe
        "drill-echo.example.com:80:22.10.85.9\nX-Inj: yes",  # CRLF header inject
        "drill-echo.example.com:80:22.10.85.9 extra",        # trailing token
        "drill-echo.example.com:80:22.10.85.9'",             # quote
        'drill-echo.example.com:80:22.10.85.9"',             # double quote
        "drill-echo.example.com:0:22.10.85.9",               # port 0
        "drill-echo.example.com:99999:22.10.85.9",           # port > 65535
        "drill-echo.example.com:80:999.10.85.9",             # octet > 255
        "drill-echo.example.com:80:22.10.85",                # not a dotted-quad
        "drill-echo.example.com:22.10.85.9",                 # missing port
        "drill-echo.example.com:80:22.10.85.9:extra",        # extra segment
        ":80:22.10.85.9",                                    # empty host
        "drill-echo.example.com:80:",                        # empty addr
        "drill-echo.example.com:80:22.10.85.9\n",            # trailing newline
    ])
    async def test_bad_resolve_rejected_before_exec(self, recorder, bad_resolve):
        result = await http_probe.ainvoke({
            "origin_pod": "client", "url": "http://drill-echo.example.com/",
            "namespace": "default", "resolve": bad_resolve, "kubeconfig": "",
        })
        assert result.startswith("Error:"), result
        assert "resolve" in result
        # the exec payload must never be built for a rejected resolve
        assert recorder.calls == []

    @pytest.mark.parametrize("good_resolve", [
        "drill-echo.example.com:80:22.10.85.9",
        "a:443:10.0.0.1",
        "svc.default.svc.cluster.local:8080:172.16.0.5",
    ])
    async def test_clean_resolve_accepted(self, recorder, good_resolve):
        result = await http_probe.ainvoke({
            "origin_pod": "client", "url": "http://drill-echo.example.com/",
            "namespace": "default", "resolve": good_resolve, "kubeconfig": "",
        })
        assert not result.startswith("Error:"), result
        assert len(recorder.calls) == 1


class TestResolveCommandAndProvenance:
    async def test_resolve_threads_into_curl_just_before_url(self, recorder):
        await http_probe.ainvoke({
            "origin_pod": "drill-http-client",
            "url": "http://drill-echo.example.com/",
            "namespace": "default",
            "resolve": "drill-echo.example.com:80:22.10.85.9",
            "kubeconfig": "",
        })
        v_args = recorder.calls[0][1]
        # the option + its value sit immediately before the URL, so the payload
        # still ENDS on the URL (the single shell-free argv invariant)
        assert (
            "--resolve drill-echo.example.com:80:22.10.85.9 "
            "http://drill-echo.example.com/" in v_args
        )
        assert v_args.rstrip().endswith("http://drill-echo.example.com/")

    async def test_resolve_appears_in_provenance(self, recorder):
        result = await http_probe.ainvoke({
            "origin_pod": "client", "url": "http://drill-echo.example.com/",
            "namespace": "default",
            "resolve": "drill-echo.example.com:80:22.10.85.9",
            "kubeconfig": "",
        })
        data = json.loads(result)
        assert data["resolve"] == "drill-echo.example.com:80:22.10.85.9"

    async def test_absent_resolve_is_byte_identical(self, recorder):
        # No resolve → no ``--resolve`` in the command AND no ``resolve`` key in
        # the JSON: every pre-resolve caller's observation is unchanged.
        result = await http_probe.ainvoke({
            "origin_pod": "client", "url": "http://svc/health",
            "namespace": "default", "kubeconfig": "",
        })
        v_args = recorder.calls[0][1]
        assert "--resolve" not in v_args
        assert "resolve" not in json.loads(result)
