"""Sentinel: recovery-timer single source (双数窗口契约) — skills-tree guard.

Root cause this sentinel pins (openspec ``hold-reanchor-recovery-grace``,
2026-09): the 150s accounting-loss defect — when a case arms its
native/host process-shaped self-recovery timer with the bare observation
window ``D`` (``sleep <duration>``), the fault self-expires at onset+D, the
framework's active recovery lands AFTER the fault is already gone, and the
platform grades the drill as "recovered by timer expiry" instead of
"recovered by the framework". The change legislates a two-number window
contract:

- 观察窗 ``D`` = ``duration_seconds`` — the framework's presence
  obligation (the contract value 严禁侵蚀/擅延);
- 安全网窗 ``D+G`` = ``recovery_timer_seconds`` — the arming value for
  every native/host process-shaped self-recovery point, computed once by
  ``utils/fault_type.py::recovery_timer_seconds`` (G =
  ``recovery_grace_seconds``, default 120) and rendered into the execute
  prompt by ``structured_params_hint`` — the LLM never self-computes it.

The sentinel converts silent drift into loud drift:

1. both SKILL.md files must keep the contract legislation anchor — the
   single source 62+ case definition lines point at ("见 SKILL.md 双数窗口
   契约"); deleting the legislation orphans every case definition.
2. catalogue cases must not arm a process-shaped self-recovery timer with
   the bare observation window — ``sleep <duration>; <逆操作>``, stress-ng
   ``--timeout <duration>s``, systemd-run ``--on-active=<duration>``,
   python ``time.sleep(<duration>)``, perl ``|| <duration>`` argv-default,
   dd-pipeline ``sleep <duration> )``, self-computed buffer formulas
   (``<duration-50>``, ``time.time() + <duration>``) — zero tolerance.
3. every case that uses ``<recovery-seconds>`` must carry its source
   reference (``recovery_timer_seconds``) — an orphaned placeholder with
   no semantics line silently regresses the transmission point (LLM
   reading the case cannot resolve the value).
4. legitimate retention surfaces stay legitimate AND stay in place (a
   future "cleanup" cannot silently strip them):
   - blade ``--timeout <duration>`` (no ``s`` suffix) — engine-pinned at
     dispatch time by
     ``providers/registry.py::enforce_contract_duration``; the case
     template keeps ``<duration>`` as the declared observation window;
   - API-write recovery-carrier timers (``sleep <duration>; sh
     /tmp/blade-restore*``, ``; kubectl scale``, ``; kubectl uncordon``,
     ``; curl`` REST patch shapes) — armed by the carrier standard's own
     duration-dual-track legislation (``references/carrier/
     recovery-carrier.md`` §duration 双轨), NOT subject to grace — these
     never match the forbidden patterns because their follow-up verb is
     not in the process-shape set;
   - observer keepalive ``-- sleep <duration>`` in the whitelist files —
     keepalive that carries no timer stays at D (asserted RETAINED);
   - structural pre-delays (the ``20`` in ``--on-active=<20+recovery-
     seconds>s`` containerd STOP-lead, the ``-50`` dd tail-cleanup margin
     as ``<recovery-seconds>-50``) — pre-amounts, not window math; the
     windowed term inside them is already ``<recovery-seconds>``-shaped.

Implementation note: assertions read files with ``pathlib.read_text`` +
``re`` (python) — NOT grep-class tooling. Both SKILL.md files carry the
legislation on a ~900-char line; grep-based tools have been observed to
miss it, which would make the anchor tooth vacuous.
"""

import pathlib
import re

_SKILLS_DIR = pathlib.Path(__file__).resolve().parents[2] / "skills"
_K8S_CATALOGUE = _SKILLS_DIR / "k8s-chaos-skills" / "references" / "catalogue"
_HOST_CATALOGUE = _SKILLS_DIR / "host-chaos-skills" / "references" / "catalogue"
_K8S_SKILL_MD = _SKILLS_DIR / "k8s-chaos-skills" / "SKILL.md"
_HOST_SKILL_MD = _SKILLS_DIR / "host-chaos-skills" / "SKILL.md"

# Legislation anchors each SKILL.md must keep (k8s and host use slightly
# different heading forms; both must keep the hint-render surface and the
# onset math that makes framework-recovery-before-self-expiry auditable).
_K8S_ANCHORS = (
    "恢复定时器值单源（双数窗口契约）",
    "recovery_timer_seconds=<N>s",
    "onset+D+G",
)
_HOST_ANCHORS = (
    "故障窗口完整 / 恢复定时器值单源（双数窗口契约）",
    "recovery_timer_seconds=<N>s",
    "onset+D+G",
)

