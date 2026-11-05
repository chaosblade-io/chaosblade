"""Tests for the preplan_probe node (fresh pre-task probes).

The probe bundle is published as ONE observation message appended to
``messages`` — persisted with the conversation, visible to every later
agent_loop entry (including replan re-entries) as plain history.
"""

from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import SystemMessage

from chaos_agent.agent.nodes.gates import preplan_probe as pp
from chaos_agent.agent.nodes.gates.preplan_probe import preplan_probe
from chaos_agent.config.settings import settings

_MODULE = "chaos_agent.agent.nodes.gates.preplan_probe"

# Minimal probe set: only probes with proven in-loop rediscovery cost stay
# here; the safety-domain probes live in the Phase 2 gate exclusively.
_ALL_PROBES = (
    "_probe_operator",
    "_probe_metrics_server",
    "_probe_carrier_images",
)


def _ok(name: str):
    return AsyncMock(return_value=("ok", f"{name} fine", {}))


@pytest.fixture
def k8s_state(sample_agent_state, monkeypatch):
    """State resolving to a reachable K8s channel."""
    monkeypatch.setattr(settings, "kubeconfig_path", "/fake/kubeconfig")
    monkeypatch.setattr(settings, "kube_connection_mode", "kubeconfig")
    monkeypatch.setattr(settings, "preplan_probes_enabled", True)
    return sample_agent_state


def _run_probes(state, overrides: dict | None = None):
    """Patch every probe function (ok by default) and run the node."""
    mocks = {name: _ok(name) for name in _ALL_PROBES}
    if overrides:
        mocks.update(overrides)
    patches = [patch(f"{_MODULE}.{name}", new=m) for name, m in mocks.items()]
    for p in patches:
        p.start()
    return mocks, patches


class TestSkipMarkers:
    """Skip paths emit nothing — no message, no state change."""

    @pytest.mark.asyncio
    async def test_disabled_via_settings(self, k8s_state, monkeypatch):
        monkeypatch.setattr(settings, "preplan_probes_enabled", False)
        with patch(f"{_MODULE}._probe_operator") as probe:
            result = await preplan_probe(k8s_state)
        assert result == {}
        probe.assert_not_called()


class TestK8sHappyPath:
    @pytest.mark.asyncio
    async def test_publishes_one_observation_message(self, k8s_state):
        mocks, patches = _run_probes(k8s_state)
        try:
            result = await preplan_probe(k8s_state)
        finally:
            for p in patches:
                p.stop()

        for m in mocks.values():
            m.assert_awaited_once()
        # Exactly ONE message, appended to the conversation history.
        messages = result["messages"]
        assert len(messages) == 1
        msg = messages[0]
        assert isinstance(msg, SystemMessage)
        assert "Pre-task environment probes" in msg.content
        assert "- chaosblade_operator [ok]: _probe_operator fine" in msg.content
        assert "- metrics_server [ok]: _probe_metrics_server fine" in msg.content
        # Hint-not-verdict discipline is carried in the message body.
        assert "planning hints, not safety verdicts" in msg.content
        # No dedicated state field anymore.
        assert "preplan_probes" not in result

    @pytest.mark.asyncio
    async def test_probe_failure_degrades_to_unknown(self, k8s_state):
        """A failing probe never blocks the node — it becomes 'unknown'."""
        mocks, patches = _run_probes(k8s_state, overrides={
            "_probe_metrics_server": AsyncMock(side_effect=RuntimeError("boom")),
        })
        try:
            result = await preplan_probe(k8s_state)
        finally:
            for p in patches:
                p.stop()

        msg = result["messages"][0]
        assert "metrics_server [unknown]: probe failed: boom" in msg.content
        # The other probe still landed in the same message.
        assert "chaosblade_operator [ok]" in msg.content

    @pytest.mark.asyncio
    async def test_probe_timeout_degrades_to_unknown(self, k8s_state, monkeypatch):
        monkeypatch.setattr(settings, "preplan_probe_timeout", 0.05)

        async def _slow(*_args, **_kwargs):
            import asyncio

            await asyncio.sleep(0.5)
            return "ok", "never reached", {}

        mocks, patches = _run_probes(k8s_state, overrides={
            "_probe_metrics_server": _slow,
        })
        try:
            result = await preplan_probe(k8s_state)
        finally:
            for p in patches:
                p.stop()

        msg = result["messages"][0]
        assert "metrics_server [unknown]" in msg.content
        assert "timed out" in msg.content


