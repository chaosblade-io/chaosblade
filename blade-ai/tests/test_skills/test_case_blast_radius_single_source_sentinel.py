"""Sentinel: the carrier-stack blast_radius_scope has ONE authority (§9), not per-case re-declaration.

Root cause this sentinel pins (inject-38a70d8c time-account): ``blast_radius_scope``
for a self-built recovery carrier is legislated ONCE by
``references/carrier/recovery-carrier.md`` §9 — "四件套 (Pod/SA/Role/RoleBinding)
→ namespace-wide" (定案免推导, precedent #44: the two-round
target-only↔namespace-wide debate cost ~110s and reached opposite conclusions).
But ~6 apiserver-write case docs each carried their OWN "爆炸半径分类（定案）：
target-only" line, reasoning "载体四件套为演练新建资产，新建资产不构成影响其他资源"
— applying SKILL.md ①'s ephemeral-debug-carrier exemption (node-debugger, NO new
RBAC) to the four-piece carrier (WITH a new namespaced RBAC grant surface). Two
"定案" then contradicted, so the planning LLM re-adjudicated the settled debate
every run (msg15 reasoning=18799 chars, blast_radius mentioned 25×).

The terminology conflation is the deeper root: a case doc's "爆炸半径分类" meant
the FAULT-BODY write-set (only the target object is mutated → genuinely
target-only, a real fact that belongs in ``blast_radius_detail``), while
``finish_planning``'s ``blast_radius_scope`` field means the TOTAL declared
impact INCLUDING the carrier RBAC surface (§9 → namespace-wide). Same term, two
definitions, two "定案" → forced reconciliation. Under-declaration is also the
worse error (starves elevated safety review — see the widest-scope rule).

The sentinel converts silent drift into loud drift:

- any catalogue case recovering via ``recovery_channel: apiserver-write``
  (four-piece carrier) may NOT declare ``blast_radius_scope=target-only`` —
  it must defer to §9 (namespace-wide, or cluster-wide for the five-piece /
  cross-ns variant);
- the six migrated cases must keep positively declaring a §9-aligned scope
  (non-vacuous: the scan has teeth and the migration actually landed);
- §9 must keep legislating four-piece → namespace-wide (the single source the
  case docs defer to);
- SKILL.md ①'s ephemeral-carrier exemption must keep the boundary note that
  distinguishes no-RBAC debug carriers from the four-piece recovery carrier —
  else the target-only/namespace-wide gap re-opens at the top authority.
"""

import pathlib
import re

_SKILLS_DIR = pathlib.Path(__file__).resolve().parents[2] / "skills"
_CATALOGUE_DIR = _SKILLS_DIR / "k8s-chaos-skills" / "references" / "catalogue"
_CARRIER_STANDARD = (
    _SKILLS_DIR / "k8s-chaos-skills" / "references" / "carrier" / "recovery-carrier.md"
)
_SKILL_MD = _SKILLS_DIR / "k8s-chaos-skills" / "SKILL.md"

# Four-piece recovery carrier signature: the case recovers by writing K8s
# objects through a self-built SA/Role/RoleBinding/Pod stack (§9①).
_CARRIER_CHANNEL_RE = re.compile(r"recovery_channel:\s*apiserver-write")

# Canonical declared-scope marker (post-migration form):
#   爆炸半径分类（定案，defer …）**：声明 `blast_radius_scope=<scope>`
# The marker is the reliable anchor for the DECLARED field value — the same
# line legitimately mentions "target-only 语义" for the fault-body write-set,
# so a naive substring scan would false-positive.
_DECLARED_SCOPE_RE = re.compile(r"blast_radius_scope\s*=\s*`?([a-z-]+)`?")
# Legacy form (un-migrated / regression): 爆炸半径分类（定案）**：`<scope>`
_LEGACY_SCOPE_RE = re.compile(r"爆炸半径分类（定案）\*\*：\s*`([a-z-]+)`")

# The six cases migrated to defer §9 ( Secret/ConfigMap content-tamper, two
# Ingress entry faults, LimitRange + NetworkPolicy misconfig). Pinned so the
# non-vacuous tooth fails loudly if a migration is silently reverted.
_MIGRATED_CASES = {
    "Secret_内容错误_凭证被篡改.md",
    "ConfigMap_内容错误_关键配置被篡改.md",
    "Ingress_入口异常_TLS证书过期.md",
    "Ingress_入口异常_后端服务不存在.md",
    "Pod_Pending_LimitRange误配.md",
    "Pod_网络故障_NetworkPolicy误配.md",
}


