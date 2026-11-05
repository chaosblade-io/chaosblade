r"""Sentinel: a host case whose ``## 注入验证`` prescribes an ACTIVE-PROBE criterion
that only the kubectl_exec channel can reach MUST disclose that it is structurally
unreachable through the host SSH channel, and MUST give the reachable host-channel
substitute. Plus: a host case prescribing ``tbf`` burst sizing MUST floor ``burst``
at the device MTU.

This is the M1 (channel-admission form) tooth for the *channel-conditional active
probe* sub-form — the axis ``criterion-reachability.md`` documents but its static
guard twin cannot fully police. Its siblings prove the other axes:
``test_host_case_commands_pass_guard.py`` (M1 — every prescribed host command in a
FENCED block whose head is a whitelisted host binary is ADMITTED by a guard
channel), ``test_host_case_criterion_reachability_sentinel.py`` (M2 — a
ptrace-exclusive tracer means demotes its window-unreachable criterion),
``test_host_case_dns_probe_universality_sentinel.py`` + 
``test_host_case_tcpdump_tool_face_sentinel.py`` (M3 — tool-face universality).

Why the M1 guard twin does NOT catch these two forms (its declared boundary):
``test_host_case_commands_pass_guard.py`` scans ONLY fenced-code-block lines whose
head is in ``_HOST_HEADS``, and ``_HOST_HEADS`` does not contain ``ip``. So

  ① an INLINE-PROSE pipe-to-sink throughput probe (``dd … | nc <sink> <port>``
     written inside a 判据 sentence, not a fenced block) is never extracted; and
  ② a fenced ``ip link add dtest0 type dummy && tc …`` go/no-go probe is skipped
     because its head is ``ip``.

Both are structurally unreachable through the host SSH channel and both shipped
undetected in ``Host_网络故障_网络带宽受限`` (Case #25, inject-2a705d2b, measured
live 2026-11). This sentinel closes form ① (the load-bearing one: it caused #25's
``overall=partial`` — the designated primary throughput-ratio evidence was never
obtainable) and the tbf burst-sizing defect that made the decisive ``overlimits>0``
criterion unreachable. Form ② (the ``ip link add`` go/no-go) is a NON-load-bearing
setup nuisance (the agent sidesteps to an existing inert interface) and shares its
root cause with five already-verified netem sibling docs, so it is fixed in the
bandwidth doc + documented in the single-source contract, but deliberately NOT
given a class-wide tooth here that would force a six-doc sweep.

Root cause this sentinel pins: the host bandwidth case was adapted from the k8s
Pod-bandwidth case (``Pod_网络故障_网络带宽不足``, live-verified via
``kubectl debug node`` + ``chroot /host`` + ``nsenter`` on a physical node — the
kubectl_exec channel, where a real shell runs pipes, ``nc``/``socat`` exist, a
cross-host sink is provisionable, and the NIC has a normal 1500 MTU). None of that
holds on the host SSH channel:

  * ``SSHChannel.wrap_command`` ``shlex.quote``s every token, so a bare ``|``
    reaches the remote shell as a LITERAL argument, not an operator — the pipe is
    physically inert, and ``is_readonly_host_command`` fail-closed refuses any
    operator form anyway. The "bounded send-traffic probe" exemption exists ONLY
    on the kubectl_exec container face, never on host SSH.
  * minimal host targets lack ``nc``/``socat``/``ncat``/``iperf3``.
  * a single-host SSH channel cannot arm a sink on ANOTHER reachable host, and a
    self-hosted sink on the target's own IP routes via ``lo``, bypassing the
    ``<if>`` root qdisc (guaranteed false negative).
  * ``host_read`` reports no elapsed time, so an in-channel timed transfer is
    impossible.

So the reachable host-channel primary criterion is the KERNEL-COUNTER DIFFERENTIAL:
``tc -s qdisc show dev <if>`` off the NIC's byte-exact ZERO ``noqueue`` baseline
(``Sent 0 bytes 0 pkt`` because ``IFF_NOQUEUE`` bypasses accounting); once tbf is
mounted, ``Sent`` climbs and ``dropped`` OR ``overlimits`` moves — white-box proof
the injected rule processed real egress. Traffic is driven by a BOUNDED
``dd``-to-stdout (``dd if=/dev/urandom bs=1M count=<N> status=none``) flowing
through the SSH channel's own egress (shaped by tbf on ``<if>``) — no sink, no pipe.
The source MUST be incompressible ``/dev/urandom``, not ``/dev/zero``: if the SSH
transport negotiates compression a zero payload collapses to near-nothing on the
wire and never exercises the shaper, whereas urandom puts real bytes on the wire
regardless of the compression setting.

The load-generation tooth (Tooth C) pins the THIRD #25 defect — the one that kept
``overall=partial`` even after the burst and channel-disclosure fixes: the
enforcement counters (``dropped``/``overlimits``) only move when offered load
approaches the ``rate``, so on an idle target (sshd-only, ~806 bit/s ≈ 1/1250 of a
1Mbit cap) the token bucket never drains and BOTH counters stay 0 for the whole
window — tbf is IN FORCE yet never EXERCISED. The doc MUST therefore pair the
counter-differential primary criterion with an IN-WINDOW active load-generation
step (a bounded ``dd``-to-stdout issued AFTER the tbf is mounted and BEFORE the
recovery carrier fires, sized ≫ the burst bucket and drainable within the
remaining window). #25 measured this live: the agent improvised the ``dd`` but
issued it ~96s AFTER the carrier had fired, so it ran against an already-unshaped
NIC and produced zero enforcement evidence.

The burst tooth pins the second #25 defect: ``tbf`` drops a packet at ENQUEUE when
``qdisc_pkt_len > burst`` (bytes). The ``rate/8`` heuristic (``1mbit`` → ``32kbit``
= 4096 bytes) is fine on a 1500-MTU NIC but on a large-MTU interface (Docker veth
MTU 65535 + kernel GSO super-packets) 4096 bytes < one packet, so tbf degrades to
enqueue-rejection (``dropped`` climbs, ``overlimits`` stays 0) instead of clean
token-starvation shaping. #25 measured exactly this (``dropped 19, overlimits 0``).
The doc MUST floor ``burst`` at the device MTU and MUST teach the verifier to
accept BOTH enforcement paths (delay via ``overlimits`` / drop via ``dropped``) so
it never judges "shaping failed" merely because ``overlimits`` stayed 0.

The teeth key on the MEANS CLASS (a ``## 注入验证`` prescribing a pipe-to-sink
throughput probe; a doc prescribing ``tbf`` ``burst`` sizing), never on a case name
(``b5c0af28``: fix the class, not the instance) — so any future host case adapted
from a kubectl_exec-channel throughput method is caught too. The single-source
authoring contract this enforces lives at
``skills/host-chaos-skills/references/criterion-reachability.md`` §「与通道准入
（M1）的边界」→「M1 的关键陷阱」.

Section extraction reuses the authoritative single source
``chaos_agent.utils.skill_case_section.find_section`` — the sentinel does NOT
reinvent section boundaries.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from chaos_agent.utils.skill_case_section import find_section

_SKILLS_DIR = pathlib.Path(__file__).resolve().parents[2] / "skills"
_HOST_CATALOGUE = _SKILLS_DIR / "host-chaos-skills" / "references" / "catalogue"

#: The single host case that (as of Case #25, measured 2026-11) prescribes a
#: pipe-to-sink egress-throughput probe and a ``tbf`` burst sizing heuristic. It
#: MUST be in both hit sets — if the extractor silently stops matching it the
#: teeth would go vacuous (the failure mode ``b5c6b377`` names). The teeth
#: themselves are "EVERY hit doc discloses / floors", NOT "hit set == this doc"
#: (a case-name branch).
_KNOWN_ACTIVE_PROBE_CASE = "Host_网络故障_网络带宽受限.md"


# ── Tooth A: pipe-to-sink throughput probe → host-channel unreachability ──────

#: Means-class signature: the ``## 注入验证`` section prescribes an ACTIVE
#: egress-throughput probe that pipes ``dd`` output to a sink tool (``nc`` /
#: ``socat``) — the kubectl_exec-channel method. Keyed on COMMAND SHAPE (dd + a
#: pipe + a sink tool, or the ``dd|nc`` / ``dd|socat`` shorthand), never a case
#: name.
_PIPE_SINK_RE = re.compile(
    r"dd[^\n]*\|\s*(?:nc|socat)\b|\bdd\|(?:nc|socat)\b|\|\s*(?:nc|socat)\b",
    re.IGNORECASE,
)

#: The M1 channel-disclosure the section MUST carry next to a pipe-to-sink probe:
#: an explicit statement that the form is STRUCTURALLY unreachable through the
#: host SSH channel (bare pipe fail-closed / kubectl_exec-only exemption), AND the
#: reachable host-channel substitute (the white-box ``tc -s`` kernel-counter
#: differential). Both elements are required (criterion-reachability §三: a
#: caveat names the trap AND the reachable substitute).
_UNREACHABLE_RE = re.compile(
    r"结构性不可达|结构性不成立|fail-closed|kubectl_exec|裸管道.*拒|host SSH 通道",
    re.IGNORECASE,
)
_WHITEBOX_SUBSTITUTE_RE = re.compile(
    r"tc -s|机制归因白盒|白盒|内核记账|计数器",
    re.IGNORECASE,
)


def _host_case_docs() -> list[pathlib.Path]:
    return sorted(_HOST_CATALOGUE.rglob("*.md"))


def _pipe_sink_verification_cases() -> list[tuple[str, str]]:
    """``(case_relpath, 注入验证_section_text)`` for every case whose 注入验证
    prescribes a pipe-to-sink throughput probe."""
    out: list[tuple[str, str]] = []
    for path in _host_case_docs():
        text = path.read_text(encoding="utf-8", errors="replace")
        section = find_section(text, "注入验证")
        if section is None:
            continue
        if not _PIPE_SINK_RE.search(section):
            continue
        out.append((str(path.relative_to(_SKILLS_DIR)), section))
    return out


_PIPE_SINK_CASES = _pipe_sink_verification_cases()


def test_pipe_sink_corpus_is_not_empty() -> None:
    """Guards the extractor: the known pipe-to-sink case MUST be detected, else
    every Tooth-A assertion below is vacuous."""
    hit = {pathlib.Path(rel).name for rel, _ in _PIPE_SINK_CASES}
    assert _KNOWN_ACTIVE_PROBE_CASE in hit, (
        f"means-signature extractor missed the known pipe-to-sink case "
        f"{_KNOWN_ACTIVE_PROBE_CASE} (detected: {sorted(hit)}) — the regex, the "
        f"section extractor, or the corpus changed shape"
    )


@pytest.mark.parametrize(
    "case,section", _PIPE_SINK_CASES, ids=[rel for rel, _ in _PIPE_SINK_CASES],
)
def test_pipe_sink_probe_discloses_host_channel_unreachability(
    case: str, section: str,
) -> None:
    assert _UNREACHABLE_RE.search(section), (
        f"{case} prescribes a pipe-to-sink egress-throughput probe (`dd … | nc/"
        f"socat`) in its `## 注入验证` section, so it MUST disclose that this form "
        f"is STRUCTURALLY unreachable through the host SSH channel — the transport "
        f"`shlex.quote`s every token so a bare `|` is a literal argument (physically "
        f"inert), `is_readonly_host_command` fail-closed refuses any operator form, "
        f"and the 'bounded send-traffic probe' exemption exists ONLY on the "
        f"kubectl_exec container face. Without the disclosure the verify agent burns "
        f"rounds retrying an unreachable pipe (Case #25 measured this live: the "
        f"designated primary throughput ratio was never obtainable → overall=partial)."
    )
    assert _WHITEBOX_SUBSTITUTE_RE.search(section), (
        f"{case} prescribes a pipe-to-sink throughput probe but does NOT name the "
        f"reachable host-channel substitute: the kernel-counter differential via "
        f"`tc -s qdisc show dev <if>` off the byte-exact ZERO `noqueue` baseline "
        f"(`Sent 0 bytes 0 pkt`), driven by a BOUNDED `dd`-to-stdout through the SSH "
        f"channel's own egress. A caveat must give the reachable substitute, not just "
        f"name the trap (criterion-reachability.md §三)."
    )


# ── Tooth B: tbf burst sizing must floor at the device MTU ────────────────────

#: Means-class signature: the doc prescribes a `tbf` qdisc with a `burst` sizing
#: heuristic. Keyed on COMMAND SHAPE (tbf + burst), never a case name.
_TBF_BURST_RE = re.compile(r"\btbf\b[^\n]*\bburst\b|\bburst\b[^\n]*\btbf\b", re.IGNORECASE)

#: The burst-sizing guidance MUST floor `burst` at the device MTU AND warn that a
#: too-small burst (rate/8 on a large-MTU veth) degrades to enqueue-rejection
#: (`dropped` climbs, `overlimits` stays 0) rather than clean delay shaping.
_BURST_MTU_FLOOR_RE = re.compile(r"burst[^\n]{0,40}MTU|MTU[^\n]{0,40}burst|≥\s*设备\s*MTU|≥\s*MTU", re.IGNORECASE)
_BURST_DEGRADATION_RE = re.compile(r"入队拒绝|入队丢包|丢包路径|退化.*丢包|overlimits[^\n]{0,20}(停|0)", re.IGNORECASE)


def _tbf_burst_cases() -> list[tuple[str, str]]:
    """``(case_relpath, full_doc_text)`` for every case prescribing tbf burst sizing.

    Scans the WHOLE doc (burst guidance lives in 演练步骤 参数说明 / 注入验证 /
    注意事项 / 基准事实, not one section).
    """
    out: list[tuple[str, str]] = []
    for path in _host_case_docs():
        text = path.read_text(encoding="utf-8", errors="replace")
        if not _TBF_BURST_RE.search(text):
            continue
        out.append((str(path.relative_to(_SKILLS_DIR)), text))
    return out


_TBF_BURST_CASES = _tbf_burst_cases()


def test_tbf_burst_corpus_is_not_empty() -> None:
    """Guards the extractor: the known tbf-burst case MUST be detected, else every
    Tooth-B assertion below is vacuous."""
    hit = {pathlib.Path(rel).name for rel, _ in _TBF_BURST_CASES}
    assert _KNOWN_ACTIVE_PROBE_CASE in hit, (
        f"means-signature extractor missed the known tbf-burst case "
        f"{_KNOWN_ACTIVE_PROBE_CASE} (detected: {sorted(hit)}) — the regex or the "
        f"corpus changed shape"
    )


@pytest.mark.parametrize(
    "case,text", _TBF_BURST_CASES, ids=[rel for rel, _ in _TBF_BURST_CASES],
)
def test_tbf_burst_floored_at_mtu(case: str, text: str) -> None:
    assert _BURST_MTU_FLOOR_RE.search(text), (
        f"{case} prescribes a `tbf` `burst` sizing heuristic but does NOT floor "
        f"`burst` at the device MTU. tbf drops a packet at ENQUEUE when its length "
        f"exceeds burst (bytes), so the `rate/8` heuristic (1mbit → 32kbit = 4096 "
        f"bytes) is fine on a 1500-MTU NIC but on a large-MTU interface (Docker veth "
        f"MTU 65535 + kernel GSO super-packets) 4096 bytes < one packet → tbf "
        f"degrades to enqueue-rejection instead of clean shaping. Case #25 measured "
        f"this live (dropped 19, overlimits 0). State `burst ≥ 设备 MTU` (probe "
        f"`cat /sys/class/net/<if>/mtu`), scaling up on large-MTU environments."
    )
    assert _BURST_DEGRADATION_RE.search(text), (
        f"{case} prescribes `tbf` `burst` sizing but does NOT teach the verifier to "
        f"recognise BOTH enforcement paths — clean token-starvation shaping (delay: "
        f"`overlimits` climbs) vs enqueue-rejection (drop: `dropped` climbs while "
        f"`overlimits` stays 0 on a large-MTU/GSO interface). Without this the "
        f"verifier judges 'shaping failed' merely because `overlimits` stayed 0 "
        f"(Case #25's decisive criterion was thus unmet → step2 partial)."
    )


# ── Tooth C: tbf enforcement-counter criterion must pair an in-window load gen ──

#: Means-class signature: the ``## 注入验证`` section adopts the tbf KERNEL-COUNTER
#: differential (BOTH ``overlimits`` AND ``dropped``) as its shaping criterion —
#: i.e. it judges "the shaper actively constrained egress" by whether the
#: enforcement counters leave their zero baseline. Keyed on CRITERION SHAPE (tbf +
#: both counters present in the section), never a case name.
def _tbf_counter_criterion_cases() -> list[tuple[str, str]]:
    """``(case_relpath, 注入验证_section_text)`` for every case whose 注入验证
    adopts the tbf kernel-counter differential (overlimits/dropped) as a criterion.

    Such a criterion is only reachable if the section ALSO prescribes an in-window
    active load-generation step: the enforcement counters move only when offered
    load approaches the ``rate``, so an idle target (sshd-only) passively keeps both
    at 0 — tbf IN FORCE yet never EXERCISED (the third #25 defect, Tooth C).
    """
    out: list[tuple[str, str]] = []
    for path in _host_case_docs():
        text = path.read_text(encoding="utf-8", errors="replace")
        section = find_section(text, "注入验证")
        if section is None:
            continue
        low = section.lower()
        if "tbf" not in low:
            continue
        if "overlimits" not in low or "dropped" not in low:
            continue
        out.append((str(path.relative_to(_SKILLS_DIR)), section))
    return out


_TBF_COUNTER_CASES = _tbf_counter_criterion_cases()

#: The in-window active load generation the section MUST prescribe: a BOUNDED
#: ``dd``-to-stdout reading an INCOMPRESSIBLE source (``/dev/urandom``/``random``),
#: so it flows through the SSH channel's own egress and is shaped by tbf. The
#: regex deliberately EXCLUDES ``/dev/zero``: (a) a zero payload collapses to
#: near-nothing under negotiated SSH compression and never exercises the shaper
#: (the M1 source-hardening), and (b) the only ``/dev/zero`` form in these docs is
#: the ``dd … | nc`` pipe-to-sink probe (Tooth A's kubectl_exec-channel domain),
#: which is NOT the in-window host-channel load gen. So this anchor doubles as the
#: incompressible-source guard — its absence is what kept #25 at ``overall=partial``.
_ACTIVE_LOADGEN_RE = re.compile(
    r"dd\s+if=/dev/(?:urandom|random)", re.IGNORECASE,
)

#: The load generation MUST be placed IN-WINDOW with explicit ACTIVE-load timing
#: discipline: issued after the tbf is mounted and BEFORE the recovery carrier
#: fires (else it runs against an already-unshaped NIC — #25's agent mistimed the
#: ``dd`` ~96s after carrier fire and got zero enforcement evidence). The regex
#: deliberately EXCLUDES bare ``窗内``/``挂载后``: those also occur in pre-existing
#: READ-timing prose ("窗内第一批快照"), which is not the load-gen discipline, so
#: including them would make the anchor vacuous (verified by mutation).
_INWINDOW_TIMING_RE = re.compile(
    r"载体\s*fire\s*前|fire\s*前|主动发|exercise|主动\s*exercise", re.IGNORECASE,
)


def test_tbf_counter_corpus_is_not_empty() -> None:
    """Guards the extractor: the known tbf-counter case MUST be detected, else every
    Tooth-C assertion below is vacuous."""
    hit = {pathlib.Path(rel).name for rel, _ in _TBF_COUNTER_CASES}
    assert _KNOWN_ACTIVE_PROBE_CASE in hit, (
        f"means-signature extractor missed the known tbf-counter case "
        f"{_KNOWN_ACTIVE_PROBE_CASE} (detected: {sorted(hit)}) — the section "
        f"extractor, the corpus, or the criterion shape changed"
    )


@pytest.mark.parametrize(
    "case,section", _TBF_COUNTER_CASES, ids=[rel for rel, _ in _TBF_COUNTER_CASES],
)
def test_tbf_counter_criterion_pairs_in_window_loadgen(
    case: str, section: str,
) -> None:
    assert _ACTIVE_LOADGEN_RE.search(section), (
        f"{case} adopts the tbf kernel-counter differential (`overlimits`/`dropped` "
        f"leaving their zero baseline) as its shaping criterion in `## 注入验证`, but "
        f"prescribes NO active load-generation step (a bounded `dd if=/dev/…`-to-"
        f"stdout). The enforcement counters move ONLY when offered load approaches "
        f"the `rate`; an idle target (sshd-only, ~806 bit/s ≈ 1/1250 of a 1Mbit cap) "
        f"passively keeps BOTH at 0 for the whole window — tbf is IN FORCE yet never "
        f"EXERCISED, so the criterion is unreachable without actively driving traffic "
        f"through the SSH channel's own shaped egress (Case #25 measured this live: "
        f"`dropped`/`overlimits` stayed 0 → overall=partial). Prescribe a bounded "
        f"incompressible `dd if=/dev/urandom bs=1M count=<N> status=none`."
    )
    assert _INWINDOW_TIMING_RE.search(section), (
        f"{case} pairs a tbf counter criterion with load generation but does NOT pin "
        f"the IN-WINDOW timing discipline: the `dd` load MUST be issued after the tbf "
        f"is mounted and BEFORE the recovery carrier fires. A load issued after the "
        f"carrier's `tc qdisc del` runs against an already-unshaped NIC and produces "
        f"zero enforcement evidence (Case #25's agent improvised the `dd` but mistimed "
        f"it ~96s after carrier fire). State the window/`载体 fire 前` timing anchor "
        f"explicitly."
    )
