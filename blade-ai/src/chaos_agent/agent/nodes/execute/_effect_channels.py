"""Per-scope post-injection sample channels.

An effect check answers "is the fault actually present?" by running a read-only
diagnostic and inspecting its output. *Where* and *how* that diagnostic runs
differs by scope — a bare host runs it directly over the transport, a node
fault runs it in the ChaosBlade tool pod on the target node, a pod fault runs
it inside the target pod (with a tool-pod fallback). *What* is run and how the
output is judged is fault-specific (fill markers, diskstats deltas).

This module owns the first axis: an ``EffectSampleChannel`` abstracts "run a
shell command for this scope and hand back stdout", and ``resolve_effect_channel``
is the registry that picks + wires the right channel. The disk fill / burn
checks (``_effect_checks``) consume ``channel.run(...)`` uniformly, so adding a
new scope's sampling mechanism is a new channel here, not another ``if scope ==``
branch across every check.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Protocol, Sequence

from chaos_agent.agent.nodes.execute._effect_probes import Probe, resolve_probe_command
from chaos_agent.config.settings import settings
from chaos_agent.transports import (
    PROFILE_K8S,
    TransportTarget,
    execute_via_transport,
)

logger = logging.getLogger(__name__)


class EffectSampleChannel(Protocol):
    """Runs a read-only diagnostic for one scope and returns its stdout.

    ``scope`` is the logical fault scope this channel samples for (``"host"`` /
    ``"node"`` / ``"pod"``); it drives semantic-probe translation in
    :meth:`sample`. ``pod_name`` / ``node_name`` expose where the sample was
    taken (empty when not applicable, e.g. the host channel) so checks can
    annotate their result.
    """

    scope: str
    pod_name: str
    node_name: str

    async def run(self, command: str) -> str: ...

    async def sample(self, probe: Probe) -> str: ...


class _SampleChannelBase:
    """Shared ``sample``: translate a semantic probe for this channel's scope,
    then delegate delivery to the concrete ``run``. Subclasses set ``scope``
    and implement ``run`` (the *where / how* of sampling); the probe→command
    translation (*what*) is owned uniformly here so no channel hardcodes a
    diagnostic string."""

    scope: str

    async def run(self, command: str) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    async def sample(self, probe: Probe) -> str:
        return await self.run(resolve_probe_command(probe, self.scope))


class HostSampleChannel(_SampleChannelBase):
    """Sample directly on the target host via the transport layer."""

    def __init__(self, state: dict | None, task_id: str) -> None:
        self._state = state
        self._task_id = task_id
        self.scope = "host"
        self.pod_name = ""
        self.node_name = ""

    async def run(self, command: str) -> str:
        from chaos_agent.agent.nodes.execute._host_verify import _run_host_diagnostic

        res = await _run_host_diagnostic(command, self._state, self._task_id)
        return getattr(res, "stdout", "") or ""


class KubectlExecChannel(_SampleChannelBase):
    """Sample by ``kubectl exec`` into a resolved pod (tool pod or target pod)."""

    def __init__(
        self,
        pod_name: str,
        pod_namespace: str,
        node_name: str,
        kubeconfig: str,
        task_id: str,
        scope: str = "pod",
    ) -> None:
        self.scope = scope
        self.pod_name = pod_name
        self.pod_namespace = pod_namespace
        self.node_name = node_name
        self._kubeconfig = kubeconfig
        self._task_id = task_id

    async def run(self, command: str) -> str:
        from chaos_agent.tools.kubectl import build_kubectl_cmd

        cmd = build_kubectl_cmd(
            "exec",
            f"{self.pod_name} -n {self.pod_namespace} -- {command}",
            kubeconfig=self._kubeconfig,
        )
        try:
            res = await execute_via_transport(
                cmd, TransportTarget.from_state({}),
                timeout=settings.timeout_kubectl, task_id=self._task_id,
                expect_profile=PROFILE_K8S,
            )
        except Exception as e:  # noqa: BLE001 — sampling is best-effort
            logger.warning(
                "effect-channel: kubectl exec failed on %s: %s", self.pod_name, e,
            )
            return ""
        return getattr(res, "stdout", "") or ""


@dataclass(frozen=True)
class _ChannelRequest:
    """Everything a channel factory needs to build (or decline) a channel."""

    scope: str
    names: str
    namespace: str
    kubeconfig: str
    task_id: str
    state: dict | None
    probe: Probe | None


EffectChannelFactory = Callable[[_ChannelRequest], Awaitable[Optional[EffectSampleChannel]]]

# scope -> factory. The dispatch that used to be an ``if scope == ...`` ladder
# is now a registry: adding a new scope's sampling mechanism is a
# ``register_effect_channel(scope, factory)`` call, not another branch here.
_EFFECT_CHANNEL_FACTORIES: dict[str, EffectChannelFactory] = {}


def register_effect_channel(scope: str, factory: EffectChannelFactory) -> None:
    """Register (or replace) the sample-channel factory for ``scope``."""
    _EFFECT_CHANNEL_FACTORIES[scope] = factory


async def _host_channel_factory(req: _ChannelRequest) -> Optional[EffectSampleChannel]:
    return HostSampleChannel(req.state, req.task_id)


async def _node_channel_factory(req: _ChannelRequest) -> Optional[EffectSampleChannel]:
    node_name = (req.names or "").strip()
    if not node_name:
        return None
    from chaos_agent.agent.nodes.execute._injection_detection import (
        discover_tool_pod_on_node,
    )
    pod = await discover_tool_pod_on_node(node_name, req.kubeconfig, req.task_id)
    if not pod:
        logger.warning(
            "effect-channel: no tool pod found on target node %s", node_name,
        )
        return None
    pod_name, pod_ns = pod
    logger.info(
        "effect-channel: using tool pod %s (ns=%s) on %s",
        pod_name, pod_ns, node_name,
    )
    return KubectlExecChannel(
        pod_name, pod_ns, node_name, req.kubeconfig, req.task_id, scope="node"
    )


async def _pod_channel_factory(req: _ChannelRequest) -> Optional[EffectSampleChannel]:
    from chaos_agent.tools.kubectl import build_kubectl_cmd
    from chaos_agent.agent.nodes.execute._injection_detection import (
        discover_tool_pod_on_node,
    )
    pod_name = (req.names or "").split(",")[0].strip()
    if not pod_name:
        return None
    pod_ns = req.namespace or "default"

    # Primary: sample directly from the target pod if it supports the probe
    # (e.g. minimal container images may lack the command). The semantic
    # probe is translated to the pod-scope command for the support check.
    if req.probe is not None:
        try:
            probe_cmd = build_kubectl_cmd(
                "exec",
                f"{pod_name} -n {pod_ns} -- {resolve_probe_command(req.probe, 'pod')}",
                kubeconfig=req.kubeconfig,
            )
            probe_res = await execute_via_transport(
                probe_cmd, TransportTarget.from_state({}),
                timeout=settings.timeout_kubectl, task_id=req.task_id,
                expect_profile=PROFILE_K8S,
            )
            if getattr(probe_res, "exit_code", 1) == 0 and getattr(probe_res, "stdout", ""):
                logger.info(
                    "effect-channel: target pod %s supports probe, sampling directly",
                    pod_name,
                )
                return KubectlExecChannel(
                    pod_name, pod_ns, "", req.kubeconfig, req.task_id, scope="pod"
                )
        except Exception:  # noqa: BLE001 — fall through to tool-pod fallback
            pass

    # Fallback: discover the pod's node, then a tool pod on that node.
    logger.info(
        "effect-channel: target pod %s not directly sampleable, "
        "falling back to tool pod", pod_name,
    )
    node_name = ""
    node_cmd = build_kubectl_cmd(
        "get",
        ["pod", pod_name, "-n", pod_ns, "-o", "jsonpath={.spec.nodeName}"],
        kubeconfig=req.kubeconfig,
    )
    try:
        nr = await execute_via_transport(
            node_cmd, TransportTarget.from_state({}),
            timeout=settings.timeout_kubectl, task_id=req.task_id,
            expect_profile=PROFILE_K8S,
        )
        if getattr(nr, "exit_code", 1) == 0 and getattr(nr, "stdout", ""):
            node_name = nr.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    if not node_name:
        logger.warning(
            "effect-channel: cannot determine node for pod %s, skipping", pod_name,
        )
        return None
    pod = await discover_tool_pod_on_node(node_name, req.kubeconfig, req.task_id)
    if not pod:
        logger.warning(
            "effect-channel: no tool pod found on node %s for pod %s",
            node_name, pod_name,
        )
        return None
    tp, tpns = pod
    logger.info(
        "effect-channel: using tool pod %s (ns=%s) on %s (fallback for pod %s)",
        tp, tpns, node_name, pod_name,
    )
    return KubectlExecChannel(
        tp, tpns, node_name, req.kubeconfig, req.task_id, scope="pod"
    )


register_effect_channel("host", _host_channel_factory)
register_effect_channel("node", _node_channel_factory)
register_effect_channel("pod", _pod_channel_factory)


async def resolve_effect_channel(
    scope: str,
    *,
    names: str,
    namespace: str,
    kubeconfig: str,
    task_id: str,
    state: dict | None,
    probe: Probe | None = None,
    allowed_scopes: Optional[Sequence[str]] = None,
) -> Optional[EffectSampleChannel]:
    """Resolve the sampling channel for ``scope`` (``None`` when unavailable).

    - ``host`` → :class:`HostSampleChannel` (transport, no cluster pod).
    - ``node`` → tool pod on the target node (``names`` = node name).
    - ``pod``  → the target pod directly when it supports ``probe`` (the probe
      is translated to the pod-scope command and run as a support check),
      else a tool pod on the target pod's node.

    Dispatch is registry-driven (:func:`register_effect_channel`); an unknown
    scope with no registered factory returns ``None``.

    ``allowed_scopes`` restricts which scopes a given check supports (disk-fill
    only samples host/node; disk-burn adds pod). Returns ``None`` for scopes
    outside that set, matching the checks' prior early-return behaviour.
    """
    if allowed_scopes is not None and scope not in allowed_scopes:
        return None

    factory = _EFFECT_CHANNEL_FACTORIES.get(scope)
    if factory is None:
        return None

    req = _ChannelRequest(
        scope=scope,
        names=names,
        namespace=namespace,
        kubeconfig=kubeconfig,
        task_id=task_id,
        state=state,
        probe=probe,
    )
    try:
        return await factory(req)
    except Exception:  # noqa: BLE001 — channel resolution is best-effort
        # Keep the full traceback so a programming error inside a factory stays
        # diagnosable, while the node still degrades gracefully (sampling is
        # skipped, the check simply returns no channel).
        logger.warning(
            "effect-channel: factory for scope=%s failed to resolve", scope,
            exc_info=True,
        )
        return None


__all__ = [
    "EffectSampleChannel",
    "HostSampleChannel",
    "KubectlExecChannel",
    "register_effect_channel",
    "resolve_effect_channel",
]
