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
_ENTRY_KEYS = frozenset({"scope", "namespace", "names", "name_prefix"})

# Re-exported for consumers that prefer importing from this module.
from .drift_policy import CLUSTER_SCOPED_KINDS  # noqa: E402


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

    Exactly one of ``names`` / ``name_prefix`` is set — enforced at
    parse time (``names`` XOR ``name_prefix``).
    """

    scope: str
    namespace: str
    names: tuple[str, ...] = ()
    name_prefix: str = ""

    def describe(self) -> str:
        """Human-facing one-liner for logs, cards and payload rendering."""
        ns = self.namespace or "<cluster>"
        if self.names:
            sel = f"names={list(self.names)}"
        else:
            sel = f"name_prefix='{self.name_prefix}'"
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
            "(valid keys: scope, namespace, names, name_prefix)",
            index, sorted(unknown),
        )
        return None

    scope = canonicalise_kind(str(item.get("scope") or "").strip())
    if not scope:
        logger.warning(
            "mechanism_writes[%d]: missing scope — entry ignored", index,
        )
        return None

    namespace = str(item.get("namespace") or "").strip()
    if scope in CLUSTER_SCOPED_KINDS:
        # Cluster-scoped kinds have no namespace; normalise away any
        # stray value so comparison with the guard's cluster-scoped
        # branch (which expects "") never silently mismatches.
        namespace = ""
    elif not namespace:
        namespace = "default"

    names = _parse_names(item.get("names"))
    name_prefix = str(item.get("name_prefix") or "").strip()
    if bool(names) == bool(name_prefix):
        # Both set (ambiguous authority) or neither (no selector):
        # the entry authorises nothing checkable — drop it.
        logger.warning(
            "mechanism_writes[%d]: exactly one of names / name_prefix "
            "is required — entry ignored (scope=%s ns=%s)",
            index, scope, namespace,
        )
        return None

    return MechanismWriteEntry(
        scope=scope, namespace=namespace,
        names=names, name_prefix=name_prefix,
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
            continue
        out.append(MechanismWriteEntry(
            scope=scope,
            namespace=str(item.get("namespace") or "").strip(),
            names=names,
            name_prefix=prefix,
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
            (name in e.names) if e.names else name.startswith(e.name_prefix)
            for e in entries
        )
        if not covered:
            return False
    return True


def entries_beyond_victim(approved: "ApprovedTarget") -> tuple[MechanismWriteEntry, ...]:
    """Entries whose domain the victim target does NOT already cover.

    This is the unattended-CLI boundary-exit predicate: such entries
    widen the write-set contract beyond what the victim approval
    (plus its secondary net) governs, so an unattended run must
    terminate before any cluster mutation instead of silently
    auto-approving a widened contract no human has seen.

    Stricter than the guard's active-domain test: a cluster-scoped
    secondary kind (node under a workload victim) is ACTIVE at the
    guard (tightening) but NOT beyond the victim (the secondary net
    already passes those writes today — no new authority is granted,
    so unattended runs keep their established semantics).
    """
    if not approved.mechanism_entries:
        return ()
    approved_scope = canonicalise_kind(approved.scope or "")
    beyond: list[MechanismWriteEntry] = []
    for entry in approved.mechanism_entries:
        if entry.scope == approved_scope and entry.namespace == approved.namespace:
            continue
        if entry.scope in set(approved.secondary_scopes or ()):
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
        else:
            sel = f"'{e.get('name_prefix', '')}' (prefix)"
        lines.append(f"  - {e.get('scope')}/{ns}: {sel}")
    return "\n".join(lines)


__all__ = [
    "CLUSTER_SCOPED_KINDS",
    "MechanismWriteEntry",
    "entries_beyond_victim",
    "entries_from_list",
    "entries_to_list",
    "format_entries_for_payload",
    "format_mechanism_writes_for_display",
    "load_case_mechanism_writes",
    "match_mechanism_entries",
    "names_within_entries",
    "names_within_entry",
    "parse_mechanism_writes",
]
