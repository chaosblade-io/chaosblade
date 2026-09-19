"""Round-25 emergency-recover carrier-split gate.

``_L4ExecutionMixin._emergency_recover`` gates on ``has_active_fault`` —
the COMMITTED predicate. The fault_handle projection has no death axis
(it mirrors the attribution slots, which no destroy path clears), so a
destroyed experiment stays committed forever; firing the recover graph
at the corpse on every CANCELLED task wastes a full LLM run, flips the
task state and pollutes the trajectory. The gate is therefore split by
carrier: experiment handles gate on ``live_liability_uids`` (the UID
axis's single-source death oracle), native handles keep the committed
predicate (no native death oracle exists; a missed emergency recovery
is strictly worse than a redundant one).
"""

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.l4.execution import _L4ExecutionMixin

HEX16 = "deadbeef00000001"


def _create_pair(tc_id: str) -> list:
    return [
        AIMessage(
            content="",
            tool_calls=[{
                "name": "blade_create",
                "args": {"command": "create k8s pod-cpu fullload"},
                "id": tc_id,
                "type": "tool_call",
            }],
        ),
        ToolMessage(
            content='{"code":200,"success":true,"result":"%s"}' % HEX16,
            name="blade_create",
            tool_call_id=tc_id,
        ),
    ]


def _destroy_pair(tc_id: str) -> list:
    return [
        AIMessage(
            content="",
            tool_calls=[{
                "name": "blade_destroy",
                "args": {"uid": HEX16},
                "id": tc_id,
                "type": "tool_call",
            }],
        ),
        ToolMessage(
            content='{"code":200,"success":true,"result":"success"}',
            name="blade_destroy",
            tool_call_id=tc_id,
        ),
    ]


class _FakeGraph:
    def __init__(self, values):
        self._values = values
        self.invocations = []

    async def aget_state(self, config):
        return SimpleNamespace(values=self._values)

    async def ainvoke(self, initial, config):
        self.invocations.append((initial, config))


class _FakePool:
    def __init__(self, values):
        self.inject_graph = _FakeGraph(values)
        self.recover_graph = _FakeGraph(None)
        self.skill_registry = object()


async def _run_gate(values, monkeypatch):
    """Drive _emergency_recover with the given checkpoint values and
    return the recover graph's invocation count."""
    async def _fake_resolve(*args, **kwargs):
        return SimpleNamespace(initial_state={"resolved": True})

    monkeypatch.setattr(
        "chaos_agent.agent.result.task_snapshot.resolve_recover_initial_state",
        _fake_resolve,
    )
    pool = _FakePool(values)
    mixin = _L4ExecutionMixin()
    await mixin._emergency_recover(pool, "task-x", {})
    return len(pool.recover_graph.invocations)


class TestEmergencyRecoverCarrierSplitGate:

    @pytest.mark.asyncio
    async def test_destroyed_experiment_does_not_fire(self, monkeypatch):
        # The post-destroy steady state: corpse slot, landed retired
        # ledger, proven destroy pair. has_active_fault stays True here
        # (committed semantics — pinned in test_state.py), but the
        # experiment-carrier gate consults the liability primitive and
        # must refuse the corpse.
        values = {
            "experiment_uid": HEX16,
            "injection_method": "chaosblade",
            "fault_handle": {
                "kind": "experiment_uid", "value": HEX16,
                "method": "chaosblade",
            },
            "retired_experiment_uids": [HEX16],
            "messages": _create_pair("tc-eg-c") + _destroy_pair("tc-eg-d"),
        }
        assert await _run_gate(values, monkeypatch) == 0

    @pytest.mark.asyncio
    async def test_live_experiment_still_fires(self, monkeypatch):
        # The positive quadrant: a genuinely live experiment (create
        # receipt, no destroy, no retire) MUST still trigger the
        # emergency recovery — the carrier split must not over-fire.
        values = {
            "experiment_uid": HEX16,
            "injection_method": "chaosblade",
            "fault_handle": {
                "kind": "experiment_uid", "value": HEX16,
                "method": "chaosblade",
            },
            "messages": _create_pair("tc-eg-live"),
        }
        assert await _run_gate(values, monkeypatch) == 1

    @pytest.mark.asyncio
    async def test_native_handle_keeps_committed_semantics(self, monkeypatch):
        # UID-less carriers have no death oracle at this gate (the
        # recover graph clears no attribution slot either), so the
        # conservative committed predicate stands: a cancelled native
        # task fires the emergency recovery even after the reverse
        # operations may have run — a missed recovery (residual
        # environment fault) is strictly worse than a redundant one.
        values = {
            "injection_method": "kubectl_native",
            "fault_handle": {"kind": "native", "method": "kubectl_native"},
            "execution_artifacts": [{"op": "kubectl patch"}],
            "messages": [],
        }
        assert await _run_gate(values, monkeypatch) == 1

    @pytest.mark.asyncio
    async def test_no_fault_does_not_fire(self, monkeypatch):
        assert await _run_gate({"messages": []}, monkeypatch) == 0

    @pytest.mark.asyncio
    async def test_destroyed_experiment_without_landed_ledger(self, monkeypatch):
        # The boundary-turn shape (pre-absorption ledger): the retired
        # list is still empty, but the PROVEN destroy pair in messages
        # is enough for the liability primitive (owned − retired −
        # message-proven destroy) — the gate must still refuse.
        values = {
            "experiment_uid": HEX16,
            "injection_method": "chaosblade",
            "fault_handle": {
                "kind": "experiment_uid", "value": HEX16,
                "method": "chaosblade",
            },
            "retired_experiment_uids": [],
            "messages": _create_pair("tc-eg-bc") + _destroy_pair("tc-eg-bd"),
        }
        assert await _run_gate(values, monkeypatch) == 0

    @pytest.mark.asyncio
    async def test_stored_handle_less_corpse_does_not_fire(self, monkeypatch):
        # Round-28 hydration lane (probe C2, new behaviour pinned): a
        # corpse UID whose checkpoint predates ``fault_handle`` carries
        # NO stored dict. The round-25 INLINE gate read the stored dict
        # only — its absence fell into the committed branch and FIRED the
        # recover graph at the corpse. The single-sourced predicate
        # materializes first, so the derived experiment handle (the
        # provider claims the bare UID slot by real method attribution)
        # reaches the liability oracle and the gate refuses.
        values = {
            "experiment_uid": HEX16,
            "injection_method": "kubectl_exec",
            "retired_experiment_uids": [HEX16],
            "messages": _create_pair("tc-eg-hyd") + _destroy_pair("tc-eg-hyd2"),
        }
        assert await _run_gate(values, monkeypatch) == 0

    @pytest.mark.asyncio
    async def test_stored_handle_less_live_still_fires(self, monkeypatch):
        # The same lane, LIVE flavour: hydration claims the bare UID
        # slot, the oracle convicts nothing, and the emergency recovery
        # still fires — the single-sourcing must not over-refuse.
        values = {
            "experiment_uid": HEX16,
            "injection_method": "kubectl_exec",
            "messages": _create_pair("tc-eg-hydl"),
        }
        assert await _run_gate(values, monkeypatch) == 1
