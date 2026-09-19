"""Sentinel: every self-built recovery-carrier case must carry the teardown tail-step knowledge.

Root cause this sentinel pins (R13-2, inject-dfee9d3d): the armed-command
template is knowledge with two sources — the carrier standard
(``references/carrier/recovery-carrier.md`` §4) and each case's inline copy.
A standard upgrade propagates to ZERO cases when the case template is a
stale snapshot: #36 executed its inline ``curl -s`` template verbatim while
the standard had already legislated the self-revoking tail step, so the
carrier RBAC outlived the drill and needed human cleanup (three times:
#12 run5, #7, #36).

The sentinel converts silent drift into loud drift:

- any catalogue case whose text carries the self-built carrier signature
  (``drill-rc-``) must also carry the teardown knowledge transmission point
  (the string ``自断授权尾步`` — pointer line or full exemplar form);
- the exemplar case (#36) must keep the full fail-open form in its inline
  template (``-sf`` + ``&&`` + self-Binding DELETE) so the reference
  implementation cannot silently regress;
- the carrier standard must keep defining the tail step (§4) — the single
  source the case pointers resolve to;
- the standard must keep the two-step stack rule legislated (§2) and no
  catalogue case may teach the merged-flag form — F-A (2026-09-17
  live-fire): ``kubectl create`` union-copies repeated ``--verb``/
  ``--resource``/``--resource-name`` flags into EVERY rule, so folding
  the self-revoke flags into the main create pollutes the main restore
  rule with ``resourceNames`` (SA token GET of the real target -> 403 ->
  case unexecutable). Two-step form measured 200 on the same stack/probe;
  the merged flag string has no legitimate appearance in a case.

Exemptions (``_VEHICLE_TEARDOWN_EXEMPT``) are for cases with a legitimate
reason to carry the carrier signature WITHOUT needing the tail step (e.g. a
future case whose timer host is an existing pod borrowing existing
credentials). Admission bar: an exemption must name its reason here, and
the named file must actually still carry the signature — a stale exemption
is itself a failure (no silent rot).

Legislation checklist (added after K1/K2/K7 — the third consecutive round
of cascade findings; F-G missed stack shapes, J1 missed the byte budget,
K missed the wrap target / count anchors / a provenance slip). Before
writing a NEW rule into the carrier standard, pass four questions:

1. Mechanism scoping — does a known mechanism fact decide WHO the rule
   applies to? (K1: only ``-f`` curls swallow bodies, so only ``-sf``
   curls need the forensic wrap; bare ``-s`` curls already log theirs.)
2. Consumer enumeration — list every referencing surface (case pointers,
   sentinel teeth, preflight verb sets, byte rulings) BEFORE writing;
   three consecutive rounds each missed one.
3. Number provenance — every figure in the rule must be measured, never
   estimated alongside measured ones (K7: 33B was written as ~34B).
4. Anchor alignment — match the word form referencing surfaces use
   (count word vs semantic phrase) to what the sentinel anchors
   (K2: pointers said “五纪律”, no tooth checked the count).
"""

import pathlib
import re

_SKILLS_DIR = pathlib.Path(__file__).resolve().parents[2] / "skills"
_CATALOGUE_DIR = _SKILLS_DIR / "k8s-chaos-skills" / "references" / "catalogue"
_CARRIER_STANDARD = (
    _SKILLS_DIR / "k8s-chaos-skills" / "references" / "carrier" / "recovery-carrier.md"
)

# Self-built carrier signature: the carrier stack naming convention.
_CARRIER_SIGNATURE = "drill-rc-"
# Transmission-point marker: the tail-step knowledge a case must carry.
_TEARDOWN_MARKER = "自断授权尾步"
# The case that legislated the tail step (#36 inject-dfee9d3d) — full form.
_EXEMPLAR_CASE = "Pod_ContainerCreating_无效挂载选项注入.md"

# Legitimate exemptions: basename -> reason. Empty today — the socat-timer
# case (Node_网络故障_节点端口占用) carries no carrier signature and is
# naturally out of scope. Add ONLY with a written reason.
_VEHICLE_TEARDOWN_EXEMPT: dict[str, str] = {}

