"""Case-file legislation audit: frontmatter keys the guard chain consumes.

The run8 inject-2a8cd99a deadlock family taught the failure shape this file
pins out: the case frontmatter is LEGISLATION — ``recovery_channel`` routes
the apiserver-write domain (D3 source 1, consulted before the verb-vocabulary
proxy; since openspec faultdrill-cluster-native-recovery M2 the declared
address is served by the programmatic recovery-carrier assembler, with the
LLM SOP as the degraded fallback) and ``mechanism_writes`` admits the
mechanism writes. Both parsers are deliberately fail-closed-quiet (unknown
value / malformed entry → WARNING log → the declaration is dropped → the
gate falls back to pre-declaration behaviour). That posture is right at
runtime (a parse must never raise and must never widen admission); the QUIET
part is what this audit replaces for the repo's own cases: a typo'd value
or a silently-dropped manifest would resurrect the deadlock with nothing
but a WARNING line to show for it.

Three invariants, over every real catalogue case:

1. ``recovery_channel`` raw value is either absent or a known channel —
   a typo (``apiserver_write``) must fail HERE, not as a silent fallback.
2. A declared ``mechanism_writes`` list survives parsing whole — zero
   parsed entries (total silent failure) or fewer than declared (partial
   drop) are both legislation erosion.
3. An ``apiserver-write`` declaration carries NO ``faultdrill`` mechanism
   entry — the faultdrill admission legislation retired with the CR
   channel (M2 tasks 2.4/2.5): the carrier stack (SA/Role/RoleBinding/
   bare Pod) is built programmatically inside the assembler tool and does
   not ride the LLM write-set at all (design ND3), so a surviving
   ``faultdrill`` entry is dead legislation whose only live consumer is
   the route gate's residual-CR-apply rejection (the inverse of the run8
   shape: there a declaration without an entry was dead; here an entry
   without a consumer is).

Enumerated-file guard: the catalogue must actually enumerate (>= 60 files)
so a renamed/moved directory fails loudly here instead of letting every
parametrized case silently collect zero items and pass vacuously.
"""

from pathlib import Path

import pytest

from chaos_agent.agent.target_guard.mechanism_writes import (
    KNOWN_RECOVERY_CHANNELS,
    RECOVERY_CHANNEL_APISERVER_WRITE,
    parse_mechanism_writes,
)
from chaos_agent.skills.loader import parse_frontmatter

_SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"
_CATALOGUE = _SKILLS_DIR / "k8s-chaos-skills" / "references" / "catalogue"
_CASE_FILES = sorted(_CATALOGUE.glob("**/*.md"))


def _frontmatter_of(case_path: Path) -> dict:
    fm = parse_frontmatter(case_path.read_text(encoding="utf-8"))
    return fm or {}


def test_catalogue_is_present_and_enumerated():
    # Silent-empty-collection guard: a moved/renamed catalogue would make
    # every parametrized audit below collect zero items and pass vacuously.
    # 68 real files at legislation time; 60 keeps headroom for curated
    # removals while still failing loudly on structural loss.
    assert len(_CASE_FILES) >= 60, (
        f"catalogue enumeration collapsed to {len(_CASE_FILES)} files "
        f"under {_CATALOGUE} — investigate before trusting the audits below"
    )


@pytest.mark.parametrize(
    "case_path", _CASE_FILES,
    ids=[str(p.relative_to(_CATALOGUE)) for p in _CASE_FILES],
)
def test_recovery_channel_value_is_legislated_or_absent(case_path):
    raw = str(_frontmatter_of(case_path).get("recovery_channel") or "").strip()
    if not raw:
        return
    assert raw.lower() in KNOWN_RECOVERY_CHANNELS, (
        f"{case_path.name}: recovery_channel {raw!r} is not a known channel "
        f"({sorted(KNOWN_RECOVERY_CHANNELS)}) — the parser would silently "
        "drop it and the route gate would fall back to the verb proxy "
        "(the run8 deadlock family: declared-but-ignored legislation)"
    )


@pytest.mark.parametrize(
    "case_path", _CASE_FILES,
    ids=[str(p.relative_to(_CATALOGUE)) for p in _CASE_FILES],
)
def test_declared_manifest_survives_parsing_whole(case_path):
    raw = _frontmatter_of(case_path).get("mechanism_writes")
    if not isinstance(raw, list) or not raw:
        return
    entries = parse_mechanism_writes(case_path.read_text(encoding="utf-8"))
    assert entries, (
        f"{case_path.name}: mechanism_writes declares {len(raw)} entries "
        "but the parser dropped the whole manifest — silent YAML/structure "
        "failure erasing the widened write-set legislation"
    )
    assert len(entries) == len(raw), (
        f"{case_path.name}: mechanism_writes declares {len(raw)} entries "
        f"but only {len(entries)} parsed — a partial drop silently "
        "narrows the approved write set (guard rejects what the case "
        "legislated)"
    )


@pytest.mark.parametrize(
    "case_path", _CASE_FILES,
    ids=[str(p.relative_to(_CATALOGUE)) for p in _CASE_FILES],
)
def test_apiserver_write_declaration_carries_no_faultdrill_entry(case_path):
    declared = str(
        _frontmatter_of(case_path).get("recovery_channel") or "",
    ).strip().lower()
    if declared != RECOVERY_CHANNEL_APISERVER_WRITE:
        return
    entries = parse_mechanism_writes(case_path.read_text(encoding="utf-8"))
    assert not any(e.scope == "faultdrill" for e in entries), (
        f"{case_path.name}: declares recovery_channel: apiserver-write and "
        "its mechanism_writes manifest still carries a faultdrill entry — "
        "the faultdrill admission legislation retired with the CR channel "
        "(M2 tasks 2.4/2.5; the assembler carrier stack is built "
        "programmatically inside the tool, design ND3, and never rides "
        "the LLM write-set), so the entry is dead legislation whose only "
        "live consumer is the route gate's residual-CR-apply rejection"
    )
