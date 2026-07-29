"""FaultProvider registry.

New fault execution backends are added by:
1. Implementing the :class:`~chaos_agent.agent.providers.base.FaultProvider`
   protocol.
2. Calling ``FaultProviderRegistry.register(MyProvider())``.
3. Declaring the matching :class:`FaultFamily` (so its ``carrier_types`` list
   includes the provider's ``carrier``) in ``fault_registry.py``.

No existing chokepoint (factory tool union, injection detection, Layer 1,
recovery, intent completeness) needs to change — each resolves the active
provider through this registry instead of a hardcoded ``if injection_method``.

Two resolution modes
---------------------
- ``resolve_by_method`` — POST-injection. The concrete ``injection_method`` is
  known (detected from message history), so the exact backend is resolvable for
  Layer-1 verification and recovery.
- ``resolve_by_scope`` — PRE-injection. Only the intent scope is known; the
  registry bridges scope → ``FaultFamily.carrier_types`` → the candidate
  providers (ordered by precedence) so intent completeness / prompt fragments
  can consult the likely backends. ``resolve_primary_by_scope`` returns the
  single most-likely one.
- ``applicable`` — PRE-injection, channel-based. Which backends can operate
  against a "k8s"/"host" profile (for prompt fragments / required-params union).
- ``all_providers`` — build-time. The factory unions every provider's tools per
  phase (tools are bound once at graph compile, not per request).

Mirrors ``TransportRegistry`` (class-level registry, ``register`` overwrites).
"""

from __future__ import annotations

import logging
from typing import Optional

from chaos_agent.agent.providers.base import FaultProvider

logger = logging.getLogger(__name__)


