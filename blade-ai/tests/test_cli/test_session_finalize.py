"""CLI failure-exit seams: auto_rollback dispatch and _format_error codes.

auto_rollback must behave identically for every carrier: blade-family handles
dispatch a UID destroy through the provider registry, UID-less native
handles decline (the recover graph's job), and errors surface as a
readable suffix instead of crashing finalize.

_format_error is the last stop before the JSON envelope, so it decides
whether a failure stays diagnosable — see
``TestFormatErrorSurfacesProviderReject`` for the LLM provider-rejection
code (4006) that issue #1344 needed and did not have.
"""

import pytest

from chaos_agent.cli.session_finalize import auto_rollback

CONFIG = {"configurable": {"thread_id": "t-1"}}


class _FakeSnapshot:
    def __init__(self, values):
        self.values = values


class _FakeGraph:
    def __init__(self, values=None, raise_exc=None):
        self._values = values
        self._raise = raise_exc

    async def aget_state(self, config):
        if self._raise is not None:
            raise self._raise
        return _FakeSnapshot(self._values)


@pytest.mark.asyncio
async def test_blade_handle_dispatches_destroy(monkeypatch):
    """Legacy checkpoint shape: the UID fact and its birth receipt survive
    (no fault_handle), hydration derives the blade handle and the registry
    dispatches destroy. Round-30: the action domain followed the r29 gate
    domain — the experiment-kind branch now sweeps the live liability SET
    (the dispatch rides the sweep's bare destroy; the suffix names every
    UID rolled back). The UID still carries the birth receipt (r26 no-net
    shape) and the legislated HEX16 form (the provenance scan's shape
    gate)."""
    from langchain_core.messages import AIMessage, ToolMessage

    from chaos_agent.agent.providers.chaosblade import cli as blade_tools_mod

    uid = "aabbccdd00000011"
    calls = []

    class _FakeDestroy:
        async def ainvoke(self, args):
            calls.append(args)
            return "destroyed"

    monkeypatch.setattr(blade_tools_mod, "blade_destroy", _FakeDestroy())
    graph = _FakeGraph({
        "experiment_uid": uid,
        "kubeconfig": "/tmp/kc",
        "messages": [
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "blade_create",
                    "args": {"command": "create k8s pod-cpu fullload"},
                    "id": "tc-r30-rb",
                    "type": "tool_call",
                }],
            ),
            ToolMessage(
                content='{"code":200,"success":true,"result":"%s"}' % uid,
                name="blade_create",
                tool_call_id="tc-r30-rb",
            ),
        ],
    })
    suffix = await auto_rollback(graph, CONFIG)
    assert suffix == f" (auto-rolled back experiment_uids={uid})"
    assert calls == [{"uid": uid, "kubeconfig": "/tmp/kc"}]


@pytest.mark.asyncio
async def test_carried_handle_wins_over_stale_legacy_uid(monkeypatch):
    """An already-projected handle wins over re-derivation: dispatch must
    follow the HANDLE's value even when a stale legacy UID lingers.
    Round-28: the live gate still passes — the handle's value rides the
    owned evidence (its birth receipt), while the stale slot value is
    exactly the corpse shape the gate exists to refuse."""
    from langchain_core.messages import AIMessage, ToolMessage

    from chaos_agent.agent.providers.chaosblade import cli as blade_tools_mod

    uid = "aabbccdd00000022"
    captured = {}

    class _CapturingDestroy:
        async def ainvoke(self, args):
            captured.update(args)
            return "destroyed"

    monkeypatch.setattr(blade_tools_mod, "blade_destroy", _CapturingDestroy())
    graph = _FakeGraph({
        "experiment_uid": "stale-uid",
        "fault_handle": {
            "kind": "experiment_uid", "value": uid, "method": "host_blade",
        },
        "kubeconfig": "",
        "messages": [
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "blade_create",
                    "args": {"command": "create k8s pod-cpu fullload"},
                    "id": "tc-r28-rb2",
                    "type": "tool_call",
                }],
            ),
            ToolMessage(
                content='{"code":200,"success":true,"result":"%s"}' % uid,
                name="blade_create",
                tool_call_id="tc-r28-rb2",
            ),
        ],
    })
    suffix = await auto_rollback(graph, CONFIG)
    assert suffix == f" (auto-rolled back experiment_uids={uid})"
    assert captured["uid"] == uid


@pytest.mark.asyncio
async def test_native_handle_declines_rollback():
    """UID-less native carriers decline the synchronous failure-path rollback;
    their safety net is the explicit recover graph."""
    graph = _FakeGraph({"injection_method": "kubectl_native"})
    assert await auto_rollback(graph, CONFIG) == ""


