"""End-to-end wiring for the host_shell backend (inject → detect → recover).

Complements the unit-level provider tests by exercising the full host path the
way production does, without a live transport:

1. inject → detect: a successful ``host_inject`` carrier on a resolved host
   channel classifies as ``host_native`` via ``detect_method`` (and NOT on a
   non-host channel).
2. detect → recover: ``HostShellProvider.recover`` is the no-LLM verdict for a
   host-native (no-UID) fault — there is no code-side reverse derivation, so it
   reports ``unrecovered``/Layer-2 skipped. The reverse command lives in the
   skill case and is executed by the LLM recover loop, not here.
3. host_read stays read-only: a mutating command is rejected at the tool gate
   without touching the transport.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import ToolMessage

from chaos_agent.agent.providers import FaultProviderRegistry
from chaos_agent.agent.providers.host_shell.provider import HostShellProvider


@pytest.fixture(autouse=True)
def _isolate_registry():
    FaultProviderRegistry.clear()
    yield
    FaultProviderRegistry.clear()
    FaultProviderRegistry.register_builtins()


def _host_inject_ok(output: str = "filled /var/lib/chaos_fill.img") -> ToolMessage:
    return ToolMessage(content=output, name="host_inject", tool_call_id="h1")


# -- 1. inject → detect ------------------------------------------------------


def test_host_inject_detects_as_host_native_on_host_channel():
    FaultProviderRegistry.register_builtins()
    msgs = [_host_inject_ok()]
    # Only a resolved host channel (is_host=True) with no blade_uid classifies
    # a raw-command carrier as host_native.
    assert FaultProviderRegistry.detect_method(msgs, is_host=True) == "host_native"


def test_host_inject_not_host_native_off_host_channel():
    FaultProviderRegistry.register_builtins()
    msgs = [_host_inject_ok()]
    assert FaultProviderRegistry.detect_method(msgs, is_host=False) is None


def test_failed_host_inject_is_not_a_carrier():
    FaultProviderRegistry.register_builtins()
    msgs = [ToolMessage(content="Error: host_inject blocked", name="host_inject",
                        tool_call_id="h1")]
    assert FaultProviderRegistry.detect_method(msgs, is_host=True) is None


# -- 2. detect → recover (no-LLM verdict, no code-side reversal) -------------


async def test_host_recover_is_no_llm_unrecovered_verdict():
    # host_shell has no blade_uid and no code-side reverse derivation (the
    # reverse command lives in the skill case, executed by the LLM recover
    # loop). The no-LLM recover() therefore reports an honest unrecovered
    # verdict with Layer 1 not-applicable and Layer 2 skipped.
    state = {
        "execution_artifacts": [
            {"type": "host_command",
             "command": "iptables -A INPUT -p tcp --dport 80 -j DROP"},
        ],
    }
    mock_exec = AsyncMock()

    with patch("chaos_agent.transports.execute_via_transport", mock_exec):
        result = await HostShellProvider().recover(state, None, task_id="t1")

    # No transport call — recover() derives no reverse command on its own.
    mock_exec.assert_not_awaited()
    assert result.recovered is False
    assert result.level == "unrecovered"
    assert result.experiment_uid == ""
    assert result.layer1["status"] == "skipped"
    assert result.layer2["status"] == "skipped"
    assert result.failure is not None
    assert result.warnings  # non-empty warning that recovery is unverified


async def test_host_recover_uid_less_renders_empty_uid():
    # Phase-6: the handle is the single identity source — a UID-less host
    # carrier with no handle renders experiment_uid empty (the legacy
    # ``kwargs['blade_uid']`` echo was retired along with old-checkpoint
    # support).
    result = await HostShellProvider().recover({}, None)
    assert result.experiment_uid == ""
    assert result.recovered is False


# -- 3. host_read stays read-only --------------------------------------------


async def test_host_read_rejects_mutating_command():
    from chaos_agent.tools.host_cmd import host_read

    # A fault/mutating binary must be rejected by the read-only gate BEFORE any
    # transport call — assert the guidance error, no transport needed.
    out = await host_read.ainvoke({"command": "rm -rf /var/lib/chaos_fill.img"})
    assert out.startswith("Error:")
    assert "read-only diagnostic" in out
    # The guidance names WHERE a fault belongs, not a tool to re-issue it with.
    # It used to assert ``"host_inject" in out``, which pinned a dead-end
    # pointer: host_read is bound only in PLAN/VERIFY and host_inject only in
    # EXECUTE/RECOVER_VERIFY — host_shell/provider.py's phase map returns one or
    # the other, never both (test_factory pins that disjointness both ways) — so
    # host_inject is unbound in EVERY phase this message can appear in. Naming
    # it sends the model at a call that cannot dispatch, the dead end host_cmd
    # already refuses to build for its pipe gate ("a hard floor may redirect
    # only to a REACHABLE path").
    assert "execution phase" in out
    assert "host_inject" not in out


async def test_host_read_refusal_never_points_at_an_unbound_tool():
    """No host_read refusal branch may name a tool that is not bound with it.

    Covers both branches — the unregistered-binary terminal, the mutating
    terminal and the shape refusal — so a future edit cannot reintroduce the
    dead-end pointer through one of them.
    """
    from chaos_agent.tools.host_cmd import host_read

    for command in (
        "rm -rf /var/lib/chaos_fill.img",          # unregistered-binary terminal
        "dd if=/dev/zero of=/tmp/x bs=1M count=1",  # mutating terminal
        "df -h | grep vda",                        # shape refusal (pipe)
    ):
        out = await host_read.ainvoke({"command": command})
        assert out.startswith("Error:"), command
        assert "host_inject" not in out, command
        # Every branch still says where a mutating action belongs instead.
        assert "execution phase" in out or "read-only phase" in out, command


async def test_unregistered_binary_refusal_does_not_invite_an_existence_probe():
    """The unregistered-binary verdict must not suggest checking installation.

    Measured cost (host Case #13, timeoffset, 2227s run): ``chronyc tracking``
    was refused here, the generic fix text offered "to check whether a binary
    exists, prefer `command -v <name>`", and the model burned a reconnaissance
    turn on ``command -v chronyc`` — which answered ``/usr/bin/chronyc``.
    Installed, and irrelevant: this verdict is about the classifier's
    registration, not the host's PATH, so no existence probe can change it. The
    verdict is now rendered by its own branch (dispatched on
    ``readonly.is_unregistered_binary_reason``, not on substring-matched prose).

    ``losetup`` stands in for the unregistered binary because ``chronyc`` has
    since been registered on its monitoring verbs — the point is the VERDICT,
    not the specific name.
    """
    from chaos_agent.tools.host_cmd import host_read

    out = await host_read.ainvoke({"command": "losetup -a"})
    assert "not a known read-only diagnostic command" in out
    # The probe may be NAMED — naming it to forbid it is clearer than omitting
    # it, since the model was reaching for it on its own. What must not survive
    # is the RECOMMENDATION, and the reason it cannot help must be stated (a
    # bare prohibition invites a retry).
    assert "prefer `command -v" not in out
    assert "Do NOT probe whether it is installed" in out
    assert "cannot change this verdict" in out
    # It must say what the verdict IS about, so the model stops probing and
    # reaches the fact another way.
    assert "registered" in out.lower()


async def test_shape_refusal_keeps_the_existence_probe_guidance():
    """The generic branch must NOT lose the ``command -v`` guidance.

    The sibling of the test above: the existence-probe bullet is wrong for the
    unregistered-binary verdict (where the answer cannot change anything) but
    right for a shape refusal, where the model is legitimately mid-diagnosis and
    a missing binary is a real possibility. Splitting the branches must not
    throw the good half away with the bad.
    """
    from chaos_agent.tools.host_cmd import host_read

    out = await host_read.ainvoke({"command": "df -h | grep vda"})
    assert "command -v" in out