class FaultProviderRegistry:
    """Class-level registry of fault execution backends, keyed by ``carrier``."""

    _providers: dict[str, FaultProvider] = {}
    # injection_method → provider, rebuilt on every register for O(1) resolve.
    _method_index: dict[str, FaultProvider] = {}

    @classmethod
    def register(cls, provider: FaultProvider) -> None:
        """Register (or replace) a provider by ``carrier``.

        Rebuilds the injection-method index. A method claimed by two providers
        is a programming error (the runtime dispatch would be ambiguous) — we
        log a warning and let the last registration win, matching
        ``TransportRegistry``'s overwrite-on-duplicate contract.
        """
        cls._providers[provider.carrier] = provider
        cls._reindex()

    @classmethod
    def _reindex(cls) -> None:
        index: dict[str, FaultProvider] = {}
        for provider in cls._providers.values():
            for method in provider.injection_methods:
                if method in index and index[method] is not provider:
                    logger.warning(
                        "injection_method %r claimed by both %r and %r; "
                        "last registration wins",
                        method, index[method].carrier, provider.carrier,
                    )
                index[method] = provider
        cls._method_index = index

    @classmethod
    def all_providers(cls) -> tuple[FaultProvider, ...]:
        """All registered providers (registration order)."""
        return tuple(cls._providers.values())

    @classmethod
    def get(cls, carrier: str) -> Optional[FaultProvider]:
        """Retrieve a provider by its ``carrier`` id, or ``None``."""
        return cls._providers.get(carrier)

    @classmethod
    def resolve_by_method(cls, injection_method: str | None) -> Optional[FaultProvider]:
        """POST-injection: resolve the backend for a detected ``injection_method``.

        Returns ``None`` for unknown / ``None`` methods so callers keep their
        existing fallback behaviour during the incremental migration.
        """
        if not injection_method:
            return None
        return cls._method_index.get(injection_method)

    @classmethod
    def resolve_by_scope(cls, scope: str | None) -> list[FaultProvider]:
        """PRE-injection: bridge an intent ``scope`` to its *candidate* backends
        via the ``FaultFamily`` registry (``carrier_types`` ↔ provider
        ``carrier``).

        A scope maps to CANDIDATES, not a single backend: pre-injection the LLM
        has not yet chosen a backend, and one scope may be served by several
        (e.g. a k8s scope by ``chaosblade`` OR ``k8s_native``). Returns the
        registered providers for ``family.carrier_types`` in precedence order,
        skipping carriers with no registered provider. Empty list when no family
        owns the scope or none of its carriers are registered. Use
        :meth:`resolve_primary_by_scope` for the single most-likely backend.
        """
        if not scope:
            return []
        # Lazy import: keep the registry importable without pulling the spec
        # package at module load (matches the codebase's lazy-import style).
        from chaos_agent.agent.spec.fault_registry import family_for_scope

        family = family_for_scope(scope)
        if family is None:
            return []
        resolved: list[FaultProvider] = []
        for carrier in family.carrier_types:
            provider = cls._providers.get(carrier)
            if provider is not None:
                resolved.append(provider)
        return resolved

    @classmethod
    def resolve_primary_by_scope(cls, scope: str | None) -> Optional[FaultProvider]:
        """PRE-injection: the single most-likely backend for ``scope`` (the first
        registered candidate from :meth:`resolve_by_scope`), or ``None``."""
        candidates = cls.resolve_by_scope(scope)
        return candidates[0] if candidates else None

    @classmethod
    def applicable(cls, profile: str) -> list[FaultProvider]:
        """PRE-injection: providers that can operate against a channel ``profile``
        ("k8s"|"host"), in registration order."""
        return [p for p in cls._providers.values() if p.matches_channel(profile)]

    @classmethod
    def union_required_params(
        cls, scope: str | None, *, profile: str | None = None
    ) -> list[str]:
        """PRE-injection: order-preserving union of every registered provider's
        ``required_params(scope)``.

        Intent completeness must consult only candidate backends compatible
        with the current environment profile. Pre-injection the concrete
        backend is unknown, so parameters are the union across candidates for
        the current scope, not every provider registered in the process. This
        prevents a future cloud provider's ``region`` requirement from leaking
        into a Kubernetes or host dialogue.
        Self-bootstraps the built-ins on an empty registry (mirrors
        :meth:`detect_method`)."""
        if not cls._providers:
            cls.register_builtins()
        # Preserve the historical public API when the caller has not resolved
        # an environment yet. New prompt/context callers pass ``profile`` and
        # receive the narrower, environment-compatible union.
        candidates = list(cls._providers.values())
        if profile is not None:
            candidates = cls.resolve_by_scope(scope)
            candidates = [p for p in candidates if p.matches_channel(profile)]
            if not candidates:
                candidates = cls.applicable(profile)
            # A resolved-but-unsupported profile must not inherit every
            # provider's parameters. The caller is expected to fail closed.
            if not candidates:
                return []
        if not candidates:
            candidates = list(cls._providers.values())

        out: list[str] = []
        for provider in candidates:
            for param in provider.required_params(scope or ""):
                if param not in out:
                    out.append(param)
        return out

    # -- built-in backends + detection orchestration -----------------------

    @classmethod
    def register_builtins(cls) -> None:
        """Register the built-in execution backends in precedence order.

        Order is load-bearing for :meth:`detect_method`: ChaosBlade (which owns
        the UID-bearing ``host_blade`` / ``kubectl_exec`` methods) must be probed
        before the UID-less ``k8s_native`` and ``host_shell`` backends, matching
        the original ``_detect_injection_method`` branch order. Lazy imports
        break the ``registry ← concrete provider ← base`` import cycle and follow
        the codebase's deferred-import style. Idempotent: ``register`` overwrites,
        so calling twice is harmless.

        This is the single ordered bootstrap of the built-in set. The providers
        package invokes it at import time (see ``providers/__init__``) so callers
        never have to remember to bootstrap; it also remains the explicit
        re-registration entry after a ``clear()`` (test fixtures) and the lazy
        self-bootstrap used by :meth:`detect_method` on an empty registry.
        """
        from chaos_agent.agent.providers.chaosblade import ChaosbladeProvider
        from chaos_agent.agent.providers.chaosblade_python import (
            ChaosbladePythonProvider,
        )
        from chaos_agent.agent.providers.host_shell import HostShellProvider
        from chaos_agent.agent.providers.k8s_native import K8sNativeProvider

        # Ordered built-in set — precedence is significant (see docstring).
        # ``chaosblade_python`` is order-insensitive: it is detected by its own
        # injection TOOL name, which no other backend scans, so it never
        # competes for attribution and can sit last.
        for provider_cls in (
            ChaosbladeProvider,
            K8sNativeProvider,
            HostShellProvider,
            ChaosbladePythonProvider,
        ):
            cls.register(provider_cls())

    @classmethod
    def detect_method(
        cls, messages: list, blade_uid: str | None, *, is_host: bool
    ) -> Optional[str]:
        """Resolve the runtime ``injection_method`` by RECENCY, not raw
        precedence: among channel-scoped providers that recognise their carrier
        in ``messages``, the one whose injection evidence is MOST RECENT wins.

        This implements "attribute the LAST successful injection": after a
        replan switches from a failed ``blade_create`` to a kubectl-native
        fallback, the later native injection out-ranks the earlier (stale)
        blade UID instead of being hijacked by it (task-76c59364). Registration
        precedence is retained only as a TIE-BREAKER (equal recency → the
        earlier-registered provider, i.e. ChaosBlade, wins) so all existing
        single-carrier attributions are unchanged.

        Candidates are scoped by CHANNEL first: ``is_host`` maps to a channel
        profile and only providers whose ``matches_channel`` accepts it are
        probed. A host backend (``host_shell``) is therefore never a candidate
        for a k8s injection, and vice versa — the channel is a hard, known fact.

        Self-bootstraps the built-in backends on an empty registry, mirroring
        ``TransportRegistry``'s ``_ensure_default``; an explicitly-populated
        registry (e.g. test fixtures) is left untouched.
        """
        if not cls._providers:
            cls.register_builtins()
        from chaos_agent.transports import PROFILE_HOST, PROFILE_K8S

        profile = PROFILE_HOST if is_host else PROFILE_K8S
        best_method: Optional[str] = None
        best_key: tuple[int, int] | None = None
        for rank, provider in enumerate(cls._providers.values()):
            if not provider.matches_channel(profile):
                continue
            method = provider.detect(messages, blade_uid, is_host=is_host)
            if not method:
                continue
            # Recency is the message index of this provider's injection
            # evidence. Providers predating the seam (e.g. test doubles) have no
            # ``injection_recency`` — fall back to 0 so ties resolve by the
            # registration-order rank below (legacy precedence behaviour).
            recency_fn = getattr(provider, "injection_recency", None)
            recency = (
                recency_fn(messages, blade_uid, is_host=is_host)
                if recency_fn is not None
                else 0
            )
            # Higher recency wins; equal recency → lower rank (earlier
            # registration precedence) wins via ``-rank``.
            key = (recency, -rank)
            if best_key is None or key > best_key:
                best_key = key
                best_method = method
        return best_method

    # -- test / lifecycle helpers ------------------------------------------

    @classmethod
    def clear(cls) -> None:
        """Drop all registrations. For tests that install fixtures."""
        cls._providers = {}
        cls._method_index = {}


__all__ = ["FaultProviderRegistry"]
