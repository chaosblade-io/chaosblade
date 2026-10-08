"""Guard-completeness invariants (design doc §1.4 I-a + §3.4 III-a).

These tests encode the "extension must wire EVERY enforcement point" contract
as MEMBER-LEVEL invariants, so a silently-missing layer turns RED at test time
instead of failing closed at runtime — the recurrence this file exists to stop:

* W-67-8: the ``network`` family had a judge (``_network_inverse``) but no
  carrier lane, so ``iptables -D`` mapped to no family and the window-internal
  pure reclaim was structurally unreachable (fail-closed disguised as safe).
* socat: a new dual-use binary landed in ``_MUTATING_BINARIES`` (one-shot
  reject) with no argument-level exemption, so bandwidth drills on slim
  terway/calico/cilium images could inject but never verify.
* derived axes: post-III-b these are declared ONCE in the
  ``mechanism_writes._DERIVED_AXES`` registry; parse / materialize /
  serialization / the safety_check trigger all iterate it. The invariants
  below pin the registry as the single enumeration source AND assert the
  dispatch is registry-driven (no per-axis attribute bolt-on), so a new axis
  is wired everywhere by construction and a hand-reverted branch turns red.

Design note (docs/design/safety-concept-encoding-root-causes.md §1.7): the
enumeration is deliberately MEMBER-LEVEL, never set-level ("intersects the
word list" would let covered members mask uncovered ones — memory fc47864e).
Each anchor is a FULL-SET anchor: the cross-validation assertions compare the
anchor against what the source of every layer actually contains, so adding a
member to a layer WITHOUT the anchor (or an anchor entry with no layer) both
turn red. A silently-empty enumeration is treated as a failure, not "clean".

When the I-c single-registry refactor lands, these source-scanning anchors are
superseded by the registry's import-time completeness check.
"""
import dataclasses
import inspect
import io
import re
import tokenize

import pytest

import chaos_agent  # noqa: F401  — parent package first (private-submodule safety)
from chaos_agent.agent import execution_artifacts
from chaos_agent.agent.nodes.gates import safety_check
from chaos_agent.agent.providers.k8s_native import provider as k8s_native_provider
from chaos_agent.agent.target_guard import (
    carriers,
    mechanism_writes,
    recoverability,
)
from chaos_agent.agent.target_guard.carriers import (
    RECOVERY_FAMILIES,
    RecoveryFamily,
)
from chaos_agent.tools import readonly


def _src(module) -> str:
    return inspect.getsource(module)


def _code_only(module_or_fn) -> str:
    """Source with COMMENT tokens stripped (docstrings/strings kept).

    An invariant that asserts "the CODE does X" must not be satisfiable by a
    prose mention of X in a comment — otherwise a comment like ``# the old
    form hardcoded e.name_from`` would falsely pin the attribute-access
    invariant. Comments always run to end-of-line, so cutting each line at its
    COMMENT token column removes exactly the prose and nothing else.
    """
    src = inspect.getsource(module_or_fn)
    lines = src.splitlines()
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            row, col = tok.start
            lines[row - 1] = lines[row - 1][:col]
    return "\n".join(lines)


# ===========================================================================
# I-a ① — bounded-RECLAIM recovery faces: ONE registry drives judge / ledger /
#          lane / render by construction (root-cause I-c landed)
# ===========================================================================
#
# These four layers used to be wired by hand-maintained string literals
# (``if family == "disk"``) scattered across four files — the W-67-8 shape, where
# ``network`` gained a judge (``_network_inverse``) but no carrier lane, so
# ``iptables -D`` mapped to no family and window-internal early recovery was
# structurally unreachable (fail-closed disguised as safe).
#
# Post-I-c they are wired by ONE registry, ``carriers.RECOVERY_FAMILIES``: each
# ``RecoveryFamily`` record declares its judge / ledger_key + extract / lane_match
# / render, and ``RecoveryFamily.__post_init__`` raises AT IMPORT TIME if any face
# is missing. Adding a family is a single record; ledger / lane / render all
# ITERATE the registry, so they stay in lock step by construction and a
# half-wired family is a loud crash, never a silent runtime gap.
#
# The invariants below pin that architecture. Source-scanning anchors that used
# to assert the literal dispatch (``'"recovery_fill_path"' in ledger_src``) are
# superseded — they would now assert the ABSENCE of the very table-driving that
# closes the gap. Each is a member/full-set anchor: a silently-empty enumeration
# is treated as a failure, not "clean" (memory fc47864e).

