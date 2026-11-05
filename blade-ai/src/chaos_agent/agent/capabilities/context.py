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
#: Layer 1 deliberately skips it (``providers/chaosblade/verify.py``:
#: "blade_query_k8s is k8s-only"), so binding it to a host turn only spends
#: context and invites a round wasted on a refusal.
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


def _visible_tool_names(names: set[str], profile: str) -> set[str]:
    """The single visibility rule shared by every capability context.

    A tool is profile-gated IFF it is provider-owned (domain-specific). The
    domain-agnostic remainder — graph control tools (``finish_execution``,
    ``request_replan``, ``update_progress``, ``submit_*``) and MCP tools — is
    NEVER profile-bound and stays visible under EVERY profile, including an
    unsupported / unresolved one. That is deliberate: stripping them is what
    turned an unsupported environment into a spin, because the model lost the
    very exits (finish / replan / report) it needed to stop honestly, while
    every injection tool was already gone. Only the provider half is gated by
    ``profile``; under an unsupported profile no provider matches, so
    ``allowed_names`` is empty and no injection tool leaks. The
    ``get_environment_profile`` guard keeps that explicit rather than relying
    on the empty-``allowed_names`` side effect alone.
    """
    owned_names, allowed_names = _split_by_ownership(names, profile)
    active = set(names) - owned_names
    if get_environment_profile(profile) is not None:
        active |= allowed_names
    return active


@dataclass(frozen=True)
class AgentCapabilityContext:
    """Capabilities that are valid for one LLM invocation."""

    profile: str
    phase: str
    target_authority: str
    provider_candidates: tuple[str, ...]
    active_tool_names: frozenset[str]
    supported: bool
    # Why ``profile`` fail-closed to UNKNOWN (one of the ``PROFILE_UNKNOWN_*``
    # constants), or None when it resolved. The capability DECISION is identical
    # for every cause (expose no provider tool), but the OPERATOR remediation is
    # not, so the cause rides alongside the profile instead of being flattened
    # into the single "unknown" string and re-derived downstream. Additive and
    # default-None: every existing consumer reads ``profile`` / ``supported``
    # only and is unaffected. ``supported=False`` with ``unknown_reason=None``
    # is the unregistered-PHASE branch (a wiring bug), not an unsupported
    # environment.
    unknown_reason: str | None = None

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


# Why a profile fail-closed to PROFILE_UNKNOWN. The capability DECISION is the
# same for all three (expose no provider tool, fail closed), but the OPERATOR
# remediation differs, so the cause is carried out beside the profile instead of
# being flattened into the single "unknown" string. Before this the cause was
# destroyed at resolve time and ``explain_tool_refusal`` had to re-call
# ``family_for_scope`` to recover cause ①, while causes ② and ③ both fell into
# one misleading "fix your kubeconfig" line — which sent #87 (a scope/transport
# CONFLICT, kubeconfig perfectly healthy) debugging in the wrong direction.
#: scope is non-empty but not a registered fault domain → fix the case frontmatter.
PROFILE_UNKNOWN_SCOPE_UNREGISTERED = "scope_unregistered"
#: scope and transport each resolve to a DIFFERENT valid profile → channel drift;
#: recover must use the injection-time channel, not the ambient one (#87).
PROFILE_UNKNOWN_CONFLICT = "profile_conflict"
#: the transport channel is invalid / unprofiled → fix kube_connection_mode /
#: kubeconfig / host fields.
PROFILE_UNKNOWN_CHANNEL = "channel_unresolvable"


@dataclass(frozen=True)
class ProfileVerdict:
    """The resolved profile plus the full diagnostic for WHY it is UNKNOWN.

    ``profile`` is byte-identical to what :func:`resolve_profile_for_state`
    returned before (every existing consumer reads only that string). The extra
    fields are the diagnostic the single source already computed as locals —
    carried out so the message layer names the exact cause instead of
    re-deriving it. ``reason`` is None whenever ``profile`` resolved (k8s/host).
    """

    profile: str
    reason: str | None = None
    scope_profile: str = ""
    transport_profile: str = ""

    @property
    def supported(self) -> bool:
        """True iff a real environment profile resolved.

        Mirrors ``AgentCapabilityContext.supported`` (both are
        ``get_environment_profile(profile) is not None``) so the loop entry
        gates can fail fast straight off the verdict without building a whole
        tool-visibility context. By construction this is the exact negation of
        ``reason is not None`` — every UNKNOWN return sets a reason, every
        resolved return clears it — but stating it through the same
        ``get_environment_profile`` predicate keeps the two ``supported``
        surfaces from ever diverging.
        """
        return get_environment_profile(self.profile) is not None


