"""Case-file ``mechanism_writes`` manifest — the write-set legislation source.

First-principles contract (openspec: write-set-approval-contract).
Authorization for cluster writes must (a) originate from a source
separate from the constrained party — the LLM that writes the plan —
and (b) exist before the run. The skill case file's frontmatter is
that source: human-authored, drill-loop-proven, inert until parsed.

One-directional chain, no LLM input point:

    case author legislates → deterministic code parses (here) →
    confirmation card renders the entries verbatim → approval
    freezes them into ``approved_target.mechanism_entries`` →
    the drift guard enforces.

What belongs in the manifest — and what does NOT: ONLY writes
outside the victim target's own coverage. PVC/persistentvolumeclaim
writes are NOT in the ``secondary_scopes`` net — they belong HERE,
legislated with name-level precision (the net deliberately skips
name validation, so kind+namespace pass is not enough for storage
claims). Other same-namespace auxiliary resources remain covered
by the frozen net, so declaring them again is double-sourcing —
the guard ignores entries in that domain rather than tightening it.
A case whose writes all fall inside the victim's coverage carries
no manifest at all; its freeze output stays byte-identical to today.

Failure posture: parsing never raises. A missing frontmatter, a
malformed block, or an invalid entry yields an empty (or partial)
manifest plus a logged warning — fail-closed is enforced downstream
at the drift guard, where the unauthorized write is rejected with
manifest attribution, not at load time where there is no human to
consult.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from .classifier import canonicalise_kind

if TYPE_CHECKING:
    from .types import ApprovedTarget, EffectiveTarget

logger = logging.getLogger(__name__)


# Keys a mechanism_writes entry may carry. Unknown keys reject the whole
# entry (logged) so a typo — ``names_prefix`` instead of ``name_prefix`` —
# surfaces as "entry ignored" at load and as a guard rejection at run,
# never as a silently-widened write set.
_ENTRY_KEYS = frozenset({
    "scope", "namespace", "names", "name_prefix", "name_from", "namespace_from",
})

# ``name_from`` — a runtime-DERIVED selector for a write target the case
# author CANNOT name statically. ``victim_node`` resolves at freeze time to
# the node(s) hosting the victim pod (W-56-1: a node-host mechanism under a
# namespaced victim writes the victim's node, whose name — e.g.
# ``cn-shanghai-cloudspe.25.209.71.148`` — is runtime-derived, neither
# listable in a portable case file nor sharing a drill prefix). The case
# legislates the SEMANTIC; :func:`materialize_derived_entries` +
# ``freeze.discover_victim_nodes`` inject the concrete name — DERIVED, never
# re-declared (same discipline as the PVC claim anchor). Unknown values are
# dropped at parse (never frozen), so a typo cannot silently widen the write
# set — the same posture ``recovery_channel`` takes.
NAME_FROM_VICTIM_NODE = "victim_node"
KNOWN_NAME_FROM = frozenset({NAME_FROM_VICTIM_NODE})

# ``namespace_from`` — the ORTHOGONAL derivation axis: a runtime-DERIVED
# NAMESPACE for a write whose KIND the case CAN name but whose namespace must
# track the victim's runtime location. ``victim`` resolves at freeze time to
# the victim's own namespace (``approved.namespace``) and — unlike
# ``name_from: victim_node`` — needs NO cluster query (the victim's ns is
# already on the spec). A NetworkPolicy that must select the victim pod has to
# live in the victim's namespace, which a portable case file cannot hardcode;
# the case legislates ``namespace_from: victim`` and
# :func:`materialize_derived_entries` injects the concrete ns. Contrast the
# statically-namespaced precedents (NXDOMAIN → ``kube-system``, unbound PVC →
# ``default``): those kinds live in a FIXED ns, a victim-scoped netpol does
# not. Mutually exclusive with a static ``namespace``; unknown values are
# dropped at parse (never frozen), the same posture ``name_from`` takes.
NAMESPACE_FROM_VICTIM = "victim"
KNOWN_NAMESPACE_FROM = frozenset({NAMESPACE_FROM_VICTIM})

# Re-exported for consumers that prefer importing from this module.
from .drift_policy import CLUSTER_SCOPED_KINDS  # noqa: E402


# ---------------------------------------------------------------------------
# Root cause III: the victim-runtime derivation REGISTRY — the single
# declaration point for every "derive this entry's target location from the
# victim's runtime placement" axis.
#
# An authorization entry sometimes has to name a target the case author
# CANNOT write statically, because its location is decided by the victim's
# RUNTIME placement (which node the victim pod landed on, which namespace it
# lives in). Each such axis used to be hand-bolted through the whole chain —
# parse-time validation, freeze-time materialization, the safety_check
# discovery trigger — so a THIRD axis meant re-deriving all of it and
# re-proving orthogonality / fail-closed by hand (the bolt-on defect).
#
# Now parse validation, materialization, serialization and the safety_check
# trigger ALL iterate this table. Adding an axis is ONE new row here (plus
# the matching dataclass field, which the serialization round-trip and the
# ``test_derived_axes_enumeration_matches_registry`` invariant pin to the
# registry). The fail-closed DROP semantics and the unknown-token rejection
# come for free and are provably uniform, because exactly one code path —
# :func:`materialize_derived_entries` and :func:`_parse_entry` — applies them.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _VictimRuntime:
    """The victim's runtime facts a derivation may resolve against.

    Populated at freeze time: ``victim_nodes`` from a live cluster query
    (``freeze.discover_victim_nodes``), ``victim_namespace`` from the
    already-known spec (no query). A resolver returns a falsy value when the
    fact is unavailable → :func:`materialize_derived_entries` DROPS the entry
    (fail closed — an empty selector would match ANY target).
    """

    victim_nodes: tuple[str, ...] = ()
    victim_namespace: str = ""


@dataclass(frozen=True)
class _DerivedAxis:
    """One victim-runtime derivation axis (a single registry row).

    Attributes:
        key: the frontmatter key AND the ``MechanismWriteEntry`` field name.
        known: accepted derivation tokens; any other value is DROPPED at
            parse (a typo must never silently widen the write set).
        target_field: the entry field the resolved value materializes into
            (``"names"`` for the name axis, ``"namespace"`` for the ns axis).
        resolve: ``_VictimRuntime ->`` the concrete value; falsy → DROP.
        needs_discovery: the resolver needs a live cluster query, so
            ``safety_check`` must call ``discover_victim_nodes`` for it (a
            namespace-only manifest skips the query).
        is_selector: the axis participates in the ``names`` / ``name_prefix``
            / derived-name three-way XOR (exactly one selector per entry).
        mutex_static_field: a static field this axis cannot coexist with
            (ambiguous authority → DROP).
        reject_on_cluster_scoped: the axis is meaningless on a cluster-scoped
            kind (no namespace to derive) → DROP at parse.
        clears_name_prefix: materializing this axis also blanks the prefix
            (the name axis replaces any selector with the derived names).
    """

    key: str
    known: frozenset[str]
    target_field: str
    resolve: Callable[[_VictimRuntime], object]
    needs_discovery: bool
    is_selector: bool
    mutex_static_field: str = ""
    reject_on_cluster_scoped: bool = False
    clears_name_prefix: bool = False


_DERIVED_AXES: tuple[_DerivedAxis, ...] = (
    _DerivedAxis(
        key="name_from",
        known=KNOWN_NAME_FROM,
        target_field="names",
        resolve=lambda rt: rt.victim_nodes,
        needs_discovery=True,
        is_selector=True,
        clears_name_prefix=True,
    ),
    _DerivedAxis(
        key="namespace_from",
        known=KNOWN_NAMESPACE_FROM,
        target_field="namespace",
        resolve=lambda rt: rt.victim_namespace,
        needs_discovery=False,
        is_selector=False,
        mutex_static_field="namespace",
        reject_on_cluster_scoped=True,
    ),
)


def _declared_axes(entry: "MechanismWriteEntry") -> tuple[_DerivedAxis, ...]:
    """The axes ``entry`` actually declares (non-empty token), registry order."""
    return tuple(a for a in _DERIVED_AXES if getattr(entry, a.key, ""))


def derived_keys() -> tuple[str, ...]:
    """Registry-driven derived field names (serialization / payload iterate this)."""
    return tuple(a.key for a in _DERIVED_AXES)


def entries_have_derived(entries) -> bool:
    """True when ANY entry declares a derivation axis (the materialize trigger)."""
    return any(_declared_axes(e) for e in entries)


def entries_needing_discovery(entries) -> bool:
    """True when any entry declares an axis whose resolver needs a live query.

    ``safety_check`` consults this to decide whether to call
    ``discover_victim_nodes`` — a namespace-only manifest skips the query. A
    NEW discovery-backed axis is picked up automatically via its
    ``needs_discovery`` flag; no safety_check edit required.
    """
    return any(a.needs_discovery for e in entries for a in _declared_axes(e))


@dataclass(frozen=True)
class MechanismWriteEntry:
    """One legislated write domain, parsed from the case frontmatter.

    Shapes:
      static         — ``names`` lists the exact objects the mechanism
                       touches (e.g. the coredns-custom ConfigMap).
      dynamic        — ``name_prefix`` covers transient objects the case
                       instructs the Agent to create with that prefix
                       (e.g. ``drill-nxdomain-`` patch ConfigMaps).
      cluster-scoped — ``namespace`` empty, ``names`` pins cluster-level
                       objects (the node-mechanism shape: Case #4).
      derived        — ``name_from`` names a runtime-derived selector the
                       case CANNOT author statically (``victim_node`` = the
                       node the victim pod runs on). Materialized into
                       ``names`` at freeze time by
                       :func:`materialize_derived_entries` (fed by
                       ``freeze.discover_victim_nodes``); until then — and
                       if discovery finds nothing — it authorises NOTHING
                       (fail closed).

    ``namespace_from`` is an ORTHOGONAL axis (not a name selector): it derives
    the NAMESPACE rather than the name. ``victim`` materializes at freeze to
    the victim's own namespace (``approved.namespace``, no cluster query
    needed) — the shape a victim-scoped NetworkPolicy requires, since it must
    live in the victim's ns to select it and a portable case cannot hardcode
    that ns. Mutually exclusive with a static ``namespace``.

    Exactly one of ``names`` / ``name_prefix`` / ``name_from`` is set —
    enforced at parse time (``namespace_from`` is independent of this XOR).
    """

    scope: str
    namespace: str
    names: tuple[str, ...] = ()
    name_prefix: str = ""
    name_from: str = ""
    namespace_from: str = ""

    def describe(self) -> str:
        """Human-facing one-liner for logs, cards and payload rendering."""
        ns = self.namespace or "<cluster>"
        if self.namespace_from and self.namespace:
            ns += f" (derived from {self.namespace_from})"
        elif self.namespace_from:
            ns = f"<from {self.namespace_from}> (unresolved)"
        if self.names:
            sel = f"names={list(self.names)}"
            if self.name_from:
                sel += f" (derived from {self.name_from})"
        elif self.name_prefix:
            sel = f"name_prefix='{self.name_prefix}'"
        else:
            sel = f"name_from='{self.name_from}' (unresolved)"
        return f"{self.scope}/{ns}: {sel}"


def _parse_names(raw: object) -> tuple[str, ...]:
    """Accept a YAML list or a CSV string; anything else is empty."""
    if raw is None:
        return ()
    if isinstance(raw, str):
        return tuple(n.strip() for n in raw.split(",") if n.strip())
    if isinstance(raw, (list, tuple)):
        return tuple(str(n).strip() for n in raw if str(n).strip())
    return ()


def _parse_entry(item: object, *, index: int) -> Optional[MechanismWriteEntry]:
    """Validate one frontmatter entry; ``None`` (logged) when invalid.

    Entry-level isolation: one bad entry never poisons its siblings —
    the valid entries still authorize their writes, the invalid one
    is dropped, and any plan needing it gets rejected at the guard
    with manifest attribution (fail closed there, not here).
    """
    if not isinstance(item, dict):
        logger.warning(
            "mechanism_writes[%d]: not a mapping — entry ignored", index,
        )
        return None

    unknown = set(item) - _ENTRY_KEYS
    if unknown:
        logger.warning(
            "mechanism_writes[%d]: unknown key(s) %s — entry ignored "
            "(valid keys: %s)",
            index, sorted(unknown), ", ".join(sorted(_ENTRY_KEYS)),
        )
        return None

    scope = canonicalise_kind(str(item.get("scope") or "").strip())
    if not scope:
        logger.warning(
            "mechanism_writes[%d]: missing scope — entry ignored", index,
        )
        return None

    # Registry-driven derivation-axis reads + validation (root cause III):
    # every axis is read, lowercased and token-checked through the SAME loop,
    # so a new axis inherits the unknown-token DROP without a new branch.
    axis_tokens: dict[str, str] = {}
    for axis in _DERIVED_AXES:
        token = str(item.get(axis.key) or "").strip().lower()
        if not token:
            continue
        if token not in axis.known:
            # An unknown derivation is dropped rather than frozen — a typo
            # must not silently widen (or vacuously satisfy) the write set.
            logger.warning(
                "mechanism_writes[%d]: unknown %s=%r — entry ignored "
                "(known: %s)", index, axis.key, token, sorted(axis.known),
            )
            return None
        axis_tokens[axis.key] = token

    # Axis structural rules, also registry-declared: mutual exclusion with a
    # static field (ambiguous authority), and meaningless-on-cluster-scoped.
    for axis in _DERIVED_AXES:
        if axis.key not in axis_tokens:
            continue
        if axis.mutex_static_field and str(
            item.get(axis.mutex_static_field) or ""
        ).strip():
            # Ambiguous authority: a static value AND a derived one. Drop
            # rather than guess which governs (fail closed at the guard,
            # with attribution).
            logger.warning(
                "mechanism_writes[%d]: %s and %s are mutually exclusive "
                "— entry ignored (scope=%s)",
                index, axis.mutex_static_field, axis.key, scope,
            )
            return None
        if axis.reject_on_cluster_scoped and scope in CLUSTER_SCOPED_KINDS:
            # A cluster-scoped kind has nothing for this axis to derive.
            logger.warning(
                "mechanism_writes[%d]: %s on cluster-scoped scope=%s "
                "— entry ignored", index, axis.key, scope,
            )
            return None

    namespace = str(item.get("namespace") or "").strip()
    derives_namespace = any(
        a.target_field == "namespace"
        for a in _DERIVED_AXES if a.key in axis_tokens
    )
    if scope in CLUSTER_SCOPED_KINDS:
        # Cluster-scoped kinds have no namespace; normalise away any
        # stray value so comparison with the guard's cluster-scoped
        # branch (which expects "") never silently mismatches.
        namespace = ""
    elif derives_namespace:
        # Derived namespace: leave empty; materialize_derived_entries injects
        # the victim's ns at freeze. Do NOT default to "default" — that would
        # silently pin the entry to the wrong ns for a non-default victim.
        namespace = ""
    elif not namespace:
        namespace = "default"

    names = _parse_names(item.get("names"))
    name_prefix = str(item.get("name_prefix") or "").strip()
    # The three-way selector XOR, registry-driven: static names, static
    # prefix, and every declared axis flagged ``is_selector`` (today:
    # ``name_from``). Exactly one selector must be present.
    selector_axes = sum(
        1 for a in _DERIVED_AXES if a.is_selector and a.key in axis_tokens
    )
    selectors_set = (
        sum(1 for present in (bool(names), bool(name_prefix)) if present)
        + selector_axes
    )
    if selectors_set != 1:
        # Several selectors (ambiguous authority) or none (nothing to check):
        # either way the entry authorises nothing the guard can validate —
        # drop it (fail closed at the guard, with manifest attribution).
        logger.warning(
            "mechanism_writes[%d]: exactly one of names / name_prefix / "
            "name_from is required — entry ignored (scope=%s ns=%s)",
            index, scope, namespace,
        )
        return None

    return MechanismWriteEntry(
        scope=scope, namespace=namespace,
        names=names, name_prefix=name_prefix,
        **{axis.key: axis_tokens.get(axis.key, "") for axis in _DERIVED_AXES},
    )


def parse_mechanism_writes(content: str) -> tuple[MechanismWriteEntry, ...]:
    """Deterministically parse the ``mechanism_writes`` frontmatter block.

    Reuses the skill loader's frontmatter splitter — the same YAML
    discipline SKILL.md already follows. Returns ``()`` for: no
    frontmatter (every legacy case — behaviour unchanged), malformed
    YAML, missing block, or a non-list block. Never raises.
    """
    if not content:
        return ()
    from chaos_agent.skills.loader import parse_frontmatter

    try:
        frontmatter = parse_frontmatter(content)
    except Exception:  # noqa: BLE001 — parse must never raise
        logger.warning("mechanism_writes: frontmatter parse failed — empty manifest")
        return ()
    if not frontmatter:
        return ()

    raw = frontmatter.get("mechanism_writes")
    if not raw:
        return ()
    if not isinstance(raw, list):
        logger.warning(
            "mechanism_writes: frontmatter block is not a list — empty manifest",
        )
        return ()

    entries: list[MechanismWriteEntry] = []
    for i, item in enumerate(raw):
        entry = _parse_entry(item, index=i)
        if entry is not None:
            entries.append(entry)
    return tuple(entries)


# ---------------------------------------------------------------------------
# ``recovery_channel`` — the case file's recovery-route legislation (D3
# source 1, openspec faultdrill-cr-channel). Same one-directional chain as
# the write-set manifest: case author legislates → deterministic code
# parses (here) → approval freezes it into the snapshot → the CR-channel
# route gate consults it BEFORE the verb-vocabulary proxy. The proxy was
# an explicitly temporary M2 stand-in ("M3 task 3.1 才写入 case
# recovery_channel 元数据，M2 门不得依赖" — tasks.md 2.4); now that the
# declaration exists, it outranks the proxy: a k8s-native mechanism whose
# taxonomy verbs happen to land in the blade vocabulary (NXDOMAIN:
# target=network action=dns) is NOT symmetric-revert reachable, and only
# the case legislation can say so.
# ---------------------------------------------------------------------------

# The only declared value with routing meaning today. Unknown values are
# dropped (logged) rather than frozen — a typo must not silently widen
# the CR channel's admission surface, and the gate falls back to the
# verb proxy exactly as if no declaration existed (fail closed to the
# pre-declaration behaviour, never fail open).
RECOVERY_CHANNEL_APISERVER_WRITE = "apiserver-write"
KNOWN_RECOVERY_CHANNELS = frozenset({RECOVERY_CHANNEL_APISERVER_WRITE})


def parse_recovery_channel(content: str) -> str:
    """Deterministically parse the ``recovery_channel`` frontmatter key.

    Returns ``""`` for: no frontmatter, missing key, empty value, or an
    unknown value (logged). Never raises — same failure posture as the
    manifest parser.
    """
    if not content:
        return ""
    from chaos_agent.skills.loader import parse_frontmatter

    try:
        frontmatter = parse_frontmatter(content)
    except Exception:  # noqa: BLE001 — parse must never raise
        logger.warning("recovery_channel: frontmatter parse failed — no declaration")
        return ""
    if not frontmatter:
        return ""

    raw = str(frontmatter.get("recovery_channel") or "").strip().lower()
    if not raw:
        return ""
    if raw not in KNOWN_RECOVERY_CHANNELS:
        logger.warning(
            "recovery_channel: unknown value %r — declaration ignored "
            "(known: %s)", raw, sorted(KNOWN_RECOVERY_CHANNELS),
        )
        return ""
    return raw


def load_case_recovery_channel(skill_name: str, case_resource_path: str) -> str:
    """Read the settled case file and return its ``recovery_channel``.

    The AUTHORITATIVE read, same discipline as
    :func:`load_case_mechanism_writes`: code re-reads the case file
    through the escape-proof resolver, so neither the Agent's prose
    reading nor any LLM-declared planning field is a routing input.
    Any failure → ``""`` (the route gate falls back to the verb proxy).
    """
    skill_name = (skill_name or "").strip()
    case_resource_path = (case_resource_path or "").strip()
    if not skill_name or not case_resource_path:
        return ""
    try:
        from chaos_agent.skills.loader import get_skills_dir, load_skill_resource

        skill_dir = get_skills_dir() / skill_name
        content = load_skill_resource(skill_dir, case_resource_path)
    except (ValueError, FileNotFoundError, OSError) as e:
        logger.warning(
            "recovery_channel: resource read failed for skill=%s path=%s (%s)",
            skill_name, case_resource_path, e,
        )
        return ""
    except Exception as e:  # noqa: BLE001 — load must never raise
        logger.warning(
            "recovery_channel: unexpected load failure for skill=%s path=%s (%s)",
            skill_name, case_resource_path, e,
        )
        return ""
    return parse_recovery_channel(content)


def derive_pvc_claims_from_writes(
    entries: tuple[MechanismWriteEntry, ...],
) -> tuple[str, ...]:
    """Derive the claim anchor's in-band half from the write legislation.

    #39 time-dimension gap, DERIVED — not re-declared: live claim
    discovery can only find PVCs that ALREADY exist, and a #38-shaped
    case applies its PVC during execution, so a freeze anchored purely
    on discovery honestly misses the claim. But the PVC the mechanism
    creates in-band is already legislated HERE — an in-band PVC write
    outside ``mechanism_writes`` is itself rejected by the drift guard —
    so the write-set IS the claim-set's time-proof half: same source,
    same trust level (the entries the confirmation card rendered and a
    human approved), zero new authoring surface, and no hand-copied
    claim list that can drift from the write it shadows (the UID-shape
    legislation lesson, applied to this domain: derive from the
    source, never re-type it). ``name_prefix`` entries contribute no
    names — a prefix authorises writes by pattern while the anchor is
    name-exact, so the prefix shape stays empty (fail closed).
    """
    names: set[str] = set()
    for entry in entries:
        if entry.scope == "pvc":  # canonical PVC kind (canonicalise_kind)
            names.update(entry.names)
    return tuple(sorted(names))


def materialize_derived_entries(
    entries: tuple[MechanismWriteEntry, ...],
    *,
    victim_nodes: tuple[str, ...] = (),
    victim_namespace: str = "",
) -> tuple[MechanismWriteEntry, ...]:
    """Resolve every derived entry into a concrete static entry.

    Two ORTHOGONAL derivation axes, both freeze-time MATERIALIZATION (the case
    legislates a SEMANTIC, code injects the concrete value so every downstream
    matcher works UNCHANGED on an ordinary static entry):

      ``name_from: victim_node`` — the DISCOVERY half is
        ``freeze.discover_victim_nodes`` (a live cluster query); the derived
        node name(s) are injected into ``names``.
      ``namespace_from: victim`` — NO discovery: the victim's own namespace
        (``approved.namespace``) is already on the spec, so it is injected
        straight into ``namespace``. This is the shape a victim-scoped
        NetworkPolicy needs — it must live in the victim's ns to select it, and
        a portable case cannot hardcode that ns.

    Fail closed on BOTH axes: a ``victim_node`` entry with no discovered node
    (victim unscheduled / absent / query failed) is DROPPED — an empty-names
    entry would fall through ``names_within_entry``'s prefix branch
    (``startswith("")`` is vacuously True) and authorise ANY node. A
    ``namespace_from: victim`` entry with no victim namespace (cluster-scoped
    victim, or a hand-built entry reaching freeze) is DROPPED — an empty ns
    never matches the write's real ns, so dropping makes the rejection explicit
    at the guard with manifest attribution.

    Pure and idempotent: entries with neither derivation pass through verbatim,
    so this is a no-op for every manifest that does not legislate a derived
    write (freeze output stays byte-identical for those cases).
    """
    if not entries_have_derived(entries):
        return entries
    runtime = _VictimRuntime(
        victim_nodes=tuple(victim_nodes or ()),
        victim_namespace=(victim_namespace or "").strip(),
    )
    out: list[MechanismWriteEntry] = []
    for entry in entries:
        axes = _declared_axes(entry)
        if not axes:
            out.append(entry)
            continue
        # The materializable fields, seeded from the entry; each declared
        # axis resolves into its ``target_field`` via the registry.
        fields: dict[str, object] = {
            "names": entry.names,
            "name_prefix": entry.name_prefix,
            "namespace": entry.namespace,
        }
        dropped = False
        for axis in axes:
            token = getattr(entry, axis.key)
            if token not in axis.known:
                # Unknown derivations are already rejected at parse; this
                # guards a hand-built entry reaching freeze. Drop (fail closed).
                logger.warning(
                    "mechanism_writes: unknown %s=%r — entry dropped",
                    axis.key, token,
                )
                dropped = True
                break
            resolved = axis.resolve(runtime)
            if not resolved:
                # No victim runtime fact to derive from (unscheduled / absent
                # / query failed / cluster-scoped victim). DROP: an empty
                # selector would fall through ``startswith("")`` (names) or
                # never match a real ns (namespace) — fail closed either way.
                logger.warning(
                    "mechanism_writes: %s=%s entry (scope=%s) could not be "
                    "materialized — victim runtime fact unavailable; entry "
                    "dropped (fail closed)", axis.key, token, entry.scope,
                )
                dropped = True
                break
            fields[axis.target_field] = resolved
            if axis.clears_name_prefix:
                fields["name_prefix"] = ""
        if dropped:
            continue
        out.append(MechanismWriteEntry(
            scope=entry.scope,
            namespace=fields["namespace"],
            names=fields["names"],
            name_prefix=fields["name_prefix"],
            **{a.key: getattr(entry, a.key) for a in _DERIVED_AXES},
        ))
    return tuple(out)


def load_case_mechanism_writes(
    skill_name: str, case_resource_path: str,
) -> tuple[MechanismWriteEntry, ...]:
    """Read the settled case file and parse its manifest.

    The AUTHORITATIVE read: the same case file the Agent shows the
    user is re-read here by code, so the Agent's reading (a
    ToolMessage summarising prose) is never an authorization input.
    ``case_resource_path`` is LLM-influenced state, so the read goes
    through the skill loader's escape-proof resolver — traversal
    and absolute-path injection land in the same rejection as the
    ``read_skill_resource`` tool. Any failure → empty manifest +
    warning (fail closed at the guard).
    """
    skill_name = (skill_name or "").strip()
    case_resource_path = (case_resource_path or "").strip()
    if not skill_name or not case_resource_path:
        return ()
    try:
        from chaos_agent.skills.loader import get_skills_dir, load_skill_resource

        skill_dir = get_skills_dir() / skill_name
        content = load_skill_resource(skill_dir, case_resource_path)
    except (ValueError, FileNotFoundError, OSError) as e:
        logger.warning(
            "case manifest: resource read failed for skill=%s path=%s (%s)",
            skill_name, case_resource_path, e,
        )
        return ()
    except Exception as e:  # noqa: BLE001 — load must never raise
        logger.warning(
            "case manifest: unexpected load failure for skill=%s path=%s (%s)",
            skill_name, case_resource_path, e,
        )
        return ()
    return parse_mechanism_writes(content)


# ---------------------------------------------------------------------------
# Serialisation (state dicts round-trip through the checkpointer)
# ---------------------------------------------------------------------------


def entries_to_list(entries) -> list[dict]:
    """Project entries into the JSON-safe list stored on ``approved_target``.

    Called only with a non-empty tuple — freeze omits the key entirely
    when there is no manifest so the no-manifest snapshot stays
    byte-identical.
    """
    return [
        {
            "scope": e.scope,
            "namespace": e.namespace,
            "names": list(e.names),
            "name_prefix": e.name_prefix,
            # Registry-driven derived fields: a new axis serializes with no
            # edit here (the round-trip invariant pins the field to the row).
            **{key: getattr(e, key) for key in derived_keys()},
        }
        for e in entries
    ]


def entries_from_list(raw: object) -> tuple[MechanismWriteEntry, ...]:
    """Hydrate frozen entries from the state dict.

    Lenient by design: the data was validated at parse time and then
    frozen; malformed rows (older checkpoints, hand-edited state) are
    skipped rather than crashing the screener.
    """
    if not raw or not isinstance(raw, (list, tuple)):
        return ()
    out: list[MechanismWriteEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        scope = canonicalise_kind(str(item.get("scope") or "").strip())
        if not scope:
            continue
        names = tuple(str(n) for n in (item.get("names") or []) if n)
        prefix = str(item.get("name_prefix") or "").strip()
        if not names and not prefix:
            # A name_from entry that never materialized (no names) carries no
            # checkable authority — drop it (fail closed), same as parse time.
            continue
        out.append(MechanismWriteEntry(
            scope=scope,
            namespace=str(item.get("namespace") or "").strip(),
            names=names,
            name_prefix=prefix,
            **{
                key: str(item.get(key) or "").strip()
                for key in derived_keys()
            },
        ))
    return tuple(out)


# ---------------------------------------------------------------------------
# Guard-side matching (drift_policy) + boundary-side coverage
# (confirmation_gate) — shared domain logic
# ---------------------------------------------------------------------------


def _entry_domain_active(
    entry: MechanismWriteEntry,
    approved_scope: str,
    approved_namespace: str,
    secondary_scopes: tuple[str, ...],
    secondary_namespace: str,
) -> bool:
    """Is this entry's (scope, namespace) domain OUTSIDE what the victim
    target and its same-namespace secondary net already govern?

    A manifest entry has authority ONLY in this domain — the
    legislation rule says the manifest declares writes outside the
    victim's own coverage. Entries falling inside are inert by
    design:

      - victim's own (scope, ns): the existing victim comparison
        already rules that domain (name subset / labels). Letting an
        entry override it would let a mis-authored manifest tighten
        or bypass the victim check the user directly approved.
      - same-namespace namespaced secondary domain: secondary scopes
        deliberately skip name validation; a same-namespace entry
        there would tighten today's permissive pass — not additive.

    Cluster-scoped secondary kinds (e.g. ``node`` under a workload
    victim) stay ACTIVE: the secondary net passes any node name
    unchecked, and the Case #4 entry exists precisely to give that
    domain name-level precision (in-zone node passes, another node
    stays drift).
    """
    if entry.scope == approved_scope and entry.namespace == approved_namespace:
        return False
    if entry.scope in set(secondary_scopes or ()):
        if entry.scope in CLUSTER_SCOPED_KINDS:
            # Cluster-scoped secondary: active (tightening intent).
            return True
        sec_ns = (secondary_namespace or "default").strip()
        if entry.namespace == sec_ns:
            return False
    return True


def match_mechanism_entries(
    approved: "ApprovedTarget", effective: "EffectiveTarget",
) -> tuple[MechanismWriteEntry, ...]:
    """Collect the frozen entries whose domain covers this effective target.

    Domain match = canonicalised scope equal + namespace equal, with
    the same cluster-scoped exemption the victim namespace check
    uses (a cluster-scoped call carries no namespace to compare).
    Multiple entries routinely share one domain — the NXDOMAIN case
    declares a static ``coredns-custom`` entry AND a dynamic
    ``drill-nxdomain-`` prefix entry, both configmap/kube-system —
    so ALL matching entries are returned and the caller treats them
    as alternatives (any entry satisfied → in-contract). Returns ``()``
    when no entry's domain matches — the call then falls through to
    the existing comparison rules byte-identically.
    """
    if not approved.mechanism_entries:
        return ()
    eff_scope = canonicalise_kind(effective.scope or "")
    if not eff_scope:
        return ()
    approved_scope = canonicalise_kind(approved.scope or "")
    matched: list[MechanismWriteEntry] = []
    for entry in approved.mechanism_entries:
        if entry.scope != eff_scope:
            continue
        if entry.scope in CLUSTER_SCOPED_KINDS:
            ns_ok = True  # no namespace to compare
        else:
            eff_ns = (effective.namespace or "default").strip()
            ns_ok = entry.namespace == eff_ns
        if not ns_ok:
            continue
        if not _entry_domain_active(
            entry, approved_scope, approved.namespace,
            approved.secondary_scopes, approved.secondary_namespace,
        ):
            continue
        matched.append(entry)
    return tuple(matched)


def names_within_entry(
    entry: MechanismWriteEntry, effective: "EffectiveTarget",
) -> bool:
    """Do the effective names satisfy THIS entry's selector?

    Static entry: every effective name must be one of the entry's
    ``names``. Prefix entry: every effective name must start with
    ``name_prefix``. Empty effective names satisfy neither — the
    call didn't pin a name, so nothing proves it stayed in-contract
    (fail closed).
    """
    if not effective.names:
        return False
    if entry.names:
        return all(n in entry.names for n in effective.names)
    if not entry.name_prefix:
        # No selector materialized (an unresolved ``name_from`` entry): it
        # authorises NOTHING. Falling through to ``startswith("")`` below
        # would be vacuously True for every name — a fail-OPEN hole.
        return False
    return all(n.startswith(entry.name_prefix) for n in effective.names)


def names_within_entries(
    entries, effective: "EffectiveTarget",
) -> bool:
    """Is every effective name covered by at least one entry (union)?

    The authorization unit is the OBJECT WRITE, not the call: entries
    sharing a domain are alternatives, so a batch call that touches
    ``coredns-custom`` (static entry) and ``drill-nxdomain-01`` (prefix
    entry) in one shot writes only legislated objects and passes —
    while a single foreign name anywhere in the batch is drift.
    """
    names = effective.names
    if not names or not entries:
        return False
    for name in names:
        covered = any(
            (name in e.names) if e.names
            else bool(e.name_prefix) and name.startswith(e.name_prefix)
            for e in entries
        )
        if not covered:
            return False
    return True


def entries_beyond_victim(approved: "ApprovedTarget") -> tuple[MechanismWriteEntry, ...]:
    """Entries whose domain the victim target does NOT already cover.

    This is the write-set-contract WIDENING predicate. Its consumers are
    VISIBILITY + AUDIT, not gating (see ``_write_set_boundary``): the
    interactive card (TUI / ``--confirm``) renders these entries verbatim
    for the human who is present; unattended channels still AUTO-approve
    (``unattended_resume_value`` is always ``"approved"`` — the manifest is
    the authority, per-run human approval of an unchanged manifest is a
    rubber stamp) but emit an auditable ``auto_approved`` event carrying
    these entries. An empty result leaves both surfaces silent, so this
    predicate never blocks or pauses a run — it only decides what a present
    human sees and what the audit log records.

    Stricter than the guard's active-domain test for STATIC entries: a
    static cluster-scoped secondary kind (node under a workload victim) is
    ACTIVE at the guard (tightening) but NOT beyond the victim — the
    secondary net already passes those NON-fault writes (taint/cordon), so
    no new authority is granted.

    A DERIVED entry (ANY ``_DERIVED_AXES`` axis — ``name_from`` today,
    ``namespace_from`` likewise) is the exception and IS beyond the victim
    even when its kind sits in the secondary net. The canonical case is
    ``name_from``: it exists only to authorize a FAULT the secondary net
    does NOT pass — ``drift_policy`` rejects a ``fault_target`` write on a
    cluster-scoped kind under a namespaced victim (the ``blade <t> targets
    node under pod approval`` branch) even though the kind is in the net, so
    the derived entry is the SOLE authority that unlocks it. That is GRANTED
    authority, not tightening, and must surface like any widened contract
    (verified end-to-end: without the entry the same node write is
    reject_drift). Every derived axis is treated uniformly (the guard reads
    ``not _declared_axes(entry)``, never a per-axis ``name_from`` check), so
    a NEW axis inherits this visibility by construction instead of needing
    its own special case here: a derived authority ALWAYS stays visible,
    and only STATIC entries take the secondary-net exemption.
    """
    if not approved.mechanism_entries:
        return ()
    approved_scope = canonicalise_kind(approved.scope or "")
    beyond: list[MechanismWriteEntry] = []
    for entry in approved.mechanism_entries:
        if entry.scope == approved_scope and entry.namespace == approved.namespace:
            continue
        # A DERIVED entry (any registry axis) never takes the secondary-net
        # exemption — see the docstring: it authorizes a fault the net
        # blocks, so it is granted authority that must stay visible.
        if (
            entry.scope in set(approved.secondary_scopes or ())
            and not _declared_axes(entry)
        ):
            if entry.scope in CLUSTER_SCOPED_KINDS:
                continue  # secondary net already covers this domain
            sec_ns = (approved.secondary_namespace or "default").strip()
            if entry.namespace == sec_ns:
                continue
        beyond.append(entry)
    return tuple(beyond)


def format_entries_for_payload(entries) -> list[dict]:
    """Verbatim rendering for the confirmation card and exit payloads.

    The card must show what the CASE legislated — scope, namespace,
    names or prefix — not run-time plan prose, so the payload uses
    the frozen entries directly.
    """
    return [
        {
            "scope": e.scope,
            "namespace": e.namespace,
            "names": list(e.names),
            "name_prefix": e.name_prefix,
            **{key: getattr(e, key) for key in derived_keys()},
            "description": e.describe(),
        }
        for e in entries
    ]


def format_mechanism_writes_for_display(payload_entries: list[dict]) -> str:
    """Human-facing text block for the manifest entries — ONE rendering
    source, every confirmation surface.

    Input is the payload shape (:func:`format_entries_for_payload`
    output — what the gate card, the boundary payload and the frozen
    snapshot all carry), so a channel that only has an interrupt
    payload renders the SAME lines as one that reads the snapshot.
    Empty input renders "" so manifest-free cards are untouched.
    """
    if not payload_entries:
        return ""
    lines = ["Mechanism writes beyond the victim target (case manifest):"]
    for e in payload_entries:
        ns = e.get("namespace") or "<cluster>"
        names = e.get("names") or []
        if names:
            sel = ", ".join(str(n) for n in names)
            if e.get("name_from"):
                sel += f" (derived from {e['name_from']})"
        else:
            sel = f"'{e.get('name_prefix', '')}' (prefix)"
        lines.append(f"  - {e.get('scope')}/{ns}: {sel}")
    return "\n".join(lines)


__all__ = [
    "CLUSTER_SCOPED_KINDS",
    "KNOWN_NAME_FROM",
    "KNOWN_NAMESPACE_FROM",
    "KNOWN_RECOVERY_CHANNELS",
    "NAME_FROM_VICTIM_NODE",
    "NAMESPACE_FROM_VICTIM",
    "RECOVERY_CHANNEL_APISERVER_WRITE",
    "MechanismWriteEntry",
    "derived_keys",
    "entries_beyond_victim",
    "entries_have_derived",
    "entries_needing_discovery",
    "materialize_derived_entries",
    "entries_from_list",
    "entries_to_list",
    "format_entries_for_payload",
    "format_mechanism_writes_for_display",
    "load_case_mechanism_writes",
    "load_case_recovery_channel",
    "match_mechanism_entries",
    "names_within_entries",
    "names_within_entry",
    "parse_mechanism_writes",
    "parse_recovery_channel",
]