_LEDGER_FN = "_mark_bounded_host_recovery"

# The bounded-RECLAIM faces the registry must cover, and the recovery_* ledger
# field each owns. cpu/mem/process are bounded too but self-terminate or pair
# inline — they own NO reclaim face, so they are correctly absent here.
_ANCHOR_FACES = {
    "disk_fill": {"family": "disk", "ledger_key": "recovery_fill_path",
                  "judge": "_disk_inverse"},
    "dm": {"family": "disk", "ledger_key": "recovery_dm_name",
           "judge": "_dm_inverse"},
    "network": {"family": "network", "ledger_key": "recovery_network_rules",
                "judge": "_network_inverse"},
}


def test_registry_is_nonempty_and_every_face_complete():
    """The registry is the single enumeration and each face wires all 4 layers.

    Defends ``__post_init__`` being weakened: a record with a non-callable face
    or an empty key would sail through construction and fail closed at runtime.
    """
    assert RECOVERY_FAMILIES, "registry came up EMPTY — suspicious, not 'clean'"
    for face in RECOVERY_FAMILIES:
        assert isinstance(face, RecoveryFamily)
        assert face.name and face.family and face.ledger_key, (
            f"face {face!r} has an empty name/family/ledger_key"
        )
        for attr in ("judge", "ledger_extract", "lane_match", "render"):
            assert callable(getattr(face, attr)), (
                f"{face.name}: the {attr} face is not callable — a half-wired "
                f"family is the W-67-8 silent fail-closed gap"
            )


def test_registry_matches_anchored_faces():
    """Full-set anchor: the registry is EXACTLY the bounded-reclaim faces.

    A face added to the registry without this anchor (or an anchor entry with no
    face) both turn red, so the enumeration cannot silently drift.
    """
    by_name = {face.name: face for face in RECOVERY_FAMILIES}
    assert set(by_name) == set(_ANCHOR_FACES), (
        f"registry faces drifted from the anchor.\n"
        f"  registered : {sorted(by_name)}\n"
        f"  anchored   : {sorted(_ANCHOR_FACES)}\n"
        f"  A new bounded-reclaim family must wire judge+ledger+lane+render in "
        f"  ONE record (and be added to _ANCHOR_FACES); a self-terminating "
        f"  family (cpu/mem/process) owns no face and must NOT appear."
    )
    for name, anchor in _ANCHOR_FACES.items():
        face = by_name[name]
        assert face.family == anchor["family"], f"{name}: family drifted"
        assert face.ledger_key == anchor["ledger_key"], f"{name}: key drifted"
        assert face.judge.__name__ == anchor["judge"], f"{name}: judge drifted"


def test_construction_raises_on_any_missing_face():
    """The by-construction guarantee itself: drop ANY face → import-time raise."""
    base = {
        "name": "probe", "family": "disk", "ledger_key": "recovery_probe",
        "judge": lambda lowered: True,
        "ledger_extract": lambda inner: "",
        "lane_match": lambda command: (),
        "render": lambda value: ("", ""),
    }
    for field in ("name", "family", "ledger_key"):
        with pytest.raises(ValueError):
            RecoveryFamily(**{**base, field: ""})
    for field in ("judge", "ledger_extract", "lane_match", "render"):
        with pytest.raises(TypeError):
            RecoveryFamily(**{**base, field: None})


def test_inverse_predicates_bijection_with_registry_judges():
    """recoverability's ``_*_inverse`` set == the registry's judge set.

    A NEW inverse predicate with no registry record is the W-67-8 recurrence (a
    family the judge recognises but no ledger/lane/render wires); a record whose
    judge is not a real inverse is a dangling face. Both turn red.
    """
    defined = set(re.findall(r"def (_\w+_inverse)\(", _src(recoverability)))
    judges = {face.judge.__name__ for face in RECOVERY_FAMILIES}
    assert defined, "enumeration came up EMPTY — suspicious, not 'clean'"
    assert defined == judges, (
        f"recoverability inverse predicates drifted from registry judges.\n"
        f"  defined in recoverability : {sorted(defined)}\n"
        f"  registry judges           : {sorted(judges)}\n"
        f"  A new inverse must gain a full RecoveryFamily record (ledger key + "
        f"  extract + lane + render), else it is a silent fail-closed gap."
    )