class TestHostChannel:
    """Host channel runs ONLY the toolkit probe; the K8s set never fires.

    The old behavior (host channel skips everything, emits nothing) was the
    root of the ss→netstat→/proc/net/tcp blind-probe waste: the host planner
    got zero pre-task facts. Now the ONE host-relevant probe runs, but a
    healthy host still emits nothing (the toolkit probe degrades to
    ``skipped`` when every binary is present), preserving zero-noise.
    """

    @pytest.mark.asyncio
    async def test_healthy_host_emits_nothing(self, sample_agent_state, monkeypatch):
        monkeypatch.setattr(settings, "preplan_probes_enabled", True)
        # Force a host-scope channel regardless of connection fields.
        monkeypatch.setattr(pp, "resolve_channel_name", lambda state: "ssh")
        k8s_mocks = {
            name: AsyncMock(return_value=("ok", "should never run", {}))
            for name in _ALL_PROBES
        }
        toolkit = AsyncMock(return_value=("skipped", "all present", {}))
        patches = [patch(f"{_MODULE}.{name}", new=m) for name, m in k8s_mocks.items()]
        patches.append(patch(f"{_MODULE}._probe_host_toolkit", new=toolkit))
        for p in patches:
            p.start()
        try:
            result = await preplan_probe(sample_agent_state)
        finally:
            for p in patches:
                p.stop()

        # The K8s probe set is meaningless on a host channel — never called.
        for m in k8s_mocks.values():
            m.assert_not_called()
        # The host-relevant probe DID run.
        toolkit.assert_awaited_once()
        # skipped → no message published: zero noise on a healthy host.
        assert result.get("messages") in (None, [])

    @pytest.mark.asyncio
    async def test_absent_toolkit_publishes_hint(self, sample_agent_state, monkeypatch):
        """An absent-binary result publishes ONE planning-hint message."""
        monkeypatch.setattr(settings, "preplan_probes_enabled", True)
        monkeypatch.setattr(pp, "resolve_channel_name", lambda state: "ssh")
        toolkit = AsyncMock(return_value=(
            "ok",
            "these host observation binaries are NOT installed on the target "
            "— sockets (netstat, ss absent) → read /proc/net/tcp",
            {"absent": ["netstat", "ss"]},
        ))
        with patch(f"{_MODULE}._probe_host_toolkit", new=toolkit):
            result = await preplan_probe(sample_agent_state)

        toolkit.assert_awaited_once()
        messages = result["messages"]
        assert len(messages) == 1
        assert isinstance(messages[0], SystemMessage)
        assert "host_toolkit [ok]" in messages[0].content
        assert "/proc/net/tcp" in messages[0].content
        # Hint-not-verdict discipline is carried in the shared message body.
        assert "planning hints, not safety verdicts" in messages[0].content

    @pytest.mark.asyncio
    async def test_absent_toolkit_mirrored_to_state(self, sample_agent_state, monkeypatch):
        """The absent set is ALSO mirrored to ``absent_host_binaries`` for the
        one consumer that cannot read the observation message: baseline's
        off-graph LLM derive. Without this mirror the primary derive re-proposes
        an absent ``ss`` and burns an exit-127 + reactive retry round (the
        preplan charter's "planning phase gets nothing" gap)."""
        monkeypatch.setattr(settings, "preplan_probes_enabled", True)
        monkeypatch.setattr(pp, "resolve_channel_name", lambda state: "ssh")
        toolkit = AsyncMock(return_value=(
            "ok",
            "these host observation binaries are NOT installed on the target "
            "— sockets (netstat, ss absent) → read /proc/net/tcp",
            {"absent": ["ss", "netstat"], "present": ["free"],
             "dimensions": {"sockets": ["netstat", "ss"]}},
        ))
        with patch(f"{_MODULE}._probe_host_toolkit", new=toolkit):
            result = await preplan_probe(sample_agent_state)

        # Sorted, de-duplicated tuple — the exact shape baseline threads into the
        # derive prompt's poisoned_binaries.
        assert result["absent_host_binaries"] == ("netstat", "ss")
        # The agent-planner observation message is unchanged (both consumers fed).
        assert len(result["messages"]) == 1

    @pytest.mark.asyncio
    async def test_healthy_host_mirrors_no_state(self, sample_agent_state, monkeypatch):
        """A fully-tooled host (probe skipped) writes NO state mirror, so the
        baseline derive behaves byte-identically to before on a healthy host."""
        monkeypatch.setattr(settings, "preplan_probes_enabled", True)
        monkeypatch.setattr(pp, "resolve_channel_name", lambda state: "ssh")
        toolkit = AsyncMock(return_value=("skipped", "all present", {}))
        with patch(f"{_MODULE}._probe_host_toolkit", new=toolkit):
            result = await preplan_probe(sample_agent_state)
        assert "absent_host_binaries" not in result


def test_absent_host_binaries_is_declared_state_channel():
    """Guard (LangGraph silent-drop lesson): the key preplan mirrors MUST be a
    declared AgentState channel AND lifecycle-registered, else
    ``StateGraph(AgentState)`` drops the node-return key without error and the
    baseline derive never sees it — a breakage function-level dict tests cannot
    catch. Ties the preplan writer to the state schema it depends on."""
    from chaos_agent.agent.state import AgentState
    from chaos_agent.agent.state_mgmt.state_lifecycle import (
        iter_state_fields,
        state_field_policy,
    )

    assert "absent_host_binaries" in AgentState.__annotations__
    assert "absent_host_binaries" in set(iter_state_fields())
    # Per-task host fact: reset on batch advance (no cross-fault leak) and on
    # recover entry; never durable (preplan re-probes each run).
    policy = state_field_policy("absent_host_binaries")
    assert policy is not None
    assert policy.reset_on_batch_fault is True
    assert policy.durable is False


