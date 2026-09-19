"""FaultFamily registry — the extensibility seam for fault types.

A :class:`FaultFamily` groups the scopes that belong to one fault domain (K8s +
ChaosBlade today, host tomorrow, cloud API later) together with the ordered
*carriers* that can execute it. The per-carrier target/action vocabulary is NOT
stored on the family — each carrier declares it once in its lightweight
``declaration`` module (``providers/<carrier>/declaration.py``), and the
providers assembly point registers it here via
``declare_carrier_vocabulary`` at package-import time, so a carrier's words
live in exactly one place.

``INTENT_SCOPES`` / ``INTENT_TARGETS`` / ``INTENT_ACTIONS`` in ``fault_spec.py``
are DERIVED from this registry. Adding a new fault type therefore means
registering a family here (scopes + carriers), declaring the vocabulary in
the carrier's ``declaration.py`` and registering it at the providers assembly
point — not editing hardcoded whitelists scattered across the CLI, HTTP
schema, LLM schema and prompt layers.

Design note
-----------
Aggregation preserves *registration order* and de-duplicates on first
occurrence, so the built-in ``k8s_chaosblade`` vocabulary keeps its historical
ordering (some consumers surface these as ordered enums / prompt strings).
Target/action aggregation walks each family's ``carrier_types`` in precedence
order; new families only ever *append* previously-unseen values.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from chaos_agent.transports import PROFILE_HOST, PROFILE_K8S

# Per-carrier intent vocabulary, DECLARED by each carrier's lightweight
# ``declaration`` module and registered here through the providers package
# assembly point (``chaos_agent.agent.providers`` imports every declaration
# module at package-import time and calls ``declare_carrier_vocabulary``).
# Registration data only — no provider module is imported here (phase-12
# spec-import-retirement), so this family catalogue stays a pure knowledge
# module. ``fault_spec`` imports the assembly point before deriving
# ``INTENT_TARGETS`` / ``INTENT_ACTIONS`` at import time; the defensive
# ``_ensure_carrier_vocab`` below re-triggers assembly for callers that
# imported this module directly.
_CARRIER_VOCAB: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
_CARRIER_VOCAB_ASSEMBLED = False

#: Command-preview builders, registered per carrier (phase-12 D3). Keyed by
#: carrier id — same registration channel as the vocabulary above.
_PREVIEW_BUILDERS: dict[str, Callable[..., str]] = {}


def declare_carrier_vocabulary(
    carrier: str,
    targets: tuple[str, ...],
    actions: tuple[str, ...],
) -> None:
    """Register (or replace) a carrier's intent vocabulary declaration."""
    global _CARRIER_VOCAB_ASSEMBLED
    _CARRIER_VOCAB[carrier] = (tuple(targets), tuple(actions))
    _CARRIER_VOCAB_ASSEMBLED = True


def declare_command_preview(carrier: str, builder: Callable[..., str]) -> None:
    """Register (or replace) a carrier's ``## Injection Command`` preview
    builder (phase-12 D3 — the spec layer's plan generator resolves preview
    construction through :func:`command_preview_for` instead of importing
    the carrier implementation)."""
    _PREVIEW_BUILDERS[carrier] = builder


def command_preview_for(scope: str, **spec_fields) -> str | None:
    """Render the ``## Injection Command`` preview for a spec (phase-12 D3).

    Resolves the scope's family and walks its ``carrier_types`` in
    precedence order, delegating to the first carrier that registered a
    preview builder. Returns ``None`` when the scope has no family or no
    carrier declared a builder — the plan generator renders no section for
    that case (matching the former hardcoded behaviour, which only ever
    served blade-shaped specs). Carriers that never produce command
    previews simply do not register a builder; no special-casing here.

    Note: builders receive ``params`` as-is and MAY mutate it (the blade
    builder's single ``--timeout`` guarantee rewrites a pre-existing
    ``timeout`` key). Callers must pass a copy they own — e.g.
    ``dict(spec.params)`` — never a dict they expect to stay untouched.
    """
    _ensure_carrier_vocab()
    family = family_for_scope(scope)
    if family is None:
        return None
    for carrier in family.carrier_types:
        builder = _PREVIEW_BUILDERS.get(carrier)
        if builder is not None:
            return builder(scope=scope, **spec_fields)
    return None


