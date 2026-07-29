"""Transport channel registry.

New connection modes are added by:
1. Implementing the ``TransportChannel`` protocol
2. Calling ``TransportRegistry.register(MyChannel())``

No existing code needs to change.
"""

from __future__ import annotations

import logging

from .base import (
    PROFILE_HOST as _PROFILE_HOST,
    PROFILE_K8S as _PROFILE_K8S,
    PROFILE_UNKNOWN as _PROFILE_UNKNOWN,
    TransportChannel,
    TransportTarget,
)
from .channels import (
    KubeconfigChannel,
    KubewizHostChannel,
    KubewizK8sChannel,
    SSHChannel,
)

logger = logging.getLogger(__name__)

# Channel names already reported as lacking ``claims``. ``resolve`` runs on
# every command, so warning each time would flood the log for one static
# misconfiguration.
_CLAIMS_WARNED: set[str] = set()


def _channel_claims(channel: TransportChannel, target: TransportTarget) -> bool:
    """Ask *channel* whether it claims *target*, tolerating older channels.

    ``TransportChannel`` is a Protocol, not an ABC, so a channel written before
    ``claims`` existed (or a third-party one) may not implement it. Treat that
    as "claims nothing": such a channel is still reachable through an explicit
    ``channel_override``, but is never auto-selected. Fail closed rather than
    raising ``AttributeError`` from the hot path — and the registry-completeness
    test flags the missing declaration at test time.

    A ``claims`` that RAISES gets the same treatment. ``resolve`` polls every
    registered channel on every command, so one broken channel would otherwise
    break transport for all of them — including channels that have nothing to do
    with it.
    """
    claims = getattr(channel, "claims", None)
    name = str(getattr(channel, "name", channel))
    if not callable(claims):
        if name not in _CLAIMS_WARNED:
            _CLAIMS_WARNED.add(name)
            logger.warning(
                "channel %r does not implement claims(); it can only be "
                "selected via an explicit channel_override",
                name,
            )
        return False
    try:
        return bool(claims(target))
    except Exception:
        if name not in _CLAIMS_WARNED:
            _CLAIMS_WARNED.add(name)
            logger.exception(
                "channel %r raised from claims(); treating it as not claiming "
                "this target so one broken channel cannot break resolution "
                "for every other one",
                name,
            )
        return False

# Channels that route through the KubeWiz gateway (``wiz task exec``).
KUBEWIZ_CHANNELS = ("kubewiz_k8s", "kubewiz_host")


class TransportRegistry:
    """Class-level registry of transport channels."""

    _channels: dict[str, TransportChannel] = {}

    @classmethod
    def _ensure_default(cls) -> None:
        if not cls._channels:
            cls.register(KubeconfigChannel())
            cls.register(KubewizK8sChannel())
            cls.register(KubewizHostChannel())
            cls.register(SSHChannel())

    @classmethod
    def register(cls, channel: TransportChannel) -> None:
        """Register a channel.  Overwrites if name already exists."""
        cls._channels[channel.name] = channel

    @classmethod
    def get(cls, name: str) -> TransportChannel:
        """Retrieve a registered channel by name."""
        cls._ensure_default()
        return cls._channels[name]

    @classmethod
    def resolve(cls, target: TransportTarget) -> TransportChannel:
        """Auto-select a channel based on the transport target.

        Selection logic:
        - ``channel_override`` non-empty (highest precedence): select that
          exact channel by name.  Unknown value → ``ValueError``.
        - otherwise: ask every registered channel whether it ``claims`` the
          target, and take the highest ``priority`` claimant.

        The claim-based form replaced a hardcoded if/else so registering a new
        channel needs no edit here. The declared priorities reproduce the old
        ordering exactly: ``kubewiz_host`` (20) before ``ssh`` (10) for host
        scope, ``kubewiz_k8s`` (10) before the ``kubeconfig`` catch-all (0) for
        k8s scope.
        """
        cls._ensure_default()
        # Explicit override wins over field-based inference.
        if target.channel_override:
            channel = cls._channels.get(target.channel_override)
            if channel is None:
                raise ValueError(
                    f"unknown channel_override: {target.channel_override!r} "
                    f"(valid: {sorted(cls._channels)})"
                )
            return channel

        claimants = [c for c in cls._channels.values() if _channel_claims(c, target)]
        if not claimants:
            # Preserve the previous, more specific diagnostics: a host scope
            # with neither addressing field is the common misconfiguration.
            if target.scope == PROFILE_HOST:
                raise ValueError("host scope requires host_name or ssh_host")
            raise ValueError(f"unsupported scope: {target.scope}")
        # Sort by priority desc, then name for a deterministic tie-break.
        # ``priority`` is read defensively for the same reason as ``claims``.
        claimants.sort(key=lambda c: (-int(getattr(c, "priority", 0) or 0), c.name))
        return claimants[0]