class TestHostToolkitProbe:
    """Unit behavior of ``_probe_host_toolkit``.

    Absent → hint with the /proc fallback; present/failed/empty → ``skipped``
    (silent); the dispatched command is a shell-free ``sh -c`` argv (never
    ``shell=True``) running a read-only ``command -v`` sweep; and the
    binary→dimension→fallback map stays single-sourced.
    """

    _EXEC = "chaos_agent.transports.executor.execute_via_transport"

    @staticmethod
    def _res(stdout: str = "", exit_code: int = 0):
        from types import SimpleNamespace

        return SimpleNamespace(stdout=stdout, stderr="", exit_code=exit_code)

    @pytest.mark.asyncio
    async def test_absent_binaries_map_to_proc_fallback(self):
        # ss/netstat absent; everything else present.
        stdout = "A ss\nA netstat\nP mpstat\nP sar\nP iostat\nP iotop\nP free\n"
        exec_mock = AsyncMock(return_value=self._res(stdout=stdout))
        with patch(self._EXEC, new=exec_mock):
            status, summary, detail = await pp._probe_host_toolkit(
                target=object(), task_id="t",
            )
        assert status == "ok"
        assert detail["absent"] == ["netstat", "ss"]
        # sockets dimension collapses ss+netstat into ONE /proc/net/tcp hint.
        assert "/proc/net/tcp" in summary
        assert detail["dimensions"] == {"sockets": ["netstat", "ss"]}
        # Dispatch shape: shell-free argv `sh -c '<loop>'`, read-only sweep,
        # internal-probe flags (skip guard + audit off), host-profile belt.
        args, kwargs = exec_mock.call_args
        cmd = args[0]
        assert cmd[0] == "sh" and cmd[1] == "-c"
        assert "command -v" in cmd[2]
        assert kwargs["skip_guard"] is True
        assert kwargs["audit"] is False
        assert kwargs["expect_profile"] == pp.PROFILE_HOST

    @pytest.mark.asyncio
    async def test_all_present_returns_skipped(self):
        stdout = "".join(f"P {b}\n" for b in sorted(pp._HOST_TOOLKIT_DIMENSIONS))
        with patch(self._EXEC, new=AsyncMock(return_value=self._res(stdout=stdout))):
            status, summary, _detail = await pp._probe_host_toolkit(target=object())
        assert status == "skipped"
        assert "all probed observation binaries present" in summary

    @pytest.mark.asyncio
    async def test_nonzero_exit_returns_skipped(self):
        with patch(self._EXEC, new=AsyncMock(
            return_value=self._res(stdout="", exit_code=127),
        )):
            status, _summary, _detail = await pp._probe_host_toolkit(target=object())
        assert status == "skipped"

    @pytest.mark.asyncio
    async def test_exception_returns_skipped(self):
        with patch(self._EXEC, new=AsyncMock(side_effect=RuntimeError("no channel"))):
            status, summary, _detail = await pp._probe_host_toolkit(target=object())
        assert status == "skipped"
        assert "unavailable" in summary

    @pytest.mark.asyncio
    async def test_empty_output_returns_skipped(self):
        with patch(self._EXEC, new=AsyncMock(return_value=self._res(stdout=""))):
            status, _summary, _detail = await pp._probe_host_toolkit(target=object())
        assert status == "skipped"

    def test_every_probed_binary_dimension_has_a_fallback(self):
        """Single-source integrity: no probed binary can go absent into a
        dimension that has no published /proc fallback."""
        for binary, dim in pp._HOST_TOOLKIT_DIMENSIONS.items():
            assert dim in pp._HOST_OBSERVATION_FALLBACKS, (
                f"{binary}→{dim} has no entry in _HOST_OBSERVATION_FALLBACKS"
            )