def _ensure_carrier_vocab() -> None:
    """Defensive assembly trigger for direct importers.

    ``fault_spec`` triggers assembly explicitly; any caller that imports
    this module directly (bypassing fault_spec) and asks for vocabulary
    before assembly would otherwise see an empty aggregate. Lazy import
    keeps the module-level dependency graph acyclic: this module has no
    module-level providers import, and the assembly point only reads the
    ``declare_*`` functions defined above.
    """
    if _CARRIER_VOCAB_ASSEMBLED:
        return
    import chaos_agent.agent.providers  # noqa: F401 — assembly side effect
    assert _CARRIER_VOCAB_ASSEMBLED, (
        "providers assembly point imported without declaring carrier "
        "vocabulary — INTENT_* derivation would be empty"
    )


@dataclass(frozen=True)
class FaultFamily:
    """One fault domain and how it is executed / recovered.

    Attributes
    ----------
    family_id:
        Stable identifier, e.g. ``"k8s_chaosblade"`` or ``"host"``.
    scopes:
        The scope vocabulary this family contributes. The *target* / *action*
        vocabulary is NOT declared here — it is owned per-carrier by the
        carrier's ``declaration`` module (the provider class reads the same
        constants as its ``supported_targets`` / ``supported_actions``) and
        registered with this registry via the providers assembly point, so a
        carrier's words live in exactly one place.
    carrier_types:
        Ordered candidate execution backends (provider ``carrier`` ids) that
        can serve this family's scopes, in precedence order. Pre-injection the
        LLM has not yet chosen a backend, so a single scope maps to *candidates*
        (e.g. a k8s scope may be served by ``chaosblade`` OR ``k8s_native``);
        the exact backend is pinned post-injection by ``resolve_by_method``.
        Each entry MUST equal a registered provider's ``carrier``. It is also
        the key into the per-carrier target/action vocabulary.
    cluster_scoped:
        Scopes that do NOT carry a Kubernetes namespace (``node`` …) or that
        are namespace-less by nature (``host``). Used by ``FaultSpec`` to
        decide whether an empty namespace is legitimate.
    profile:
        The capability *profile* of this family — the single source that
        ``profile_of_scope`` (and thus ``profile_for_spec``) resolves to. It is
        a free-form string (``"k8s"`` / ``"host"`` today, a third environment
        tomorrow), NOT a boolean, so the profile axis is genuinely N-ary rather
        than "host vs. everything-else". A new family declares its own profile;
        no downstream ``== PROFILE_K8S`` branch needs editing.
    """

    family_id: str
    scopes: tuple[str, ...]
    carrier_types: tuple[str, ...]
    cluster_scoped: tuple[str, ...] = ()
    profile: str = PROFILE_K8S


# Registration-ordered registry (dict preserves insertion order).
_REGISTRY: dict[str, FaultFamily] = {}


def register_family(family: FaultFamily) -> None:
    """Register (or replace) a fault family by ``family_id``."""
    _REGISTRY[family.family_id] = family


def get_family(family_id: str) -> FaultFamily | None:
    return _REGISTRY.get(family_id)


def all_families() -> tuple[FaultFamily, ...]:
    return tuple(_REGISTRY.values())


def family_for_scope(scope: str) -> FaultFamily | None:
    """Return the first family that owns ``scope`` (registration order)."""
    for family in _REGISTRY.values():
        if scope in family.scopes:
            return family
    return None