def test_ledger_dispatch_is_registry_driven():
    """_mark_bounded_host_recovery iterates the registry — no literal dispatch."""
    src = _code_only(getattr(execution_artifacts, _LEDGER_FN))
    assert (
        "RECOVERY_FAMILIES" in src
        and "face.ledger_extract" in src
        and "face.ledger_key" in src
    ), (
        "the ledger no longer dispatches through RECOVERY_FAMILIES — a "
        "hand-written per-family branch is the W-67-8 shape returning"
    )
    assert not re.search(r'family\s*==\s*"(?:disk|network)"', src), (
        "the ledger reverted to a string-literal family dispatch"
    )


def test_lane_dispatch_is_registry_driven():
    """_resolve_carrier_from_artifact's early lane iterates the registry."""
    src = _code_only(getattr(carriers, "_resolve_carrier_from_artifact"))
    assert (
        "RECOVERY_FAMILIES" in src
        and "face.lane_match" in src
        and "face.ledger_key" in src
    ), "the early-recovery lane no longer dispatches through RECOVERY_FAMILIES"
    assert not re.search(
        r'_normalise_family\([^)]*\)\s*==\s*"(?:disk|network)"', src
    ), "the lane reverted to a per-family string-literal branch (W-67-8 shape)"


def test_render_dispatch_is_registry_driven():
    """provider.recovery_facts_render iterates the registry — no literal reads."""
    src = _code_only(
        k8s_native_provider.K8sNativeProvider.recovery_facts_render
    )
    assert (
        "RECOVERY_FAMILIES" in src
        and "face.render" in src
        and "face.ledger_key" in src
    ), "the render layer no longer dispatches through RECOVERY_FAMILIES"
    assert not re.search(
        r'artifact\.get\(\s*"recovery_(?:fill_path|dm_name|network_rules)"',
        src,
    ), "the render reverted to hardcoded recovery_* field reads"


def test_render_precedence_is_unique():
    """Each face's ``render_precedence`` is distinct across the registry.

    The render layer picks the present face with ``min(present,
    key=render_precedence)``; a tie is broken SILENTLY by iteration order, so
    two faces sharing a precedence would make the rendered reversal depend on
    registry position rather than the intended dm > network > disk-fill
    priority. ``render_precedence`` defaults to 0, so a new face that forgets
    to set it collides with dm — ``__post_init__`` cannot catch that (it sees
    one instance, not its siblings), hence this registry-level invariant.
    """
    precs = [face.render_precedence for face in RECOVERY_FAMILIES]
    assert len(set(precs)) == len(precs), (
        f"render_precedence collided across faces — min() would break the tie "
        f"by iteration order, not intent.\n  "
        f"{[(f.name, f.render_precedence) for f in RECOVERY_FAMILIES]}\n"
        f"  A new face must set a render_precedence distinct from every other."
    )


# ===========================================================================
# I-a ② — readonly dual-use classification (_MUTATING_BINARIES name honesty)
# ===========================================================================
#
# _MUTATING_BINARIES is the terminal one-shot reject, but SOME members have an
# argument-level exemption branch BEFORE the terminal check (a read-only probe
# form is allowed). Those members' membership in the "one-shot reject" set is
# nominal — the set's name lies about them. This anchor makes the split
# EXPLICIT and pins it, so adding an exemption branch for a member (or adding
# a member) without updating the classification turns red.
DUAL_USE_EXEMPT = frozenset({"dd", "nc", "ncat", "socat"})
ONE_SHOT_REJECT = frozenset({"stress", "stress-ng", "fallocate", "fio"})

_TERMINAL_REJECT = "if binary in _MUTATING_BINARIES:"
_DISPATCH = "_BINARY_HANDLERS.get(binary)"


def _pre_terminal_branch_binaries() -> set[str]:
    """Binaries with an argument-level handler consulted BEFORE the terminal.

    Root-cause I-c made this by-construction: ``_classify_argv`` dispatches
    through ``_BINARY_HANDLERS`` (the single binary→handler table) ahead of the
    terminal ``_MUTATING_BINARIES`` check, so the set of binaries carrying a
    pre-terminal argument-level verdict IS the table's key set. The former
    source scan of ``binary == "..."`` literals is gone — a table-driven
    refactor can no longer silently drop a branch head, because the keys ARE
    the enumeration (adding/removing handling is one dict entry).
    """
    return set(readonly._BINARY_HANDLERS)