class TestRollbackDomainAlignment:
    """Round-30: the r29 LIVE gate judges the PLURAL liability set, so the
    ACTION must cover the same set. The old experiment-kind branch
    dispatched the SINGULAR handle value (the attribution slot, which a
    composite create parks with its FIRST birth) — the gate licensed a
    rollback on the live sibling while the dispatch destroyed a corpse
    and the suffix reported success (round-30 K1')."""

    @staticmethod
    def _stub_sweep_destroy(monkeypatch, method="host_blade") -> list:
        import json as _json

        from chaos_agent.agent.providers import FaultProviderRegistry

        provider = FaultProviderRegistry.resolve_by_method(method)
        calls: list = []

        async def _fake_destroy(uid, kubeconfig=""):
            calls.append(uid)
            return _json.dumps({"code": 200, "success": True, "result": uid})

        monkeypatch.setattr(provider, "layer1_raw_destroy", _fake_destroy)
        return calls

    @pytest.mark.asyncio
    async def test_live_sibling_recovered_not_the_dead_slot(self, monkeypatch):
        """K1' flipped: first birth dead + sibling live — the sweep
        dispatches the SIBLING, the corpse stays untouched."""
        calls = self._stub_sweep_destroy(monkeypatch)
        graph = _FakeGraph({
            "experiment_uid": "aabbccdd000000aa",  # slot = first birth
            "injection_method": "host_blade",
            "owned_experiment_uids": ["aabbccdd000000aa", "aabbccdd000000bb"],
            "retired_experiment_uids": ["aabbccdd000000aa"],
            "fault_handle": {
                "kind": "experiment_uid",
                "value": "aabbccdd000000aa",
                "method": "host_blade",
            },
            "kubeconfig": "/tmp/kc",
            "messages": [],
        })
        suffix = await auto_rollback(graph, CONFIG)
        assert calls == ["aabbccdd000000bb"]
        assert suffix == (
            " (auto-rolled back experiment_uids=aabbccdd000000bb)"
        )

    @pytest.mark.asyncio
    async def test_all_dead_rolls_back_nothing(self, monkeypatch):
        """The r29 corpse protection survives the domain change: an empty
        live set renders "" through the sweep's own live filter."""
        calls = self._stub_sweep_destroy(monkeypatch)
        graph = _FakeGraph({
            "experiment_uid": "aabbccdd000000aa",
            "injection_method": "host_blade",
            "owned_experiment_uids": ["aabbccdd000000aa"],
            "retired_experiment_uids": ["aabbccdd000000aa"],
            "fault_handle": {
                "kind": "experiment_uid",
                "value": "aabbccdd000000aa",
                "method": "host_blade",
            },
            "kubeconfig": "/tmp/kc",
            "messages": [],
        })
        assert await auto_rollback(graph, CONFIG) == ""
        assert calls == []

    @pytest.mark.asyncio
    async def test_plural_live_set_all_recovered(self, monkeypatch):
        """Two live births (contract replacement shape): the sweep covers
        EVERY live UID — the domain the gate licensed."""
        calls = self._stub_sweep_destroy(monkeypatch)
        graph = _FakeGraph({
            "experiment_uid": "aabbccdd000000bb",  # last-write-wins slot
            "injection_method": "host_blade",
            "owned_experiment_uids": ["aabbccdd000000aa", "aabbccdd000000bb"],
            "retired_experiment_uids": [],
            "fault_handle": {
                "kind": "experiment_uid",
                "value": "aabbccdd000000bb",
                "method": "host_blade",
            },
            "kubeconfig": "/tmp/kc",
            "messages": [],
        })
        suffix = await auto_rollback(graph, CONFIG)
        assert sorted(calls) == ["aabbccdd000000aa", "aabbccdd000000bb"]
        assert suffix == (
            " (auto-rolled back experiment_uids="
            "aabbccdd000000aa, aabbccdd000000bb)"
        )

    @pytest.mark.asyncio
    async def test_incluster_sibling_surfaces_guidance_not_false_success(
        self, monkeypatch,
    ):
        """The K1' composite shape in its real delivery channel (kubectl
        exec): the sweep cannot deterministically reach a CRD-created
        experiment from the host, so the suffix surfaces the guidance —
        an honest "still owed" instead of the old suffix that reported
        the corpse's slot uid as rolled back."""
        calls = self._stub_sweep_destroy(monkeypatch)
        graph = _FakeGraph({
            "experiment_uid": "aabbccdd000000aa",
            "injection_method": "kubectl_exec",
            "owned_experiment_uids": ["aabbccdd000000aa", "aabbccdd000000bb"],
            "retired_experiment_uids": ["aabbccdd000000aa"],
            "fault_handle": {
                "kind": "experiment_uid",
                "value": "aabbccdd000000aa",
                "method": "kubectl_exec",
            },
            "kubeconfig": "/tmp/kc",
            "messages": [],
        })
        suffix = await auto_rollback(graph, CONFIG)
        assert calls == []  # no host destroy is fired at a CRD experiment
        assert "rollback INCOMPLETE" in suffix
        assert "aabbccdd000000bb" in suffix
        assert "kubectl exec" in suffix


