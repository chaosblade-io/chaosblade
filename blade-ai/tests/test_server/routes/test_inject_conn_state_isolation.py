"""Per-request connection identity flows through graph STATE, not a global.

Design invariant under test (ground-truth-single-authority): the ``inject`` /
``inject-stream`` routes carry per-request connection identity (kubeconfig /
context / mode / kubewiz / ssh) in the graph STATE they build
(``build_inject_initial_state``). They NEVER write the process-global
``_default_settings`` singleton and NEVER enter an ambient Settings scope.

Every kubectl/blade command builder on the per-task path resolves its
``--kubeconfig`` / ``--context`` and its channel from
``resolve_transport_target(state)`` (state > settings). Because each task owns
its own checkpointed state, two concurrent injects read their OWN identity —
there is no shared mutable carrier left to stomp. This is the root-cause cure
for the measured defect where a global-singleton write made request A build
``--kubeconfig=<B's value> --context=<B's value>``.

These tests exercise the real primitives — ``build_inject_initial_state``,
``resolve_transport_target``, ``build_kubectl_cmd``, ``TransportRegistry`` and
``_default_settings`` — without spinning up the graph, so they are
deterministic and offline.
"""

import asyncio

import pytest

from chaos_agent.agent.state_mgmt.state_builders import build_inject_initial_state
from chaos_agent.config.settings import _default_settings
from chaos_agent.tools.kubectl_cli import build_kubectl_cmd
from chaos_agent.transports import (
    KUBEWIZ_CHANNELS,
    TransportRegistry,
    resolve_channel_name,
    resolve_transport_target,
)


def _state(**conn_fields) -> dict:
    """Initial inject state carrying the given per-request connection fields."""
    return build_inject_initial_state(
        task_id="t-probe",
        fault_spec=None,
        kubeconfig=conn_fields.get("kubeconfig", ""),
        kube_context=conn_fields.get("context", ""),
        kube_connection_mode=conn_fields.get("kube_connection_mode", ""),
        kubewiz_cluster_uuid=conn_fields.get("cluster_uuid", ""),
        kubewiz_profile=conn_fields.get("profile", ""),
    )


class TestStateCarrier:
    def test_request_identity_lands_in_state(self):
        """Per-request connection fields are carried by the state dict."""
        st = _state(kubeconfig="/kc/A", context="ctx-A", kube_connection_mode="kubeconfig")
        assert st["kubeconfig"] == "/kc/A"
        assert st["kube_context"] == "ctx-A"
        assert st["kube_connection_mode"] == "kubeconfig"

    def test_unset_request_fields_stay_empty_in_state(self):
        """An unset field is stored empty/None so the seam falls back to settings."""
        st = _state()
        assert st["kubeconfig"] == ""
        assert st["kube_context"] == ""
        assert st["ssh_port"] is None  # None, never a truthy placeholder (D1)

    def test_target_hydrates_from_state_over_settings(self):
        """resolve_transport_target prefers state, falls back to settings."""
        saved_kc = _default_settings.kubeconfig_path
        saved_ctx = _default_settings.kube_context
        try:
            object.__setattr__(_default_settings, "kubeconfig_path", "/kc/GLOBAL")
            object.__setattr__(_default_settings, "kube_context", "ctx-GLOBAL")
            # State value wins.
            t = resolve_transport_target(_state(kubeconfig="/kc/A", context="ctx-A"))
            assert t.kubeconfig == "/kc/A"
            assert t.kube_context == "ctx-A"
            # Empty state → settings default (byte-identical standalone path).
            t0 = resolve_transport_target(_state())
            assert t0.kubeconfig == "/kc/GLOBAL"
            assert t0.kube_context == "ctx-GLOBAL"
        finally:
            object.__setattr__(_default_settings, "kubeconfig_path", saved_kc)
            object.__setattr__(_default_settings, "kube_context", saved_ctx)


class TestBuildReadsTargetNotGlobal:
    def test_build_uses_state_identity_not_process_global(self):
        """Core c1 invariant: --kubeconfig/--context come from the target, and
        the target comes from STATE — never read bare from the global settings.

        Pre-vs-post discriminator: the pre-cure builder read
        ``settings.kube_context`` directly, so a global write (or another
        concurrent task's write) changed the built command. With the sentinel
        global set to a DIFFERENT value than the state, the built command must
        carry the STATE value — which fails against the old code.
        """
        saved_kc = _default_settings.kubeconfig_path
        saved_ctx = _default_settings.kube_context
        try:
            object.__setattr__(_default_settings, "kubeconfig_path", "/kc/SENTINEL")
            object.__setattr__(_default_settings, "kube_context", "ctx-SENTINEL")
            target = resolve_transport_target(_state(kubeconfig="/kc/A", context="ctx-A"))
            cmd = build_kubectl_cmd("get", ["pods"], target=target)
            assert cmd[cmd.index("--kubeconfig") + 1] == "/kc/A"
            assert cmd[cmd.index("--context") + 1] == "ctx-A"
            assert "ctx-SENTINEL" not in cmd
        finally:
            object.__setattr__(_default_settings, "kubeconfig_path", saved_kc)
            object.__setattr__(_default_settings, "kube_context", saved_ctx)


