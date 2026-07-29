"""Transport Channel abstraction layer.

Public API:
    TransportTarget   — per-injection connection parameter container
    TransportChannel  — Protocol for transport channel implementations
    TransportRegistry — Channel registry with auto-resolution
    execute_via_transport  — Unified execution entry (Guard + Channel + Audit)
    display_via_transport  — Human-readable command display
"""

from .base import TransportChannel, TransportTarget
from .channels import strip_execution_location
from .executor import display_via_transport, execute_via_transport
from .registry import (
    KUBEWIZ_CHANNELS,
    PROFILE_HOST,
    PROFILE_K8S,
    PROFILE_UNKNOWN,
    TransportRegistry,
    channel_profiles,
    host_scope_channels,
    is_host_scope_channel,
    is_kubewiz_channel,
    profile_of,
    resolve_channel_name,
)

__all__ = [
    "TransportChannel",
    "TransportTarget",
    "TransportRegistry",
    "execute_via_transport",
    "display_via_transport",
    "resolve_channel_name",
    "is_kubewiz_channel",
    "is_host_scope_channel",
    "profile_of",
    "PROFILE_K8S",
    "PROFILE_HOST",
    "PROFILE_UNKNOWN",
    "KUBEWIZ_CHANNELS",
    "channel_profiles",
    "host_scope_channels",
    "strip_execution_location",
]