def resolve_channel_name(state: dict | None = None) -> str:
    """Resolve the transport channel name for *state* (settings defaults when
    ``None``/empty).

    Centralizes the ``from_state → resolve`` boilerplate. If resolution raises
    ``ValueError`` (invalid channel override or under-specified scope), return
    the explicit unknown sentinel. Callers that construct an execution or LLM
    capability context must fail closed rather than silently targeting the
    default kubeconfig cluster.
    """
    try:
        target = TransportTarget.from_state(state or {})
        return TransportRegistry.resolve(target).name
    except ValueError as exc:
        logger.warning("Transport resolve failed, marking channel unknown: %s", exc)
        return PROFILE_UNKNOWN


def is_kubewiz_channel(state: dict | None = None) -> bool:
    """True when the resolved channel routes through the KubeWiz gateway."""
    return resolve_channel_name(state) in KUBEWIZ_CHANNELS


# Capability profiles: what the executor *can run* against a target
# (kubectl vs host shell), independent of the fault type. These two string
# constants are the AUTHORITATIVE vocabulary for the "profile" axis shared by
# baseline capture, feasibility probing and side-effect observation — every
# consumer imports these instead of hardcoding ``"k8s"`` / ``"host"`` so the
# values can never drift or typo.
#
# Defined in ``base`` (which both ``channels`` and this module import) so a
# channel can DECLARE its own profile without a circular import; re-exported
# here because every existing consumer imports them from ``transports``.
PROFILE_K8S = _PROFILE_K8S
PROFILE_HOST = _PROFILE_HOST
PROFILE_UNKNOWN = _PROFILE_UNKNOWN


def profile_of(channel: str) -> str:
    """Map a channel name to its capability profile.

    Derived from the channel's OWN ``profile`` declaration rather than a
    parallel table, so a newly registered channel cannot be missing here.
    Unknown channels are explicit ``unknown`` rather than silently treated as
    Kubernetes.  Callers that need to execute commands must fail closed until
    an environment profile is registered for that channel.
    """
    TransportRegistry._ensure_default()
    channel_obj = TransportRegistry._channels.get(channel)
    if channel_obj is None:
        return PROFILE_UNKNOWN
    return getattr(channel_obj, "profile", PROFILE_UNKNOWN) or PROFILE_UNKNOWN


def channel_profiles() -> dict[str, str]:
    """Return ``{channel name: profile}`` for every registered channel."""
    TransportRegistry._ensure_default()
    return {
        name: getattr(channel, "profile", PROFILE_UNKNOWN) or PROFILE_UNKNOWN
        for name, channel in TransportRegistry._channels.items()
    }


def host_scope_channels() -> tuple[str, ...]:
    """Channel names whose target is a bare host (no K8s cluster).

    Derived from each channel's own ``profile`` declaration, so registering a
    host channel needs no second edit. A FUNCTION rather than a module constant
    on purpose: a constant would freeze whatever the registry held at import
    time and would also force channel registration during module import.
    """
    return tuple(
        name for name, prof in channel_profiles().items() if prof == PROFILE_HOST
    )


def is_host_scope_channel(state: dict | None = None) -> bool:
    """True when the resolved channel targets a host (ssh / kubewiz_host)."""
    return resolve_channel_name(state) in host_scope_channels()