def test_binary_handler_table_is_well_formed():
    """The dispatch table is non-empty and every key maps to a callable."""
    table = readonly._BINARY_HANDLERS
    assert table, "handler table came up EMPTY — suspicious, not 'clean'"
    for binary, handler in table.items():
        assert callable(handler), f"{binary!r} maps to a non-callable handler"


def test_handler_dispatch_precedes_terminal_reject():
    """The table is consulted BEFORE the terminal one-shot reject.

    Dual-use members (dd/nc/ncat/socat) rely on this ordering: their handler's
    argument-level exemption must run before ``_MUTATING_BINARIES`` refuses
    them. Moving the dispatch below the terminal would silently revoke every
    exemption — a fail-closed regression that reads as "the guard working".

    Comment-stripped (``_code_only``) like the sibling dispatch invariants: a
    prose mention of either literal in a comment must not be what this
    measures, or a comment quoting the terminal above the real dispatch would
    false-red the ordering (empirically confirmed).
    """
    src = _code_only(readonly)
    dispatch_idx = src.index(_DISPATCH)
    terminal_idx = src.index(_TERMINAL_REJECT)
    assert dispatch_idx < terminal_idx, (
        "the _BINARY_HANDLERS dispatch no longer precedes the terminal "
        "_MUTATING_BINARIES reject — dual-use exemptions would be revoked"
    )


def test_dual_use_classification_covers_every_mutating_binary():
    """Full coverage: every member is classified exactly once (member-level)."""
    members = readonly._MUTATING_BINARIES
    assert DUAL_USE_EXEMPT.isdisjoint(ONE_SHOT_REJECT), "a binary is in both classes"
    assert DUAL_USE_EXEMPT | ONE_SHOT_REJECT == members, (
        f"classification does not partition _MUTATING_BINARIES.\n"
        f"  members            : {sorted(members)}\n"
        f"  dual-use (exempt)  : {sorted(DUAL_USE_EXEMPT)}\n"
        f"  one-shot (reject)  : {sorted(ONE_SHOT_REJECT)}\n"
        f"  A NEW member must be classified — an unclassified member defaults "
        f"  to the terminal one-shot reject (the socat recurrence)."
    )


def test_dual_use_exempt_members_have_pre_terminal_branch():
    """Each exempt member actually has an argument-level branch above the
    terminal reject; each one-shot member has none."""
    branched = readonly._MUTATING_BINARIES & _pre_terminal_branch_binaries()
    assert branched == DUAL_USE_EXEMPT, (
        f"pre-terminal exemption branches drifted from the anchor.\n"
        f"  members with a branch : {sorted(branched)}\n"
        f"  anchored dual-use     : {sorted(DUAL_USE_EXEMPT)}\n"
        f"  A new exemption branch means the member is dual-use (read-only "
        f"  probe form allowed) and must move to DUAL_USE_EXEMPT; a branch "
        f"  removed means it is now truly one-shot."
    )


# ===========================================================================
# III-a — derived-axis completeness (parse / materialize / trigger / fail-closed)
# ===========================================================================
#
# A derived axis lets a portable case authorise a write whose TARGET LOCATION
# is decided at cluster runtime (which node the victim scheduled to, which ns
# it lives in). Each axis must be wired at every point; missing one silently
# widens (vacuous match) or drops the authorisation. The anchor maps each axis
# to its known value, its materialize kwarg, and the entry attribute the
# derived value resolves into.
DERIVED_AXES = {
    "name_from": {
        "known_value": mechanism_writes.NAME_FROM_VICTIM_NODE,
        "known_set": mechanism_writes.KNOWN_NAME_FROM,
        "materialize_kwarg": "victim_nodes",
        "materialize_ok": ("node-a",),
        "materialize_empty": (),
        "resolved_attr": "names",
        "resolved_expect": ("node-a",),
        "valid_parse_item": {"scope": "Node", "name_from": "victim_node"},
        "entry": mechanism_writes.MechanismWriteEntry(
            scope="Node", namespace="", name_from="victim_node",
        ),
    },
    "namespace_from": {
        "known_value": mechanism_writes.NAMESPACE_FROM_VICTIM,
        "known_set": mechanism_writes.KNOWN_NAMESPACE_FROM,
        "materialize_kwarg": "victim_namespace",
        "materialize_ok": "ns-a",
        "materialize_empty": "",
        "resolved_attr": "namespace",
        "resolved_expect": "ns-a",
        "valid_parse_item": {
            "scope": "NetworkPolicy", "names": ["drill-x"],
            "namespace_from": "victim",
        },
        "entry": mechanism_writes.MechanismWriteEntry(
            scope="NetworkPolicy", namespace="", names=("drill-x",),
            namespace_from="victim",
        ),
    },
}