def _catalogue_cases() -> list[pathlib.Path]:
    return sorted(_CATALOGUE_DIR.rglob("*.md"))


def _carrier_cases() -> list[tuple[pathlib.Path, str]]:
    """Cases recovering via the apiserver-write four-piece carrier."""
    out: list[tuple[pathlib.Path, str]] = []
    for path in _catalogue_cases():
        text = path.read_text(encoding="utf-8", errors="replace")
        if _CARRIER_CHANNEL_RE.search(text):
            out.append((path, text))
    return out


def _blast_line(text: str) -> str | None:
    """The 爆炸半径分类 declaration line, if the case carries one."""
    for line in text.splitlines():
        if "爆炸半径分类" in line:
            return line
    return None


def _declared_blast_scope(blast_line: str) -> str | None:
    """The DECLARED blast_radius_scope value (canonical marker wins over legacy)."""
    m = _DECLARED_SCOPE_RE.search(blast_line)
    if m:
        return m.group(1)
    m = _LEGACY_SCOPE_RE.search(blast_line)
    if m:
        return m.group(1)
    return None


def test_carrier_cases_do_not_under_declare_target_only() -> None:
    """A four-piece-carrier case must defer to §9, never declare target-only."""
    offenders: list[str] = []
    for path, text in _carrier_cases():
        line = _blast_line(text)
        if line is None:
            # No explicit declaration → the case defers to §9 implicitly. Fine.
            continue
        if _declared_blast_scope(line) == "target-only":
            offenders.append(str(path.relative_to(_SKILLS_DIR)))
    assert not offenders, (
        "These cases recover via the apiserver-write four-piece carrier "
        "(Pod/SA/Role/RoleBinding → a new namespaced RBAC grant surface) yet "
        "declare blast_radius_scope=target-only — contradicting "
        "recovery-carrier §9① (four-piece → namespace-wide, 定案免推导) and "
        "re-opening the #44-settled ~110s target-only↔namespace-wide debate "
        "every planning run. Under-declaration also starves elevated safety "
        "review (the worse error). Declare `blast_radius_scope=namespace-wide` "
        "(cluster-wide for the five-piece/cross-ns variant) and keep the "
        "fault-body write-set fact (target-only) in blast_radius_detail: "
        f"{offenders}"
    )


def test_blast_radius_scan_is_not_vacuous() -> None:
    """The sentinel keeps scanning a real corpus AND the migration landed."""
    cases = _carrier_cases()
    assert len(cases) >= len(_MIGRATED_CASES), (
        "apiserver-write carrier cases dropped below the migrated baseline — "
        "either the recovery_channel convention changed or the catalogue "
        "shrank; re-baseline deliberately, not silently"
    )
    declared: dict[str, str | None] = {}
    for path, text in cases:
        if path.name in _MIGRATED_CASES:
            line = _blast_line(text)
            declared[path.name] = _declared_blast_scope(line) if line else None
    missing = _MIGRATED_CASES - set(declared)
    assert not missing, (
        f"migrated cases no longer carry the apiserver-write channel: {missing}"
    )
    for name, scope in declared.items():
        assert scope in ("namespace-wide", "cluster-wide"), (
            f"{name}: expected a §9-aligned declared scope "
            f"(namespace-wide/cluster-wide), got {scope!r} — migration reverted?"
        )


def test_carrier_standard_defines_four_piece_namespace_wide() -> None:
    """§9 — the single source the case docs defer to — must keep legislating."""
    text = _CARRIER_STANDARD.read_text(encoding="utf-8", errors="replace")
    assert "四件套" in text and "namespace-wide" in text, (
        "recovery-carrier §9 must keep legislating four-piece → namespace-wide; "
        "the case-doc defer pointers resolve to nothing if this is removed"
    )
    assert "定案免推导" in text, (
        "§9 must keep the 定案免推导 marker — it is what tells the planning LLM "
        "to copy the value instead of re-deriving it"
    )


def test_skill_md_keeps_carrier_boundary_crossref() -> None:
    """SKILL.md ① must keep distinguishing no-RBAC debug carriers from §9's four-piece."""
    text = _SKILL_MD.read_text(encoding="utf-8", errors="replace")
    assert "无新增 RBAC" in text and "四件套 carrier" in text, (
        "SKILL.md ①'s ephemeral-carrier exemption must keep the boundary note "
        "that a four-piece recovery carrier (new namespaced RBAC) is NOT the "
        "no-RBAC debug-carrier exemption and is governed by §9 → namespace-wide; "
        "without it the target-only/namespace-wide gap re-opens at the top authority"
    )
