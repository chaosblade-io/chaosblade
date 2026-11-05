"""Tests for ``chaos_agent.agent.nodes.execute._effect_probes``.

The probe catalog owns the *what command* axis of a post-injection effect
check: a semantic :class:`Probe` (``kind`` + ``args``) is translated to a
concrete shell command per scope, with ``"*"`` (:data:`ANY_SCOPE`) as the
scope-agnostic default. These tests exercise default vs scope-specific
dispatch, arg interpolation, error handling for unknown probes, and the
extension path (registering a fault whose command differs by scope).
"""

from __future__ import annotations

import pytest

from chaos_agent.agent.nodes.execute import _effect_probes as ep
from chaos_agent.agent.nodes.execute._effect_probes import (
    ANY_SCOPE,
    Probe,
    register_probe,
    resolve_probe_command,
)


@pytest.fixture(autouse=True)
def _restore_registry():
    """Snapshot and restore the global probe registry around each test so
    dynamic registrations don't leak across tests."""
    snapshot = {k: dict(v) for k, v in ep._PROBE_REGISTRY.items()}
    try:
        yield
    finally:
        ep._PROBE_REGISTRY.clear()
        ep._PROBE_REGISTRY.update(snapshot)


def test_builtin_disk_probes_use_any_scope_across_scopes():
    # disk fill/burn commands are POSIX-standard and identical everywhere, so
    # every scope resolves to the same command via the ANY_SCOPE default.
    for scope in ("host", "node", "pod"):
        assert resolve_probe_command(Probe("disk_usage", {"path": "/tmp"}), scope) == "df -h /tmp"
        assert (
            resolve_probe_command(Probe("disk_fill_listing", {"path": "/data"}), scope)
            == "ls -lh /data"
        )
        assert resolve_probe_command(Probe("diskstats"), scope) == "cat /proc/diskstats"


def test_args_are_interpolated_by_builder():
    assert resolve_probe_command(Probe("disk_usage", {"path": "/host/var"}), "host") == (
        "df -h /host/var"
    )


def test_missing_path_arg_falls_back_to_default():
    # Builders default ``path`` to /tmp so a probe without args is still valid.
    assert resolve_probe_command(Probe("disk_usage"), "host") == "df -h /tmp"


def test_scope_specific_builder_wins_over_any_scope():
    # The core extensibility guarantee: a fault whose command differs by
    # environment registers a scope-specific builder alongside a "*" default;
    # the specific one is preferred, others fall through to "*".
    register_probe(
        "net_connections",
        {
            "host": lambda a: "ss -tnp",
            ANY_SCOPE: lambda a: "cat /proc/net/tcp",
        },
    )
    assert resolve_probe_command(Probe("net_connections"), "host") == "ss -tnp"
    # node / pod (container-flavoured) have no specific entry → ANY_SCOPE.
    assert resolve_probe_command(Probe("net_connections"), "node") == "cat /proc/net/tcp"
    assert resolve_probe_command(Probe("net_connections"), "pod") == "cat /proc/net/tcp"


def test_register_probe_replaces_existing_kind():
    register_probe("disk_usage", {ANY_SCOPE: lambda a: "custom-df"})
    assert resolve_probe_command(Probe("disk_usage"), "host") == "custom-df"


def test_unknown_kind_raises_key_error():
    with pytest.raises(KeyError, match="unknown probe kind"):
        resolve_probe_command(Probe("does_not_exist"), "host")


def test_scope_without_builder_and_no_default_raises():
    # A probe registered only for a specific scope, with no ANY_SCOPE default,
    # must raise for other scopes rather than silently yield an empty command.
    register_probe("host_only", {"host": lambda a: "uptime"})
    assert resolve_probe_command(Probe("host_only"), "host") == "uptime"
    with pytest.raises(KeyError, match="no builder for scope"):
        resolve_probe_command(Probe("host_only"), "pod")


# ---------------------------------------------------------------------------
# http_response — the typed L7 probe shared with the verify phase (L1).
#
# The command shape is the SINGLE SOURCE OF TRUTH for both the execute phase's
# effect check and ``tools/http_probe_cli.py``. It is pinned byte-for-byte here
# because its read-only guarantee is *pre-proven by construction*: ``-s`` is
# valueless, ``-o /dev/null`` routes the body to a discard sink, ``-w`` only
# reads curl's own measurements. Any drift in this literal could silently turn
# a read-only probe into a mutating shell, so it is locked rather than derived.
# ---------------------------------------------------------------------------