# Forbidden shapes: a process-shaped self-recovery point armed with the
# bare observation window. Each entry is (pattern, human label).
_FORBIDDEN_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sleep <duration>; (tc |iptables |kill |rm |chmod |cat |echo )"), "process-shaped sleep timer"),
    (re.compile(r"&& sleep <duration>"), "in-chain sleep timer"),
    (re.compile(r"`sleep <duration>`"), "backtick-quoted sleep timer"),
    (re.compile(r"整体阻塞 `<duration>`"), "blocking-window prose"),
    (re.compile(r"RuntimeMaxSec=<duration>"), "systemd transient-unit lifetime"),
    (re.compile(r"--timeout <duration>s"), "stress-ng self-stop timeout"),
    (re.compile(r"--on-active=<duration>"), "systemd-run on-active timer"),
    (re.compile(r"<duration-50>"), "self-computed buffer formula"),
    (re.compile(r"time\.time\(\) \+ <duration>"), "python deadline self-math"),
    (re.compile(r"time\.sleep\(<duration>"), "python-payload sleep timer"),
    (re.compile(r"\|\| <duration>"), "perl argv-default duration"),
    (re.compile(r"sleep <duration> \)"), "dd-pipeline keepalive sleep"),
    (re.compile(r"timeout <duration> \S"), "timeout-command process timer"),
)


def _line_is_exempt(line: str) -> bool:
    """Blade command lines: their ``--timeout`` is engine-pinned before
    dispatch (enforce_contract_duration), so a parameterized blade line
    (e.g. ``--timeout <duration> --waiting-time 60s``) is legitimate."""
    return "blade create" in line or "blade destroy" in line or "blade status" in line


def _catalogue_cases() -> list[pathlib.Path]:
    return sorted(_K8S_CATALOGUE.rglob("*.md")) + sorted(_HOST_CATALOGUE.rglob("*.md"))


# Observer-keepalive whitelist: ``kubectl debug node … -- sleep <duration>``
# keepalive that carries NO recovery timer legitimately stays at D. The
# sentinel asserts these files KEEP the shape (anti-over-cleanup) instead of
# exempting them from anything — the keepalive line matches no forbidden
# pattern on its own.
_KEEPALIVE_WHITELIST: tuple[str, ...] = (
    "Node_网络故障_节点网络隔离.md",
    "Node_网络故障_可用区网络分区.md",
    "Node_不可用_节点宕机.md",
    "Pod_Terminating_节点宕机kubelet失联.md",
)

# Anti-vacuous baselines, measured at sweep completion (2026-09-22):
# 86 catalogue files (68 k8s + 18 host), 62 of them using the placeholder.
_MIN_CASES = 80
_MIN_PLACEHOLDER_FILES = 55


def test_skill_md_contract_anchor_present() -> None:
    """Both SKILL.md files keep the 双数窗口契约 legislation — the single
    source every case definition line points at."""
    for path, anchors in ((_K8S_SKILL_MD, _K8S_ANCHORS), (_HOST_SKILL_MD, _HOST_ANCHORS)):
        text = path.read_text(encoding="utf-8")
        for anchor in anchors:
            assert anchor in text, f"{path.name} 丢失立法锚点: {anchor!r}"


def test_no_process_shaped_timer_armed_with_observation_window() -> None:
    """Catalogue-wide zero-tolerance: no process-shaped self-recovery timer
    may be armed with the bare observation window ``<duration>``."""
    offenders: list[str] = []
    for case in _catalogue_cases():
        for lineno, line in enumerate(case.read_text(encoding="utf-8").splitlines(), 1):
            if _line_is_exempt(line):
                continue
            for pattern, label in _FORBIDDEN_PATTERNS:
                if pattern.search(line):
                    offenders.append(
                        f"{case.relative_to(_SKILLS_DIR)}:{lineno} [{label}] {line.strip()[:80]}"
                    )
    assert not offenders, (
        "进程型自恢复定时器仍用裸观察窗 <duration> 武装（应为 <recovery-seconds>，"
        "取 prompt 下发的 recovery_timer_seconds）:\n" + "\n".join(offenders)
    )


def test_placeholder_files_carry_source_reference() -> None:
    """Every case using ``<recovery-seconds>`` must reference
    ``recovery_timer_seconds`` — an orphaned placeholder is a silent
    regression of the value's transmission point."""
    orphans: list[str] = []
    files = 0
    for case in _catalogue_cases():
        text = case.read_text(encoding="utf-8")
        if "<recovery-seconds>" not in text:
            continue
        files += 1
        if "recovery_timer_seconds" not in text:
            orphans.append(str(case.relative_to(_SKILLS_DIR)))
    assert not orphans, (
        "<recovery-seconds> 占位符缺 recovery_timer_seconds 来源说明:\n" + "\n".join(orphans)
    )
    assert files >= _MIN_PLACEHOLDER_FILES, (
        f"使用 <recovery-seconds> 的文件数 {files} 低于基线 {_MIN_PLACEHOLDER_FILES}——疑似批量回退"
    )


def test_observer_keepalive_whitelist_retained() -> None:
    """Whitelist files keep their ``-- sleep <duration>`` observer keepalive
    (keepalive carrying no timer legitimately stays at D)."""
    for name in _KEEPALIVE_WHITELIST:
        matches = list(_K8S_CATALOGUE.rglob(name))
        assert len(matches) == 1, f"白名单文件缺失或重名: {name}"
        assert "-- sleep <duration>" in matches[0].read_text(encoding="utf-8"), (
            f"{name} 的观察载体保活形态被过度清理"
        )


def test_scan_not_vacuous() -> None:
    """The sweep baseline: the sentinel must actually be scanning the real
    catalogue, not an empty/renamed tree."""
    cases = _catalogue_cases()
    assert len(cases) >= _MIN_CASES, f"catalogue 文件数 {len(cases)} 低于基线 {_MIN_CASES}"
