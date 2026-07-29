"""Semantic diagnostic probes and their per-scope command translations.

An effect check needs three orthogonal things: *what evidence to gather* (the
semantic probe), *what shell command yields that evidence in a given scope*
(this module), and *where / how to run it* (``_effect_channels``). The first
two used to be fused: checks hardcoded shell strings (``ls -lh`` /
``cat /proc/diskstats``) and handed them straight to the channel. That works
only while every scope happens to share the same command — true for disk
fill/burn (all POSIX-standard), but false the moment a fault's diagnostic
differs by environment (e.g. ``ss`` on a bare host vs ``cat /proc/net/tcp``
inside a minimal container without ``ss``).

This module owns that missing axis. A :class:`Probe` is a *semantic intent*
(``kind`` + ``args``) carrying no command; the registry maps
``(kind, scope) -> command`` with :data:`ANY_SCOPE` (``"*"``) as the
scope-agnostic default. A check declares ``Probe("disk_usage", {"path": p})``;
the channel — which knows its own scope — resolves it to a concrete command.
Adding a fault whose command differs by scope is a new registry entry
(``{"host": ..., "*": ...}``), not an ``if scope ==`` branch inside the check.
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


__all__ = [
    "ANY_SCOPE",
    "Probe",
    "ProbeCommandBuilder",
    "register_probe",
    "resolve_probe_command",
]