_EXPECTED_HTTP_CMD = (
    "curl -s -o /dev/null -w "
    "'connect=%{time_connect} starttransfer=%{time_starttransfer} "
    "http=%{http_code} exit=%{exitcode} len=%{size_download}\\n' "
    "--max-time 10 http://svc.default.svc.cluster.local/health"
)


def test_http_response_builder_is_scope_agnostic_and_pinned():
    # Registered under ANY_SCOPE: an HTTP GET probe is identical whether it runs
    # from a client pod or (conceptually) any other scope, and the verify phase
    # resolves it with scope="pod".
    for scope in ("pod", "host", "node"):
        assert resolve_probe_command(
            Probe("http_response", {"url": "http://svc.default.svc.cluster.local/health"}),
            scope,
        ) == _EXPECTED_HTTP_CMD


def test_http_response_interpolates_max_time_and_url():
    cmd = resolve_probe_command(
        Probe("http_response", {"url": "http://backend:8080/live", "max_time": "5"}),
        "pod",
    )
    assert "--max-time 5 http://backend:8080/live" in cmd
    # the write-out template is untouched by arg interpolation
    assert "http=%{http_code}" in cmd


def test_http_response_defaults_max_time_to_ten():
    cmd = resolve_probe_command(Probe("http_response", {"url": "http://x/y"}), "pod")
    assert "--max-time 10 http://x/y" in cmd


def test_http_response_command_is_pre_proven_readonly():
    # The whole point of routing the L7 observation through a typed probe: the
    # canonical form must survive the read-only screen that rejected Case #84's
    # hand-written ``curl -sS -D -`` (``-D`` writes a local file). Asserted
    # against the same predicate the exec screen uses.
    from chaos_agent.tools.readonly import kubectl_exec_rejection_reason

    curl_cmd = resolve_probe_command(
        Probe("http_response", {"url": "http://svc.default.svc.cluster.local/health"}),
        "pod",
    )
    exec_payload = f"client-pod -n default -- {curl_cmd}"
    assert kubectl_exec_rejection_reason(exec_payload) is None, (
        "canonical http_response probe must be classified read-only"
    )
    # contrast: the pre-L1 hand-written form the verifier used to emit.
    assert kubectl_exec_rejection_reason(
        "client-pod -n default -- curl -sS -D - http://svc/health"
    ) is not None


# ---------------------------------------------------------------------------
# http_response capture modes (A1/A7) — the SAME builder branches on
# ``capture`` to fold the header/body verdict layers into the typed probe. The
# literal command strings are pinned here because they are the single source
# both the execute effect-check and ``http_probe_cli`` resolve, and because
# every mode's read-only guarantee is pre-proven by construction (a bounded GET,
# body either discarded to ``-o /dev/null`` or streamed behind a sentinel as a
# SINGLE shell-free curl — never a ``-D``/``-X``/``-d``/``-T`` mutation, never a
# shell pipe; size governance lives in the framework output layer).
# ---------------------------------------------------------------------------

# The five-metric write-out body, shared by every mode (``\n`` below is the
# literal backslash-n curl's ``-w`` interprets, matching the builder source).
_W = (
    "connect=%{time_connect} starttransfer=%{time_starttransfer} "
    "http=%{http_code} exit=%{exitcode} len=%{size_download}"
)


def test_http_response_capture_metadata_equals_pinned_form():
    # default capture == the pre-capture byte-for-byte form (body discarded).
    assert resolve_probe_command(
        Probe("http_response", {
            "url": "http://svc.default.svc.cluster.local/health",
            "capture": "metadata",
        }), "pod",
    ) == _EXPECTED_HTTP_CMD


def test_http_response_capture_headers_appends_header_json_keeps_discard_sink():
    cmd = resolve_probe_command(
        Probe("http_response", {"url": "http://x/y", "capture": "headers"}), "pod")
    assert "-o /dev/null" in cmd                       # body STILL discarded
    assert f"-w '{_W} headers=%{{header_json}}\\n'" in cmd
    assert "head -c" not in cmd                        # no body stream to bound


