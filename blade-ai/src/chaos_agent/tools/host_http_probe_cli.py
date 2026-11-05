"""Typed HTTP probe tool for the HOST / application (python) verification path.

Why this tool exists (the host-side sibling of ``http_probe_cli``)
------------------------------------------------------------------
``http_probe`` gave the k8s verify / recover-verify phases a framework-owned,
born-structured L7 GET probe so an HTTP fault's status / latency / header / body
evidence lands on the observation timeline as parsed JSON — closing the Case #84
gap where an LLM-hand-written ``kubectl_read exec ... curl`` was (a) rejected by
the read-only screen (``-D`` writes a file) and (b) invisible to the metric
extractor (no ``curl -w`` parser), so http_code / time_connect never reached the
deterministic cross-check.

That cure was k8s-only by construction: ``http_probe`` hard-binds the delivery
to ``kubectl exec`` into an ``origin_pod`` / ``namespace``. A fault on a BARE
HOST — or an application-side (``scope=python``) HTTP fault whose target is
reached from the host the transport addresses — has no client pod to exec into.
Its verifier could only observe HTTP by hand-writing a ``host_read`` / in-window
``host_inject`` curl, which returns BARE TEXT: the read-only screen still has to
prove the hand-written shell is read-only, and the metric extractor still cannot
parse it, so the python / host L7 observation starves the cross-check exactly
the way the pre-``http_probe`` k8s path did.

This tool closes that gap on the host face. The LLM keeps the SEMANTIC decision
it is good at and that only it has (WHICH url, WHICH capture layer — case
knowledge it reads from the skill doc); the FRAMEWORK owns the command shape
(the SAME canonical read-only ``curl -w`` resolved through the SAME typed-probe
registry ``http_probe`` uses) and the
born-structured parsing (the SAME ``http_probe_cli`` validators and parsers, so
the two tools can never drift). "Think with the model, guarantee with the
program."

Relationship to ``host_read`` / in-window ``host_inject`` curl
--------------------------------------------------------------
This is the PREFERRED L7 observation channel for a host / python HTTP fault —
it is the only one whose http_code / latency / headers / body reach the
cross-check timeline as structured metrics. A hand-written ``host_read`` curl
remains a fallback for a shape this tool cannot express (a query-string URL, a
non-HTTP protocol), but its output is bare text the extractor cannot parse, so
PREFER this tool whenever the verdict is an HTTP status / latency / header /
body claim. (This mirrors the k8s split: ``http_probe`` is preferred over a
hand-written ``kubectl_read exec ... curl``.)

Safety
------
The tool is classified ``SCOPE_READONLY`` by NAME in the target_guard and is a
member of the ``memory.hook._OBSERVATION_TOOLS`` provenance allowlist (it can
only ever run the framework-built GET probe), so the classifier trusts it. The
real safety boundary therefore lives HERE: every interpolated parameter —
including ``capture``, validated against a closed enum — is checked fail-closed
(the validators are IMPORTED from ``http_probe_cli``, the single source, so the
two tools reject the identical metacharacter set) before it can reach the exec
payload, so no shell metacharacter injection is possible even on a channel that
folds argv back into a shell string. Delivery goes through
``_run_host_diagnostic`` (``skip_guard=True``, ``expect_profile=PROFILE_HOST``):
a single bounded GET run exec-form on the target host — no shell, no pipe.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

# The typed-probe registry is the single source of the command shape, shared
# with ``http_probe``. ``_effect_probes`` is a dependency-free leaf (stdlib
# only) and the ``agent`` / ``agent.nodes`` package inits are empty, so this
# upward import introduces no cycle (the same justification ``http_probe_cli``
# relies on).
from chaos_agent.agent.nodes.execute._effect_probes import (
    Probe,
    resolve_probe_command,
)

# The sanctioned host-delivery seam: resolve_transport_target(state) +
# execute_via_transport(skip_guard=True, expect_profile=PROFILE_HOST),
# single-sourced in ``_run_host_diagnostic``. Reusing it (rather than
# re-deriving the transport call) keeps this tool on that exact bounded host
# path.
from chaos_agent.agent.nodes.execute._host_verify import _run_host_diagnostic

# Single source of truth for the input allowlists and the born-structured
# parsers. Importing them (not copying) is what keeps ``host_http_probe`` and
# ``http_probe`` byte-for-byte identical on the safety boundary and the metric
# keys the extractor / cross-check consume — a drift here would silently desync
# the two L7 probes.
from chaos_agent.tools.http_probe_cli import (
    _BODY_CAPTURES,
    _CAPTURE_OK,
    _HEADER_CAPTURES,
    _MAX_TIME_CEIL,
    _MAX_TIME_FLOOR,
    _RESOLVE_OK_RE,
    _URL_OK_RE,
    _parse_curl_w,
    _split_body_meta,
    _split_headers,
)
from chaos_agent.transports.executor import PROFILE_MISMATCH_EXIT_CODE
from chaos_agent.utils.truncation import apply_output_safety_valve

logger = logging.getLogger(__name__)


@tool
async def host_http_probe(
    url: str,
    max_time: int = 10,
    capture: str = "metadata",
    resolve: str = "",
    task_id: str = "",
    *,
    state: Annotated[dict | None, InjectedState] = None,
) -> str:
    """READ-ONLY L7 HTTP probe run directly on the TARGET HOST — measure an
    endpoint's HTTP response from the machine the configured transport addresses.

    Use this (NOT a hand-written ``host_read`` / in-window ``host_inject`` curl)
    whenever you need an HTTP status / latency / header / body measurement during
    host or application-side (python) fault verification. It is the host sibling
    of ``http_probe``: the framework builds the canonical read-only ``curl -w``
    and parses it, so the observation lands on the cross-check timeline as
    structured metrics instead of bare text the extractor cannot read. Use
    ``http_probe`` instead when the vantage is an in-cluster client POD (a k8s
    Service fault); use THIS when the request must originate from the host itself
    (a bare-host fault, or an application endpoint reached from the host).

    Inputs:
      - url: the target endpoint, ``http(s)://host[:port][/path]``. No query
        string / fragment (fail-closed).
      - max_time: per-request cap in seconds, 1-60 (default 10).
      - capture: WHICH evidence layers to return (default ``metadata``):
        ``metadata`` (status/timing/size/exit, body discarded), ``headers``
        (+response headers), ``body`` (+response body), ``all`` (headers AND
        body). Use ``headers`` when the verdict lives in a response header,
        ``body`` when in the body. Any other value is rejected fail-closed.
      - resolve: optional curl ``--resolve host:port:IPv4`` DNS override for a
        VIRTUAL-HOST-routed endpoint the plain URL cannot reach (a gateway
        routing by ``Host`` / SNI). Pinning the NAME (not the bare IP in ``url``)
        makes curl send the right Host header AND SNI. The port MUST equal the
        URL's effective port (80 http / 443 https) or curl ignores the override
        and DNS-fails (``exit_code=6``). Empty (default) = a directly-routable
        target, command byte-identical.

    Output: a JSON object — provenance (``url`` / ``capture`` / ``resolve`` when
    set) plus measured ``http_code`` (``000`` if the request never completed),
    ``time_connect``, ``time_starttransfer``, ``size_download``, curl
    ``exit_code`` (``0`` ok / ``6`` DNS / ``7`` refused / ``28`` timeout). With
    headers, a ``headers`` field carries the raw ``header_json`` string; with
    body, a ``body`` field carries the response text (output safety-valve
    backstopped). A faulted target is DATA (``http_code=503``, ``exit_code=7``),
    never a tool error; a transport-level failure (host unreachable, curl
    missing, profile mismatch) IS ``Error: ...``.

    Side effects: none — a single bounded GET run exec-form on the host.
    ``metadata`` / ``headers`` mode discards the body (``-o /dev/null``);
    ``body`` / ``all`` captures it read-only. Always a SINGLE plain curl (no
    shell pipe — the exec-form channel runs no shell), bounded by ``--max-time``
    and the framework output safety valve.
    """
    # --- fail-closed input validation (the real safety boundary) -----------
    # Validators are the SAME objects ``http_probe`` uses, so the two L7 probes
    # reject the identical metacharacter set — no drift on the safety boundary.
    if not isinstance(url, str) or not _URL_OK_RE.match(url):
        return (
            "Error: host_http_probe url must be a clean http(s)://host[:port]"
            "[/path] endpoint with no whitespace, query string, fragment or "
            f"shell metacharacter; got {url!r}."
        )
    try:
        max_time_int = int(max_time)
    except (TypeError, ValueError):
        return (
            "Error: host_http_probe max_time must be an integer 1-60; got "
            f"{max_time!r}."
        )
    if not (_MAX_TIME_FLOOR <= max_time_int <= _MAX_TIME_CEIL):
        return (
            "Error: host_http_probe max_time must be between "
            f"{_MAX_TIME_FLOOR} and {_MAX_TIME_CEIL} seconds; got {max_time_int}."
        )
    if not isinstance(capture, str) or capture not in _CAPTURE_OK:
        return (
            "Error: host_http_probe capture must be one of "
            f"{sorted(_CAPTURE_OK)}; got {capture!r}."
        )
    # Optional --resolve DNS override — same fail-closed ``host:port:IPv4``
    # allowlist ``http_probe`` applies (imported, single source), so the value
    # can carry no whitespace, quote, or shell metacharacter into the payload.
    if resolve:
        m = _RESOLVE_OK_RE.match(resolve) if isinstance(resolve, str) else None
        if not m:
            return (
                "Error: host_http_probe resolve must be curl --resolve form "
                "'host:port:IPv4' (e.g. 'drill-echo.example.com:80:22.10.85.9') "
                "— lowercase RFC1123 host, port 1-65535, dotted-quad address, "
                "with no whitespace / quote / shell metacharacter; got "
                f"{resolve!r}."
            )
        if not 1 <= int(m.group("port")) <= 65535:
            return (
                "Error: host_http_probe resolve port must be 1-65535; got "
                f"{resolve!r}."
            )
        if any(int(octet) > 255 for octet in m.group("addr").split(".")):
            return (
                "Error: host_http_probe resolve address must be a valid IPv4 "
                f"dotted-quad (each octet 0-255); got {resolve!r}."
            )

    # --- framework-owned command shape (single source: the probe registry) --
    # scope="host" resolves the SAME ``http_response`` builder ``http_probe``
    # uses (registered under ANY_SCOPE, so the command is byte-identical across
    # scopes) — the shape lives in the registry, never hand-written here.
    probe_args = {"url": url, "max_time": str(max_time_int), "capture": capture}
    if resolve:
        probe_args["resolve"] = resolve
    curl_cmd = resolve_probe_command(Probe("http_response", probe_args), "host")

    result = await _run_host_diagnostic(curl_cmd, state, task_id)

    # A profile mismatch means the resolved channel is NOT a host face (a k8s
    # channel answering from the platform executor) — an honest channel fault,
    # never dressed up as a measurement.
    if result.exit_code == PROFILE_MISMATCH_EXIT_CODE:
        return f"Error: host_http_probe {(result.stderr or '').strip()}"

    # Probe-mode discriminator (mirrors ``_kubectl_impl(probe_mode=True)``):
    # curl SPEAKS IN EXIT CODES — a non-zero exit (7 refused / 28 timeout / 6
    # DNS) is the probe's OWN answer that the fault LANDED, and curl still
    # prints its ``-w`` write-out to stdout on every one of them. So non-empty
    # stdout is the reliable "the command really ran and answered" signal and is
    # returned for parsing regardless of exit code; only an EMPTY stdout (host
    # unreachable, curl missing, transport drop — which write stderr only) stays
    # an honest "Error:".
    raw = apply_output_safety_valve(result.stdout or "", kind="success-output")
    if not raw.strip():
        stderr = (result.stderr or "").strip()
        detail = stderr or f"(exit {result.exit_code}, no output)"
        return (
            "Error: host_http_probe ran but produced no output — the host may "
            f"be unreachable or lack a curl binary. Detail: {detail}"
        )

    # Split the capture layers before parsing (SAME helpers as ``http_probe``):
    # body/all stream the body ahead of HTTP_BODY_SENTINEL; headers/all append
    # the multi-line header JSON to the meta region; the 5 metrics always sit in
    # the meta region BEFORE ``headers=``, so slicing headers off first keeps the
    # whitespace-token metric parser clean.
    body_text, meta = _split_body_meta(raw)
    metrics_part, headers_raw = _split_headers(meta)
    fields = _parse_curl_w(metrics_part)
    if not fields:
        # The exec ran but produced no recognisable write-out (e.g. curl missing
        # on the host). Surface the raw output rather than a silent empty probe.
        return (
            "Error: host_http_probe ran but the host curl produced no "
            f"recognised write-out fields. Raw output:\n{raw.strip()}\n"
            "Confirm the target host has a curl binary."
        )

    out = {
        "tool": "host_http_probe",
        "url": url,
        "capture": capture,
        # stable, canonical metric keys — byte-for-byte the ``http_probe`` set
        # (see http_probe_cli._W_NAME_TO_METRIC), which is what lets the shared
        # _metric_extractor._parse_http_probe and the cross-check consume both.
        "http_code": fields.get("http_code", ""),
        "time_connect": fields.get("time_connect", ""),
        "time_starttransfer": fields.get("time_starttransfer", ""),
        "size_download": fields.get("size_download", ""),
        "exit_code": fields.get("exit_code", ""),
    }
    # Provenance / capture-layer fields, present only when set — so a metadata
    # probe's JSON never carries an empty resolve/headers/body.
    if resolve:
        out["resolve"] = resolve
    if capture in _HEADER_CAPTURES:
        out["headers"] = headers_raw
    if capture in _BODY_CAPTURES:
        out["body"] = body_text
    return json.dumps(out)


__all__ = ["host_http_probe"]