# The merged self-revoke flag strings (F-A propagation vector): a case
# teaching the model to fold these into ``kubectl create role/clusterrole``
# ships the union-copy pollution (resourceNames leak into the main restore
# rule). Counter-example TEACHING belongs to the standard's generic
# placeholder form (``--resource=<bindings 类>``), never these exact flags
# — so the exact strings have no legitimate appearance in a catalogue case.
_MERGED_FLAG_PATTERNS = (
    "--verb=delete --resource=rolebindings --resource-name=",
    "--verb=delete --resource=clusterrolebindings --resource-name=",
)


def _catalogue_cases() -> list[pathlib.Path]:
    return sorted(_CATALOGUE_DIR.rglob("*.md"))


def _vehicle_cases() -> list[tuple[pathlib.Path, str]]:
    """Cases whose text carries the self-built carrier signature."""
    out: list[tuple[pathlib.Path, str]] = []
    for path in _catalogue_cases():
        text = path.read_text(encoding="utf-8", errors="replace")
        if _CARRIER_SIGNATURE in text:
            out.append((path, text))
    return out


# Six-object stacks (five-piece variant: Role + ClusterRole, TWO
# Bindings) need the dual self-revoke form — see the tests below.
# Declared-stack marker: the two five-piece cases state it as
# "本用例 RBAC 是五件套变体"; #36 references 五件套 only to describe the
# cluster-only four-object variant (五件套 minus the namespaced chain),
# so the "是五件套变体" phrase discriminates real six-object stacks.
_SIX_OBJECT_STACK_MARKER = "是五件套变体"
# The dual-Binding guidance every six-object case pointer must carry.
_DUAL_BINDING_GUIDANCE = "两个 Binding 都删"


def test_vehicle_cases_carry_teardown_knowledge() -> None:
    """Every carrier-signature case must transmit the tail-step knowledge."""
    missing: list[str] = []
    for path, text in _vehicle_cases():
        if path.name in _VEHICLE_TEARDOWN_EXEMPT:
            continue
        if _TEARDOWN_MARKER not in text:
            missing.append(str(path.relative_to(_SKILLS_DIR)))
    assert not missing, (
        "Cases build a self-recovery carrier but never mention the "
        "self-revoking teardown tail step — the carrier RBAC will outlive "
        "the drill (inject-dfee9d3d). Add the carrier-standard §4 pointer "
        f"next to the armed template, or file an exemption with a reason: {missing}"
    )


def test_exemptions_are_not_stale() -> None:
    """An exemption entry must still point at a signature-carrying case."""
    signature_names = {path.name for path, _ in _vehicle_cases()}
    for name in _VEHICLE_TEARDOWN_EXEMPT:
        assert name in signature_names, (
            f"exemption '{name}' no longer carries the carrier signature — "
            "remove the stale entry"
        )


def test_vehicle_scan_is_not_vacuous() -> None:
    """The sentinel must keep scanning a non-trivial corpus."""
    cases = _vehicle_cases()
    assert cases, "no carrier-signature cases found — signature moved?"
    assert len(cases) >= 8, (
        "carrier-signature cases dropped below the known baseline (8) — "
        "either the signature convention changed or the catalogue shrank; "
        "re-baseline deliberately, not silently"
    )


def test_exemplar_case_keeps_full_fail_open_form() -> None:
    """#36's inline template must keep the exemplar fail-open tail step."""
    path = _CATALOGUE_DIR / "Pod_ContainerCreating" / _EXEMPLAR_CASE
    text = path.read_text(encoding="utf-8", errors="replace")
    assert "curl -sf -X PATCH" in text, "exemplar main restore curl lost -sf"
    assert "&& curl -sf -X DELETE" in text, "fail-open && chain broken"
    assert "clusterrolebindings/drill-rc-<hash>" in text, (
        "self-revoke must DELETE the case's own ClusterRoleBinding"
    )


def test_carrier_standard_defines_the_tail_step() -> None:
    """The single source the case pointers resolve to must keep §4 intact."""
    text = _CARRIER_STANDARD.read_text(encoding="utf-8", errors="replace")
    assert "自断授权尾步" in text
    assert "两环 `-f` 缺一不可" in text, (
        "the compact-form exit-code clause (R13-3) was the point of the "
        "review that produced this sentinel — keep it"
    )