def test_http_response_capture_body_streams_stdout_behind_sentinel():
    cmd = resolve_probe_command(
        Probe("http_response", {"url": "http://x/y", "capture": "body"}), "pod")
    assert "-o /dev/null" not in cmd                   # body streamed to stdout
    assert f"-w '\\n{ep.HTTP_BODY_SENTINEL}\\n{_W}\\n'" in cmd
    # ROOT-FIX invariant: a SINGLE shell-free plain curl. The exec-form channel
    # runs no shell, so the old ``| head -c 4096`` suffix never bounded anything
    # — it degraded to literal argv tokens curl mis-parsed as extra URLs (fake
    # DNS-fail exit 6) plus a ``-c`` cookie-jar WRITE flag. Body size is now
    # governed by the framework output layer, never a payload-side pipe.
    assert cmd.endswith("http://x/y")                  # ends on the URL, no pipe
    assert "|" not in cmd and "head -c" not in cmd     # no shell pipe tokens
    assert "headers=%{header_json}" not in cmd


def test_http_response_capture_all_streams_body_and_headers():
    cmd = resolve_probe_command(
        Probe("http_response", {"url": "http://x/y", "capture": "all"}), "pod")
    assert "-o /dev/null" not in cmd
    assert f"-w '\\n{ep.HTTP_BODY_SENTINEL}\\n{_W} headers=%{{header_json}}\\n'" in cmd
    assert cmd.endswith("http://x/y")                  # single plain curl, no pipe
    assert "|" not in cmd and "head -c" not in cmd


@pytest.mark.parametrize("capture", ["metadata", "headers", "body", "all"])
def test_capture_command_is_shell_free_single_argv(capture):
    """ROOT-FIX teeth: the built command, once routed through the REAL
    ``build_kubectl_cmd`` argv path, must contain NO shell metacharacter token
    for ANY capture mode. This is the blind spot the old shape-only assertions
    left — they checked the command STRING but never the argv the exec-form
    channel actually execs, so a ``| head -c 4096`` pipe sailed through green
    while silently becoming literal ``|``/``head``/``-c``/``4096`` argv tokens
    after ``--`` that the remote curl mis-parsed. Any future re-introduction of
    a pipe / redirect / ``;`` fails here, not in production."""
    from chaos_agent.tools.kubectl_cli import build_kubectl_cmd

    curl_cmd = resolve_probe_command(
        Probe("http_response", {
            "url": "http://svc.default.svc.cluster.local/health",
            "capture": capture,
        }), "pod")
    argv = build_kubectl_cmd("exec", f"client -n default -- {curl_cmd}", "")
    if isinstance(argv, tuple):
        argv = argv[0]
    shell_meta = {"|", ";", "&&", "||", ">", "<", ">>", "(", ")", "`", "$("}
    assert not (set(argv) & shell_meta), f"shell metachar in argv: {argv}"
    # The specific bug fossils must never reappear as standalone argv tokens.
    assert "head" not in argv and "4096" not in argv
    payload = argv[argv.index("--") + 1:]
    assert payload[0] == "curl"
    assert "-c" not in payload                         # no cookie-jar write flag
    assert "-D" not in payload and "-X" not in payload # no dump-header / method


@pytest.mark.parametrize("capture", ["metadata", "headers", "body", "all"])
def test_every_capture_mode_survives_readonly_screen(capture):
    # A1's whole point: all four modes stay a single bounded GET the read-only
    # screen accepts — including body/all, which drop ``-o /dev/null`` and stream
    # the body to stdout behind a sentinel (contrast Case #84's rejected
    # ``curl -sS -D -``). Bounded == ``--max-time`` (time) + framework output
    # safety valve (size), never a shell pipe.
    from chaos_agent.tools.readonly import kubectl_exec_rejection_reason

    curl_cmd = resolve_probe_command(
        Probe("http_response", {
            "url": "http://svc.default.svc.cluster.local/health",
            "capture": capture,
        }), "pod")
    assert kubectl_exec_rejection_reason(
        f"client-pod -n default -- {curl_cmd}") is None, capture


def test_unknown_capture_degrades_to_metadata_shape():
    # Defence in depth: the tool's fail-closed enum check rejects an unknown
    # capture BEFORE the builder runs, so this path is never reached in
    # production — but were it, the builder degrades to the metadata default
    # rather than inventing a mutating shape.
    assert resolve_probe_command(
        Probe("http_response", {
            "url": "http://svc.default.svc.cluster.local/health",
            "capture": "bogus",
        }), "pod",
    ) == _EXPECTED_HTTP_CMD


