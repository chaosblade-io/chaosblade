"""Typed HTTP probe tool for the read-only verification phases.

Why this tool exists (L1 root-cause cure)
------------------------------------------
The verify / recover-verify phases observe with a read-only tool surface. For
an L7 HTTP fault (status-code tampering, latency, connection loss) the only
ground truth is a ``curl`` from an INDEPENDENT non-backend client pod to the
target Service DNS — from the backend pod itself the kube-proxy hairpin yields
a false ``exit 28`` / ``http 000`` (measured: Case #82/#84 ``drill-http-client``
is a dedicated client for exactly this reason).

Before this tool, the verifier LLM hand-wrote that ``curl`` as a bare
``kubectl_read exec`` payload. Two bounded enumerations then had to chase an
unbounded generator (the LLM's command creativity):

  * the read-only screen (``readonly.py``) had to PROVE the hand-written shell
    was read-only. Case #84's ``curl -sS -D -`` was rejected (``-D`` writes a
    local file), and because a batch is screened together, three legitimate
    sibling ``kubectl`` reads were skipped alongside it — burning a whole
    verify iteration on a false rejection;
  * the metric extractor (``_metric_extractor.py``) had to REVERSE-PARSE the
    hand-written ``-w`` output. It had no ``curl -w`` parser, so http_code /
    time_connect / time_starttransfer never entered the observation timeline
    and the deterministic cross-check logged an extractor coverage gap.

This tool closes both at the class level. The LLM keeps the SEMANTIC decision
it is good at and that only it has (WHICH client pod, WHICH url — case
knowledge it reads from the skill doc); the FRAMEWORK owns the command shape
(the canonical, pre-proven read-only ``curl -w`` form, resolved through the
SAME typed-probe registry ``host_http_probe`` uses) and the born-structured
parsing. "Think with the model, guarantee with the program."

Capture layers
--------------
The default ``metadata`` capture returns only status/timing/size/exit — enough
for a latency or status-code verdict, but NOT for an L7 fault whose evidence
lives in a response HEADER (Case 1.7) or the response BODY (Case 1.6). Those
used to force the verifier back to a hand-written ``curl -D -`` / plain
``curl`` — reopening exactly the two enumerations above. The ``capture``
parameter (``metadata`` / ``headers`` / ``body`` / ``all``) folds those verdict
layers into the SAME typed probe: the framework still owns the command shape
(``%{header_json}`` for headers, a sentinel-split bounded body stream), so
header/body evidence now arrives born-structured and the hand-written curl
retires for the HTTP domain entirely.

Safety
------
The tool is classified ``SCOPE_READONLY`` by NAME in the target_guard (it can
only ever run the framework-built GET probe), so the classifier trusts it. The
real safety boundary therefore lives HERE: every interpolated parameter —
including ``capture``, validated against a closed enum — is checked fail-closed
before it can reach the exec payload, so no shell metacharacter injection is
possible even on a channel that folds argv back into a shell string. Every
capture mode stays a single bounded GET (measured to pass the read-only screen).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Annotated

from langchain_core.tools import InjectedToolArg, tool
from langgraph.prebuilt import InjectedState

# The typed-probe registry is the single source of the command shape, shared
# with ``host_http_probe``. ``_effect_probes`` is a dependency-free leaf
# (stdlib only) and both ``agent`` / ``agent.nodes`` package inits are empty,
# so this upward import introduces no cycle.
from chaos_agent.agent.nodes.execute._effect_probes import (
    HTTP_BODY_SENTINEL,
    Probe,
    resolve_probe_command,
)
from chaos_agent.tools.kubectl_cli import _kubectl_impl
from chaos_agent.transports import resolve_transport_target

logger = logging.getLogger(__name__)

# A probe URL is a Service endpoint (scheme://host[:port][/path]). Positive
# allowlist, fail-closed: it deliberately EXCLUDES every shell-dangerous
# metacharacter (whitespace ; & | $ ` < > ( ) { } [ ] \ ' " * ? ! ~ #) so the
# url can never inject a second command on a shell-folding channel. Query
# strings / fragments are therefore out of scope — a chaos verification probe
# targets a health/status endpoint, not a query API; if a future case genuinely
# needs one, that is a deliberate extension carrying its own escaping, not a
# relaxation here. ``\Z`` (not ``$``) so a single trailing newline cannot slip
# past the anchor.
_URL_OK_RE = re.compile(r"^https?://[A-Za-z0-9._~:/@%+,=-]+\Z")

# K8s object names (pod / namespace): RFC 1123 label — lower alnum and '-',
# start/end alnum. Rejects whitespace and every metacharacter. ``\Z`` (not
# ``$``) so a trailing newline is refused, not silently anchored past.
_K8S_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?\Z")

# Optional curl ``--resolve`` DNS override, ``host:port:IPv4``. Positive
# allowlist, fail-closed: the character set is host (RFC 1123 — alnum, '.' and
# '-', alnum-anchored) + port (1-5 digits) + a dotted-quad, so it structurally
# EXCLUDES every shell metacharacter, all whitespace, both quote kinds and
# CR/LF — the value can never inject a second command or a header on a
# shell-folding channel. Port range and octet range are checked on top so a typo
# surfaces as a clear tool error, not a cryptic downstream curl DNS failure.
# ``\Z`` (not ``$``) so a trailing newline is refused, not anchored past.
_RESOLVE_OK_RE = re.compile(
    r"^(?P<host>[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?)"
    r":(?P<port>[0-9]{1,5})"
    r":(?P<addr>(?:[0-9]{1,3}\.){3}[0-9]{1,3})\Z"
)

# curl ``-w`` write-out name -> the canonical metric key emitted to the LLM and
# to the observation timeline (via _metric_extractor._parse_http_probe). The
# two must agree byte-for-byte: the LLM cites the key it SEES in this tool's
# JSON, and the deterministic cross-check does an exact-key lookup against the
# timeline the extractor builds from the same JSON.
_W_NAME_TO_METRIC = {
    "http": "http_code",
    "connect": "time_connect",
    "starttransfer": "time_starttransfer",
    "exit": "exit_code",
    "len": "size_download",
}

# Hard bound on --max-time so a probe can never hang the verify loop longer
# than the fault window itself; also keeps the value a plain int token.
_MAX_TIME_FLOOR = 1
_MAX_TIME_CEIL = 60

# Capture modes — WHICH evidence layers the probe returns. Validated fail-closed
# (an unknown mode is rejected before the exec payload is built, exactly like a
# bad url / max_time) and the ONLY values the ``http_response`` builder branches
# on. ``metadata`` (default) is the pre-capture form (status + timing + size +
# exit, body discarded); ``headers`` / ``body`` / ``all`` additionally capture
# the response headers (curl ``%{header_json}``) and/or the response body, so an
# L7 fault whose verdict lives in a HEADER (Case 1.7) or the BODY (Case 1.6) is
# observed through this SAME typed probe instead of an LLM-hand-written curl.
_CAPTURE_METADATA = "metadata"
_CAPTURE_OK = frozenset({_CAPTURE_METADATA, "headers", "body", "all"})
# Modes that stream the body (no ``-o /dev/null``; the write-out is pushed
# behind ``HTTP_BODY_SENTINEL`` so body and meta can be split).
_BODY_CAPTURES = frozenset({"body", "all"})
# Modes that append ``headers=%{header_json}`` to the write-out.
_HEADER_CAPTURES = frozenset({"headers", "all"})
# The write-out name curl's ``%{header_json}`` is bound to in the builder. Its
# VALUE is a MULTI-LINE JSON object containing spaces (measured on curl 8.7.1:
# each header on its own line, Date values space-bearing), so it canNOT be
# recovered by the whitespace-token ``_parse_curl_w`` — the blob is shredded
# into unrecognised tokens and silently dropped. It is sliced off the meta
# region by ``_split_headers`` instead. Kept byte-for-byte in sync with the
# builder's literal `` headers=%{header_json}``.
_HEADERS_WNAME = "headers="


def _parse_curl_w(raw: str) -> dict[str, str]:
    """Parse the canonical ``curl -w`` write-out line into metric fields.

    Tolerant of surrounding text: scans for ``name=value`` tokens and keeps
    only the write-out names this probe emits (``_W_NAME_TO_METRIC``), so a
    stray curl notice on the same stream cannot invent a metric. Returns ``{}``
    when nothing recognised is present.
    """
    out: dict[str, str] = {}
    for token in (raw or "").split():
        name, sep, value = token.partition("=")
        if not sep:
            continue
        metric = _W_NAME_TO_METRIC.get(name)
        if metric is not None:
            out[metric] = value
    return out


def _split_body_meta(raw: str) -> tuple[str, str]:
    """Split a body-capture probe's stdout into ``(body, meta)``.

    In body/all mode the builder streams the response body first and pushes the
    write-out behind the ``HTTP_BODY_SENTINEL`` line (curl emits the body, THEN
    the ``-w`` template, which opens with ``\\n===SENTINEL===\\n``). So exactly
    one trailing newline on the pre-sentinel text belongs to the write-out
    prefix, not the body — it is stripped to recover the body verbatim; the
    post-sentinel text (leading prefix newline stripped) is the meta region.
    When the sentinel is absent (metadata/headers modes, or a capture truncated
    before the write-out) the whole output is meta and the body is empty.

    A tampered body that itself contains the sentinel would mis-split, but drill
    target bodies are small, known, controlled content; the sentinel is obscure
    enough that this is not a realistic collision for an L7 verification probe.
    """
    body, sep, meta = raw.partition(HTTP_BODY_SENTINEL)
    if not sep:
        return "", raw
    if body.endswith("\n"):  # the write-out prefix newline, not body content
        body = body[:-1]
    return body, meta.lstrip("\n")


def _split_headers(meta: str) -> tuple[str, str]:
    """Split the meta region into ``(metrics_part, headers_raw)``.

    ``headers=%{header_json}`` is always the TRAILING write-out element (the
    builder appends it last), so everything from the first ``headers=`` marker
    to the end of the meta region is the raw header JSON — recovered by SLICING,
    never by whitespace tokenisation (the blob is multi-line and space-bearing;
    see ``_HEADERS_WNAME``). The metric tokens all precede the marker, so
    ``_parse_curl_w`` runs on ``metrics_part`` alone and the JSON can never
    pollute a metric. Returns ``(meta, "")`` when no header was captured.
    """
    idx = meta.find(_HEADERS_WNAME)
    if idx == -1:
        return meta, ""
    return meta[:idx], meta[idx + len(_HEADERS_WNAME):].strip()


@tool
async def http_probe(
    origin_pod: str,
    url: str,
    namespace: str = "default",
    max_time: int = 10,
    capture: str = "metadata",
    resolve: str = "",
    kubeconfig: Annotated[str, InjectedToolArg] = "",
    state: Annotated[dict | None, InjectedState] = None,
) -> str:
    """READ-ONLY L7 HTTP probe — measure a Service's HTTP response from an
    independent client pod.

    Use this (NOT a hand-written ``kubectl_read exec ... curl``) whenever you
    need a target Service's HTTP status / latency / headers / body during
    verification. The framework builds the canonical read-only ``curl -w`` and
    parses it, so the observation is structured and never rejected as a
    mutating shell.

    Inputs:
      - origin_pod: an EXISTING pod to curl FROM. For an L7 Service fault it
        MUST be an independent, non-backend client pod (curling the target's own
        Service from a backend pod gives a false timeout via kube-proxy hairpin);
        the skill case names it.
      - url: the target endpoint, ``http(s)://host[:port][/path]``. No query
        string / fragment (fail-closed).
      - namespace: namespace of ``origin_pod`` (default ``default``).
      - max_time: per-request cap in seconds, 1-60 (default 10).
      - capture: WHICH evidence layers to return (default ``metadata``):
        ``metadata`` (status/timing/size/exit, body discarded), ``headers``
        (+response headers), ``body`` (+response body), ``all`` (headers AND
        body). Use ``headers`` when the verdict lives in a response header,
        ``body`` when in the body. Any other value is rejected fail-closed.
      - resolve: optional curl ``--resolve host:port:IPv4`` DNS override for a
        VIRTUAL-HOST-routed endpoint the plain URL cannot reach (an Ingress
        controller ClusterIP routing by ``Host``, a TLS gateway by SNI). Pinning
        the NAME (not the bare IP in ``url``) makes curl send the right Host
        header AND SNI, e.g. ``url='http://drill-echo.example.com/'`` +
        ``resolve='drill-echo.example.com:80:22.10.85.9'``. The port MUST equal
        the URL's effective port (80 http / 443 https) or curl ignores the
        override and DNS-fails (``exit_code=6``). Empty (default) = a
        directly-routable target, command byte-identical. PREFER this over a
        hand-written ``curl -H 'Host:'`` — only the typed probe lands
        ``http_code`` in the cross-check timeline.

    Output: a JSON object — provenance (``origin_pod``/``namespace``/``url``/
    ``capture``/``resolve`` when set) plus measured ``http_code`` (``000`` if the
    request never completed), ``time_connect``, ``time_starttransfer``,
    ``size_download``, curl ``exit_code`` (``0`` ok/``6`` DNS/``7`` refused/
    ``28`` timeout). With headers, a ``headers`` field carries the raw
    ``header_json`` string; with body, a ``body`` field carries the response text
    (output safety-valve backstopped). A faulted target is DATA (``http_code=503``,
    ``exit_code=7``), never a tool error; a kubectl-level failure (pod not found,
    exec refused) IS ``Error: ...``.

    Side effects: none — a single bounded GET. ``metadata``/``headers`` mode
    discards the body (``-o /dev/null``); ``body``/``all`` captures it read-only.
    Always a SINGLE plain curl (no shell pipe — the exec-form channel runs no
    shell), bounded by ``--max-time`` and the framework output safety valve.
    """
    # --- fail-closed input validation (the real safety boundary) -----------
    if not isinstance(origin_pod, str) or not _K8S_NAME_RE.match(origin_pod):
        return (
            "Error: http_probe origin_pod must be a single RFC1123 pod name "
            f"(lowercase alnum and '-'); got {origin_pod!r}."
        )
    if not isinstance(namespace, str) or not _K8S_NAME_RE.match(namespace):
        return (
            "Error: http_probe namespace must be a single RFC1123 namespace "
            f"name; got {namespace!r}."
        )
    if not isinstance(url, str) or not _URL_OK_RE.match(url):
        return (
            "Error: http_probe url must be a clean http(s)://host[:port][/path] "
            "endpoint with no whitespace, query string, fragment or shell "
            f"metacharacter; got {url!r}."
        )
    try:
        max_time_int = int(max_time)
    except (TypeError, ValueError):
        return f"Error: http_probe max_time must be an integer 1-60; got {max_time!r}."
    if not (_MAX_TIME_FLOOR <= max_time_int <= _MAX_TIME_CEIL):
        return (
            "Error: http_probe max_time must be between "
            f"{_MAX_TIME_FLOOR} and {_MAX_TIME_CEIL} seconds; got {max_time_int}."
        )
    if not isinstance(capture, str) or capture not in _CAPTURE_OK:
        return (
            "Error: http_probe capture must be one of "
            f"{sorted(_CAPTURE_OK)}; got {capture!r}."
        )
    # Optional --resolve DNS override (see the builder). Fail-closed allowlist:
    # ``host:port:IPv4`` only — the strict character set excludes every shell
    # metacharacter, whitespace, quote and CR/LF, so the value can never inject
    # into the exec payload on a shell-folding channel. Port range and IPv4 octet
    # range are checked too, so a typo surfaces as a clear tool error rather than
    # a cryptic downstream curl DNS failure. Empty (the default) is a no-op and
    # leaves the built command byte-identical.
    if resolve:
        m = _RESOLVE_OK_RE.match(resolve) if isinstance(resolve, str) else None
        if not m:
            return (
                "Error: http_probe resolve must be curl --resolve form "
                "'host:port:IPv4' (e.g. 'drill-echo.example.com:80:22.10.85.9') "
                "— lowercase RFC1123 host, port 1-65535, dotted-quad address, "
                "with no whitespace / quote / shell metacharacter; got "
                f"{resolve!r}."
            )
        if not 1 <= int(m.group("port")) <= 65535:
            return f"Error: http_probe resolve port must be 1-65535; got {resolve!r}."
        if any(int(octet) > 255 for octet in m.group("addr").split(".")):
            return (
                "Error: http_probe resolve address must be a valid IPv4 "
                f"dotted-quad (each octet 0-255); got {resolve!r}."
            )

    # --- framework-owned command shape (single source: the probe registry) --
    probe_args = {"url": url, "max_time": str(max_time_int), "capture": capture}
    if resolve:
        probe_args["resolve"] = resolve
    curl_cmd = resolve_probe_command(Probe("http_response", probe_args), "pod")
    v_args = f"{origin_pod} -n {namespace} -- {curl_cmd}"

    raw = await _kubectl_impl(
        "exec", v_args, kubeconfig, probe_mode=True,
        target=resolve_transport_target(state),
    )

    # A kubectl client-side failure (pod not found, exec refused, transport
    # fault) carries no probe stdout and stays an honest "Error:" — do NOT
    # dress it up as a measurement.
    if raw.startswith("Error:"):
        return raw

    # Split the capture layers before parsing. body/all modes stream the body
    # ahead of the HTTP_BODY_SENTINEL line; headers/all modes append the
    # multi-line header JSON to the meta region. The 5 metrics always sit in the
    # meta region BEFORE ``headers=``, so slicing headers off first keeps the
    # whitespace-token metric parser clean.
    body_text, meta = _split_body_meta(raw)
    metrics_part, headers_raw = _split_headers(meta)
    fields = _parse_curl_w(metrics_part)
    if not fields:
        # The exec ran but produced no recognisable write-out (e.g. curl missing
        # in the client image). Surface the raw output so the model can pick a
        # different client pod rather than silently reporting an empty probe.
        return (
            "Error: http_probe ran but the target pod's curl produced no "
            f"recognised write-out fields. Raw output:\n{raw.strip()}\n"
            "Confirm origin_pod has a curl binary, or choose another client pod."
        )

    result = {
        "tool": "http_probe",
        "origin_pod": origin_pod,
        "namespace": namespace,
        "url": url,
        "capture": capture,
        # stable, canonical metric keys (see _W_NAME_TO_METRIC)
        "http_code": fields.get("http_code", ""),
        "time_connect": fields.get("time_connect", ""),
        "time_starttransfer": fields.get("time_starttransfer", ""),
        "size_download": fields.get("size_download", ""),
        "exit_code": fields.get("exit_code", ""),
    }
    # Provenance / capture-layer fields, present only when set — so a metadata
    # probe's JSON stays byte-for-byte the pre-capture schema (plus the
    # ``capture`` provenance key) and never carries an empty resolve/headers/body.
    if resolve:
        result["resolve"] = resolve
    if capture in _HEADER_CAPTURES:
        result["headers"] = headers_raw
    if capture in _BODY_CAPTURES:
        result["body"] = body_text
    return json.dumps(result)


__all__ = ["http_probe"]
