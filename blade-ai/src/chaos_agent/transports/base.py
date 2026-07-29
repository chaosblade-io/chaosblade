"""Transport Channel abstraction — Protocol and TransportTarget.

Design principle: per-injection, not per-session.
TransportTarget's lifetime = one injection or one recovery, not the
entire session.  A single injection/recovery binds exactly one
connection mode (kubeconfig / kubewiz_k8s / kubewiz_host / ssh —
pick one).  Different operations within the same session may use
different connection modes; ``resolve_transport_target(state)``
re-derives from the current state + settings each time, automatically
switching channels when configuration changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from chaos_agent.config.settings import settings
from chaos_agent.models.command_result import CommandResult

# Capability profile vocabulary. Defined at this level (not in ``registry``)
# because a channel must be able to DECLARE its own profile, and ``registry``
# imports ``channels`` — the other direction would be circular. ``registry``
# re-exports these, which is where every consumer imports them from.
PROFILE_K8S = "k8s"
PROFILE_HOST = "host"
PROFILE_UNKNOWN = "unknown"


@dataclass
class TransportTarget:
    """Transport target — superset of all connection parameters.

    ``scope`` determines the top-level category (``"k8s"`` or
    ``"host"``).  Within each scope, the presence of specific fields
    determines which channel is selected by
    :meth:`TransportRegistry.resolve`.
    """

    scope: str = "k8s"  # "k8s" | "host"
    # Explicit channel override — when non-empty, TransportRegistry.resolve
    # selects this exact channel by name and ignores field-based inference.
    # Valid values: "kubeconfig" | "kubewiz_k8s" | "kubewiz_host" | "ssh".
    channel_override: str = ""
    # K8s (kubeconfig mode)
    kubeconfig: str = ""
    kube_context: str = ""
    # kubewiz-k8s
    kubewiz_cluster_uuid: str = ""
    kubewiz_profile: str = ""
    # kubewiz-host
    host_name: str = ""  # wiz --name parameter (host IP/hostname)
    # SSH
    ssh_host: str = ""
    ssh_user: str = ""
    ssh_key_path: str = ""
    ssh_port: int = 22

    @classmethod
    def from_state(cls, state: dict) -> TransportTarget:
        """Construct from AgentState or settings (migration bridge).

        Priority: state fields > settings defaults.

        .. note::
            When called with ``from_state({})`` (empty dict), ALL fields
            fall back to ``settings`` — this is effectively
            ``from_settings()``.  Tools that call ``from_state({})``
            implicitly depend on ``sync_kubewiz_runtime(state)`` being
            called earlier in the pipeline to sync per-session values
            from state into settings.

            Once all tools accept a ``TransportTarget`` parameter (passed
            from the graph node via ``resolve_transport_target(state)``),
            the ``from_state({})`` bridge will be removed.
        """
        t = cls()

        # --- Explicit channel override (highest precedence) ---
        # When set, TransportRegistry.resolve ignores field inference and
        # selects this exact channel.  Priority: state > settings.
        t.channel_override = (
            state.get("kube_connection_mode", "")
            or getattr(settings, "kube_connection_mode", "")
        )

        # --- Populate the full connection-field superset (state > settings) ---
        # Populate every field unconditionally so that an explicit
        # ``channel_override`` always finds the fields its channel needs,
        # regardless of which fields field-based inference would have keyed
        # on.  ``resolve()`` only reads the subset relevant to the selected
        # channel, so populating unused fields is harmless.
        t.kubeconfig = state.get("kubeconfig", "") or settings.kubeconfig_path
        t.kube_context = state.get("kube_context", "") or settings.kube_context
        t.kubewiz_cluster_uuid = (
            state.get("kubewiz_cluster_uuid", "") or settings.kubewiz_cluster_uuid
        )
        t.kubewiz_profile = state.get("kubewiz_profile", "") or settings.kubewiz_profile
        # Use getattr for forward-compat: these settings fields may not exist
        # yet during incremental migration.
        t.host_name = state.get("host_name", "") or getattr(settings, "host_name", "")
        t.ssh_host = state.get("ssh_host", "") or getattr(settings, "ssh_host", "")
        t.ssh_user = state.get("ssh_user", "") or getattr(settings, "ssh_user", "")
        t.ssh_key_path = state.get("ssh_key_path", "") or getattr(settings, "ssh_key_path", "")
        ssh_port = state.get("ssh_port") or getattr(settings, "ssh_port", 22)
        t.ssh_port = ssh_port if ssh_port else 22

        # --- Scope inference (only consulted when channel_override is empty) ---
        # host_name or ssh_host presence → host scope; otherwise k8s.
        t.scope = "host" if (t.host_name or t.ssh_host) else "k8s"

        return t


@runtime_checkable
class TransportChannel(Protocol):
    """Transport channel abstraction.

    Each channel knows how to:
    - ``profile``: which capability profile it provides (k8s vs host)
    - ``claims``: whether it should be selected for a given target
    - ``wrap_command``: wrap a raw semantic command into its transport format
    - ``adapt_result``: parse the transport layer's output protocol
    - ``preflight``: validate that required target fields are present
    - ``display_command``: return a human-readable command string

    ``profile`` and ``claims`` exist so adding a channel is a pure
    registration: before, a new channel also had to be listed in a separate
    ``_CHANNEL_PROFILE`` dict AND wired into ``TransportRegistry.resolve``'s
    hardcoded if/else. Forgetting the former silently produced
    ``PROFILE_UNKNOWN`` (safe, but the error only said "unknown channel").
    """

    @property
    def name(self) -> str:
        """Channel identifier (e.g. ``"kubeconfig"``, ``"kubewiz_k8s"``)."""
        ...

    @property
    def profile(self) -> str:
        """Capability profile this channel provides (``PROFILE_K8S`` / ``PROFILE_HOST``).

        Declared here rather than in a parallel table, so the capability gate
        can never disagree with the channel about what it is.
        """
        ...

    def claims(self, target: TransportTarget) -> bool:
        """Whether this channel should handle *target* by field inference.

        Consulted only when ``target.channel_override`` is empty. Channels are
        tried in ``priority`` order (descending) so a more specific channel can
        pre-empt a catch-all default.
        """
        ...

    @property
    def priority(self) -> int:
        """Selection precedence among channels whose ``claims`` match.

        Higher wins. A catch-all fallback (e.g. ``kubeconfig``) declares a low
        value so a specific channel is preferred.
        """
        ...

    def wrap_command(self, cmd: list[str], target: TransportTarget, timeout: float | None = None) -> list[str]:
        """Wrap a raw semantic command into transport format.

        ``timeout`` is the caller's per-command timeout (seconds); gateway
        channels use it to size ``wiz --wait-timeout`` so long commands are
        not cut at wiz's 10s default.
        """
        ...

    def adapt_result(self, result: CommandResult, target: TransportTarget) -> CommandResult:
        """Parse transport layer output protocol and return clean result."""
        ...

    def preflight(self, target: TransportTarget) -> list[str]:
        """Validate target fields.  Returns error list (empty = pass)."""
        ...

    def display_command(self, cmd: list[str], target: TransportTarget | None = None) -> str:
        """Return a human-readable command string for display.

        The transport wrapper is stripped for readability, but the execution
        LOCATION must be appended: the wrapper is the only place that says which
        machine will answer, and dropping it entirely hid a cross-machine read
        in task-46317228. ``target`` is optional so older callers keep working.
        """
        ...
