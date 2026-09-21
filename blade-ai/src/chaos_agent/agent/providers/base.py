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
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Optional, Protocol, runtime_checkable

if TYPE_CHECKING:  # imported only for typing — avoids import cycles at runtime
    from langchain_core.tools import BaseTool

    from chaos_agent.tools.request_identity import RequestFingerprint
    from chaos_agent.agent.result.verdict import Layer1Result
    from chaos_agent.agent.target_guard.types import EffectiveTarget


# Phases at which a provider may contribute LLM tools. Mirrors the tool-set
# split in ``factory.py`` (clarification/phase1/phase2/verifier/recover).
PLAN = "plan"
EXECUTE = "execute"
VERIFY = "verify"
RECOVER_VERIFY = "recover_verify"

ProviderPhase = str  # one of the constants above


def coerce_tool_args_dict(tool_args: Any) -> dict:
    """Best-effort coerce of various tool_args shapes into a dict.

    Shared by the providers' ``classify_tool_target`` hooks (phase-7 T5):
    ``tool_args`` arrives from the guard classifier in its raw shape; dict
    consumers (blade / host classifiers) pass it through here so a
    non-dict call coerces to "no arguments" instead of raising."""
    if isinstance(tool_args, dict):
        return tool_args
    return {}


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
    experiment_uid:
        The experiment UID echoed into ``result['experiment_uid']`` (legacy
        ``result['blade_uid']`` mirror; empty for host / non-ChaosBlade
        carriers).
    handle:
        The recovery identity handle the provider was dispatched with
        (echoed for result assembly / audit).
    failure:
        ``(FailureCategory, detail)`` to merge via ``fail_state`` when not
        recovered; ``None`` when recovered (no failure state).
    execution_artifacts:
        Host carrier write-back list to persist (``None`` = leave untouched;
        only the host backend sets it).
    tracker_message:
        Optional override for the node's ``tracker.complete`` text.
    """

    recovered: bool = False
    level: str = "skipped"
    layer1: Optional[dict] = None
    layer2: Optional[dict] = None
    warnings: tuple[str, ...] = ()
    experiment_uid: str = ""
    handle: Optional[dict] = None
    failure: Optional[tuple] = None  # (FailureCategory, detail)
    execution_artifacts: Optional[list] = None
    tracker_message: str = ""


@dataclass
class StepActionScan:
    """Result of a provider's ``scan_step_actions`` hook (phase-8 Form B).

    ``required`` maps each injection token this backend's vocabulary found
    in the drill steps to the step text that named it (token → step);
    ``executed`` is the set of tokens whose action reached the target
    (high tolerance: an attempt counts, only pre-execution rejections are
    excluded). The generic step self-check diffs the two and soft-reminds
    on the missing side.
    """

    required: dict[str, str]
    executed: set[str]


class DestroyOutcome(StrEnum):
    """Three-state verdict on a raw destroy output.

    Legislation for the destroy-decision family: before it, three tables
    judged the SAME raw output three different ways (the registry sweep's
    prefix table, the verify-replan retire filter's prefix pair and the
    carrier's own death-proof predicate — a non-JSON no-keyword output
    retired on one table and stayed live on another). Every framework-side
    consumer now composes the carrier's single :meth:`FaultProvider.
    classify_destroy_output` classifier.

    SUCCESS proves the destroy happened. NOT_FOUND is the failure that
    smells like the experiment being already gone — the sweep's
    convergence valve re-checks through the carrier's status face before
    surfacing the UID. FAILED is everything else: doubt is failure, a
    false retire hides a LIVE experiment from every future recovery.
    """

    SUCCESS = "success"
    NOT_FOUND = "not_found"
    FAILED = "failed"


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
    #: Whether this UID-less backend is the verdict-default carrier when fault
    #: dispatch exhausts every identity claim (no handle, no message-history
    #: evidence, no attributed method) — the carrier whose deterministic
    #: Layer-1 verdict is ``skipped``. Declared by exactly one builtin
    #: (kubectl-native); the registry's claim-4 fallback consumes this
    #: property instead of naming a provider class. Semantically defaults to
    #: ``False`` (the registry reads it via getattr), so backends that are not
    #: the fallback carrier may omit it.
    uid_less_verdict_default: bool
    #: Kind of fault handle this backend builds (``build_fault_handle``):
    #: ``"experiment_uid"`` for experiment carriers (the handle carries the
    #: experiment UID; renamed off ``"blade_uid"`` in phase-14 G7),
    #: ``"native"`` for UID-less carriers (the handle carries
    #: only the method). Empty for backends that never own a committed fault.
    handle_kind: str
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
    #: Tool names that accept a ``kubeconfig`` parameter and need the execute
    #: loop's safety-net kubeconfig injection (the channel's resolved
    #: kubeconfig is filled in when the LLM omitted it). Members MUST be
    #: tools whose signature actually declares the parameter — naming a tool
    #: without it injects a kwarg its schema rejects. Declared here so the
    #: injector consults the provider union instead of hardcoded name
    #: prefixes in the generic layer (phase-7 T2).
    kubeconfig_scoped_tool_names: frozenset[str]
    #: Tool names whose call must be bound to the graph's current
    #: ``task_id`` (runtime audit identity — never LLM-plannable). Members
    #: are the backend's tools whose signature declares ``task_id``. The
    #: audit-binding pass unions this across providers.
    audit_scoped_tool_names: frozenset[str]
    #: Tool names whose completed runs are shipped to the L4 SLS log channel
    #: by the observability execution hook. Unioned across providers for the
    #: log-shipping pass.
    log_shipping_tool_names: frozenset[str]
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
    #: Namespaces hosting this backend's injection-infrastructure pods
    #: (e.g. the ChaosBlade Operator's tool pods). Tier-1 exemption data:
    #: an outer exec into one of these namespaces is access to the injection
    #: MACHINERY — the actual target's namespace legitimately differs from
    #: the tool pod's — so the cross-namespace drift check (``drift_policy``)
    #: and the inline Tier-1 carrier detection union this across providers
    #: instead of hardcoding carrier namespaces in the generic layer.
    #: Backends without dedicated infra pods declare the empty set.
    tool_pod_namespaces: frozenset[str] = frozenset()
    #: Tool names whose RESULT-UNCERTAIN outcomes arm the create-reconcile
    #: gate: a create whose transport failed ambiguously (timeout /
    #: transient no-UID error) leaves the executor not knowing whether the
    #: experiment was created, and a blind retry of a NON-IDEMPOTENT create
    #: can materialise a duplicate experiment on the same target. The
    #: generic gate (``agent/nodes/execute/_reconcile_gate.py``) scans and
    #: intercepts by the provider UNION of this attribute
    #: (:meth:`FaultProviderRegistry.union_tool_names`) instead of
    #: hardcoded tool names. Backends whose creates are retry-safe (or that
    #: create no experiment record at all) declare the empty set and are
    #: invisible to the gate.
    reconcile_create_tool_names: frozenset[str] = frozenset()
    #: Read-only tool names whose execution after a result-uncertain create
    #: counts as reconciliation for THIS backend's gate — the release
    #: condition that lets an honest post-reconcile retry through instead
    #: of intercepting it (spec: 宽进). Members may be other carriers' or
    #: generic read tools the LLM can legitimately reconcile with: the
    #: declaration owns the judgement "this read resolves MY uncertain
    #: create", not the tool itself. Unioned across providers for the
    #: whitelist scan.
    reconcile_read_tool_names: frozenset[str] = frozenset()
    #: Tool names whose RESULT SHAPE this backend owns the failure verdict
    #: for. The framework's generic verdict reads ``status == "error"`` and
    #: the textual ``Error`` / ``[target_guard]`` renderings; a tool that
    #: reports failures STRUCTURALLY (a JSON receipt) is invisible to both,
    #: so the carrier that can read its own receipt declares the tool here
    #: and answers :meth:`tool_result_error_text`. Consulted through
    #: ``agent/tool_verdicts.py`` — the single source every failure-detection
    #: consumer routes through, so a new JSON-shaped tool needs a
    #: declaration, not a new ``if name == ...`` branch in each consumer.
    #: Text-dialect tools declare the empty set and stay on the generic path.
    result_shape_tool_names: frozenset[str] = frozenset()

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
        self, messages: list, *, is_host: bool, is_teardown=None,
    ) -> Optional[str]:
        """Return this backend's ``injection_method`` if its carrier is found in
        ``messages``, else ``None``. The registry arbitrates competing carriers
        by RECENCY (see :meth:`injection_recency`), using registration order
        only as a tie-breaker.

        ``is_teardown`` (P3 matcher threading): the teardown≠mutation
        exemption closure (``execution_artifacts.make_teardown_matcher``).
        Kubectl-vocabulary carriers thread it into their scans; carriers
        whose evidence can never be a registered-vehicle delete accept and
        ignore it (protocol uniformity — the registry passes it to every
        backend)."""
        ...

    def injection_recency(
        self, messages: list, *, is_host: bool, is_teardown=None,
    ) -> int:
        """Message index of this backend's most-recent injection evidence, or
        ``-1`` when its carrier is absent. The registry attributes the method
        with the highest recency (the LAST successful injection) so a later
        kubectl-/host-native fallback out-ranks an earlier, stale blade UID."""
        ...

    def build_fault_handle(self, values: dict) -> Optional[dict]:
        """Build this backend's fault handle from attribution facts, or ``None``
        when the facts do not describe a fault THIS backend owns.

        ``values`` is a state-like mapping (may carry the legacy attribution
        fields only — pre-handle checkpoints never have ``fault_handle``). The
        returned shape is owned by the backend: experiment carriers return
        ``{"kind": "experiment_uid", "value": <uid>, "method": <method>}``; UID-less
        carriers return ``{"kind": "native", "method": <method>}``. Consulted
        by ``FaultProviderRegistry.derive_handle_from_legacy`` — the single
        hydration seam for legacy checkpoints and persisted snapshots.
        """
        ...

    def build_handle_from_messages(
        self, messages: list, retired: object = None, values: Optional[dict] = None
    ) -> Optional[dict]:
        """Defense-in-depth hydration: build this backend's handle from live
        experiment evidence found in ``messages``, or ``None``.

        Used only when neither the durable ``fault_handle`` nor the legacy
        attribution facts are present (heavily compacted / legacy
        checkpoints). Only meaningful for ``has_experiment_uid`` backends;
        UID-less backends never claim here. Consulted via
        ``FaultProviderRegistry.derive_handle_from_messages``."""
        ...

    def extract_experiment_id(
        self, messages: list, retired: object = None
    ) -> str:
        """Extract this backend's live experiment id from ``messages``, or ``""``.

        Only meaningful for ``has_experiment_uid`` backends; the registry calls
        it from ``extract_experiment_uid`` so the execute loop's attribution
        sync never names a carrier-specific extractor. ``retired`` filters ids
        already destroyed by framework-side cleanup."""
        ...

    def created_experiment_ids(self, messages: list, state: dict) -> set[str]:
        """Every experiment id this backend's create results prove THIS task
        created, unioned with the backend's durable record in ``state``.

        Includes terminal create failures — their CRDs still need cleanup even
        though they never count as an active experiment. Consulted by the tool
        screener's destroy-provenance gate through the registry aggregate so
        generic nodes never name a carrier-specific extractor. UID-less
        backends have no experiment ids to prove; they return the empty set."""
        return set()

    def destroyed_experiment_ids(self, messages: list) -> set[str]:
        """Every experiment id this backend's destroy tool calls have
        TARGETED — the terminal-state attribution scan, union-aggregated by
        the registry (issued = terminal: an attempted destroy must stop the
        UID being re-claimed as the live fault, whether or not the output
        confirms the kill). Only meaningful for ``has_experiment_uid``
        backends; UID-less backends return the empty set."""
        return set()

    def destroyed_proven_experiment_ids(self, messages: list) -> set[str]:
        """Destroys whose PAIRED tool output PROVES the kill — the death
        registration feed for the retire ledger.

        Stricter twin of :meth:`destroyed_experiment_ids` on purpose: a
        retire excludes a UID from every live-liability read, so only an
        output-proven death may register — a false entry hides a LIVE
        experiment (strictly worse than the orphan the sweep exists to
        prevent). Only meaningful for ``has_experiment_uid`` backends;
        UID-less backends return the empty set."""
        return set()

    def classify_tool_target(
        self, tool_name: str, tool_args: Any, raw_command: str
    ) -> Optional["EffectiveTarget"]:
        """Guard-side classification of a tool call into an ``EffectiveTarget``.

        TargetGuard seam (phase-7 T5): the generic classifier
        (``classifier.infer_effective_target``) dispatches each tool call
        through ``FaultProviderRegistry.classify_tool_target`` so every
        carrier names and classifies its OWN tools — including its
        read-only ones (each provider may short-circuit them to
        ``SCOPE_READONLY`` here). ``tool_args`` arrives in its raw shape
        (dict / list / str / None); each provider coerces it to the form
        its classifier needs. ``raw_command`` is the audit-facing rendering
        pre-computed by the generic layer. ``None`` (the default) means the
        call is not this backend's tool and the registry scan continues.
        Channel-independent: the guard classifies the tool_call itself, so
        no ``is_host`` gate applies. Migration is a pure move — behaviour
        is byte-identical (the guard is a security layer)."""
        return None

    def tool_result_error_text(
        self, tool_name: str, content: str
    ) -> Optional[str]:
        """This backend's failure verdict on one of its OWN tool results.

        Return the failure evidence (the message a classifier should read)
        when the result IS a failure, ``None`` otherwise. ``None`` is an
        ABSTENTION, not a success verdict: it covers "this shape is not a
        failure" and "I do not recognise this shape" alike, and callers must
        not invert it — a compacted receipt is JSON-shaped but unparseable,
        and unknown is not success. The default abstains on everything;
        only tools declared in :attr:`result_shape_tool_names` are ever
        routed here (``agent/tool_verdicts.provider_error_text``)."""
        return None

    def parse_injection_params(
        self, tool_name: str, tool_args: dict
    ) -> Optional[dict]:
        """Structured injection parameters this backend can parse from a
        freshly ISSUED tool call, for the verifier / recover context.

        Issue-time extraction: the execute loop consults the registry right
        before dispatch, so each backend recognises its OWN tool-call forms
        (the ChaosBlade provider parses both ``blade_create`` flags and the
        ``kubectl exec ... blade create`` embedded form). ``None`` (the
        default) means the call is not this backend's injection carrier; an
        empty dict means the carrier is recognised but carries no key
        parameters — the execute loop writes nothing in either case.
        Consulted via ``FaultProviderRegistry.parse_injection_params`` so the
        generic loop never names a carrier-specific parser."""
        return None

    def issue_time_method(
        self, tool_name: str, tool_args: dict, *, is_host: bool
    ) -> Optional[str]:
        """Map a freshly-issued tool call to the ``injection_method`` it enacts.

        Issue-time attribution (Direction B): the method is recorded the
        moment the injection is ISSUED (from the AIMessage tool_call), not
        reverse-reconstructed from history later. Each backend recognises
        its OWN carrier forms — the direct tool call plus any embedded
        delivery it rides (e.g. ChaosBlade owns both ``blade_create`` and
        the ``kubectl exec ... blade create`` fallback). ``None`` (the
        default) means the call is not this backend's injection carrier and
        the registry scan continues. ``is_host`` carries the resolved
        channel so host carriers only claim on a host channel. Consulted
        via ``FaultProviderRegistry.issue_time_method`` so the generic
        classifier never names a carrier-specific tool or method."""
        return None

    def build_reconcile_fingerprint(
        self, tool_name: str, tool_args: Any
    ) -> Optional["RequestFingerprint"]:
        """Request identity (four-dimension fingerprint) for a create tool
        call this backend recognises, or ``None`` when the call is not this
        backend's create tool.

        Create-reconcile seam (blade-create-reconcile-before-retry D6):
        the generic gate (``agent/nodes/execute/_reconcile_gate.py``) owns
        the three-state scan / interception / release / cap state machine;
        WHICH argument keys form the request identity and how the raw LLM
        argument shapes normalise (CSV vs list, dict vs CSV, key-face
        lowercasing) is carrier judgment material — the ChaosBlade
        provider normalises the unified scope/target/action key face into
        the same fingerprint construction safety_check's conflict query
        consumes. Consulted via
        :meth:`FaultProviderRegistry.build_reconcile_fingerprint` so the
        generic scan never names a carrier-specific tool or argument key.
        Backends outside the gate (empty ``reconcile_create_tool_names``)
        pin ``None``."""
        return None

    async def reconcile_hold_feedback(
        self,
        tool_name: str,
        fp: "RequestFingerprint",
        hold_count: int,
        block_limit: int,
        kubeconfig: str = "",
        task_id: str = "",
    ) -> Optional[tuple[str, bool]]:
        """Interception-time cluster probe plus hold-feedback text for a
        held create retry this backend recognises, or ``None`` when the
        call is not this backend's create tool.

        Returns ``(feedback_text, counts_as_reconciliation)``: the text is
        the fabricated ToolMessage body for the held create
        (GuardFeedback semantics — reason / fix / not-a-ban — headed by
        ``GATE_RECONCILE_BLOCKED_MARKER`` so the generic three-state scan
        recognises it as never-executed), and the flag says whether the
        probe COMPLETED (a completed probe reconciles: the next retry is
        released). The probe itself — which cluster query answers "is my
        uncertain create already in effect", and which scopes CANNOT be
        probed (host-scope experiments record on the host's local DB, not
        cluster CRDs) — is carrier judgment material. Consulted via
        :meth:`FaultProviderRegistry.reconcile_hold_feedback` so the
        generic gate never names a carrier-specific query or tool.
        Backends outside the gate pin ``None``."""
        return None

    def reconcile_batch_held_feedback(
        self, tool_name: str, other_tool_name: str
    ) -> Optional[str]:
        """Fabricated notice for the OTHER calls of a batch held back
        together with a held create (``tool_name`` = the create that held
        the batch; ``other_tool_name`` = the call being answered), or
        ``None`` when ``tool_name`` is not this backend's create tool.

        The text names the carrier's reconciliation tools and must carry
        the "was NOT executed" wording so the generic three-state scan
        treats it as never-executed (``status="error"`` is set by the
        generic caller). Text content is carrier judgment material, hence
        a hook rather than a generic template. Consulted via
        :meth:`FaultProviderRegistry.reconcile_batch_held_feedback`.
        Backends outside the gate pin ``None``."""
        return None

    def scan_step_actions(
        self, steps: list[str], messages: list, *, is_teardown=None,
    ) -> Optional[StepActionScan]:
        """OPTIONAL hook (phase-8 Form B): scan drill ``steps`` + execution
        ``messages`` with THIS backend's injection vocabulary and report the
        required-vs-executed action sets for the step self-check.

        ``None`` (the default) = this carrier does not participate in the
        step self-check. Native carriers implement it with their own token
        vocabulary (kubectl write verbs, host injection binaries);
        experiment-UID carriers explicitly decline — completion is judged
        by the experiment evidence chain (the UID), not step-verb
        heuristics. Third parties may omit the hook entirely (the generic
        caller getattr-skips a missing hook)."""
        return None

    def was_injection_attempted(self, messages: list, *, is_teardown=None) -> bool:
        """OPTIONAL hook (phase-8 Form B): back-scan the message history for
        whether THIS backend's native injection was attempted at all (e.g.
        a mutating ``kubectl exec`` fallback after a failed experiment
        create).

        Semantic distinction vs :meth:`was_fault_create_attempted`
        (orthogonal, not duplication): THAT hook is the experiment
        carrier's terminal "attempted-but-no-UID" state judgement
        (recover Layer-1 routing; UID-less carriers pin ``False``); THIS
        hook is the native carrier's "did a native injection happen"
        message back-scan. Defaults to ``False`` — a backend without a
        back-scan vocabulary never claims one."""
        return False

    def issue_disproven(self, messages: list, *, is_teardown=None) -> bool:
        """True when ``messages`` carry EXPLICIT counter-evidence that this
        backend's issue-time (channel A) attribution provably never committed.

        Issue-time attribution records the injection at the moment the tool
        call is issued, before the result exists; this hook is the revocation
        seam consulted by the execute loop when such an attribution is live.
        Counter-evidence must be explicit — a trustworthy result proving the
        mutation never landed. Absence of a result (pending call, severed
        feedback channel) is NEVER counter-evidence, and carriers whose
        results are untrustworthy by nature (the forensic paradox: the fault
        severs its own carrier channel) must return False unconditionally.
        Experiment carriers key attribution on the UID (result-born), so they
        have nothing to revoke. Default False."""
        return False

    async def rollback_handle(self, handle: dict, **kwargs) -> str:
        """Best-effort deterministic rollback of a committed fault described by
        ``handle`` (failure-path auto-rollback), returning a human-readable
        status suffix ("" = nothing rolled back). Backends without a
        deterministic undo (native carriers) return "" — their faults are the
        recover graph's job, not a synchronous rollback."""
        ...

    def was_fault_create_attempted(
        self, messages: list, injection_method: str | None = None,
        *, is_teardown=None,
    ) -> bool:
        """Whether this backend's experiment CREATE was attempted but no live
        experiment (UID) resulted — the terminal "attempted and failed, nothing
        to recover by UID" state.

        Experiment carriers override this with their own combination
        judgement (durable attribution first, then message evidence).
        UID-less carriers MUST keep the default ``False``: they create no
        experiment record at all, so "attempted-but-no-UID" is semantically
        impossible for them, not merely unimplemented — a True here would
        mis-route recover Layer 1 into the terminal failed branch and steer
        the verify warning path the same way."""
        return False

    async def layer1_verify(self, state: dict, **kwargs) -> "Layer1Result":
        """Deterministic Layer-1 verification for this backend (e.g. blade_status,
        or ``skipped`` for carriers with no tool-level status)."""
        ...

    async def layer1_destroy(
        self, uid: str, kubeconfig: str = "", *, messages: list | None = None,
        injection_method: str | None = None, artifacts: list | None = None,
    ) -> "Layer1Result":
        """Deterministic Layer-1 RECOVERY execution for this backend (the
        experiment destroy + destroyed-state verification), consumed by the
        recover nodes' LLM flow before Layer 2.

        Experiment carriers run their own destroy pipeline; a backend with
        nothing to destroy deterministically returns ``skipped``.
        ``uid`` empty mirrors :meth:`run_layer1_destroy`'s semantics: a
        create-attempted-but-UID-less state is ``failed`` (terminal), a
        UID-less fault is ``skipped`` (Layer 2 proceeds). A UID-less carrier
        can still own a deterministic replay — it hydrates its recovery
        identity from the evidence arguments below instead of from ``uid``.

        ``messages`` and ``artifacts`` are the two evidence sources for that
        hydration, and both are passed because neither alone survives every
        recover entry: ``messages`` is the inject history, which a CROSS-TASK
        recover (``blade-ai recover --task-id``) does not inherit at all —
        the recover-state builder in ``state_mgmt.recovery_state`` flattens it
        into the ``inject_context`` string — and which ``memory.tool_compactor``
        truncates outside the recent window even within one task;
        ``artifacts`` is ``state["execution_artifacts"]``, the durable ledger
        that DOES cross both boundaries. A backend that records its recovery
        identity in the ledger reads it from here; one that does not leaves
        the argument unused. Neutral by design — the list is generic state,
        and recognising any particular artifact type stays provider-side."""
        ...

    async def layer1_raw_destroy(self, uid: str, kubeconfig: str = "") -> str:
        """Bare deterministic destroy for the Layer-2 still-active RETRY path:
        a single destroy invocation returning its raw output, with NO
        destroyed-state verification (the retry prompt only needs the destroy
        output to re-verify against). Only experiment carriers override this;
        the default ``""`` means nothing was destroyed programmatically."""
        return ""

    def classify_destroy_output(self, output: str) -> "DestroyOutcome":
        """Three-state verdict on a raw :meth:`layer1_raw_destroy` output —
        the single destroy-decision source for every framework-side
        consumer (the registry sweep's retire/failure fork and the
        verify-replan retire filter both compose it, so the decision tables
        can never drift apart again).

        Only experiment carriers implement this; the default is fail-closed
        FAILED. NOT_FOUND is the convergence-valve signal: the sweep gives
        the carrier one status-face re-check before surfacing the UID."""
        return DestroyOutcome.FAILED

    def recovery_vehicle(self, state: dict) -> str:
        """Durable recovery-vehicle locator this backend recorded at injection
        time (e.g. the tool pod a kubectl-exec delivery ran in), or ``""``.

        Consumed by the generic recover/verify context rendering so those
        flows never name a carrier-specific state field. UID-less carriers
        have no vehicle record and return ``""``."""
        ...

    #: Whether this backend owns a programmatic Layer-1 recovery (a
    #: deterministic destroy of its own experiment through ``layer1_destroy``).
    #: Experiment carriers (blade family) are True; UID-less native carriers
    #: are False — the LLM-driven undo flow IS their Layer 1.
    has_deterministic_recover: bool

    def blocks_deterministic_destroy(
        self, state: dict, messages: list | None = None
    ) -> bool:
        """True when this backend's deterministic destroy CANNOT run even
        though its experiment handle is live: the experiment was delivered
        through a channel the backend's own destroy tooling cannot reach
        (e.g. an in-cluster exec delivery), so the LLM-driven Layer-1 flow is
        the only recover vehicle. Consulted by the generic recover routing so
        it never names a carrier-specific delivery."""
        ...

    def recovery_facts_render(
        self, state: dict, *, spec_params: dict | None = None
    ) -> str:
        """Render this backend's own injection-fact lines for the Layer-1
        recovery context (e.g. the experiment UID, carrier-parsed operation
        parameters), each as a ``Label: value\n`` line. ``spec_params`` is
        the neutral fault-spec parameter set the generic layer already
        renders — backends skip their parsed-parameter line when the spec
        already carries them. Empty string contributes nothing."""
        ...

    def merge_deterministic_recover_verdict(
        self, layer1, state: dict, part_override: dict | None = None
    ):
        """Merge this backend's deterministic destroy verdict (its experiment,
        destroyed programmatically before the LLM flow) into the LLM's
        Layer-1 verdict, producing the composite result. Backends with no
        deterministic part return ``layer1`` unchanged."""
        ...

    def layer1_recover_guidance(
        self, state: dict, experiment_uid: str, *,
        combo_native: bool = False, combo_part: dict | None = None,
    ) -> str:
        """Layer-1 recovery guidance appended to the inject context when the
        experiment needs LLM-driven recovery (an in-cluster delivery the
        backend cannot destroy programmatically, or a combo whose experiment
        part was already destroyed deterministically). Empty string
        contributes nothing."""
        ...

    def layer2_facts_note(self, state: dict) -> str:
        """Carrier-owned fact lines for the recover Layer-2 verification
        context (e.g. parsed operation parameters and the auto-expiry note
        derived from them). Empty string contributes nothing."""
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
        self, state: dict, layer1, *, is_deterministic: bool, experiment_uid: str,
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
        (``build_recovery_handle``: ``{"kind": "experiment_uid"|"artifact"|"native", ...}``).
        The recover seam also passes the signals the historical dispatch keyed
        on as ``kwargs``: ``experiment_uid``, ``kubeconfig``, ``messages``,
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
    "StepActionScan",
    "ProviderPhase",
    "PLAN",
    "EXECUTE",
    "VERIFY",
    "RECOVER_VERIFY",
]