# ---------------------------------------------------------------------------
# http_response --resolve DNS override — the ONE canonical, read-only curl idiom
# for reaching a virtual-host-routed endpoint (an Ingress controller Service
# ClusterIP that routes by Host, a TLS gateway by SNI) from an in-cluster
# vantage. This is the reachability class the plain-URL probe cannot express;
# without it the verifier is pushed onto a hand-written ``curl -H 'Host:'`` the
# metric extractor will not parse, blinding the deterministic cross-check. The
# option is emitted JUST BEFORE the URL so every capture mode still ends on the
# URL and stays a single shell-free argv; ABSENT is byte-for-byte the old form.
# ---------------------------------------------------------------------------

_RESOLVE = "drill-echo.example.com:80:22.10.85.9"
_RESOLVE_URL = "http://drill-echo.example.com/"


def test_http_response_resolve_inserted_just_before_url():
    cmd = resolve_probe_command(
        Probe("http_response", {"url": _RESOLVE_URL, "resolve": _RESOLVE}), "pod")
    # option + single value token immediately precede the URL ...
    assert f"--resolve {_RESOLVE} {_RESOLVE_URL}" in cmd
    # ... so the command STILL ends on the URL (single shell-free argv invariant)
    assert cmd.endswith(_RESOLVE_URL)
    # the metadata capture shape is otherwise untouched
    assert cmd.startswith("curl -s -o /dev/null -w ")


def test_http_response_no_resolve_is_byte_identical():
    # An absent OR empty resolve leaves the command EXACTLY the pre-resolve form
    # — no ``--resolve`` token, no stray whitespace.
    assert resolve_probe_command(
        Probe("http_response", {"url": _RESOLVE_URL}), "pod",
    ) == f"curl -s -o /dev/null -w '{_W}\\n' --max-time 10 {_RESOLVE_URL}"
    assert resolve_probe_command(
        Probe("http_response", {"url": _RESOLVE_URL, "resolve": ""}), "pod",
    ) == f"curl -s -o /dev/null -w '{_W}\\n' --max-time 10 {_RESOLVE_URL}"
    assert "--resolve" not in resolve_probe_command(
        Probe("http_response", {"url": _RESOLVE_URL}), "pod")


@pytest.mark.parametrize("capture", ["metadata", "headers", "body", "all"])
def test_resolve_command_is_shell_free_single_argv(capture):
    """Same teeth as ``test_capture_command_is_shell_free_single_argv``, for the
    resolve override: routed through the REAL ``build_kubectl_cmd`` argv path,
    the ``--resolve`` option and its ``host:port:addr`` value must stay clean
    argv tokens (the value is ONE token, no shell metacharacter), the payload
    must still start with ``curl`` and END on the URL, and no write flag
    (``-c``/``-D``/``-X``) may ride in."""
    from chaos_agent.tools.kubectl_cli import build_kubectl_cmd

    curl_cmd = resolve_probe_command(
        Probe("http_response", {
            "url": _RESOLVE_URL, "resolve": _RESOLVE, "capture": capture,
        }), "pod")
    argv = build_kubectl_cmd("exec", f"client -n default -- {curl_cmd}", "")
    if isinstance(argv, tuple):
        argv = argv[0]
    shell_meta = {"|", ";", "&&", "||", ">", "<", ">>", "(", ")", "`", "$("}
    assert not (set(argv) & shell_meta), f"shell metachar in argv: {argv}"
    payload = argv[argv.index("--") + 1:]
    assert payload[0] == "curl"
    assert "--resolve" in payload
    assert _RESOLVE in payload                        # value is a SINGLE token
    assert payload[-1] == _RESOLVE_URL                # ends on the URL
    assert "-c" not in payload
    assert "-D" not in payload and "-X" not in payload


@pytest.mark.parametrize("capture", ["metadata", "headers", "body", "all"])
def test_resolve_command_survives_readonly_screen(capture):
    """THE point of the override: the framework-built ``--resolve`` probe must be
    classified read-only for every capture mode, so an Ingress entry probe stays
    on the typed channel (structured, cross-checkable) instead of falling back to
    a hand-written curl. Asserted against the SAME predicate the exec screen
    uses — the real consumption surface, not an internal helper."""
    from chaos_agent.tools.readonly import kubectl_exec_rejection_reason

    curl_cmd = resolve_probe_command(
        Probe("http_response", {
            "url": _RESOLVE_URL, "resolve": _RESOLVE, "capture": capture,
        }), "pod")
    assert kubectl_exec_rejection_reason(
        f"client-pod -n default -- {curl_cmd}") is None, capture