def _entry_axis_fields() -> set[str]:
    return {
        f.name
        for f in dataclasses.fields(mechanism_writes.MechanismWriteEntry)
        if f.name.endswith("_from")
    }


def _registry_axis_keys() -> set[str]:
    return {a.key for a in mechanism_writes._DERIVED_AXES}


def test_derived_axes_enumeration_matches_registry_and_dataclass():
    """THREE-WAY enumeration lock (root cause III, post-III-b registry): the
    derivation registry ``_DERIVED_AXES`` (the single declaration point), the
    ``*_from`` fields on the entry dataclass, and this test's anchor must all
    agree. A new axis added as a dataclass field WITHOUT a registry row (or a
    registry row without the field, or either without an anchor entry here)
    turns red — the bolt-on-without-full-wiring recurrence."""
    fields = _entry_axis_fields()
    registry = _registry_axis_keys()
    assert fields, "dataclass enumeration came up EMPTY — suspicious, not 'clean'"
    assert registry, "registry enumeration came up EMPTY — suspicious, not 'clean'"
    assert registry == fields == set(DERIVED_AXES), (
        f"derived-axis enumeration drifted.\n"
        f"  dataclass *_from fields : {sorted(fields)}\n"
        f"  registry axis keys      : {sorted(registry)}\n"
        f"  anchored axes           : {sorted(DERIVED_AXES)}"
    )


def test_registry_rows_are_wellformed():
    """Each registry row points at REAL entry fields (its own key and its
    materialize target) and carries a non-empty known-token set + resolver. A
    malformed row would resolve into a phantom field (silent no-op) or accept
    any token (fail-open). This is the by-construction shape check that
    supersedes source-scanning for the derived-axis chain."""
    field_names = {
        f.name for f in dataclasses.fields(mechanism_writes.MechanismWriteEntry)
    }
    for axis in mechanism_writes._DERIVED_AXES:
        assert axis.key in field_names, (
            f"registry key {axis.key!r} is not an entry field"
        )
        assert axis.target_field in field_names, (
            f"axis {axis.key!r} materializes into phantom field "
            f"{axis.target_field!r}"
        )
        assert axis.known, f"axis {axis.key!r} has an EMPTY known set (fail-open)"
        assert callable(axis.resolve), f"axis {axis.key!r} has no resolver"


@pytest.mark.parametrize("axis", sorted(DERIVED_AXES))
def test_axis_known_set_is_populated_and_contains_value(axis):
    """Anti-silent-empty: the KNOWN_*_FROM set is non-empty and holds the value."""
    spec = DERIVED_AXES[axis]
    assert spec["known_set"], f"{axis}: KNOWN set is empty (fail-open risk)"
    assert spec["known_value"] in spec["known_set"]


@pytest.mark.parametrize("axis", sorted(DERIVED_AXES))
def test_axis_parse_rejects_unknown_value(axis):
    """Parse layer fail-closed: an unknown derivation value is dropped (None),
    never frozen — a typo must not silently widen the write set."""
    spec = DERIVED_AXES[axis]
    bogus = {**spec["valid_parse_item"], axis: "not-a-real-derivation"}
    assert mechanism_writes._parse_entry(bogus, index=0) is None


@pytest.mark.parametrize("axis", sorted(DERIVED_AXES))
def test_axis_parse_accepts_known_value(axis):
    """Parse layer: the known value survives and is preserved on the entry."""
    spec = DERIVED_AXES[axis]
    entry = mechanism_writes._parse_entry(spec["valid_parse_item"], index=0)
    assert entry is not None, f"{axis}: known value {spec['known_value']!r} rejected"
    assert getattr(entry, axis) == spec["known_value"]