def test_carrier_standard_defines_two_step_stack_rule() -> None:
    """The standard must keep the two-step stack rule legislated (F-A).

    Live-fired 2026-09-17: the merged-flag create form produced a
    resourceNames-polluted main rule (SA token GET of the real target:
    403), the two-step form (create, then json-patch append) produced
    the clean two-rule readback and a 200 probe. The legislation text is
    what routes every case builder away from the merged form.
    """
    text = _CARRIER_STANDARD.read_text(encoding="utf-8", errors="replace")
    assert "两步建栈法" in text, "two-step stack rule heading lost"
    assert '"path":"/rules/-"' in text, (
        "the json-patch append form (op add on /rules/-) is the legislated "
        "shape — losing it sends case builders back to flag merging"
    )
    assert "复制进每一条规则" in text, (
        "the pflag union-copy pollution rationale is what makes the ban "
        "stick — keep it"
    )


def test_catalogue_has_no_merged_flag_guidance() -> None:
    """No case may teach the merged-flag form (F-A propagation vector)."""
    offenders: list[str] = []
    for path in _catalogue_cases():
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in _MERGED_FLAG_PATTERNS:
            if pattern in text:
                offenders.append(f"{path.name}: {pattern.strip()}")
    assert not offenders, (
        "A catalogue case carries the merged self-revoke flag form "
        "('--verb=delete --resource=<bindings> --resource-name=...'): "
        "kubectl create union-copies repeated flags into EVERY rule, so "
        "the main restore rule gets resourceNames pollution (GET of the "
        "real target -> 403 -> case unexecutable by construction). Point "
        "the case at the standard §2 two-step stack rule (create, then "
        f"json-patch append the self-revoke rule) instead: {offenders}"
    )


def test_exemplar_case_keeps_two_step_stack_form() -> None:
    """#36 must keep the two-step form in its build step (F-A exemplar)."""
    path = _CATALOGUE_DIR / "Pod_ContainerCreating" / _EXEMPLAR_CASE
    text = path.read_text(encoding="utf-8", errors="replace")
    assert "kubectl patch clusterrole" in text, (
        "exemplar lost the json-patch append command for the self-revoke "
        "rule — the reference implementation regressed to the merged form"
    )
    assert "两步建栈法" in text, (
        "exemplar must cite the standard's two-step stack rule so the "
        "pointer chain resolves to the single source"
    )


def test_carrier_standard_defines_six_object_dual_self_revoke() -> None:
    """The standard must legislate the six-object dual self-revoke (F-G2).

    A six-object stack has two authorization chains (Role + ClusterRole)
    and two Bindings. Deleting only the namespaced RoleBinding — the
    single-stack pointer form the two five-piece cases carried before
    F-G1 — leaves the ClusterRoleBinding (the cluster-scoped grant)
    alive until skeleton expiry: the tail step's purpose is half
    defeated. The standard's §2 must keep (a) the dual self-revoke
    heading, (b) the namespaced append command (``kubectl patch role``
    with ``-n``), and (c) the CRB-first byte-budget tiebreak.
    """
    text = _CARRIER_STANDARD.read_text(encoding="utf-8", errors="replace")
    assert "六对象栈的双自删形态" in text, (
        "six-object dual self-revoke heading lost — five-piece cases "
        "regress to single-Binding deletion and the ClusterRoleBinding "
        "survives to skeleton expiry"
    )
    assert "kubectl patch role drill-rc-<hash>" in text, (
        "the namespaced append command (json-patch on the Role) is the "
        "legislated second half of the dual form — losing it leaves "
        "six-object builders with no template for the RoleBinding rule"
    )
    assert "优先删 ClusterRoleBinding" in text, (
        "the CRB-first byte-budget tiebreak is the decision rule when "
        "both DELETEs do not fit — cluster-scoped grant residue is the "
        "larger surface"
    )


