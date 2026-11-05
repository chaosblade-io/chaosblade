"""Semantic diagnostic probes and their per-scope command translations.

A diagnostic needs two orthogonal things, and this module owns both: *what
evidence to gather* (the semantic probe) and *what shell command yields that
evidence in a given scope* (the translation registry). The two used to be
fused: callers hardcoded shell strings (``ls -lh`` / ``cat /proc/diskstats``)
and ran them directly. That works only while every scope happens to share the
same command — true for the disk probes (all POSIX-standard), but false the
moment a diagnostic differs by environment (e.g. ``ss`` on a bare host vs
``cat /proc/net/tcp`` inside a minimal container without ``ss``).

This module owns that missing axis. A :class:`Probe` is a *semantic intent*
(``kind`` + ``args``) carrying no command; the registry maps
``(kind, scope) -> command`` with :data:`ANY_SCOPE` (``"*"``) as the
scope-agnostic default. A caller declares ``Probe("http_response", {...})``;
the consumer — which knows its own scope — resolves it to a concrete command
via :func:`resolve_probe_command`. Adding a diagnostic whose command differs by
scope is a new registry entry (``{"host": ..., "*": ...}``), not an
``if scope ==`` branch at the call site.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping

# A builder turns a probe's semantic args into a concrete shell command.
ProbeCommandBuilder = Callable[[Mapping[str, str]], str]

# Scope key meaning "use this command for any scope without a specific entry".
ANY_SCOPE = "*"


@dataclass(frozen=True)
class Probe:
    """A scope-independent diagnostic intent.

    ``kind`` selects a registered command family (e.g. ``"disk_usage"``);
    ``args`` carries semantic parameters (e.g. ``{"path": "/tmp"}``) the
    per-scope builder interpolates. A probe deliberately carries no shell
    string — the concrete command is resolved against the sampling scope by
    :func:`resolve_probe_command`, so one intent yields host- or
    container-appropriate commands without the caller branching on scope.
    """

    kind: str
    args: Mapping[str, str] = field(default_factory=dict)


_PROBE_REGISTRY: dict[str, dict[str, ProbeCommandBuilder]] = {}


def register_probe(kind: str, builders: Mapping[str, ProbeCommandBuilder]) -> None:
    """Register (or replace) a probe ``kind`` with its per-scope builders.

    ``builders`` is keyed by scope (``"host"`` / ``"node"`` / ``"pod"`` / ...)
    with :data:`ANY_SCOPE` (``"*"``) as the fallback used for any scope without
    a specific entry. A command identical everywhere registers a single
    ``{"*": builder}``; one that differs by environment adds the specific
    scopes alongside a ``"*"`` default.
    """
    _PROBE_REGISTRY[kind] = dict(builders)


def resolve_probe_command(probe: Probe, scope: str) -> str:
    """Translate ``probe`` into the concrete command for ``scope``.

    Prefers a scope-specific builder, else the :data:`ANY_SCOPE` default.
    Raises ``KeyError`` when the kind is unknown or has no applicable builder —
    an unregistered probe is a programming error, not a silent empty command.
    """
    try:
        builders = _PROBE_REGISTRY[probe.kind]
    except KeyError:
        raise KeyError(f"unknown probe kind: {probe.kind!r}") from None
    builder = builders.get(scope) or builders.get(ANY_SCOPE)
    if builder is None:
        raise KeyError(
            f"probe {probe.kind!r} has no builder for scope {scope!r} "
            f"and no {ANY_SCOPE!r} default"
        )
    return builder(probe.args)


# ---------------------------------------------------------------------------
# Built-in disk probes. Fill/burn diagnostics are POSIX-standard and identical
# across host / node / pod, so each registers a single ANY_SCOPE builder. A
# future fault whose command differs by environment would add scope-specific
# entries here, e.g. ``{"host": lambda a: "ss -tnp", ANY_SCOPE: lambda a: "cat
# /proc/net/tcp"}`` — the check and channel stay untouched.
# ---------------------------------------------------------------------------
register_probe(
    "disk_fill_listing",
    {ANY_SCOPE: lambda a: f"ls -lh {a.get('path', '/tmp')}"},
)
register_probe(
    "disk_usage",
    {ANY_SCOPE: lambda a: f"df -h {a.get('path', '/tmp')}"},
)
register_probe(
    "diskstats",
    {ANY_SCOPE: lambda a: "cat /proc/diskstats"},
)

# ---------------------------------------------------------------------------
# Built-in HTTP probe. Unlike the disk probes above (consumed by the execute
# phase's effect channels), this kind is resolved by the ``http_probe`` tool —
# the registry is the one shared typed-probe mechanism across phases, which is
# exactly why the command shape lives here rather than being hand-written by
# the LLM. Every mode is a single bounded read-only GET: ``-s`` (silent), ``-w``
# (the observation is the write-out template's VALUE, not a mutation), a bounded
# ``--max-time``, and NO ``-D``/``-X``/``-d``/``-T``. The body is either routed
# to the ``-o /dev/null`` discard sink (metadata/headers) or streamed to stdout
# ahead of a sentinel (body/all) — both forms are a SINGLE shell-free argv
# command and pass the read-only screen (``readonly.kubectl_exec_rejection_reason``)
# by construction (measured), so the shape needs no per-call shell screening.
# No shell pipe is ever emitted: the exec-form channel runs no shell, so body
# size is governed by the framework output layer, not the payload.
# ``%{exitcode}`` carries curl's
# own exit (7 refused / 28 timeout / 0 ok) so a faulted target is DATA, not lost.
#
# The template's ``%{...}`` are curl write-out variables — LITERAL text. They
# live in the plain-string constants below (never inside an f-string literal,
# whose ``{}`` would collide); the builder f-strings only substitute the
# already-built ``metrics`` / ``tail`` values, so no brace escaping is needed.
# Only ``url`` / ``max_time`` / ``capture`` interpolate, and the ``http_probe``
# tool validates all three (clean URL token, positive int, capture enum)
# fail-closed BEFORE resolving, so no shell-metacharacter injection reaches the
# exec payload here.
# ---------------------------------------------------------------------------

# The canonical write-out metric tokens — shared by EVERY capture mode so the
# five metric keys stay byte-for-byte identical across modes and match both
# ``http_probe_cli._W_NAME_TO_METRIC`` and
# ``_metric_extractor._HTTP_PROBE_METRIC_KEYS`` (the exact-key cross-check
# contract). Plain string: the ``%{...}`` are literal curl variables.
_HTTP_W_METRICS = (
    "connect=%{time_connect} starttransfer=%{time_starttransfer} "
    "http=%{http_code} exit=%{exitcode} len=%{size_download}"
)
# Sentinel separating a streamed body (BEFORE) from the write-out meta line
# (AFTER): curl emits the body first and the ``-w`` template last, so the
# ``http_probe`` parser splits stdout on this marker. PUBLIC single source of
# truth — ``http_probe_cli`` imports it to split on the exact same bytes the
# builder emits (a drift here would silently mis-split body from meta).
HTTP_BODY_SENTINEL = "===HTTPPROBE_META==="
# Body-size governance is deliberately NOT done here. The builder emits a single
# shell-free curl (see _build_http_response); a shell ``| head -c`` cap cannot
# work in the exec-form channel and would corrupt the probe. Runaway stdout is
# backstopped by the framework's single authoritative output layer
# (apply_output_safety_valve, reached via probe_mode=True in http_probe_cli).
# Drill target bodies are always <1KB, so the backstop never bites in practice.


def _build_http_response(a: Mapping[str, str]) -> str:
    """Build the canonical read-only L7 GET probe for the requested capture mode.

    Four orthogonal modes (``http_probe`` validates ``capture`` against the enum
    fail-closed before this builder runs, so an unrecognised value degrades to
    the ``metadata`` default rather than inventing a shape):

    * ``metadata`` (default) — ``-o /dev/null`` discards the body; the write-out
      carries only the five metrics. Byte-for-byte the pre-capture form.
    * ``headers`` — as metadata plus ``headers=%{header_json}``; body discarded.
    * ``body`` — drops ``-o /dev/null`` so the body streams to stdout and moves
      the write-out behind the ``===HTTPPROBE_META===`` sentinel. A SINGLE plain
      curl: no shell pipe (the exec-form channel runs no shell), so body size is
      backstopped by the framework output layer, not by a payload-side ``head -c``.
    * ``all`` — body plus ``headers=%{header_json}``.
    """
    capture = a.get("capture", "metadata")
    # Optional DNS override ``--resolve host:port:addr`` — pins a hostname to a
    # literal address WITHOUT editing /etc/hosts, so the request still carries
    # the hostname in BOTH the HTTP Host header AND (for https) the TLS SNI. It
    # is the ONE canonical, read-only curl idiom for reaching a virtual-host-
    # routed endpoint from an in-cluster vantage — an Ingress controller Service
    # ClusterIP that routes by Host, a domain-fronted gateway — precisely the
    # reachability class the plain-URL probe cannot express (which otherwise
    # pushes the verifier onto a hand-written curl the metric extractor will not
    # parse, blinding the cross-check). Emitted as a single ``--resolve`` option
    # plus a single ``host:port:addr`` value token placed JUST BEFORE the URL, so
    # every capture mode still ENDS on the URL and stays one shell-free argv. The
    # value is validated fail-closed by ``tools/http_probe_cli.py`` against a
    # strict ``RFC1123-host:port:IPv4`` allowlist before it reaches here, so it
    # can carry no whitespace, quote, or shell metacharacter. ABSENT (the default
    # and every pre-resolve caller) leaves the command BYTE-IDENTICAL.
    resolve = a.get("resolve", "")
    resolve_opt = f"--resolve {resolve} " if resolve else ""
    tail = f"--max-time {a.get('max_time', '10')} {resolve_opt}{a.get('url', '')}"
    metrics = _HTTP_W_METRICS
    if capture in ("headers", "all"):
        metrics += " headers=%{header_json}"
    if capture in ("body", "all"):
        # Single plain curl — NO shell pipe. The exec-form transport channel
        # never runs a shell (create_subprocess_exec on the direct channel;
        # per-token shlex.quote on the wiz channel), so a ``| head -c N`` suffix
        # does NOT bound anything: it degrades to literal argv tokens after the
        # ``--`` separator and the remote curl mis-parses ``|`` / ``head`` as
        # extra URLs (DNS-fail exit 6) and ``-c N`` as a cookie-jar WRITE flag —
        # turning a healthy target into a fake ``http_code=000 exit_code=6``.
        # Body-size governance is NOT the payload's job: it belongs to the
        # framework's single authoritative output layer (apply_output_safety_valve,
        # reached via probe_mode=True in http_probe_cli), which already backstops
        # runaway stdout. The body streams to stdout ahead of the sentinel;
        # --max-time is the time bound. Bounded == time-bounded + framework-
        # backstopped, never shell-piped.
        return f"curl -s -w '\\n{HTTP_BODY_SENTINEL}\\n{metrics}\\n' {tail}"
    return f"curl -s -o /dev/null -w '{metrics}\\n' {tail}"


register_probe("http_response", {ANY_SCOPE: _build_http_response})


__all__ = [
    "ANY_SCOPE",
    "HTTP_BODY_SENTINEL",
    "Probe",
    "ProbeCommandBuilder",
    "register_probe",
    "resolve_probe_command",
]