@pytest.mark.asyncio
async def test_no_attribution_is_a_noop():
    graph = _FakeGraph({"safety_status": "rejected"})
    assert await auto_rollback(graph, CONFIG) == ""


@pytest.mark.asyncio
async def test_state_read_failure_surfaces_suffix():
    graph = _FakeGraph(raise_exc=RuntimeError("checkpoint gone"))
    suffix = await auto_rollback(graph, CONFIG)
    assert suffix.startswith(" (rollback FAILED:")


def test_server_inject_routes_use_shared_auto_rollback():
    """The HTTP inject routes must dispatch through the single tested seam —
    a route-local twin copy of the rollback logic is how the CLI and server
    paths drifted before (guard against re-inlining)."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for rel in (
        "src/chaos_agent/server/routes/inject.py",
        "src/chaos_agent/server/routes/inject_stream.py",
    ):
        text = (root / rel).read_text(encoding="utf-8")
        assert "from chaos_agent.cli.session_finalize import auto_rollback" in text, (
            f"{rel}: must dispatch via the shared auto_rollback seam"
        )
        assert "rollback_handle(" not in text, (
            f"{rel}: must not inline a direct registry rollback dispatch"
        )
        assert "blade_destroy" not in text, (
            f"{rel}: must not inline a carrier-specific destroy"
        )


class TestFormatErrorSurfacesProviderReject:
    """Layer-3 exit: a provider rejection must reach the CLI envelope as 4006.

    ``_format_error`` needed NO change for the new error type — that is the
    whole point of carrying ``error_code`` on the class. These pin the
    zero-change claim, because the failure they guard against is exactly what
    made issue #1344 undiagnosable: the rejection was flattened into the
    generic 4001 and the actionable part of the provider's message was lost.
    """

    def test_provider_reject_maps_to_4006_with_the_hint_intact(self):
        from chaos_agent.cli.session_finalize import _format_error
        from chaos_agent.errors import LLMProviderRejectError

        err = LLMProviderRejectError(
            "LLM provider rejection (HTTP 400): provider rejected the "
            "tool_call/ToolMessage pairing"
        )
        code, msg = _format_error(err)
        assert code == 4006
        assert msg.startswith("LLMProviderRejectError:")
        assert "tool_call/ToolMessage pairing" in msg  # hint survives verbatim

    def test_provider_reject_is_permanent_not_transient(self):
        """A deterministic rejection must not be retried by any severity-driven
        consumer (tools/retry.py keys off ``is_transient``)."""
        from chaos_agent.errors import (
            ErrorSeverity,
            LLMProviderRejectError,
            is_transient,
        )

        err = LLMProviderRejectError("hint")
        assert LLMProviderRejectError.severity is ErrorSeverity.PERMANENT
        assert is_transient(err) is False

    def test_original_provider_exception_stays_reachable_as_cause(self):
        """``raise ... from e`` keeps the full provider body for debugging."""
        from chaos_agent.errors import LLMProviderRejectError

        raw = RuntimeError(
            "Messages with role 'tool' must be a response to a preceding "
            "message with 'tool_calls'"
        )
        try:
            try:
                raise raw
            except RuntimeError as exc:
                raise LLMProviderRejectError("hint") from exc
        except LLMProviderRejectError as wrapped:
            assert wrapped.__cause__ is raw
            assert "must be a response to a preceding" in str(wrapped.__cause__)

    def test_unclassified_exception_still_falls_back_to_4001(self):
        """The new code must not swallow the generic bucket."""
        from chaos_agent.cli.session_finalize import _format_error

        assert _format_error(ValueError("boom")) == (4001, "ValueError: boom")

    def test_envelope_code_4006_exits_nonzero(self):
        """CI contract: any non-zero envelope code exits 1 (4006 included)."""
        import typer

        from chaos_agent.preflight import exit_for_envelope

        with pytest.raises(typer.Exit) as excinfo:
            exit_for_envelope({"code": 4006, "message": "LLMProviderRejectError: ..."})
        assert excinfo.value.exit_code == 1
