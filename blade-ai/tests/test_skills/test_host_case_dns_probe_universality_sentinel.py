r"""Sentinel: a host case whose INJECTION MEANS block or redirect DNS resolution
must carry a UNIVERSAL NSS-path resolution probe (``getent`` / ``ping``) in its
``## 注入验证`` section — never rely solely on ``nslookup`` / ``dig``.

This is the M3 (tool-face reachability) tooth. Its siblings
``test_host_case_commands_pass_guard.py`` (M1 — every prescribed host command is
ADMITTED by a guard channel) and
``test_host_case_criterion_reachability_sentinel.py`` (M2 — a ptrace-exclusive
tracer means demotes its window-unreachable criterion) prove the other two axes
of criterion reachability. M3 is a distinct root cause: the criterion command is
perfectly admissible (M1) and the injector does not block it at runtime (M2), yet
it cannot produce evidence because the TOOL it depends on is absent on the target
or reads the wrong observation face.

Root cause this sentinel pins (Case #21, ``Host_网络故障_DNS不可用``,
inject-2ecb824e, measured live 2026-11): the doc hardcoded ``nslookup <domain>``
as the load-bearing resolution probe in all three of baseline / 注入验证 /
恢复验证. But ``nslookup`` ships in dnsutils, which a minimal ubuntu target does
NOT install by default — the probe exits 127 and yields no evidence at all, so
the verify agent burned a round discovering the absence before sidestepping to
``getent`` (verified, but the doc was not self-sufficient). The same dnsutils
assumption made ``blade create network dns_down --allow_domain`` fail wholesale
(exit 127 / code 63063) because that flag internally shells out to
``nslookup ... | sudo tee -a /etc/hosts``.

The general fix (see ``references/criterion-reachability.md`` §五 「工具面」):
DNS-resolution criteria must probe via the app-equivalent glibc NSS path —
``getent hosts <domain>`` (honours nsswitch ``files→dns``, universally present,
same observation face the application uses) or ``ping <domain>`` (also honours the
resolver) — relegating ``nslookup`` / ``dig`` to optional DNS-layer-only
diagnostics. ``nslookup`` / ``dig`` are additionally wrong for the sister case
``Host_网络故障_DNS劫持`` because they bypass ``/etc/hosts``, the very layer its
手段1 hijacks — that doc already uses ``ping`` as its criterion; this sentinel
keeps both honest.

The tooth keys on the MEANS CLASS (a DNS-blocking / DNS-redirecting injection
command form), never on a case name — so a future DNS-fault case is caught too
(``b5c0af28``: fix the class, not the instance). The single-source authoring
contract this enforces lives at
``skills/host-chaos-skills/references/criterion-reachability.md``.

Section extraction reuses the authoritative single source
``chaos_agent.utils.skill_case_section.find_section`` — the sentinel does NOT
reinvent section boundaries. ``find_section`` returns the ORIGINAL section text
(fenced code intact; masking is only used internally for boundary detection), so
the universal probe is detected whether it lives in a fenced ``getent hosts``
command block or in the 判据 prose. Both DNS docs carry it in both places, so the
tooth is robust to either authoring style while still failing a doc that prescribes
``nslookup`` / ``dig`` as its only resolution criterion.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from chaos_agent.utils.skill_case_section import find_section

_SKILLS_DIR = pathlib.Path(__file__).resolve().parents[2] / "skills"
_HOST_CATALOGUE = _SKILLS_DIR / "host-chaos-skills" / "references" / "catalogue"

#: Means-class signature: the doc prescribes a DNS-resolution fault — either
#: blocking DNS queries (dns_down) or redirecting them to a forged resolver
#: (dns hijack). Keyed on COMMAND SHAPE (not case name):
#:   手段1 — ChaosBlade DNS executors: ``blade create network dns_down`` (block)
#:           / ``blade create network dns`` (hijack, /etc/hosts layer)
#:   手段2 — native iptables on the DNS port: ``--dport 53`` (DROP = block,
#:           DNAT = redirect). Port 53 IS DNS by definition, so this form is
#:           inherently a DNS fault, never an unrelated injection.
_DNS_FAULT_MEANS_RE = re.compile(
    r"blade\s+create\s+network\s+dns(?:_down)?\b"   # 手段1: ChaosBlade dns / dns_down
    r"|--dport\s+53\b",                             # 手段2: iptables DNS port 53
    re.IGNORECASE,
)

#: The universal app-equivalent resolution probe the criterion MUST use. Either
#: glibc NSS ``getent`` (honours ``files→dns``, universally present) or ``ping``
#: (also honours the resolver / ``/etc/hosts``). A DNS-fault doc whose
#: ``## 注入验证`` carries NEITHER relies solely on ``nslookup`` / ``dig`` — the
#: dnsutils-dependent, /etc/hosts-bypassing anti-pattern this sentinel rules out.
_UNIVERSAL_NSS_PROBE_RE = re.compile(r"\bgetent\b|\bping\b", re.IGNORECASE)

#: Anti-vacuous baseline (measured 2026-11): exactly these two host docs use a
#: DNS-blocking / DNS-redirecting means. They MUST be in the hit set — if the
#: extractor silently stops matching them the tooth would go vacuous (the failure
#: mode ``b5c6b377`` names). The tooth itself is "EVERY hit doc carries a
#: universal NSS-path probe", NOT "hit set == these two" (a case-name branch).
_KNOWN_DNS_MEANS_CASES = (
    "Host_网络故障_DNS不可用.md",
    "Host_网络故障_DNS劫持.md",
)


def _host_case_docs() -> list[pathlib.Path]:
    return sorted(_HOST_CATALOGUE.rglob("*.md"))


def _uses_dns_fault_means(text: str) -> bool:
    """True if the doc prescribes a DNS-blocking / DNS-redirecting injection."""
    return _DNS_FAULT_MEANS_RE.search(text) is not None


def _dns_means_cases() -> list[tuple[str, str]]:
    """``(case_relpath, 注入验证_section_text)`` for every DNS-fault-means case.

    The 注入验证 body comes from the authoritative single-source extractor; a doc
    with no such section yields ``""`` (the assertion then fails loudly — a
    DNS-fault case with no 注入验证 section is itself a defect).
    """
    out: list[tuple[str, str]] = []
    for path in _host_case_docs():
        text = path.read_text(encoding="utf-8", errors="replace")
        if not _uses_dns_fault_means(text):
            continue
        rel = str(path.relative_to(_SKILLS_DIR))
        section = find_section(text, "注入验证")
        out.append((rel, section if section is not None else ""))
    return out


_DNS_CASES = _dns_means_cases()


def test_dns_means_corpus_is_not_empty() -> None:
    """Guards the extractor: the known DNS-fault-means cases MUST be detected,
    else every assertion below is vacuous."""
    hit_names = {pathlib.Path(rel).name for rel, _ in _DNS_CASES}
    missing = set(_KNOWN_DNS_MEANS_CASES) - hit_names
    assert not missing, (
        f"means-signature extractor missed known DNS-fault cases: {sorted(missing)} "
        f"(detected: {sorted(hit_names)}) — the regex or the corpus changed shape"
    )


@pytest.mark.parametrize(
    "case,section",
    _DNS_CASES,
    ids=[rel for rel, _ in _DNS_CASES],
)
def test_dns_means_case_pins_universal_resolution_probe(
    case: str, section: str,
) -> None:
    assert _UNIVERSAL_NSS_PROBE_RE.search(section), (
        f"{case} injects a DNS-resolution fault (blocks or redirects DNS), so its "
        f"`## 注入验证` load-bearing resolution criterion MUST probe via the "
        f"app-equivalent glibc NSS path — `getent hosts <domain>` (honours "
        f"nsswitch files→dns, universally present, same observation face the app "
        f"uses) or `ping <domain>` (also honours the resolver). Relying solely on "
        f"`nslookup` / `dig` is the M3 tool-face defect: dnsutils is NOT installed "
        f"on minimal targets (probe exits 127, yields no evidence — Case #21 "
        f"measured this live), and nslookup/dig bypass /etc/hosts so they cannot "
        f"see a hosts-layer hijack. Demote nslookup/dig to optional DNS-layer "
        f"diagnostics and carry the universal probe as the criterion. See "
        f"skills/host-chaos-skills/references/criterion-reachability.md §五 "
        f"「工具面（探针普适性，M3）」."
    )
