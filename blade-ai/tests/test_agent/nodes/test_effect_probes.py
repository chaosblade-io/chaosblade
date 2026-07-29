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