def test_five_piece_cases_carry_dual_binding_guidance() -> None:
    """A six-object case must teach BOTH-Binding deletion (F-G1).

    The two five-piece cases' tail-step pointers said "本用例
    namespaced 栈删 rolebindings" — contradicting each case's own
    declared stack shape and halving the tail step. A case declaring
    the five-piece variant must point at the six-object dual self-
    revoke form and must not carry the single-stack stale phrase.
    """
    missing: list[str] = []
    stale: list[str] = []
    for path, text in _vehicle_cases():
        if _SIX_OBJECT_STACK_MARKER not in text:
            continue
        if _DUAL_BINDING_GUIDANCE not in text:
            missing.append(str(path.relative_to(_SKILLS_DIR)))
        if "namespaced 栈删" in text:
            stale.append(str(path.relative_to(_SKILLS_DIR)))
    assert not missing, (
        "A six-object (five-piece) case teaches single-Binding deletion: "
        "the ClusterRoleBinding would survive to skeleton expiry — the "
        "tail step's purpose is half defeated. Point the case at the "
        f"standard §2 six-object dual self-revoke form: {missing}"
    )
    assert not stale, (
        "A six-object (five-piece) case still carries the single-stack "
        "stale phrase 'namespaced 栈删' — it contradicts the case's own "
        f"declared five-piece stack shape: {stale}"
    )


def test_five_piece_cases_pin_byte_budget_rulings() -> None:
    """Dual-deletion byte rulings must stay pinned per case (J1).

    "Two Bindings both deleted" was legislated without counting bytes:
    measured on the CSI case's own template, the full dual tail step
    lands at ~1061B — over the wiz 1024B hard cap, i.e. NOT physically
    executable in the inline form. The fix pre-computes the budget so
    planning never re-derives it in-flight (#44 lesson). Losing the
    pinned ruling sends the builder back to either in-flight
    arithmetic or an over-cap payload rejected by the channel.
    """
    csi = (
        _CATALOGUE_DIR
        / "Pod_ContainerCreating"
        / "Pod_ContainerCreating_Volume挂载超时CSI异常.md"
    )
    text = csi.read_text(encoding="utf-8", errors="replace")
    assert "超 1024B 硬上限" in text and "双删不可行" in text, (
        "the CSI case lost its pinned byte-budget ruling — the dual "
        "tail step measures over the 1024B hard cap on this template "
        "(~1061B); without the ruling the builder constructs a payload "
        "the channel will reject"
    )
    assert "定案走单删 ClusterRoleBinding 形态" in text, (
        "the CRB-only fallback ruling is the executable form for this "
        "case (~938B, under the cap) — losing it leaves the builder "
        "with an infeasible instruction"
    )
    taint = (
        _CATALOGUE_DIR / "Pod_Pending" / "Pod_Pending_节点Taint无对应Toleration.md"
    )
    taint_text = taint.read_text(encoding="utf-8", errors="replace")
    assert "硬限内但贴限" in taint_text, (
        "the taint case lost its dual-deletion budget reference — the "
        "~1002B near-cap figure and the over-cap fallback order must "
        "stay pinned so planning does not re-derive them"
    )


# L1/L2 (2026-09-17): the -sf body-swallow rule and the forensic-wrap
# discipline. Live evidence: three-form curl matrix on a real carrier pod
# (-sf -> empty body + rc=22; -s -> full body + rc=0; --fail-with-body ->
# body + rc=22, but npd's curl 7.61.1 lacks the flag entirely, so
# template-wide use kills the whole restore chain on that image). The t4
# death chain (near-empty restore.log misdiagnosed as "timer never ran")
# replayed byte-for-byte on curl 8.14.1.


def test_carrier_standard_defines_sf_swallow_troubleshooting() -> None:
    """The standard must keep the -sf body-swallow troubleshooting rule (L1).

    ``-f`` discards the response body on 4xx/5xx, so a failed fire leaves
    restore.log with no apiserver error detail — which got misdiagnosed as
    "the timer never ran" (t4, replayed on 8.14.1; an hour lost chasing
    the wrong suspect). The rule must keep (a) its heading, (b) the
    out-of-band replay shape (same curl minus ``-f``), (c) the
    do-not-strip warning, and (d) the npd 7.61.1 version evidence that
    bans ``--fail-with-body`` from template forms.
    """
    text = _CARRIER_STANDARD.read_text(encoding="utf-8", errors="replace")
    assert "吞 body 排障律" in text, (
        "the -sf body-swallow troubleshooting rule heading was lost — a "
        "near-empty restore.log no longer routes the reader to the "
        "out-of-band replay diagnosis"
    )
    assert "去掉 `-f`" in text, (
        "the diagnostic replay shape (same curl minus -f) is the actionable "
        "core of the rule — without it the rule names the disease but "
        "prescribes no cure"
    )
    assert "不进模板" in text, (
        "the --fail-with-body template ban must stay: npd's curl 7.61.1 "
        "rejects the unknown flag and the whole restore chain dies — "
        "strictly worse than the silent body it was meant to fix"
    )
    assert "7.61.1" in text, (
        "the version evidence is what makes the --fail-with-body ban "
        "stick — losing it invites re-litigating a dead option"
    )


