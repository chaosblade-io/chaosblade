"""Resolve the current environment, provider candidates and visible tools.

This module is the bridge between the execution architecture and prompting:
the same resolved context is used to describe capabilities to the LLM and to
remove unrelated provider tools before ``bind_tools``.  ToolNode/TargetGuard
remain the fail-closed enforcement layer after the model emits a call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Sequence

from chaos_agent.agent.environment_profiles import get_environment_profile
from chaos_agent.agent.providers import FaultProviderRegistry
from chaos_agent.agent.providers.base import EXECUTE, PLAN, RECOVER_VERIFY, VERIFY
from chaos_agent.agent.spec.fault_registry import family_for_scope
from chaos_agent.transports import (
    PROFILE_K8S,
    PROFILE_UNKNOWN,
    profile_of,
    resolve_channel_name,
)


_PHASE_TO_PROVIDER_PHASE = {
    "intent": PLAN,
    "plan": PLAN,
    "execute": EXECUTE,
    "verify": VERIFY,
    "recover_verify": RECOVER_VERIFY,
}

# Every phase a provider can contribute tools to. The ownership index below is
# built from the UNION of these, which is what makes "is this a provider tool?"
# independent of the phase being gated.
#
# Derived from the phase-mapping's value set rather than written out again: a
# hand-copied list is exactly the kind of parallel table this whole change
# exists to remove — adding a phase to ``providers.base`` and forgetting it here
# would silently shrink the ownership index and reopen defect A.
_ALL_PROVIDER_PHASES = tuple(sorted(set(_PHASE_TO_PROVIDER_PHASE.values())))

logger = logging.getLogger(__name__)


def provider_tool_owners() -> dict[str, tuple]:
    """Map every provider-contributed tool name to ALL its owning providers.

    Built from the union of all provider phases — deliberately NOT per-phase.
    task-46317228: the previous per-phase derivation asked "is this tool in
    THIS phase's provider list?", so a provider tool bound in a phase where its
    own provider declares a different tool (``host_read`` is HostShell's PLAN /
    VERIFY tool, ``host_inject`` its EXECUTE one) fell into the "not a provider
    tool" bucket and skipped the gate entirely. Ownership is a property of the
    tool, not of the phase.

    A tool can have SEVERAL owners: ``blade_help`` / ``blade_status`` are
    declared by both ChaosBlade providers (one accepts both profiles, the other
    only host). Recording just the first would make the gate's verdict depend on
    registration order, so every claimant is kept and reachability is the UNION
    (see :func:`_tool_allowed_for_profile`) — a tool is reachable wherever any
    provider that offers it can operate.
    """
    if not FaultProviderRegistry.all_providers():
        FaultProviderRegistry.register_builtins()
    owners: dict[str, list] = {}
    for provider in FaultProviderRegistry.all_providers():
        for phase in _ALL_PROVIDER_PHASES:
            for tool in provider.tools(phase):
                name = getattr(tool, "name", "")
                if name and provider not in owners.setdefault(name, []):
                    owners[name].append(provider)
    return {name: tuple(providers) for name, providers in owners.items()}


#: Tools whose OWNING provider spans several profiles but which are themselves
#: meaningful in only one. Provider-level ``matches_channel`` is too coarse for
#: these: ``ChaosbladeProvider`` legitimately accepts host (``blade create cpu
#: load`` runs on a bare machine), so every tool it declares was reaching the
#: host tool surface — including ``blade_query_k8s``, which queries a cluster CRD
#: that host scope does not have. The tool itself already refuses there
#: (``tools/blade.py``: "does not apply to host-scope experiments") and host
#: Layer 1 deliberately skips it (``verify/_verifier_layer1.py``: "blade_query_k8s
#: is k8s-only"), so binding it to a host turn only spends context and invites a
#: round wasted on a refusal.
#:
#: This gate is about VISIBILITY (what enters the LLM's context), which is
#: separate from ``tools/_tool_profiles.TOOL_PROFILE`` — that table asserts a
#: transport profile at EXECUTION time and deliberately excludes the blade family
#: so a legitimate host-channel blade injection is never refused.
_TOOL_ONLY_PROFILE: dict[str, str] = {
    "blade_query_k8s": PROFILE_K8S,
}


def _tool_allowed_for_profile(owners: tuple, profile: str, name: str = "") -> bool:
    """True when *profile* may see this tool.

    A tool declared for exactly one profile (see :data:`_TOOL_ONLY_PROFILE`)
    answers on its own; otherwise reachability is the UNION over owning
    providers — a tool is reachable wherever any provider offering it operates.
    """
    only = _TOOL_ONLY_PROFILE.get(name)
    if only is not None:
        return profile == only
    return any(provider.matches_channel(profile) for provider in owners)


def _split_by_ownership(
    names: set[str], profile: str
) -> tuple[set[str], set[str]]:
    """Return (all provider-owned names, names allowed for *profile*)."""
    owners = provider_tool_owners()
    owned = {name for name in names if name in owners}
    allowed = {
        name for name in owned
        if _tool_allowed_for_profile(owners[name], profile, name)
    }
    return owned, allowed


@dataclass(frozen=True)
class AgentCapabilityContext:
    """Capabilities that are valid for one LLM invocation."""

    profile: str
    phase: str
    target_authority: str
    provider_candidates: tuple[str, ...]
    active_tool_names: frozenset[str]
    supported: bool

    def prompt_fragment(self) -> str:
        environment = get_environment_profile(self.profile)
        if environment is None:
            return (
                "## Capability Profile\n"
                "The current environment profile is unsupported. Do not attempt "
                "injection, recovery, or baseline collection; report the missing "
                "environment capability."
            )
        return environment.prompt_fragment(self.phase)


def resolve_profile_for_state(state: dict | None) -> str:
    """Resolve one profile only when scope and transport agree.

    The fault scope describes the requested fault domain while the transport
    describes the environment that would actually receive commands. A mismatch
    is unsafe: it must not expose either environment's execution tools.
    """
    state = state or {}
    spec_data = state.get("fault_spec")
    scope = ""
    if isinstance(spec_data, dict):
        scope = str(spec_data.get("scope") or "")
    scope_profile = ""
    if scope:
        family = family_for_scope(scope)
        # A non-empty scope is an assertion about the fault domain. Checkpoints
        # and external callers can bypass normal FaultSpec construction, so an
        # unregistered value must not inherit the transport default.
        if family is None:
            return PROFILE_UNKNOWN
        scope_profile = family.profile

    transport_profile = profile_of(resolve_channel_name(state))
    if scope_profile and transport_profile != scope_profile:
        return PROFILE_UNKNOWN
    return scope_profile or transport_profile


def build_capability_context(
    state: dict | None,
    phase: str,
    available_tools: Sequence[object] | None = (),
) -> AgentCapabilityContext:
    """Build a fail-closed profile-aware capability context.

    Non-provider tools such as ``finish_planning`` or ``submit_verification``
    stay visible. Provider-contributed tools are visible only if their OWNING
    provider supports the resolved environment profile.
    """
    profile = resolve_profile_for_state(state)
    environment = get_environment_profile(profile)

    names = {getattr(tool, "name", "") for tool in (available_tools or ())}
    names.discard("")

    # An unregistered phase means the caller and this gate disagree about the
    # vocabulary. Previously that yielded an empty provider set, i.e. EVERY
    # tool passed — a typo silently disabled the gate. Fail closed instead and
    # make the misconfiguration loud.
    provider_phase = _PHASE_TO_PROVIDER_PHASE.get(phase)
    if provider_phase is None:
        logger.error(
            "build_capability_context: unregistered phase %r (known: %s) — "
            "failing closed with no active tools",
            phase, sorted(_PHASE_TO_PROVIDER_PHASE),
        )
        return AgentCapabilityContext(
            profile=profile,
            phase=phase,
            target_authority="",
            provider_candidates=(),
            active_tool_names=frozenset(),
            supported=False,
        )

    owned_names, allowed_names = _split_by_ownership(names, profile)

    # Unsupported or conflicting environments receive no tools at all. This
    # prevents a generic graph tool from advancing an unvalidated request to a
    # later mutation-capable phase.
    active: set[str] = set()
    if environment is not None:
        active = names - owned_names
        active.update(allowed_names)

    return AgentCapabilityContext(
        profile=profile,
        phase=phase,
        target_authority=environment.target_authority if environment else "",
        provider_candidates=tuple(
            provider.carrier for provider in FaultProviderRegistry.applicable(profile)
        ) if environment else (),
        active_tool_names=frozenset(active),
        supported=environment is not None,
    )


def build_intent_discovery_context(
    state: dict | None,
    available_tools: Sequence[object] | None = (),
) -> AgentCapabilityContext:
    """Build the read-only discovery surface for semantic intent collection.

    Intent must see the entire fault/skill catalog regardless of its provisional
    ``scope``. It may still inspect the *currently configured environment* to
    collect target candidates. Therefore this context resolves from transport
    only, deliberately ignoring ``fault_spec.scope``; compatibility between the
    recognized fault domain and the transport is enforced later in Agent Loop.
    """
    profile = profile_of(resolve_channel_name(state))
    environment = get_environment_profile(profile)
    names = {getattr(tool, "name", "") for tool in (available_tools or ())}
    names.discard("")
    owned_names, allowed_names = _split_by_ownership(names, profile)
    active: set[str] = set()
    if environment is not None:
        active = names - owned_names
        active.update(allowed_names)
    return AgentCapabilityContext(
        profile=profile,
        phase="intent_discovery",
        target_authority=environment.target_authority if environment else "",
        provider_candidates=tuple(
            provider.carrier for provider in FaultProviderRegistry.applicable(profile)
        ) if environment else (),
        active_tool_names=frozenset(active),
        supported=environment is not None,
    )


def filter_tools_for_context(
    tools: Iterable[object] | None, context: AgentCapabilityContext
) -> list[object]:
    """Return only tools visible to this context, preserving input order."""
    return [
        tool for tool in (tools or ())
        if getattr(tool, "name", "") in context.active_tool_names
    ]


def is_tool_name_allowed_for_context(
    tool_name: str, state: dict | None, phase: str,
) -> bool:
    """Return whether a static ToolNode may execute a provider tool.

    ``bind_tools`` is not an enforcement boundary: restored checkpoints can
    carry tool calls produced under a previous context. Phase screeners use
    this helper before dispatching to a static ToolNode.
    """
    profile = resolve_profile_for_state(state)
    if get_environment_profile(profile) is None:
        return False
    # Unregistered phase → fail closed. The old ``return True`` meant a typo in
    # a screener's phase string silently disabled runtime enforcement for every
    # tool it screened.
    if _PHASE_TO_PROVIDER_PHASE.get(phase) is None:
        logger.error(
            "is_tool_name_allowed_for_context: unregistered phase %r "
            "(known: %s) — refusing %r",
            phase, sorted(_PHASE_TO_PROVIDER_PHASE), tool_name,
        )
        return False
    owners = provider_tool_owners()
    tool_owners = owners.get(tool_name)
    # Not provider-owned (graph control tools, MCP tools) → not profile-bound.
    if not tool_owners:
        return True
    return _tool_allowed_for_profile(tool_owners, profile, tool_name)


def tool_call_field(call: object, field: str, default: str = "") -> str:
    """Read *field* off a tool_call in either shape.

    A tool_call is a dict (LangChain >= 0.3 TypedDict) or an object (older
    releases / custom wrappers). Every screener re-implemented this; one reader
    keeps them behaving identically across SDK upgrades.
    """
    if isinstance(call, dict):
        return call.get(field) or default
    return getattr(call, field, default) or default


def tool_call_allowed(
    tool_name: str, state: dict | None, phase: str = "", *, discovery: bool = False,
) -> bool:
    """THE per-call capability verdict — the one every screener must use.

    Wraps the two policy functions so the answer, and the behaviour when the
    gate itself raises, live in ONE place. This screen sits on the critical path
    of every ToolNode: letting an exception out would abort the node, and in the
    verify phase that kills the run while the fault is still injected. Refuse
    instead, and keep the cause visible via ``logger.exception``.

    Callers MUST supply either ``phase`` or ``discovery=True``. Passing neither
    is a programming error, and it degrades the way the rest of this module does:
    ``is_tool_name_allowed_for_context`` reports an unregistered phase (naming
    the known ones) and refuses. Loud and closed, never silently permissive.

    ``discovery=True`` selects the intent rule (transport only, provisional
    scope deliberately ignored) instead of the scope+transport rule.

    NOTE: this is the CAPABILITY verdict only. ``phase1_screener``'s
    mutation-equivalence classifier is a separate check with the opposite error
    policy (fail-OPEN, so a classifier bug cannot produce a rejection the model
    has no way to satisfy).
    """
    try:
        if discovery:
            return is_tool_name_allowed_for_intent_discovery(tool_name, state)
        return is_tool_name_allowed_for_context(tool_name, state, phase)
    except Exception:
        logger.exception(
            "capability gate raised while checking %r (phase=%r, discovery=%s) — "
            "refusing the call rather than aborting the node",
            tool_name, phase, discovery,
        )
        return False


def explain_tool_refusal(
    tool_name: str, state: dict | None, phase: str = "",
    *, discovery: bool = False,
) -> tuple[str, str]:
    """Return ``(reason, suggestion)`` naming WHY this tool is not available.

    Lives beside :func:`tool_call_allowed` because only this module holds the
    three judgements the verdict is made from — the resolved profile, the phase
    registration, and which providers own the tool. A caller can only restate
    the verdict ("unavailable for the current environment capability profile"),
    which is the same sentence for every tool in every profile: it names neither
    the profile in force, nor the profile the tool belongs to, nor what to use
    instead. That is a template, not a cause, and a model given it has nothing
    to act on except retrying the same call.

    ``discovery`` must mirror the flag the VERDICT was made with (see
    :func:`is_tool_name_allowed_for_intent_discovery`): that rule resolves the
    profile from the transport alone and deliberately ignores a provisional
    ``fault_spec.scope``. Explaining such a refusal through the scope-agreement
    rule instead would report "environment not registered" for a perfectly
    healthy environment — a host-scoped intent on a k8s transport is normal in
    that phase, not a misconfiguration.

    Best-effort: any failure degrades to the generic pair rather than raising,
    since this runs on a rejection path that must not itself fail.
    """
    generic = (
        "tool is unavailable for the current environment capability profile",
        "Use only tools bound for the current environment.",
    )
    try:
        profile = (
            profile_of(resolve_channel_name(state)) if discovery
            else resolve_profile_for_state(state)
        )
        if get_environment_profile(profile) is None:
            return (
                f"the environment profile in force ({profile or '<unresolved>'}) "
                "is not a registered capability profile, so no provider tool can "
                "be dispatched",
                "Fix the environment configuration (kubeconfig / host transport) "
                "so a supported profile resolves, then retry. No tool choice can "
                "work around an unsupported environment.",
            )
        if phase and _PHASE_TO_PROVIDER_PHASE.get(phase) is None:
            # A screener passed a phase string the registry does not know. This
            # is a wiring bug, not something the model can repair — say so
            # rather than implying the tool choice was wrong.
            return (
                f"the capability gate was consulted with an unregistered phase "
                f"({phase!r}), so it refused rather than guess",
                "This is an internal wiring error, not a problem with the "
                "tool call. Report it instead of retrying variations.",
            )

        owners = provider_tool_owners()
        tool_owners = owners.get(tool_name)
        if not tool_owners:
            # Not provider-owned, so the capability gate would have ALLOWED it.
            # Reaching here means the refusal came from somewhere else.
            return generic

        # The common case: the tool exists, but belongs to other profile(s).
        owner_profiles = sorted({
            channel
            for provider in tool_owners
            for channel in _provider_channels(provider)
        })
        available = sorted(
            name for name, owns in owners.items()
            if _tool_allowed_for_profile(owns, profile, name)
        )
        reason = (
            f"'{tool_name}' is provided for the "
            f"{', '.join(owner_profiles) or '<unknown>'} profile, but the "
            f"environment in force is '{profile}'"
        )
        suggestion = (
            f"Express this operation with a tool available in the '{profile}' "
            f"profile: {', '.join(available) or '<none>'}. Adding arguments to "
            f"'{tool_name}' cannot make it reachable here."
        )
        return reason, suggestion
    except Exception:
        logger.exception(
            "explain_tool_refusal failed for %r (phase=%r); falling back to the "
            "generic wording", tool_name, phase,
        )
        return generic


def _provider_channels(provider: object) -> tuple[str, ...]:
    """Channels/profiles a provider can operate on, best-effort.

    Providers expose reachability only through ``matches_channel`` — there is no
    listing to read — so probe every REGISTERED profile instead of reaching into
    provider internals. Registered profiles are the full universe here: a
    profile the registry does not know cannot be resolved for a call either.
    """
    from chaos_agent.agent.environment_profiles import EnvironmentProfileRegistry

    # ``get_environment_profile`` lazily registers the builtins; call it once so
    # an untouched registry does not read as "no profiles exist".
    get_environment_profile(PROFILE_UNKNOWN)
    result: list[str] = []
    for env in EnvironmentProfileRegistry.all():
        try:
            if provider.matches_channel(env.profile_id):
                result.append(env.profile_id)
        except Exception:
            continue
    return tuple(result)


def screen_tool_calls(
    calls: Iterable[object] | None,
    state: dict | None,
    phase: str = "",
    *,
    discovery: bool = False,
) -> tuple[list, list]:
    """Split *calls* into ``(allowed, rejected)`` by the capability verdict.

    Per-call rather than per-batch: a batch mixing a legitimate call with a
    cross-profile one should lose only the latter.
    """
    allowed: list = []
    rejected: list = []
    for call in calls or ():
        name = tool_call_field(call, "name")
        target = allowed if tool_call_allowed(
            name, state, phase, discovery=discovery
        ) else rejected
        target.append(call)
    return allowed, rejected


def is_tool_name_allowed_for_intent_discovery(
    tool_name: str, state: dict | None,
) -> bool:
    """Allow a provider discovery tool only for the current transport.

    Unlike ``is_tool_name_allowed_for_context``, this intentionally ignores a
    provisional semantic scope. Intent may recognize any registered family;
    only its read-only probe must match the environment it is inspecting.
    """
    profile = profile_of(resolve_channel_name(state))
    if get_environment_profile(profile) is None:
        return False
    owner = provider_tool_owners().get(tool_name)
    if not owner:
        return True
    return _tool_allowed_for_profile(owner, profile, tool_name)


__all__ = [
    "AgentCapabilityContext",
    "build_capability_context",
    "build_intent_discovery_context",
    "explain_tool_refusal",
    "filter_tools_for_context",
    "is_tool_name_allowed_for_context",
    "is_tool_name_allowed_for_intent_discovery",
    "resolve_profile_for_state",
    "screen_tool_calls",
    "tool_call_allowed",
    "tool_call_field",
]