class TestOperatorFallbackAttribution:
    """_probe_operator attaches node attribution to the fallback hint.

    Node attribution is the load-bearing fact: the fallback carrier for any
    fault family is the tool pod on the target node (inject-ccfadf7d spent
    ~5 LLM rounds rediscovering exactly this).
    """

    _DETECT = (
        "chaos_agent.tools.pod_discovery."
        "discover_tool_pods_cluster_wide_with_nodes"
    )
    _PODS = [
        ("chaosblade-tool-abc", "chaosblade", "node-a"),
        ("chaosblade-tool-def", "chaosblade", "node-b"),
    ]

    @staticmethod
    def _op_result(passed: bool):
        from types import SimpleNamespace

        return SimpleNamespace(passed=passed, message="operator not ready", fix="")

    @pytest.mark.asyncio
    async def test_fallback_lists_pods_with_nodes(self):
        with patch(
            "chaos_agent.preflight.check_chaosblade_operator",
            AsyncMock(return_value=self._op_result(False)),
        ), patch(self._DETECT, AsyncMock(return_value=self._PODS)):
            status, summary, detail = await pp._probe_operator("/fake/kc")
        assert status == "warning"
        assert "chaosblade-tool-abc on node-a" in summary
        assert "chaosblade-tool-def on node-b" in summary
        # Broken operator -> heuristic consequence statement only; never an
        # imperative planning directive (path choice stays with the planner).
        assert "will not be reconciled" in summary
        assert "planning directive" not in summary
        assert detail["tool_pods"][0] == {
            "pod": "chaosblade-tool-abc", "namespace": "chaosblade",
            "node": "node-a",
        }
        # No target_node -> pod/container scope: no exec-carrier claims.
        assert "PRIMARY injection" not in summary

    @pytest.mark.asyncio
    async def test_target_node_carrier_is_highlighted(self):
        with patch(
            "chaos_agent.preflight.check_chaosblade_operator",
            AsyncMock(return_value=self._op_result(False)),
        ), patch(self._DETECT, AsyncMock(return_value=self._PODS)):
            _status, summary, detail = await pp._probe_operator(
                "/fake/kc", target_node="node-b",
            )
        assert "ON the target node node-b: chaosblade/chaosblade-tool-def" in summary
        assert detail["tool_pod_on_target"] == {
            "pod": "chaosblade-tool-def", "namespace": "chaosblade",
            "node": "node-b",
        }
        # Carrier presence is stated as fact; no imperative path ordering.
        assert "planning directive" not in summary

    @pytest.mark.asyncio
    async def test_target_node_missing_from_roster_is_flagged(self):
        with patch(
            "chaos_agent.preflight.check_chaosblade_operator",
            AsyncMock(return_value=self._op_result(False)),
        ), patch(self._DETECT, AsyncMock(return_value=self._PODS)):
            _status, summary, _detail = await pp._probe_operator(
                "/fake/kc", target_node="node-x",
            )
        assert "none on target node node-x" in summary
        # Unavailability is reported as a fact; the planner decides the path.
        assert "currently unavailable there" in summary
        assert "planning directive" not in summary

    @pytest.mark.asyncio
    async def test_no_tool_pods_anywhere_reports_fallback_unavailable(self):
        with patch(
            "chaos_agent.preflight.check_chaosblade_operator",
            AsyncMock(return_value=self._op_result(False)),
        ), patch(self._DETECT, AsyncMock(return_value=[])):
            _status, summary, _detail = await pp._probe_operator(
                "/fake/kc", target_node="node-x",
            )
        assert "no tool pods found anywhere" in summary
        assert "planning directive" not in summary

    @pytest.mark.asyncio
    async def test_discovery_failure_reports_unverified(self):
        with patch(
            "chaos_agent.preflight.check_chaosblade_operator",
            AsyncMock(return_value=self._op_result(False)),
        ), patch(self._DETECT, AsyncMock(side_effect=RuntimeError("boom"))):
            _status, summary, _detail = await pp._probe_operator(
                "/fake/kc", target_node="node-x",
            )
        # Unknown is reported as unknown — the planner verifies in-loop.
        assert "carrier availability currently unknown" in summary
        assert "planning directive" not in summary

    @pytest.mark.asyncio
    async def test_operator_healthy_skips_fallback_discovery(self):
        discover = AsyncMock(return_value=self._PODS)
        with patch(
            "chaos_agent.preflight.check_chaosblade_operator",
            AsyncMock(return_value=self._op_result(True)),
        ), patch(self._DETECT, discover):
            status, summary, _detail = await pp._probe_operator(
                "/fake/kc", target_node="node-a",
            )
        discover.assert_not_awaited()
        assert status == "ok"
        assert "tool pods" not in summary
        # Healthy operator -> no consequence statement, no directive.
        assert "planning directive" not in summary
        assert "will not be reconciled" not in summary


class TestTargetNodePlumbing:
    """Node-scope specs name the target node; the probe receives it."""

    @pytest.mark.asyncio
    async def test_node_scope_spec_passes_target_node(self, k8s_state):
        from chaos_agent.agent.spec.fault_spec import FaultSpec

        k8s_state["fault_spec"] = FaultSpec(
            scope="node", names=("node-b",),
            fault_target="mem", fault_action="load",
        ).to_dict()
        mocks, patches = _run_probes(k8s_state)
        try:
            await preplan_probe(k8s_state)
        finally:
            for p in patches:
                p.stop()
        assert mocks["_probe_operator"].await_args.kwargs["target_node"] == "node-b"

    @pytest.mark.asyncio
    async def test_pod_scope_spec_passes_no_target_node(self, k8s_state):
        from chaos_agent.agent.spec.fault_spec import FaultSpec

        k8s_state["fault_spec"] = FaultSpec(
            scope="pod", namespace="cms-demo", labels={"app": "myapp"},
            fault_target="cpu", fault_action="fullload",
        ).to_dict()
        mocks, patches = _run_probes(k8s_state)
        try:
            await preplan_probe(k8s_state)
        finally:
            for p in patches:
                p.stop()
        assert mocks["_probe_operator"].await_args.kwargs["target_node"] == ""