def test_carrier_standard_defines_forensic_wrap_discipline() -> None:
    """The fifth tail-step discipline — forensic wrap (L2) — must stay.

    The wrap captures failure evidence in-band: the full tier re-issues
    the same curl without ``-f`` (the error body lands in restore.log,
    +134B on the compact form), the light tier logs the failing action
    and rc class (+37B). ``false`` is load-bearing in BOTH tiers — the
    ``||`` right side returning 0 masks the failure and defeats the
    fail-open gate (same disease as the R13 compact-form fix; the
    counter-example ``curl -sf ... || echo ...`` leaves the chain exit
    at 0, measured). Budget is subordinate: tier down (full -> light ->
    bare -sf) before trimming the restore; the self-revoke DELETE is
    never wrapped.
    """
    text = _CARRIER_STANDARD.read_text(encoding="utf-8", errors="replace")
    assert "取证包裹律" in text, (
        "the fifth discipline heading was lost — main restore curls lose "
        "the in-band failure-evidence option and planners fall back to "
        "out-of-band replay only"
    )
    assert "|| { curl -s" in text, (
        "the full-tier wrap shape is the mechanism itself — without the "
        "literal form the discipline is unactionable prose"
    )
    assert "轻档" in text, (
        "the light tier is the budget escape hatch for near-cap cases "
        "(J1-pinned CSI: full tier lands over the 1024B cap, light tier "
        "fits) — losing it makes the discipline inapplicable exactly "
        "where diagnosability matters most"
    )
    assert "自删 DELETE 不包" in text, (
        "the self-revoke DELETE exemption keeps the wrap budget honest: "
        "its failure is authorization residue (collected by the teardown "
        "sweep), not a restore-path diagnostic target"
    )
    assert "包裹对象 = 带 `-sf` 的 curl" in text, (
        "the wrap-target scoping clause (K1) was lost — bare -s curls "
        "already log their bodies, so wrapping them is pure byte waste "
        "(CSI full-wrap lands over the 1024B cap while sf-only fits at "
        "971B)"
    )


# K2: the case pointers reference the tail-step discipline list BY COUNT
# ("五纪律"), and no tooth checked that count — a discipline merge would
# drift all 8 pointers silently. The sentinel's philosophy prefers
# semantic anchors, but the pointers chose count words; the sides must
# align at one of them. This tooth aligns them at the live count.
_CN_DIGITS = {"四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_POINTER_COUNT_RE = re.compile(r"([四五六七八九十])纪律以标准件第四节为准")
_STANDARD_COUNT_RE = re.compile(r"([四五六七八九十])条纪律：")


def test_case_pointer_discipline_count_matches_standard() -> None:
    """Every count-word discipline pointer must match the live count (K2)."""
    standard = _CARRIER_STANDARD.read_text(encoding="utf-8", errors="replace")
    m = _STANDARD_COUNT_RE.search(standard)
    assert m is not None, (
        "the standard lost its 'N条纪律：' heading — the live discipline "
        "count is no longer machine-readable; re-pin the heading so the "
        "count-consistency tooth keeps working"
    )
    live = _CN_DIGITS[m.group(1)]
    stale: list[str] = []
    for path, text in _vehicle_cases():
        for pm in _POINTER_COUNT_RE.finditer(text):
            if _CN_DIGITS[pm.group(1)] != live:
                stale.append(
                    f"{path.name}: pointer says {pm.group(1)}纪律, "
                    f"the standard now has {live}"
                )
    assert not stale, (
        "Case pointers reference the §4 discipline list by a stale count "
        "word — the standard's discipline list changed without the "
        "cascade update (three consecutive rounds missed a consumer "
        f"surface; legislation checklist Q2): {stale}"
    )