@pytest.mark.parametrize("axis", sorted(DERIVED_AXES))
def test_axis_materialize_fail_closed_on_empty_derivation(axis):
    """Materialize layer fail-closed: no derived value → the entry is DROPPED
    (an empty selector would match ANY target — vacuous authorisation)."""
    spec = DERIVED_AXES[axis]
    out = mechanism_writes.materialize_derived_entries(
        (spec["entry"],), **{spec["materialize_kwarg"]: spec["materialize_empty"]},
    )
    assert out == (), f"{axis}: entry not dropped when derivation is empty"


@pytest.mark.parametrize("axis", sorted(DERIVED_AXES))
def test_axis_materialize_resolves_present_derivation(axis):
    """Materialize layer: a present derivation resolves into the entry attr."""
    spec = DERIVED_AXES[axis]
    out = mechanism_writes.materialize_derived_entries(
        (spec["entry"],), **{spec["materialize_kwarg"]: spec["materialize_ok"]},
    )
    assert len(out) == 1, f"{axis}: entry vanished despite a present derivation"
    assert getattr(out[0], spec["resolved_attr"]) == spec["resolved_expect"]


def test_safety_check_trigger_is_registry_driven():
    """Trigger layer (post-III-b): safety_check — the single authoritative
    materialize point — consults the REGISTRY HELPERS, not per-axis attribute
    access, so a new axis is materialized (and, if discovery-backed, queried)
    with NO safety_check edit. Comment-stripped so the wiring comment's prose
    cannot satisfy this; and the old hardcoded ``.name_from`` /
    ``.namespace_from`` special-casing MUST be gone (reverting to a per-axis
    bolt-on turns this red)."""
    code = _code_only(safety_check)
    assert re.search(r"entries_have_derived\s*\(", code), (
        "safety_check no longer gates materialization on the registry helper — "
        "a new derived axis would freeze unmaterialized"
    )
    assert re.search(r"entries_needing_discovery\s*\(", code), (
        "safety_check no longer gates the victim-node query on the registry "
        "helper — a discovery-backed axis would materialize from empty nodes"
    )
    assert not re.search(r"\.name_from\b|\.namespace_from\b", code), (
        "safety_check special-cases a derived axis by attribute — the per-axis "
        "bolt-on the registry exists to remove; route it through "
        "entries_have_derived / entries_needing_discovery instead"
    )


@pytest.mark.parametrize("axis", sorted(DERIVED_AXES))
def test_axis_needs_discovery_flag_matches_its_resolver(axis):
    """The registry's ``needs_discovery`` flag routes an axis to the live
    victim-node query. The name axis derives from a cluster fact
    (discover_victim_nodes → victim_nodes) so it MUST be flagged; the namespace
    axis derives from the already-known spec.namespace (no query) so it MUST
    NOT be — a wrong flag either skips a needed query (empty derivation →
    fail-closed DROP) or fires a pointless one."""
    reg = {a.key: a for a in mechanism_writes._DERIVED_AXES}[axis]
    expect = DERIVED_AXES[axis]["materialize_kwarg"] == "victim_nodes"
    assert reg.needs_discovery is expect


def test_materialize_dispatch_is_registry_driven():
    """Materialize layer (post-III-b): the dispatch iterates the registry
    (``_declared_axes`` / ``axis.resolve`` / ``axis.target_field``) instead of
    hand-written per-axis ``if entry.name_from:`` branches. A new axis then
    materializes through the SAME fail-closed path for free; re-introducing a
    per-axis branch turns this red."""
    code = _code_only(mechanism_writes.materialize_derived_entries)
    assert "_declared_axes(" in code
    assert "axis.resolve(" in code
    assert "axis.target_field" in code
    assert not re.search(r"entry\.name_from\b|entry\.namespace_from\b", code), (
        "materialize_derived_entries special-cases an axis by attribute — the "
        "bolt-on the registry removes"
    )


def test_parse_validation_is_registry_driven():
    """Parse layer (post-III-b): unknown-token rejection and the structural
    rules iterate the registry, so a new axis inherits fail-closed validation
    with no new branch."""
    code = _code_only(mechanism_writes._parse_entry)
    assert "_DERIVED_AXES" in code
    assert "axis.known" in code
