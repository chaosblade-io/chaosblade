r"""Sentinel: a host case whose ``## 注入验证`` section prescribes ``tcpdump`` as
an observation probe MUST disclose that ``tcpdump`` is a NON-UNIVERSAL tool face
(absent on minimal targets — ``which`` empty / ``exit 127``), and that its absence
does NOT invalidate the universal white-box criterion nor justify retrying.

This is the M3 (tool-face reachability) tooth for the *corroboration-probe*
sub-form. Its siblings prove the other axes / sub-forms of criterion reachability:
``test_host_case_commands_pass_guard.py`` (M1 — every prescribed host command is
ADMITTED by a guard channel),
``test_host_case_criterion_reachability_sentinel.py`` (M2 — a ptrace-exclusive
tracer means demotes its window-unreachable criterion), and
``test_host_case_dns_probe_universality_sentinel.py`` (M3 承重 sub-form — a
DNS-fault criterion must probe via the universal glibc NSS path, never rely solely
on dnsutils ``nslookup`` / ``dig``). M3 has two sub-forms with different fixes:

  ① LOAD-BEARING probe non-universal (DNS ``nslookup``): the criterion itself
     depends on an absent tool, so it MUST be swapped to a universal probe on the
     same observation face (``getent`` / ``ping``).
  ② CORROBORATION probe non-universal (netem weak-criterion family ``tcpdump``):
     the probe is only a conditional side-evidence — the load-bearing criterion is
     the universal white-box ``tc -s qdisc show`` (netem sub-band ``Sent … pkt > 0``).
     Here the fix is NOT a swap but a DISCLOSURE: state that ``tcpdump`` may be
     absent, that its absence does not negate the white-box criterion, and that the
     agent must not retry or judge the injection failed because of it.

Sub-form ② carries TWO teeth, because disclosure is only the defensive half:

  * ``test_tcpdump_verification_case_discloses_tool_face`` (DEFENSIVE) — every
    ``tcpdump``-prescribing doc discloses the tool face is non-universal. Fires on
    all four tcpdump docs (incl. 网络丢包隔离).
  * ``test_netem_corroboration_case_addresses_ping_face`` (OFFENSIVE) — every netem
    weak-criterion doc additionally ADDRESSES the ICMP-ping universal behavioural
    face: ``ping`` is universally present and ICMP echo has no receiver-side seq
    dedup, so a narrowed pingable target yields a behavioural signature directly.
    Two legitimate forms: (a) reachable -> prescribe ping as the equivalent
    corroboration (报文重复 ``+N duplicates`` / 报文损坏 retrans-latency); (b)
    structurally unreachable -> declare it plus rationale (报文乱序: companion delay
    => no overtaking, fast-interval ping flood-rejected). Scoped by MEANS CLASS
    (``netem`` in the section) so 网络丢包隔离 (no netem qdisc) is excluded. This is
    the family invariant whose absence was measured live on Case #23 (报文重复 bound
    only tcpdump, forcing the verify agent to re-derive ICMP-no-dedup on its own).

Root cause this sentinel pins (Case #23, ``Host_网络故障_报文乱序``,
inject-27a33773, measured live 2026-11): the netem weak-criterion docs
(``报文乱序`` / ``报文重复`` / ``报文损坏``) prescribed ``tcpdump -ni <if> -c 200``
as the behaviour-level corroboration for an effect the TCP stack conditionally
absorbs, but never disclosed that ``tcpdump`` ships in a package a minimal ubuntu
target does NOT install by default. The verify agent had to discover the absence at
runtime (``which`` empty / ``exit 127``) and improvise an ICMP-ping sidestep — for
reorder that sidestep is additionally structurally unreachable (the mandatory
companion delay means a later packet cannot overtake an earlier one at the default
1s interval, and the fast-interval form ``ping -i <0.1`` is rejected as
traffic-amplification / flood by the read-only guard). So ``icmp_seq`` staying
monotonic is channel-unreachability, not under-sampling — the white-box
``tc -s qdisc show`` was already sufficient. ``Host_网络故障_网络丢包隔离`` offers
``tcpdump`` as one alternative for the ``in`` direction and is held to the same
disclosure contract.

The tooth keys on the MEANS CLASS (a ``## 注入验证`` section that prescribes
``tcpdump``), never on a case name — so any future host case leaning on ``tcpdump``
is caught too (``b5c0af28``: fix the class, not the instance; ``b4922816``: the fix
covers every sibling doc sharing the root cause). The single-source authoring
contract this enforces lives at
``skills/host-chaos-skills/references/criterion-reachability.md`` §五 「工具面」.

Section extraction reuses the authoritative single source
``chaos_agent.utils.skill_case_section.find_section`` — the sentinel does NOT
reinvent section boundaries. ``find_section`` returns the ORIGINAL section text
(fenced code intact), so ``tcpdump`` is detected whether it lives in a fenced
command block or in the 判据 prose, and the disclosure is detected wherever in the
section it appears.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from chaos_agent.utils.skill_case_section import find_section

_SKILLS_DIR = pathlib.Path(__file__).resolve().parents[2] / "skills"
_HOST_CATALOGUE = _SKILLS_DIR / "host-chaos-skills" / "references" / "catalogue"

#: Means-class signature: the doc's ``## 注入验证`` section prescribes ``tcpdump``
#: as an observation probe. Keyed on COMMAND SHAPE (the tool name in the section),
#: never on a case name. ``tcpdump`` is the packet-capture tool that a minimal
#: target does NOT install by default, so any criterion leaning on it inherits the
#: M3 tool-face risk.
_TCPDUMP_RE = re.compile(r"\btcpdump\b", re.IGNORECASE)

#: The M3 disclosure the section MUST carry next to a prescribed ``tcpdump``: an
#: explicit statement that the tool face is non-universal / not installed by
#: default / absent (``which`` empty / ``exit 127``). Any of these honest phrasings
#: satisfies the tooth; a doc that prescribes ``tcpdump`` with NONE of them is the
#: M3 corroboration-probe defect this sentinel rules out.
_M3_DISCLOSURE_RE = re.compile(
    r"工具面|非普适|默认不装|未安装|未默认安装|exit\s*127|which\s*空",
    re.IGNORECASE,
)

#: Anti-vacuous baseline (measured 2026-11): exactly these four host docs prescribe
#: ``tcpdump`` in their ``## 注入验证`` section. They MUST be in the hit set — if the
#: extractor silently stops matching them the tooth would go vacuous (the failure
#: mode ``b5c6b377`` names). The tooth itself is "EVERY hit doc discloses the M3
#: tool-face risk", NOT "hit set == these four" (a case-name branch).
_KNOWN_TCPDUMP_CASES = (
    "Host_网络故障_报文乱序.md",
    "Host_网络故障_报文重复.md",
    "Host_网络故障_报文损坏.md",
    "Host_网络故障_网络丢包隔离.md",
)

#: Means-class scoping for the SECOND tooth (ICMP-ping behavioural face): the netem
#: weak-criterion family (``报文乱序`` / ``报文重复`` / ``报文损坏``) hangs a ``netem``
#: qdisc rule and prescribes ``tcpdump`` as its behaviour-level corroboration, so it
#: inherits the ICMP-ping face obligation. ``网络丢包隔离`` also prescribes
#: ``tcpdump`` (for the ``in`` direction) but does NOT hinge on a ``netem`` qdisc —
#: its behavioural face is peer-access / drop-rate, not ping-duplicates — so keying
#: the second tooth on ``netem`` in the section (MEANS CLASS, never a case name)
#: cleanly excludes it. Verified against the authoritative extractor: the three
#: netem docs carry ``netem`` in 注入验证, 网络丢包隔离 does not.
_NETEM_RE = re.compile(r"\bnetem\b", re.IGNORECASE)

#: The ICMP-ping universal behavioural face a netem weak-criterion doc MUST address
#: next to its non-universal ``tcpdump`` corroboration probe. ``ping`` is universally
#: present (unlike ``tcpdump``) and ICMP echo has NO receiver-side seq dedup (unlike
#: TCP), so when the target is narrowed to a pingable address ``ping`` yields a
#: behavioural signature directly. Disclosure ("tcpdump may be absent, don't retry")
#: is only the DEFENSIVE half; the OFFENSIVE half is addressing the ping face, in one
#: of exactly two legitimate ways — the tooth requires the section to mention
#: ping/ICMP AND carry EITHER form:
#:   (a) REACHABLE -> prescribe ping as the equivalent behavioural corroboration
#:       (``_PING_REACHABLE_RE``: duplicate ``+N duplicates``/``(DUP!)``; corrupt
#:       retrans/latency via ``ss -ti`` / ``ping``);
#:   (b) STRUCTURALLY UNREACHABLE (reorder) -> declare the unreachability plus its
#:       rationale (``_PING_UNREACHABLE_RE``: the mandatory companion delay means a
#:       later packet cannot overtake an earlier one at the default interval, and the
#:       fast-interval form ``ping -i <0.1`` is flood-rejected by the read-only guard).
#: A netem doc that prescribes ``tcpdump`` but SILENTLY SKIPS the ping face (neither
#: (a) nor (b)) is the family-invariant defect this tooth rules out — the exact gap
#: measured live on Case #23 (``报文重复``), where the doc bound only ``tcpdump`` and
#: the verify agent had to re-derive ICMP-no-dedup on its own.
_PING_FACE_RE = re.compile(r"\bping\b|ICMP", re.IGNORECASE)
_PING_REACHABLE_RE = re.compile(
    r"普适在场|等价佐证|作佐证|作行为佐证|替代观测面|duplicates|DUP|看重传|看时延|重传\s*/\s*时延",
    re.IGNORECASE,
)
_PING_UNREACHABLE_RE = re.compile(
    r"结构性不可达|无法超车|算术上|flood|流量放大|通道不可达",
    re.IGNORECASE,
)

#: Anti-vacuous baseline for the second tooth (verified 2026-11 against the
#: authoritative extractor): exactly these three netem weak-criterion docs prescribe
#: ``tcpdump`` AND ``netem`` in 注入验证. They MUST be in the hit set, and
#: ``网络丢包隔离`` MUST NOT (it is not netem family) — else the means-class scoping
#: has drifted and the tooth is either vacuous or over-firing.
_KNOWN_NETEM_CORROBORATION_CASES = (
    "Host_网络故障_报文乱序.md",
    "Host_网络故障_报文重复.md",
    "Host_网络故障_报文损坏.md",
)


def _host_case_docs() -> list[pathlib.Path]:
    return sorted(_HOST_CATALOGUE.rglob("*.md"))


def _tcpdump_cases() -> list[tuple[str, str]]:
    """``(case_relpath, 注入验证_section_text)`` for every case prescribing tcpdump.

    The 注入验证 body comes from the authoritative single-source extractor; a doc
    with no such section is skipped (it prescribes nothing to verify against). The
    trigger is scoped to the 注入验证 section only — a tcpdump mention elsewhere
    (e.g. 手段2 recovery prose) is not a verification criterion.
    """
    out: list[tuple[str, str]] = []
    for path in _host_case_docs():
        text = path.read_text(encoding="utf-8", errors="replace")
        section = find_section(text, "注入验证")
        if section is None:
            continue
        if not _TCPDUMP_RE.search(section):
            continue
        rel = str(path.relative_to(_SKILLS_DIR))
        out.append((rel, section))
    return out


_TCPDUMP_VERIFICATION_CASES = _tcpdump_cases()

#: netem weak-criterion family subset of the tcpdump corpus — the docs the SECOND
#: tooth (ICMP-ping behavioural face) fires on. Scoped by MEANS CLASS (``netem`` in
#: the 注入验证 section), which excludes 网络丢包隔离 (tcpdump for `in`-direction
#: peer observation, no netem qdisc).
_NETEM_CORROBORATION_CASES = [
    (rel, section)
    for rel, section in _TCPDUMP_VERIFICATION_CASES
    if _NETEM_RE.search(section)
]


def test_tcpdump_verification_corpus_is_not_empty() -> None:
    """Guards the extractor: the known tcpdump-prescribing cases MUST be detected,
    else every assertion below is vacuous."""
    hit_names = {pathlib.Path(rel).name for rel, _ in _TCPDUMP_VERIFICATION_CASES}
    missing = set(_KNOWN_TCPDUMP_CASES) - hit_names
    assert not missing, (
        f"means-signature extractor missed known tcpdump-prescribing cases: "
        f"{sorted(missing)} (detected: {sorted(hit_names)}) — the regex, the "
        f"section extractor, or the corpus changed shape"
    )


@pytest.mark.parametrize(
    "case,section",
    _TCPDUMP_VERIFICATION_CASES,
    ids=[rel for rel, _ in _TCPDUMP_VERIFICATION_CASES],
)
def test_tcpdump_verification_case_discloses_tool_face(
    case: str, section: str,
) -> None:
    assert _M3_DISCLOSURE_RE.search(section), (
        f"{case} prescribes `tcpdump` in its `## 注入验证` section, so it MUST "
        f"disclose that tcpdump is a NON-UNIVERSAL tool face — a minimal target "
        f"does not install it by default (`which` empty / `exit 127`). Without the "
        f"disclosure the verify agent discovers the absence at runtime and burns a "
        f"round improvising a sidestep (Case #23 measured this live). State that "
        f"tcpdump may be absent, that its absence does NOT negate the universal "
        f"white-box criterion (`tc -s qdisc show` sub-band `Sent … pkt > 0` for the "
        f"netem weak-criterion family; the peer-access path for 网络丢包隔离 `in`), "
        f"and that the agent must NOT retry or judge the injection failed because "
        f"of it. See skills/host-chaos-skills/references/criterion-reachability.md "
        f"§五 「工具面（探针普适性，M3）」 — corroboration-probe sub-form ②."
    )


def test_netem_corroboration_corpus_is_scoped_correctly() -> None:
    """Guards the SECOND tooth's means-class scoping: the three netem
    weak-criterion docs MUST be in the corpus, and 网络丢包隔离 (tcpdump but no
    netem qdisc) MUST NOT — else the scoping drifted and the tooth below is either
    vacuous (empty corpus) or over-firing on a non-netem doc."""
    hit_names = {pathlib.Path(rel).name for rel, _ in _NETEM_CORROBORATION_CASES}
    missing = set(_KNOWN_NETEM_CORROBORATION_CASES) - hit_names
    assert not missing, (
        f"netem-family extractor missed known netem+tcpdump cases: {sorted(missing)} "
        f"(detected: {sorted(hit_names)}) — the netem regex, the tcpdump corpus, or "
        f"the section extractor changed shape"
    )
    assert "Host_网络故障_网络丢包隔离.md" not in hit_names, (
        "网络丢包隔离 leaked into the netem-family corpus: it prescribes tcpdump for "
        "the `in` direction but hangs no netem qdisc, so its behavioural face is "
        "peer-access, not ICMP-ping duplicates — the means-class scoping (_NETEM_RE) "
        "must exclude it"
    )


@pytest.mark.parametrize(
    "case,section",
    _NETEM_CORROBORATION_CASES,
    ids=[rel for rel, _ in _NETEM_CORROBORATION_CASES],
)
def test_netem_corroboration_case_addresses_ping_face(
    case: str, section: str,
) -> None:
    """SECOND tooth (OFFENSIVE half of M3②): a netem weak-criterion doc that
    prescribes ``tcpdump`` as corroboration MUST address the ICMP-ping universal
    behavioural face — either (a) prescribe ping as the equivalent corroboration
    (reachable: 报文重复/报文损坏) or (b) declare ping's structural unreachability
    with rationale (报文乱序). Silently skipping the ping face is the family
    invariant defect measured live on Case #23."""
    mentions_ping = _PING_FACE_RE.search(section)
    reachable = _PING_REACHABLE_RE.search(section)
    unreachable = _PING_UNREACHABLE_RE.search(section)
    assert mentions_ping and (reachable or unreachable), (
        f"{case} is a netem weak-criterion doc prescribing `tcpdump` corroboration, "
        f"so it MUST address the ICMP-ping universal behavioural face — `ping` is "
        f"universally present (unlike tcpdump) and ICMP echo has no receiver-side seq "
        f"dedup, so a narrowed pingable target yields a behavioural signature "
        f"directly. Disclosure that tcpdump may be absent is only the DEFENSIVE half; "
        f"the OFFENSIVE half is the ping face, in one of two legitimate forms: "
        f"(a) REACHABLE -> prescribe ping as the equivalent behavioural corroboration "
        f"(duplicate `+N duplicates`/`(DUP!)`; corrupt retrans/latency via `ss -ti`/"
        f"`ping`); or (b) STRUCTURALLY UNREACHABLE (reorder) -> declare it plus the "
        f"rationale (companion delay => no overtaking at the default interval; the "
        f"fast-interval form `ping -i <0.1` is flood-rejected by the read-only guard). "
        f"Detected: mentions_ping={bool(mentions_ping)}, reachable_form="
        f"{bool(reachable)}, unreachable_form={bool(unreachable)}. Silently skipping "
        f"the ping face forces the verify agent to re-derive ICMP-no-dedup at runtime "
        f"(Case #23 measured this live). See criterion-reachability.md §五 M3②."
    )