def _dedup_ordered(values: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return tuple(out)


def _aggregate(attr: str) -> tuple[str, ...]:
    collected: tuple[str, ...] = ()
    for family in _REGISTRY.values():
        collected += getattr(family, attr)
    return _dedup_ordered(collected)


def _aggregate_carrier_vocab(index: int) -> tuple[str, ...]:
    """Aggregate a per-carrier vocabulary across families.

    Walks each family (registration order) and, within it, each
    ``carrier_types`` entry (precedence order), reading the registered
    declaration tuple (``index`` 0 = targets, 1 = actions). First-occurrence
    dedup is preserved, so the derived INTENT vocabulary is byte-identical to
    the former flat tuple that lived on the family — only its *source* moved
    to the carriers.
    """
    _ensure_carrier_vocab()
    collected: tuple[str, ...] = ()
    for family in _REGISTRY.values():
        for carrier in family.carrier_types:
            collected += _CARRIER_VOCAB.get(carrier, ((), ()))[index]
    return _dedup_ordered(collected)


def carrier_targets(carrier: str) -> tuple[str, ...]:
    """Fault target TYPES declared by ``carrier`` (empty tuple if unknown)."""
    _ensure_carrier_vocab()
    return _CARRIER_VOCAB.get(carrier, ((), ()))[0]


def carrier_actions(carrier: str) -> tuple[str, ...]:
    """Fault action verbs declared by ``carrier`` (empty tuple if unknown)."""
    _ensure_carrier_vocab()
    return _CARRIER_VOCAB.get(carrier, ((), ()))[1]


def aggregate_scopes() -> tuple[str, ...]:
    return _aggregate("scopes")


def aggregate_targets() -> tuple[str, ...]:
    return _aggregate_carrier_vocab(0)


def aggregate_actions() -> tuple[str, ...]:
    return _aggregate_carrier_vocab(1)


def aggregate_cluster_scoped() -> frozenset[str]:
    return frozenset(_aggregate("cluster_scoped"))


# The bare-host execution channel is identified by the ``host_shell`` carrier.
# Kept as the single literal so "which scopes are bare-host" is derived from the
# registry rather than a ``== "host"`` string duplicated across guard / classifier
# / recover / blade call sites.
_HOST_CARRIER = "host_shell"


def host_scopes() -> frozenset[str]:
    """Scopes that target a bare host (SSH/local channel) rather than a cluster
    resource — i.e. scopes of any family served by the ``host_shell`` carrier.

    Data-driven from the registry: registering a new host-family scope makes it
    a recognised host scope everywhere ``is_host_scope`` is used, with no call
    site edits.
    """
    return frozenset(
        scope
        for family in _REGISTRY.values()
        if _HOST_CARRIER in family.carrier_types
        for scope in family.scopes
    )


def is_host_scope(scope: str | None) -> bool:
    """True if ``scope`` runs against a bare host (see :func:`host_scopes`).

    Replaces the scattered ``scope == "host"`` scope checks with one
    registry-derived predicate. It is deliberately a *scope* test — distinct
    from a transport-channel ``profile == "host"`` test (use
    ``transports.profile_of`` for that), so the two semantics never get merged.
    """
    return bool(scope) and scope in host_scopes()


# The in-process Python application channel is identified by the
# ``chaosblade_python`` carrier. Kept as the single literal so "which scopes are
# in-process application faults" is derived from the registry rather than a
# ``== "python"`` string duplicated across the execute / classifier call sites.
_PYTHON_CARRIER = "chaosblade_python"


def python_scopes() -> frozenset[str]:
    """Scopes whose faults live INSIDE an application process (injected by the
    ChaosBlade Python agent) rather than in an OS / cluster resource — i.e.
    scopes of any family served by the ``chaosblade_python`` carrier."""
    return frozenset(
        scope
        for family in _REGISTRY.values()
        if _PYTHON_CARRIER in family.carrier_types
        for scope in family.scopes
    )


def is_python_scope(scope: str | None) -> bool:
    """True if ``scope`` is an in-process application fault (see
    :func:`python_scopes`).

    Distinct from :func:`is_host_scope`: both resolve to the ``host`` capability
    *profile* (the injection command runs on the machine hosting the process),
    but an in-process fault modifies no OS state, so the execute / verify paths
    must not apply host-resource logic to it.
    """
    return bool(scope) and scope in python_scopes()


def profile_of_scope(scope: str | None) -> str:
    """Return the capability profile of ``scope`` (the family's ``profile``).

    Single N-ary source for the profile axis: every ``profile_for_spec`` and
    guard cross-profile comparison resolves through here, so a family that
    declares a third profile is honoured everywhere without touching a binary
    ``host vs. k8s`` branch. Unknown scopes fall back to :data:`PROFILE_K8S`
    (the historical default), keeping the kubectl-flavored path for misrouted
    input rather than failing closed.
    """
    family = family_for_scope(scope) if scope else None
    return family.profile if family else PROFILE_K8S


def is_memory_burn_scope(scope: str | None, target: str | None) -> bool:
    """True for the pod-memory fault shape whose downstream logic is
    memory-burn specific (pod memory-limit prefetch + OOMKill risk warning).

    Single source for the ``scope == "pod" and target == "mem"`` gate that
    used to be duplicated across the setup / execute nodes (pod memory-limit
    prefetch + OOMKill risk warning). Comparison is exact — callers pass the
    same (already-normalised) operands they compared before, so the gate's
    behaviour is unchanged.
    """
    return scope == "pod" and target == "mem"


# Scopes that name a workload (a controller/pod that runs containers), as
# opposed to cluster-scoped kinds (RBAC/storage) or owner scopes
# (replicaset/job/cronjob). Single source for the ``scope in ("pod",
# "deployment", "statefulset", "daemonset")`` workload test used by the guard's
# freeze snapshot. Deliberately *not* merged with the guard's
# ``CLUSTER_SCOPED_KINDS`` / ``OWNER_SCOPES`` — those cover different domains
# (non-fault cluster resources, ownership traversal) and share only incidental
# members.
WORKLOAD_SCOPES: frozenset[str] = frozenset({
    "pod", "deployment", "statefulset", "daemonset",
})


def is_workload_scope(scope: str | None) -> bool:
    """True if ``scope`` names a workload (see :data:`WORKLOAD_SCOPES`)."""
    return bool(scope) and scope in WORKLOAD_SCOPES


def required_intent_params(scope: str | None) -> list[str]:
    """Backend-agnostic required intent parameters for a ``scope``.

    The ``(scope, target, action)`` triple is always required. ``namespace`` is
    required only for non-cluster-scoped scopes — cluster-scoped scopes
    (``node`` / ``host`` / ``pv`` …, declared per :class:`FaultFamily`) are
    namespace-less.

    This is the SINGLE source shared by ``FaultSpec.is_complete``, every
    provider's ``required_params`` and the intent completeness prompt, so the
    three never disagree about when a namespace is *missing* versus *absent by
    nature*. It is deliberately scope-driven (not provider-driven): pre-injection
    the execution backend is unknown, and a single scope may be served by
    several backends — but the namespace rule is a property of the scope, not of
    any one carrier."""
    base = ["scope", "target", "action"]
    if scope and scope not in aggregate_cluster_scoped():
        base.append("namespace")
    return base


# ---------------------------------------------------------------------------
# Built-in families
# ---------------------------------------------------------------------------

# K8s + ChaosBlade — the historical vocabulary (order preserved verbatim).
# Targets / actions are NOT listed here: they are aggregated from the family's
# ``carrier_types`` (``chaosblade`` → OS subsystems + ChaosBlade verbs;
# ``k8s_native`` → kubectl resource types + mutation verbs), preserving the
# original concatenation order (chaosblade first, then k8s_native).
#
# ``faultdrill_cr`` (openspec faultdrill-cr-channel) sits LAST with an
# intentionally EMPTY vocabulary declaration: routing into the CR channel
# is metadata-driven (skill-case ``recovery_channel`` + planning + write-set
# validation), never scope-bridged, so INTENT_TARGETS / INTENT_ACTIONS stay
# byte-identical whether or not the channel is registered (dark launch:
# ``resolve_by_scope`` skips carriers with no registered provider).
register_family(
    FaultFamily(
        family_id="k8s_chaosblade",
        scopes=(
            "pod", "node", "container",
            "deployment", "statefulset", "daemonset",
            "service",
        ),
        carrier_types=("chaosblade", "k8s_native", "faultdrill_cr"),
        # NOTE (R21/G-5): this is a DELIBERATE parallel declaration, not a
        # stale copy — the spec layer sits BELOW the agent layer and must
        # not import up. The agent-tree single source of the same topology
        # is ``agent.target_guard.classifier.CLUSTER_SCOPED_KINDS`` /
        # ``is_cluster_scoped_kind``; the two are cross-referenced by this
        # comment only. Add a cluster-scoped kind to BOTH (or lift the
        # topology into a spec-level module both can import).
        cluster_scoped=(
            "node", "pv", "namespace", "clusterrole", "clusterrolebinding",
            "storageclass",
        ),
        profile=PROFILE_K8S,
    )
)

# Host — bare-metal / VM faults via local blade ``--channel ssh`` or native
# commands. Same subsystem vocabulary as ChaosBlade OS executor (aggregated
# from the ``chaosblade`` / ``host_shell`` carriers); the host scope is
# namespace-less. Recovery is owned per-carrier by the FaultProvider
# (``chaosblade`` → blade_destroy; ``host_shell`` → LLM Layer-2 reverse command
# sourced from the skill case), not declared here.
register_family(
    FaultFamily(
        family_id="host",
        scopes=("host",),
        carrier_types=("chaosblade", "host_shell"),
        cluster_scoped=("host",),
        profile=PROFILE_HOST,
    )
)

# Python application — in-process method-level faults injected by the ChaosBlade
# Python agent (``blade create python <target> <action>``). The vocabulary is
# middleware clients / method-level verbs, aggregated from the sole
# ``chaosblade_python`` carrier. The scope is namespace-less (the fault lives in
# an application PROCESS, not a namespaced cluster object) and its profile is
# ``host``: the injection command must run on the machine hosting that process,
# because blade reaches the in-process agent over localhost HTTP.
register_family(
    FaultFamily(
        family_id="python_app",
        scopes=("python",),
        carrier_types=("chaosblade_python",),
        cluster_scoped=("python",),
        profile=PROFILE_HOST,
    )
)