class TestCarrierImageDiscovery:
    """_probe_carrier_images: healthy-DS images → settings.discovered."""

    @staticmethod
    def _ds(name, ns, desired, ready, images):
        return {
            "metadata": {"name": name, "namespace": ns},
            "spec": {"template": {"spec": {
                "containers": [{"image": i} for i in images],
            }}},
            "status": {
                "desiredNumberScheduled": desired,
                "numberReady": ready,
            },
        }

    @staticmethod
    def _raw_result(items):
        import json as _json

        class _R:
            exit_code = 0
            stdout = _json.dumps({"items": items})

        return _R()

    @staticmethod
    def _nodes_raw(count, cordoned=0, notready=0):
        """Node list JSON: ``count`` Ready+schedulable, plus optional
        cordoned (``spec.unschedulable``) and NotReady nodes the coverage
        floor must EXCLUDE."""
        import json as _json

        ready = {"conditions": [{"type": "Ready", "status": "True"}]}
        down = {"conditions": [{"type": "Ready", "status": "False"}]}
        nodes = [{"spec": {}, "status": ready} for _ in range(count)]
        nodes += [{"spec": {"unschedulable": True}, "status": ready}
                  for _ in range(cordoned)]
        nodes += [{"spec": {}, "status": down} for _ in range(notready)]

        class _R:
            exit_code = 0
            stdout = _json.dumps({"items": nodes})

        return _R()

    async def _run(self, items, monkeypatch, node_count=1):
        # node_count defaults to 1: these tests predate the coverage gate
        # and vary DS ``desired`` (2..48) with no consistent cluster size —
        # a floor of 1 keeps the gate inert so they stay focused on their
        # original concern. Gate-specific tests pass an explicit floor.
        raw = self._raw_result(items)
        nodes_raw = self._nodes_raw(node_count)
        monkeypatch.setattr(settings, "recovery_carrier_discovered_images", "")

        async def _fake_exec(_sub, v_args, *_a, **_k):
            resource = v_args[0] if isinstance(v_args, list) and v_args else ""
            return nodes_raw if resource == "nodes" else raw

        # Patch the real module object (``kubectl_cli``) from sys.modules —
        # the same module the in-function ``from … import`` resolves
        # against. (Renamed from ``kubectl`` in the R67 namespace fix: the
        # old submodule filename equalled the exported tool symbol, so the
        # package attribute was the TOOL, not the module.)
        import sys

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]

        with patch.object(
            kubectl_mod, "exec_kubectl_raw", new=AsyncMock(side_effect=_fake_exec),
        ):
            return await pp._probe_carrier_images("/fake/kubeconfig")

    @pytest.mark.asyncio
    async def test_healthy_ds_images_added_to_discovered(self, monkeypatch):
        items = [
            self._ds("terway-eniip", "kube-system", 41, 41,
                     ["registry.local/acs/terway:v1"]),
            # Unhealthy DS: coverage unproven — must NOT be discovered.
            self._ds("broken", "kube-system", 10, 7, ["registry.local/bad:v1"]),
            # Zero-scale DS: desired 0 — skip.
            self._ds("idle", "default", 0, 0, ["registry.local/idle:v1"]),
        ]
        status, summary, detail = await self._run(items, monkeypatch)
        assert status == "ok"
        assert settings.recovery_carrier_discovered_images == (
            "registry.local/acs/terway:v1"
        )
        assert detail["images"] == ["registry.local/acs/terway:v1"]
        assert "terway" in summary
        assert "registry.local/bad:v1" not in summary

    @pytest.mark.asyncio
    async def test_summary_lists_all_candidates_no_truncation(
        self, monkeypatch,
    ):
        """#36 retest evidence: the [:6] alphabetical cap buried terway
        (the empirically preferred carrier image, alphabetically last)
        behind "…and 3 more" — the planner's ONLY channel is the summary
        (detail stays tracker-side), so a hidden candidate forces in-loop
        re-discovery. Every candidate must be listed."""
        items = [
            self._ds(f"ds{i}", "kube-system", 3, 3, [f"reg/img{i}:v1"])
            for i in range(8)
        ]
        items.append(self._ds(
            "terway-eniip", "kube-system", 48, 48, ["reg/zz-terway:v1"],
        ))
        status, summary, detail = await self._run(items, monkeypatch)
        assert status == "ok"
        for i in range(8):
            assert f"reg/img{i}:v1" in summary
        # The alphabetically LAST candidate — exactly what the old cap hid.
        assert "reg/zz-terway:v1" in summary
        assert "…" not in summary
        assert " more" not in summary
        assert detail["images"] == sorted(
            [f"reg/img{i}:v1" for i in range(8)] + ["reg/zz-terway:v1"]
        )

    @pytest.mark.asyncio
    async def test_configured_allowlist_not_duplicated(self, monkeypatch):
        monkeypatch.setattr(
            settings, "recovery_carrier_allowed_images", "busybox:1.36",
        )
        items = [self._ds("bb", "x", 3, 3, ["busybox:1.36", "reg/x:v9"])]
        status, _, detail = await self._run(items, monkeypatch)
        assert status == "ok"
        # busybox stays out of discovered (already configured); reg/x:v9 in.
        assert settings.recovery_carrier_discovered_images == "reg/x:v9"

    @pytest.mark.asyncio
    async def test_reentry_merge_is_idempotent(self, monkeypatch):
        monkeypatch.setattr(
            settings, "recovery_carrier_discovered_images", "reg/x:v9",
        )
        items = [self._ds("x", "x", 3, 3, ["reg/x:v9"])]
        await self._run(items, monkeypatch)
        # Same set re-probed — no duplication, order stable.
        assert settings.recovery_carrier_discovered_images == "reg/x:v9"

    @pytest.mark.asyncio
    async def test_no_healthy_ds_reports_warning(self, monkeypatch):
        items = [self._ds("broken", "kube-system", 10, 7, ["reg/bad:v1"])]
        status, summary, _ = await self._run(items, monkeypatch)
        assert status == "warning"
        assert "no healthy DaemonSet images" in summary
        assert settings.recovery_carrier_discovered_images == ""

    @pytest.mark.asyncio
    async def test_kubectl_failure_degrades_to_unknown(self, monkeypatch):
        class _Bad:
            exit_code = 1
            stdout = ""

        import sys

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]

        with patch.object(
            kubectl_mod, "exec_kubectl_raw",
            new=AsyncMock(return_value=_Bad()),
        ):
            status, summary, _ = await pp._probe_carrier_images("/fake")
        assert status == "unknown"
        assert "auto-discovery failed" in summary

    @pytest.mark.asyncio
    async def test_retry_recovers_from_first_attempt_jitter(self, monkeypatch):
        """R (run5): ONE gateway timeout is jitter, not a cluster fact —
        the retry must rescue the probe from a first-attempt failure to a
        full ``ok`` with the discovery set populated."""
        class _Bad:
            exit_code = 1
            stdout = ""

        import sys

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(settings, "recovery_carrier_discovered_images", "")
        # No real 0.5s backoff sleep in tests.
        monkeypatch.setattr(pp, "_DS_FETCH_RETRY_BACKOFF_SECONDS", 0.0)

        good = self._raw_result([self._ds(
            "terway-eniip", "kube-system", 41, 41,
            ["registry.local/acs/terway:v1"],
        )])
        empty = self._raw_result([])
        nodes_raw = self._nodes_raw(1)
        ds_calls = {"n": 0}

        async def _fake(_sub, v_args, *_a, **_k):
            resource = v_args[0] if isinstance(v_args, list) and v_args else ""
            if resource == "nodes":
                return nodes_raw
            if "kruise" in resource:
                return empty  # best-effort resource: always empty here
            ds_calls["n"] += 1
            return _Bad() if ds_calls["n"] == 1 else good

        exec_mock = AsyncMock(side_effect=_fake)
        with patch.object(kubectl_mod, "exec_kubectl_raw", new=exec_mock):
            status, summary, detail = await pp._probe_carrier_images("/fake")
        assert status == "ok"
        # Each attempt queries THREE resources (ds + kruise + nodes)
        # concurrently: 2 attempts × 3 = 6 kubectl calls.
        assert exec_mock.await_count == 6
        assert settings.recovery_carrier_discovered_images == (
            "registry.local/acs/terway:v1"
        )
        assert detail["images"] == ["registry.local/acs/terway:v1"]
        assert "terway" in summary

    @pytest.mark.asyncio
    async def test_unknown_reports_retry_attempt_count(self, monkeypatch):
        """The unknown message distinguishes "retried and still failing"
        (attempt count in the note) from a one-shot failure."""
        class _Bad:
            exit_code = 1
            stdout = ""

        import sys

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(pp, "_DS_FETCH_RETRY_BACKOFF_SECONDS", 0.0)

        exec_mock = AsyncMock(return_value=_Bad())
        with patch.object(kubectl_mod, "exec_kubectl_raw", new=exec_mock):
            status, summary, _ = await pp._probe_carrier_images("/fake")
        assert status == "unknown"
        # 2 attempts × 3 resources (ds + kruise + nodes) = 6 kubectl calls,
        # but the retry NOTE still counts ATTEMPTS (2), not calls.
        assert exec_mock.await_count == 6
        assert "auto-discovery failed" in summary
        assert "2 attempt(s)" in summary

    @pytest.mark.asyncio
    async def test_first_attempt_success_skips_retry(self, monkeypatch):
        """Retry budget is spent only on failure — a healthy first attempt
        makes exactly ONE attempt's worth of kubectl calls (ds + kruise,
        concurrently), never a second attempt."""
        import sys

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(settings, "recovery_carrier_discovered_images", "")

        ds_raw = self._raw_result(
            [self._ds("x", "kube-system", 2, 2, ["reg/x:v1"])],
        )
        nodes_raw = self._nodes_raw(1)

        async def _fake(_sub, v_args, *_a, **_k):
            resource = v_args[0] if isinstance(v_args, list) and v_args else ""
            return nodes_raw if resource == "nodes" else ds_raw

        exec_mock = AsyncMock(side_effect=_fake)
        with patch.object(kubectl_mod, "exec_kubectl_raw", new=exec_mock):
            status, _, _ = await pp._probe_carrier_images("/fake")
        assert status == "ok"
        # ONE attempt = THREE concurrent queries (ds + kruise + nodes).
        assert exec_mock.await_count == 3

    async def _run_split(self, ds_items, kruise_items, monkeypatch,
                         kruise_ok=True, node_count=1):
        """Run the probe with DIFFERENT responses per resource type —
        ``ds`` (primary) vs ``daemonsets.apps.kruise.io`` (best-effort).
        ``kruise_ok=False`` simulates a cluster without the OpenKruise CRD
        (the kruise query exits non-zero). ``node_count`` is the coverage
        floor (defaults to 1 so the gate stays inert for these tests)."""
        import sys

        ds_raw = self._raw_result(ds_items)
        kruise_raw = self._raw_result(kruise_items)
        nodes_raw = self._nodes_raw(node_count)
        monkeypatch.setattr(settings, "recovery_carrier_discovered_images", "")

        class _Bad:
            exit_code = 1
            stdout = ""

        async def _fake(_sub, v_args, *_a, **_k):
            resource = v_args[0] if isinstance(v_args, list) and v_args else ""
            if resource == "nodes":
                return nodes_raw
            if "kruise" in resource:
                return _Bad() if not kruise_ok else kruise_raw
            return ds_raw

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        with patch.object(
            kubectl_mod, "exec_kubectl_raw", new=AsyncMock(side_effect=_fake),
        ):
            return await pp._probe_carrier_images("/fake/kubeconfig")

    @pytest.mark.asyncio
    async def test_kruise_ds_images_discovered(self, monkeypatch):
        """The terway CNI on ASI/sigma runs as an OpenKruise Advanced
        DaemonSet — invisible to a ``ds``-only query. Its healthy images
        must join the discovered allowlist so the assembler accepts it as a
        node-cached carrier (the openssl-capable fast path)."""
        status, summary, detail = await self._run_split(
            ds_items=[self._ds("chaosblade-tool", "default", 25, 25,
                               ["ghcr.io/chaosblade-io/chaosblade-tool:1.8.0"])],
            kruise_items=[self._ds("terway", "kube-system", 25, 25,
                                   ["registry-vpc/asi/terway:v1.4.3"])],
            monkeypatch=monkeypatch,
        )
        assert status == "ok"
        assert set(detail["images"]) == {
            "ghcr.io/chaosblade-io/chaosblade-tool:1.8.0",
            "registry-vpc/asi/terway:v1.4.3",
        }
        assert "registry-vpc/asi/terway:v1.4.3" in summary

    @pytest.mark.asyncio
    async def test_kruise_multi_container_all_images_collected(self, monkeypatch):
        """terway's openssl lives in ``containers[1]``, not ``[0]`` — the
        collector must sweep ALL containers of a healthy kruise DaemonSet."""
        status, _, detail = await self._run_split(
            ds_items=[],
            kruise_items=[self._ds("terway", "kube-system", 25, 25, [
                "registry-vpc/asi/cni-config-manager:v1.0",
                "registry-vpc/asi/terway:v1.4.3",
            ])],
            monkeypatch=monkeypatch,
        )
        assert status == "ok"
        assert set(detail["images"]) == {
            "registry-vpc/asi/cni-config-manager:v1.0",
            "registry-vpc/asi/terway:v1.4.3",
        }

    @pytest.mark.asyncio
    async def test_kruise_crd_absent_degrades_gracefully(self, monkeypatch):
        """A cluster without the OpenKruise CRD: the kruise query fails, but
        discovery still succeeds on the primary ``ds`` alone — the additive
        resource must never starve the fast path."""
        status, _, detail = await self._run_split(
            ds_items=[self._ds("chaosblade-tool", "default", 25, 25,
                               ["ghcr.io/chaosblade-io/chaosblade-tool:1.8.0"])],
            kruise_items=[],
            monkeypatch=monkeypatch,
            kruise_ok=False,  # CRD absent → non-zero exit
        )
        assert status == "ok"
        assert detail["images"] == [
            "ghcr.io/chaosblade-io/chaosblade-tool:1.8.0",
        ]

    @pytest.mark.asyncio
    async def test_unhealthy_kruise_ds_filtered(self, monkeypatch):
        """An unhealthy kruise DaemonSet (zero-scale like gpu-device-plugin
        0/0, or partial coverage 20/25) proves no every-node coverage — its
        images must NOT be discovered; only the fully-ready one survives."""
        _, _, detail = await self._run_split(
            ds_items=[],
            kruise_items=[
                self._ds("gpu-device-plugin", "kube-system", 0, 0,
                         ["registry-vpc/asi/device-plugins:v1"]),
                self._ds("partial", "kube-system", 25, 20,
                         ["registry-vpc/asi/partial:v1"]),
                self._ds("terway", "kube-system", 25, 25,
                         ["registry-vpc/asi/terway:v1.4.3"]),
            ],
            monkeypatch=monkeypatch,
        )
        assert detail["images"] == ["registry-vpc/asi/terway:v1.4.3"]

    @pytest.mark.asyncio
    async def test_restricted_ds_below_node_coverage_excluded(self, monkeypatch):
        """Coverage gate: a DaemonSet fully ready on only its OWN
        node-selector subset (``desired`` < schedulable node count) does NOT
        prove its images are cached where the freely-scheduled carrier may
        land. Only the cluster-wide DaemonSet (``desired`` >= floor) is
        admitted. Live shape (25-node / 23-schedulable cluster):
        log-collector desired=4 (resource-pool=test), kubewiz-daemon
        desired=1 (pinned to one hostname) — both fully ready, both
        subset-cached only."""
        status, _, detail = await self._run(
            [
                self._ds("log-collector", "app-system", 4, 4,
                         ["reg/acs/busybox:latest"]),
                self._ds("kubewiz-daemon", "kubewiz", 1, 1,
                         ["reg/kubewiz-oss-daemon@sha256:abc"]),
                self._ds("node-exporter", "kube-system", 25, 25,
                         ["reg/acs/node-exporter:v0.17"]),
            ],
            monkeypatch,
            node_count=23,
        )
        assert status == "ok"
        assert detail["images"] == ["reg/acs/node-exporter:v0.17"]

    @pytest.mark.asyncio
    async def test_coverage_floor_excludes_cordoned_and_notready(self, monkeypatch):
        """The floor counts ONLY nodes the carrier can land on: cordoned
        (``spec.unschedulable``) and NotReady nodes are excluded. So a
        DaemonSet covering the 3 schedulable nodes passes even though the
        raw node total is 6 — and a 2-node subset still fails."""
        import sys

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(settings, "recovery_carrier_discovered_images", "")
        nodes_raw = self._nodes_raw(3, cordoned=2, notready=1)
        ds_raw = self._raw_result([
            self._ds("cluster-wide", "kube-system", 3, 3, ["reg/wide:v1"]),
            self._ds("subset", "kube-system", 2, 2, ["reg/subset:v1"]),
        ])

        async def _fake(_sub, v_args, *_a, **_k):
            resource = v_args[0] if isinstance(v_args, list) and v_args else ""
            return nodes_raw if resource == "nodes" else ds_raw

        with patch.object(
            kubectl_mod, "exec_kubectl_raw", new=AsyncMock(side_effect=_fake),
        ):
            status, _, detail = await pp._probe_carrier_images("/fake")
        assert status == "ok"
        # floor=3: cluster-wide desired=3 >= 3 admitted; subset 2 < 3 dropped.
        assert detail["images"] == ["reg/wide:v1"]

    @pytest.mark.asyncio
    async def test_node_count_fetch_failure_fails_closed(self, monkeypatch):
        """If the coverage floor cannot be determined (nodes query fails),
        discovery fails CLOSED to unknown rather than silently dropping the
        gate and admitting unproven images."""
        import sys

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(pp, "_DS_FETCH_RETRY_BACKOFF_SECONDS", 0.0)
        ds_raw = self._raw_result([
            self._ds("terway", "kube-system", 25, 25, ["reg/terway:v1"]),
        ])

        class _Bad:
            exit_code = 1
            stdout = ""

        async def _fake(_sub, v_args, *_a, **_k):
            resource = v_args[0] if isinstance(v_args, list) and v_args else ""
            return _Bad() if resource == "nodes" else ds_raw

        with patch.object(
            kubectl_mod, "exec_kubectl_raw", new=AsyncMock(side_effect=_fake),
        ):
            status, summary, _ = await pp._probe_carrier_images("/fake")
        assert status == "unknown"
        assert "auto-discovery failed" in summary
        assert settings.recovery_carrier_discovered_images == ""