def resolve_profile_verdict(state: dict | None) -> ProfileVerdict:
    """Resolve one profile only when scope and transport agree, and say WHY not.

    The fault scope describes the requested fault domain while the transport
    describes the environment that would actually receive commands. A mismatch
    is unsafe: it must not expose either environment's execution tools. Three
    distinct conditions fail closed to UNKNOWN — an unregistered scope, a
    scope/transport conflict, and an unresolvable channel — and each carries a
    different remediation, so the cause is returned alongside the profile.
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
            return ProfileVerdict(
                PROFILE_UNKNOWN, PROFILE_UNKNOWN_SCOPE_UNREGISTERED,
            )
        scope_profile = family.profile

    transport_profile = profile_of(resolve_channel_name(state))
    if scope_profile and transport_profile != scope_profile:
        # One branch, two causes: the transport resolved to a DIFFERENT valid
        # profile (a genuine scope/transport conflict — #87, the recover channel
        # drifted off the injection scope), or the transport itself is
        # unresolvable while the scope is fine. The remediation differs, so name
        # them apart instead of collapsing both into "conflict".
        reason = (
            PROFILE_UNKNOWN_CHANNEL
            if transport_profile == PROFILE_UNKNOWN
            else PROFILE_UNKNOWN_CONFLICT
        )
        return ProfileVerdict(
            PROFILE_UNKNOWN, reason,
            scope_profile=scope_profile, transport_profile=transport_profile,
        )
    profile = scope_profile or transport_profile
    if profile == PROFILE_UNKNOWN:
        # No fault scope declared AND no resolvable channel — nothing to act on.
        return ProfileVerdict(
            PROFILE_UNKNOWN, PROFILE_UNKNOWN_CHANNEL,
            scope_profile=scope_profile, transport_profile=transport_profile,
        )
    return ProfileVerdict(
        profile, None,
        scope_profile=scope_profile, transport_profile=transport_profile,
    )


def resolve_profile_for_state(state: dict | None) -> str:
    """Resolve one profile only when scope and transport agree.

    Thin string-preserving wrapper over :func:`resolve_profile_verdict`. Every
    existing consumer reads the bare profile string and is unaffected by the
    reason the verdict now also carries.
    """
    return resolve_profile_verdict(state).profile


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
    verdict = resolve_profile_verdict(state)
    profile = verdict.profile
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
            unknown_reason=verdict.reason,
        )

    # Visibility is profile-gated ONLY for provider-owned (domain-specific)
    # tools. Domain-agnostic control/MCP tools stay visible even when the
    # profile is unsupported, so the model keeps its finish/replan/report exits
    # instead of spinning on an empty toolset. The old "no tools at all" rule
    # feared a control tool advancing an unvalidated request to a mutation
    # phase — but every mutation phase re-resolves the same unsupported profile
    # and strips its provider tools there too, so no mutation is reachable; the
    # only effect of the old rule was removing the exits.
    active = _visible_tool_names(names, profile)

    return AgentCapabilityContext(
        profile=profile,
        phase=phase,
        target_authority=environment.target_authority if environment else "",
        provider_candidates=tuple(
            provider.carrier for provider in FaultProviderRegistry.applicable(profile)
        ) if environment else (),
        active_tool_names=frozenset(active),
        supported=environment is not None,
        unknown_reason=verdict.reason,
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
    # Same visibility rule as build_capability_context (see _visible_tool_names):
    # domain-agnostic tools are never profile-bound, so an unresolved transport
    # still leaves the model its control exits.
    active = _visible_tool_names(names, profile)
    return AgentCapabilityContext(
        profile=profile,
        phase="intent_discovery",
        target_authority=environment.target_authority if environment else "",
        provider_candidates=tuple(
            provider.carrier for provider in FaultProviderRegistry.applicable(profile)
        ) if environment else (),
        active_tool_names=frozenset(active),
        supported=environment is not None,
        # Discovery resolves from transport alone (it deliberately ignores a
        # provisional scope), so the ONLY way it lands on UNKNOWN is an
        # unresolvable channel — never a scope cause.
        unknown_reason=(
            None if environment is not None else PROFILE_UNKNOWN_CHANNEL
        ),
    )


def filter_tools_for_context(
    tools: Iterable[object] | None, context: AgentCapabilityContext
) -> list[object]:
    """Return only tools visible to this context, preserving input order."""
    return [
        tool for tool in (tools or ())
        if getattr(tool, "name", "") in context.active_tool_names
    ]


def _allowance_verdict(
    tool_name: str, profile: str, *, phase: str | None = None,
) -> bool:
    """The single runtime allowance rule, mirroring :func:`_visible_tool_names`.

    Two orthogonal gates, in this order:

    1. An unregistered ``phase`` (when one is supplied) is a CALLER wiring bug
       — a typo'd phase string — and fails closed for EVERY tool, control
       tools included. This is NOT the "unsupported environment" condition; it
       means the gate cannot even locate itself in the graph, so it refuses
       rather than guesses.
    2. Profile support gates ONLY provider-owned (domain-specific) tools. A
       domain-agnostic tool (graph control, MCP) is never profile-bound and is
       allowed under every profile, so an unsupported environment strips the
       injection tools but leaves the model its finish/replan/report exits
       instead of stranding it on an empty toolset.
    """
    if phase is not None and _PHASE_TO_PROVIDER_PHASE.get(phase) is None:
        logger.error(
            "capability gate consulted with unregistered phase %r (known: %s) "
            "— refusing %r rather than guessing",
            phase, sorted(_PHASE_TO_PROVIDER_PHASE), tool_name,
        )
        return False
    tool_owners = provider_tool_owners().get(tool_name)
    # Not provider-owned (graph control tools, MCP tools) → not profile-bound.
    if not tool_owners:
        return True
    if get_environment_profile(profile) is None:
        return False
    return _tool_allowed_for_profile(tool_owners, profile, tool_name)


def is_tool_name_allowed_for_context(
    tool_name: str, state: dict | None, phase: str,
) -> bool:
    """Return whether a static ToolNode may execute a tool call.

    ``bind_tools`` is not an enforcement boundary: restored checkpoints can
    carry tool calls produced under a previous context. Phase screeners use
    this helper before dispatching to a static ToolNode. Provider tools are
    profile-gated; domain-agnostic control/MCP tools are allowed regardless of
    profile, and an unregistered phase fails closed for both (see
    :func:`_allowance_verdict`).
    """
    return _allowance_verdict(
        tool_name, resolve_profile_for_state(state), phase=phase,
    )


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


def _unknown_environment_explanation(
    verdict: ProfileVerdict, state: dict | None,
) -> tuple[str, str]:
    """The cause-specific ``(reason, suggestion)`` for an unresolved profile.

    SINGLE renderer shared by :func:`explain_tool_refusal` (per-tool refusal)
    and the execute / recover entry gates (loop terminal), so the operator sees
    the same accurate cause whichever surface reports it. The cause is read off
    the verdict the single source already computed — NOT re-derived here. The
    old code re-called ``family_for_scope`` to recover the scope cause and lumped
    the conflict and channel causes into ONE misleading "fix your kubeconfig"
    line, which sent #87 (a scope/transport CONFLICT on a perfectly healthy
    kubeconfig) debugging in the wrong direction, and #34-R (an unregistered
    scope) after a config that was never broken.
    """
    if verdict.reason == PROFILE_UNKNOWN_SCOPE_UNREGISTERED:
        _spec = (state or {}).get("fault_spec")
        _scope = str(_spec.get("scope") or "") if isinstance(_spec, dict) else ""
        from chaos_agent.agent.spec.fault_spec import INTENT_SCOPES
        return (
            f"the fault scope in force ({_scope!r}) is not a "
            "registered fault domain, so no capability profile "
            "resolves and no provider tool can be dispatched "
            f"(valid scopes: {', '.join(INTENT_SCOPES)})",
            "Correct the fault scope to a registered domain — the "
            "source is the case frontmatter / spec authoring, not "
            "the cluster. No tool choice and no kubeconfig change "
            "can work around an unregistered scope.",
        )
    if verdict.reason == PROFILE_UNKNOWN_CONFLICT:
        return (
            f"the fault scope resolves to the '{verdict.scope_profile}' profile "
            f"but the transport channel in force resolves to "
            f"'{verdict.transport_profile}', so the two disagree and neither "
            "environment's execution tools may be exposed",
            "This is a channel/scope mismatch, not a broken kubeconfig. "
            "The fault must be recovered on the channel it was injected "
            "through (the injection-time channel), not the ambient one — "
            "align kube_connection_mode with the injection scope, then "
            "retry. No tool choice can work around the mismatch.",
        )
    # CHANNEL_UNRESOLVABLE (or a defensively-None reason): the transport channel
    # itself is invalid / unprofiled, so no environment resolves.
    return (
        f"the environment profile in force ({verdict.profile or '<unresolved>'}) "
        "is not a registered capability profile, so no provider tool can "
        "be dispatched",
        "Fix the environment configuration (kubeconfig / host transport) "
        "so a supported profile resolves, then retry. No tool choice can "
        "work around an unsupported environment.",
    )


def explain_unsupported_environment(
    verdict: ProfileVerdict, state: dict | None,
) -> str:
    """One operator-facing terminal sentence for an unresolved profile.

    Shared by the loop ENTRY gates (``execute_loop`` and the recover verifier)
    that must fail fast on ``profile=UNKNOWN``: a deterministic dead-end the
    ReAct loop can neither repair nor converge on by retrying, so the gate
    terminates in O(1) with zero tokens instead of running ``bind_tools([])``
    until the budget is burned. Both gates name the SAME cause off the SAME
    verdict through the one renderer (:func:`_unknown_environment_explanation`)
    that ``explain_tool_refusal`` uses — neither re-derives it, so the per-tool
    refusal wording and the loop-terminal wording can never drift apart.
    """
    reason, suggestion = _unknown_environment_explanation(verdict, state)
    return f"{reason}. {suggestion}"


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
        if discovery:
            # Discovery resolves from transport alone and deliberately ignores a
            # provisional scope (see docstring), so its ONLY UNKNOWN cause is an
            # unresolvable channel — never a scope cause.
            _transport_profile = profile_of(resolve_channel_name(state))
            verdict = ProfileVerdict(
                _transport_profile,
                None if get_environment_profile(_transport_profile) is not None
                else PROFILE_UNKNOWN_CHANNEL,
            )
        else:
            verdict = resolve_profile_verdict(state)
        # Bound once from the verdict so the cross-profile tail below (which
        # names the environment in force) sees it on BOTH paths.
        profile = verdict.profile
        if get_environment_profile(profile) is None:
            return _unknown_environment_explanation(verdict, state)
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
    provisional semantic scope and carries no phase gate (discovery is a single
    read-only surface). Intent may recognize any registered family; only its
    read-only probe must match the environment it is inspecting. Domain-agnostic
    tools stay allowed regardless of transport (see :func:`_allowance_verdict`).
    """
    return _allowance_verdict(
        tool_name, profile_of(resolve_channel_name(state)),
    )


__all__ = [
    "AgentCapabilityContext",
    "PROFILE_UNKNOWN_CHANNEL",
    "PROFILE_UNKNOWN_CONFLICT",
    "PROFILE_UNKNOWN_SCOPE_UNREGISTERED",
    "ProfileVerdict",
    "build_capability_context",
    "build_intent_discovery_context",
    "explain_tool_refusal",
    "explain_unsupported_environment",
    "filter_tools_for_context",
    "is_tool_name_allowed_for_context",
    "is_tool_name_allowed_for_intent_discovery",
    "resolve_profile_for_state",
    "resolve_profile_verdict",
    "screen_tool_calls",
    "tool_call_allowed",
    "tool_call_field",
]
