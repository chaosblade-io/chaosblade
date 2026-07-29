"""Carrier-agnostic target identity protocol.

The target-drift guard historically assumed every target is a Kubernetes
resource (``namespace`` + ``labels`` + ``names``). As new fault carriers
arrive (host via SSH, cloud APIs, ...), the question "what resource did the
user approve?" needs a carrier-agnostic shape.

A :class:`TargetProtocol` answers the three questions the guard needs,
regardless of carrier:

  - :meth:`identity` — a hashable tuple uniquely naming the target in scope
  - :meth:`matches`  — whether another target refers to the same thing
  - :meth:`describe` — a short human string for audit logs / confirm cards

:class:`K8sTarget` keeps the namespace / labels / names semantics;
:class:`HostTarget` identifies by host name. Both are plain frozen value
objects. The guard's existing K8s comparison logic stays intact — it only
consults these for the carrier-agnostic identity / describe seam, so adding
a new carrier does not require rewriting the comparison internals.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@runtime_checkable
class TargetProtocol(Protocol):
    """Minimal contract every carrier's target record must satisfy."""

    scope: str

    def identity(self) -> tuple:
        """Hashable identity, comparable only within the same carrier."""
        ...

    def matches(self, other: "TargetProtocol") -> bool:
        """True when ``other`` refers to the same concrete target."""
        ...

    def describe(self) -> str:
        """Short human-readable label for audit logs / confirm cards."""
        ...


@dataclass(frozen=True)
class K8sTarget:
    """Kubernetes resource identity (namespace / names / labels)."""

    scope: str
    namespace: str = ""
    names: tuple[str, ...] = ()
    labels: dict[str, str] = field(default_factory=dict)

    def identity(self) -> tuple:
        return (
            "k8s",
            self.scope,
            self.namespace,
            tuple(sorted(self.names)),
            tuple(sorted(self.labels.items())),
        )

    def matches(self, other: "TargetProtocol") -> bool:
        return isinstance(other, K8sTarget) and self.identity() == other.identity()

    def describe(self) -> str:
        selector = (
            ",".join(self.names)
            or ",".join(f"{k}={v}" for k, v in sorted(self.labels.items()))
            or "<namespace-wide>"
        )
        ns = f" -n {self.namespace}" if self.namespace else ""
        return f"{self.scope}/{selector}{ns}"


@dataclass(frozen=True)
class HostTarget:
    """Bare-metal / VM host identity (identified by host name)."""

    scope: str = "host"
    host_name: str = ""

    def identity(self) -> tuple:
        return ("host", self.scope, self.host_name)

    def matches(self, other: "TargetProtocol") -> bool:
        return isinstance(other, HostTarget) and self.identity() == other.identity()

    def describe(self) -> str:
        return f"host/{self.host_name or '<unspecified>'}"


__all__ = ["TargetProtocol", "K8sTarget", "HostTarget"]