# Direct reference to the real refresh entry point, taken at import time:
# the global conftest neutralises the MODULE attribute so unrelated
# screener tests never reach a real cluster — these tests invoke the
# function object itself instead of the (patched) module attribute.
_refresh_real = pp.refresh_carrier_image_discovery


class TestCarrierImageLazyRefresh:
    """L: execute-time second chance for a starved discovery set."""

    @staticmethod
    def _ds(name, ns, desired, ready, images):
        return {
            "metadata": {"name": name, "namespace": ns},
            "spec": {"template": {"spec": {
                "containers": [{"image": i} for i in images],
            }}},
            "status": {
                "desiredNumberScheduled": desired,
                "numberReady": ready,
            },
        }

    @staticmethod
    def _ok_result(items):
        import json as _json

        class _R:
            exit_code = 0
            stdout = _json.dumps({"items": items})

        return _R()

    async def _refresh(self, raw_results, monkeypatch, node_count=1):
        import sys

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(settings, "recovery_carrier_discovered_images", "")
        monkeypatch.setattr(pp, "_DS_FETCH_RETRY_BACKOFF_SECONDS", 0.0)
        # raw_results[0] is the primary ``ds`` response; the kruise Advanced
        # DaemonSet query is best-effort additive and returns empty here so
        # these tests stay focused on the refresh latch/merge semantics.
        ds_raw = raw_results[0]
        kruise_raw = self._ok_result([])
        nodes_raw = self._ok_result([
            {"spec": {},
             "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
            for _ in range(node_count)
        ])

        async def _fake(_sub, v_args, *_a, **_k):
            resource = v_args[0] if isinstance(v_args, list) and v_args else ""
            if resource == "nodes":
                return nodes_raw
            return kruise_raw if "kruise" in resource else ds_raw

        exec_mock = AsyncMock(side_effect=_fake)
        with patch.object(kubectl_mod, "exec_kubectl_raw", new=exec_mock):
            ok = await _refresh_real("/fake")
        return ok, exec_mock

    @pytest.mark.asyncio
    async def test_starved_set_refilled_by_refresh(self, monkeypatch):
        """run5 shape: the preplan probe starved the set (one gateway
        jitter), the guard-time refresh refills it from a healthy DS."""
        ok, _ = await self._refresh([self._ok_result([self._ds(
            "terway-eniip", "kube-system", 41, 41,
            ["registry.local/acs/terway:v1"],
        )])], monkeypatch)
        assert ok is True
        assert settings.recovery_carrier_discovered_images == (
            "registry.local/acs/terway:v1"
        )

    @pytest.mark.asyncio
    async def test_refresh_is_once_per_process(self, monkeypatch):
        """A second call must not re-fetch: a genuinely empty cluster must
        not pay a probe on every carrier call (the latch is the bound —
        it trips whether the first refresh succeeded OR found nothing)."""
        ok, exec_mock = await self._refresh(
            [self._ok_result([])], monkeypatch,
        )
        assert ok is False
        # Second call: latch short-circuits BEFORE any fetch.
        ok2 = await _refresh_real("/fake")
        assert ok2 is False
        # ONE attempt = THREE concurrent queries (ds + kruise + nodes); the
        # latch trips on the first attempt so no second attempt runs.
        assert exec_mock.await_count == 3

    @pytest.mark.asyncio
    async def test_refresh_no_healthy_ds_returns_false(self, monkeypatch):
        """Fetch ok but zero healthy candidates → nothing to merge."""
        ok, _ = await self._refresh([self._ok_result([self._ds(
            "broken", "kube-system", 10, 7, ["reg/bad:v1"],
        )])], monkeypatch)
        assert ok is False
        assert settings.recovery_carrier_discovered_images == ""
