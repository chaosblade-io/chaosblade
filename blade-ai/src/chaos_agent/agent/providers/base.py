"""FaultProvider — the behaviour seam for fault execution backends.

Where :class:`~chaos_agent.agent.spec.fault_registry.FaultFamily` is the
*vocabulary / metadata* seam (which scopes / targets / actions exist, which
``carrier_types`` executes them, whether a scope is namespace-less), a
``FaultProvider`` is the *behaviour* seam: for one execution backend it owns
how a fault is

  - offered to the LLM as tools (``tools`` — fed into the factory tool union),
  - recognised in message history (``detect`` — the injection-method decision),
  - Layer-1 verified (``layer1_verify``),
  - recovered (``recover`` — consumes the carrier-agnostic recovery handle),
  - and described to the LLM in the shared prompt (``prompt_fragments`` for the
    pre-injection identity fragment; ``verify_prompt_note`` /
    ``recover_layer2_context`` for the post-injection, per-method language).

Design
------
The codebase already discovered the right extensibility pattern twice —
``TransportRegistry`` (self-registering channels) and the baseline capability
*profiles* — but applied it only locally. Everywhere else the per-backend
difference is a hardcoded ``if injection_method == "..."`` scattered across
``execute_loop`` (detection), ``_verifier_layer1`` (Layer 1) and
``_recover_verifier_loop`` (recovery). ``FaultProvider`` lifts that manual
discriminated-union into one dispatch point so adding a new backend is a
registration, not an edit across five modules.

Backend != environment
-----------------------
"k8s vs host" is the *environment / channel* axis, already handled by
``TransportRegistry`` (``profile_of``). A provider is keyed by *execution
backend* (``carrier``): how the fault is injected AND how it is undone. On a
k8s cluster there are two backends — ChaosBlade and kubectl-native — so the
provider count is not the channel count.

Delivery != backend
--------------------
Within one backend a fault may reach the target through different *delivery*
channels. ChaosBlade's ``host_blade`` (local blade binary) and ``kubectl_exec``
(fallback: ``kubectl exec`` into a tool pod) are two deliveries of the SAME
backend — same ``blade create`` injection, same ``blade destroy`` recovery
semantics — so they map to one provider, with the delivery difference handled
*inside* it (kubectl_exec must also destroy via ``kubectl exec``). Note that
the historical method name ``host_blade`` refers to the delivery *location*
(the blade binary runs on the host / agent machine); the fault it creates is
still a K8s experiment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Protocol, runtime_checkable

if TYPE_CHECKING:  # imported only for typing — avoids import cycles at runtime
    from langchain_core.tools import BaseTool

    from chaos_agent.agent.result.verdict import Layer1Result


# Phases at which a provider may contribute LLM tools. Mirrors the tool-set
# split in ``factory.py`` (clarification/phase1/phase2/verifier/recover).
PLAN = "plan"
EXECUTE = "execute"
VERIFY = "verify"
RECOVER_VERIFY = "recover_verify"

ProviderPhase = str  # one of the constants above


@dataclass(frozen=True)
class ProviderPrompts:
    """Per-provider *pre-injection* prompt fragment injected into the shared
    U-shaped prompt.

    Only ``identity`` remains here (profile-keyed, contributed before the
    concrete backend is known). The per-method verify / recover language is
    *post-injection* and lives in the method-aware provider methods
    (``verify_prompt_note`` / ``recover_layer2_context``), because the detected
    ``injection_method`` is a strictly better key than the channel profile once
    injection has happened.

    An empty string means "contribute nothing" so the prompt assembler can skip
    a provider without special-casing it.
    """

    identity: str = ""  # role / hard-boundary fragment (see sections/identity.py)


@dataclass(frozen=True)
class RecoverResult:
    """Carrier-agnostic, deterministic (no-LLM) recovery outcome returned by
    :meth:`FaultProvider.recover`.

    A *rich* contract: it carries everything the recover node needs to assemble
    the standard ``recover_verification`` ``result_dict`` uniformly, so the
    per-backend recovery dispatch (host reverse-commands / ``blade_destroy`` /
    kubectl-exec-unreachable / non-ChaosBlade skip) lives entirely inside the
    providers while result-dict assembly, ``sync_to_store`` and tracker events
    stay in the node.

    Fields
    ------
    recovered:
        Whether the fault was verifiably undone (drives ``result['recovered']``
        and the failure gate).
    level:
        Recovery verification level: ``recovered`` | ``unrecovered`` |
        ``partial`` | ``skipped``.
    layer1 / layer2:
        The ``recover_verification`` layer dicts (``layer1`` is usually
        ``Layer1Result.model_dump()``; ``layer2`` is the skip note whose text is
        backend-specific — host reversal vs "no LLM").
    warnings:
        Human-facing warnings appended to ``recover_verification['warnings']``.
    blade_uid:
        Echoed into ``result['blade_uid']`` (empty for host / non-ChaosBlade).
    failure:
        ``(FailureCategory, detail)`` to merge via ``fail_state`` when not
        recovered; ``None`` when recovered (no failure state).
    execution_artifacts:
        Host carrier write-back list to persist (``None`` = leave untouched;
        only the host backend sets it).
    tracker_message:
        Optional override for the node's ``tracker.complete`` text.

    ``status`` / ``details`` / ``raw_output`` are retained for backward
    compatibility with lightweight callers and fixtures.
    """

    recovered: bool = False
    level: str = "skipped"
    layer1: Optional[dict] = None
    layer2: Optional[dict] = None
    warnings: tuple[str, ...] = ()
    blade_uid: str = ""
    failure: Optional[tuple] = None  # (FailureCategory, detail)
    execution_artifacts: Optional[list] = None
    tracker_message: str = ""
    # legacy / back-compat
    status: str = ""
    details: str = ""
    raw_output: str = ""


@runtime_checkable
class FaultProvider(Protocol):
    """One fault execution backend. See module docstring for the seam rationale.

    ``carrier`` is the stable backend id and the registry key. It is aligned
    with :attr:`FaultFamily.carrier_types` so ``resolve_by_scope`` can bridge an
    intent scope to its provider. ``injection_methods`` are the runtime-detected
    ``injection_method`` values this backend claims (the checkpoint-facing
    contract strings, e.g. ``("host_blade", "kubectl_exec")``).
    """

    carrier: str
    injection_methods: tuple[str, ...]
    #: Whether this backend creates a ChaosBlade-style experiment UID. Drives the
    #: recover Layer-1 label and the execute-loop method-upgrade precedence (a
    #: UID-bearing backend supersedes a UID-less one when both are detected).
    has_experiment_uid: bool
    #: Whether injection may span multiple steps with no single completion marker
    #: (kubectl-native config mutation), gating the execute-loop step-completeness
    #: nudge before a text-only exit is allowed.
    is_multi_step: bool
    #: Tool names whose successful run counts as THIS backend's injection carrier
    #: (host-shell raw commands). Declared here so ``detect`` scans by the
    #: provider's own vocabulary instead of a constant hardcoded in the execute
    #: node. Empty for backends not detected by tool-name.
    inject_tool_names: frozenset[str]
    #: kubectl subcommands whose successful post-``blade_create`` run counts as
    #: THIS backend's (kubectl-native) injection. Empty for non-kubectl backends.
    inject_kubectl_subcommands: frozenset[str]
    #: Intent vocabulary this carrier contributes to the FaultFamily aggregate:
    #: the fault target TYPES and action verbs it can execute. The family owns
    #: the scopes/cluster-scoped domain metadata and names its ``carrier_types``;
    #: the per-carrier target/action vocabulary is declared HERE (single source),
    #: so ``INTENT_TARGETS`` / ``INTENT_ACTIONS`` derive from providers rather
    #: than a flat tuple duplicated on the family.
    supported_targets: tuple[str, ...]
    supported_actions: tuple[str, ...]
    #: Executable binaries this backend needs admitted through the tool guard's
    #: Gate-① binary whitelist (``blade`` for ChaosBlade; ``kubectl`` / ``wiz``
    #: for kubectl-native; the raw host injection/recovery commands for
    #: host-shell). Declared HERE so "which binaries a backend runs" is knowledge
    #: owned by the backend, and ``ToolGuard`` assembles its default whitelist as
    #: the union of these plus its own base diagnostic set — instead of one
    #: hardcoded list divorced from the backends.
    #:
    #: SECURITY (hard boundary): this is a *knowledge-ownership* refactor, NOT a
    #: policy relaxation. The guard unions these sets verbatim — no wildcard, no
    #: auto-discovery — so a binary is admitted ONLY if some provider explicitly
    #: lists it (equivalent to manual review). Interpreters / shells
    #: (``sh`` / ``bash`` / ``python`` / ``perl``) and the guardrails themselves
    #: MUST NEVER appear here. Gate ② (solo-token / param blacklist / per-binary
    #: guards) is entirely unaffected — this only feeds Gate ①.
    injection_binaries: frozenset[str]

    def matches_channel(self, profile: str) -> bool:
        """True if this backend can operate against a ``profile`` ("k8s"|"host").

        Used pre-injection (before the concrete method is known) to decide
        which providers contribute required-params / prompt fragments.
        """
        ...

    def required_params(self, scope: str) -> list[str]:
        """Intent parameters that MUST be filled for this backend + ``scope``.

        Replaces the hardcoded ``["scope","target","action","namespace"]`` in
        the intent completeness prompt; namespace is required only for
        non-cluster-scoped k8s scopes.
        """
        ...

    def tools(self, phase: ProviderPhase) -> list["BaseTool"]:
        """LLM tools this backend contributes at ``phase``.

        Tools are bound at graph *build* time (factory unions all providers'
        tools per phase); returning ``[]`` for a phase contributes nothing.
        """
        ...

    def detect(
        self, messages: list, blade_uid: str | None, *, is_host: bool
    ) -> Optional[str]:
        """Return this backend's ``injection_method`` if its carrier is found in
        ``messages``, else ``None``. The registry arbitrates competing carriers
        by RECENCY (see :meth:`injection_recency`), using registration order
        only as a tie-breaker."""
        ...

    def injection_recency(
        self, messages: list, blade_uid: str | None, *, is_host: bool
    ) -> int:
        """Message index of this backend's most-recent injection evidence, or
        ``-1`` when its carrier is absent. The registry attributes the method
        with the highest recency (the LAST successful injection) so a later
        kubectl-/host-native fallback out-ranks an earlier, stale blade UID."""
        ...

    async def layer1_verify(self, state: dict, **kwargs) -> "Layer1Result":
        """Deterministic Layer-1 verification for this backend (e.g. blade_status,
        or ``skipped`` for carriers with no tool-level status)."""
        ...

    def verify_prompt_note(
        self, injection_method: str, *, injection_pod_name: str | None = None
    ) -> str:
        """Post-injection, per-method verifier prompt note for this backend.

        Returns the "Injection Method Note" plus any delivery-specific guidance
        (e.g. ChaosBlade's kubectl-exec BusyBox reference), or ``""`` when the
        backend has no method-specific note. Resolved via ``resolve_by_method``
        at the verifier prompt/context assembly sites — this replaces the former
        ``if injection_method == ...`` blocks in ``_verifier_messages`` /
        ``_verifier_hints``."""
        ...

    def recover_layer2_context(
        self, state: dict, layer1, *, is_deterministic: bool, blade_uid: str,
        is_host_scope: bool,
    ) -> tuple[str, str]:
        """Post-injection recover Layer-2 framing for this backend.

        Returns ``(layer1_context, layer2_instruction)`` — the ChaosBlade-vs-
        non-ChaosBlade wording that was an ``if`` tree on carrier / kubectl-exec /
        deterministic inside ``_run_layer2_verification``. ``layer1`` is the
        recover Layer-1 result; ``is_deterministic`` is the resolved
        deterministic-Layer-1 flag; ``is_host_scope`` selects host-diagnostic vs
        kubectl wording."""
        ...

    async def recover(
        self, state: dict, handle: Optional[dict], **kwargs
    ) -> RecoverResult:
        """Deterministic (no-LLM) recovery for this backend, returning a rich
        :class:`RecoverResult` the node assembles into the standard result dict.

        ``handle`` is the carrier-agnostic recovery handle
        (``build_recovery_handle``: ``{"kind": "blade_uid"|"artifact", ...}``).
        The recover seam also passes the signals the historical dispatch keyed
        on as ``kwargs``: ``blade_uid``, ``kubeconfig``, ``messages``,
        ``task_id`` — so a backend picks its own sub-variant (e.g. ChaosBlade
        derives kubectl-exec delivery from ``messages`` to choose its
        kubectl-exec-unreachable path vs local ``blade_destroy``)."""
        ...

    def prompt_fragments(self) -> ProviderPrompts:
        """Pre-injection identity fragment this backend injects into the shared prompt."""
        ...


__all__ = [
    "FaultProvider",
    "ProviderPrompts",
    "RecoverResult",
    "ProviderPhase",
    "PLAN",
    "EXECUTE",
    "VERIFY",
    "RECOVER_VERIFY",
]