class TestConcurrentStateIsolation:
    @pytest.mark.asyncio
    async def test_concurrent_tasks_build_their_own_identity(self):
        """Two concurrent per-task states each build their OWN identity.

        Formalizes the measured defect WITHOUT any scope or global write: each
        task resolves its target from its own state after a suspension point,
        so interleaving cannot cross identities.
        """
        saved_kc = _default_settings.kubeconfig_path
        saved_ctx = _default_settings.kube_context
        results: dict[str, tuple[str, str]] = {}
        try:
            object.__setattr__(_default_settings, "kubeconfig_path", "/kc/GLOBAL")
            object.__setattr__(_default_settings, "kube_context", "ctx-GLOBAL")

            async def _run_graph_build(name: str, kc: str, ctx: str):
                st = _state(kubeconfig=kc, context=ctx)
                # Mirror the graph: resolve the target from THIS task's state,
                # then build after a suspension point (interleaving window).
                target = resolve_transport_target(st)
                await asyncio.sleep(0.05)
                cmd = build_kubectl_cmd("get", ["pods"], target=target)
                results[name] = (
                    cmd[cmd.index("--kubeconfig") + 1],
                    cmd[cmd.index("--context") + 1],
                )

            await asyncio.gather(
                _run_graph_build("A", "/kc/A", "ctx-A"),
                _run_graph_build("B", "/kc/B", "ctx-B"),
            )
            assert results["A"] == ("/kc/A", "ctx-A")
            assert results["B"] == ("/kc/B", "ctx-B")
        finally:
            object.__setattr__(_default_settings, "kubeconfig_path", saved_kc)
            object.__setattr__(_default_settings, "kube_context", saved_ctx)

    @pytest.mark.asyncio
    async def test_global_singleton_never_mutated(self):
        """The state-carrier path never touches ``_default_settings``."""
        before_kc = _default_settings.kubeconfig_path
        before_ctx = _default_settings.kube_context

        async def _run_graph_build(kc: str, ctx: str):
            target = resolve_transport_target(_state(kubeconfig=kc, context=ctx))
            await asyncio.sleep(0.02)
            build_kubectl_cmd("get", ["pods"], target=target)

        await asyncio.gather(
            _run_graph_build("/kc/A", "ctx-A"),
            _run_graph_build("/kc/B", "ctx-B"),
        )
        assert _default_settings.kubeconfig_path == before_kc
        assert _default_settings.kube_context == before_ctx


class TestChannelSelectionIsStateAware:
    """D3 invariant for the blade tools (create/destroy/query_k8s/status/help).

    The blade command SHAPE must be derived from the SAME state-resolved target
    the command executes over — ``TransportRegistry.resolve(_target).name`` —
    not from the settings-only ``resolve_channel_name()``. A per-request
    ``kube_connection_mode`` in state otherwise makes the shape diverge from the
    execution channel (measured: shape said ``kubeconfig`` while the target ran
    over ``kubewiz_k8s``).
    """

    def test_state_override_drives_execution_channel(self):
        saved_mode = _default_settings.kube_connection_mode
        try:
            object.__setattr__(_default_settings, "kube_connection_mode", "kubeconfig")
            st = _state(kube_connection_mode="kubewiz_k8s", cluster_uuid="uuid-A")
            target = resolve_transport_target(st)
            # What the blade tools now read (state-aware D3):
            exec_channel = TransportRegistry.resolve(target).name
            assert exec_channel == "kubewiz_k8s"
            assert exec_channel in KUBEWIZ_CHANNELS
            # The settings-only read the D3 migration replaced — it diverges
            # under a per-request override, which is exactly why blade tools
            # must NOT use it for command shape.
            assert resolve_channel_name() == "kubeconfig"
        finally:
            object.__setattr__(_default_settings, "kube_connection_mode", saved_mode)

    def test_no_override_settings_and_state_agree(self):
        """Common path: with no state override the two reads are identical."""
        saved_mode = _default_settings.kube_connection_mode
        try:
            object.__setattr__(_default_settings, "kube_connection_mode", "kubeconfig")
            target = resolve_transport_target(_state())
            assert TransportRegistry.resolve(target).name == resolve_channel_name()
        finally:
            object.__setattr__(_default_settings, "kube_connection_mode", saved_mode)


class TestRouteDoesNotWriteGlobal:
    """Route-level regression: POST /inject must not mutate ``_default_settings``.

    The old handler wrote connection identity straight onto the proxy
    (``settings.kubeconfig_path = request.…``) SYNCHRONOUSLY in the handler body
    — with no ContextVar set, that landed on the global ``_default_settings``.
    The cured handler only builds the initial STATE, so asserting the singleton
    is untouched after the request returns fails against the old code and passes
    against the fix, independent of the background task.
    """

    def _client(self):
        from unittest.mock import AsyncMock

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from chaos_agent.server.app import TaskTracker
        from chaos_agent.server.routes.inject import inject_router

        app = FastAPI()
        app.state.agents = {"pipeline": AsyncMock()}
        app.state.task_tracker = TaskTracker()
        app.include_router(inject_router)
        return TestClient(app)

    def test_inject_route_leaves_global_singleton_untouched(self):
        sentinel_kc = "/kc/global-sentinel"
        sentinel_ctx = "ctx-global-sentinel"
        saved_kc = _default_settings.kubeconfig_path
        saved_ctx = _default_settings.kube_context
        try:
            object.__setattr__(_default_settings, "kubeconfig_path", sentinel_kc)
            object.__setattr__(_default_settings, "kube_context", sentinel_ctx)

            client = self._client()
            resp = client.post(
                "/api/v1/inject",
                json={
                    "input": "cpu fullload on my-app",
                    "confirm": True,
                    "kubeconfig": "/kc/route",
                    "context": "ctx-route",
                },
            )
            assert resp.status_code == 200
            # The handler ran to completion (it created the background task);
            # the global singleton must be exactly the sentinel — the route
            # carries the request identity in the graph STATE, not the global.
            assert _default_settings.kubeconfig_path == sentinel_kc
            assert _default_settings.kube_context == sentinel_ctx
        finally:
            object.__setattr__(_default_settings, "kubeconfig_path", saved_kc)
            object.__setattr__(_default_settings, "kube_context", saved_ctx)
