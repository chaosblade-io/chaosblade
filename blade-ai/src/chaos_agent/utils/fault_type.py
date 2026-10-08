"""Fault-type classification utilities.

Phase-9 T3.3: the ChaosBlade create-args constructor
``build_blade_create_args`` moved to the carrier side (phase-12: now in
``providers/chaosblade/declaration.py``; sole consumer is the /plan
preview); ``validate_blade_params`` and its scope/target tables
(``VALID_SCOPES``/``VALID_TARGETS``) were dead code (zero consumers
repo-wide) and deleted outright. What remains is carrier-agnostic
fault-type knowledge: duration policy, timeout normalization, category
extraction, and K8s quantity parsing.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


def normalize_timeout_flag(argv: list[str]) -> str | None:
    """Normalize one ``--timeout`` argument in-place and return its value.

    ChaosBlade accepts both ``--timeout 2700`` and ``--timeout=2700``.  The
    latter used to evade the duration guard, which then appended a second
    timeout and let the CLI decide which value won.  Keep a single canonical
    ``--timeout <seconds>`` pair before callers apply the minimum-duration
    policy.  A malformed empty timeout is removed and treated as unspecified.
    """
    occurrences: list[tuple[int, int, str]] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--timeout":
            if (
                index + 1 < len(argv)
                and argv[index + 1]
                and not argv[index + 1].startswith("--")
            ):
                occurrences.append((index, 2, argv[index + 1].rstrip("sS")))
                index += 2
                continue
            occurrences.append((index, 1, ""))
        elif token.startswith("--timeout="):
            occurrences.append((index, 1, token.split("=", 1)[1].rstrip("sS")))
        index += 1

    valid = [occurrence for occurrence in occurrences if occurrence[2]]
    if not valid:
        for index, width, _ in reversed(occurrences):
            del argv[index:index + width]
        return None

    # Retain the last explicit value, matching normal CLI option precedence,
    # while removing all duplicate spellings before dispatch.
    insert_at = occurrences[0][0]
    value = valid[-1][2]
    for index, width, _ in reversed(occurrences):
        del argv[index:index + width]
    argv[insert_at:insert_at] = ["--timeout", value]
    return value


# ``--timeout`` inside a COMMAND STRING (``--timeout 300`` / ``--timeout=300``
# / ``--timeout 300s``). The lookbehind keeps a token boundary so a flag like
# ``--timeout-x`` is never matched; the value stops at whitespace and is
# unquoted by the readers below.
_TIMEOUT_FLAG_RE = re.compile(r"(?<!\S)--timeout(?:=|\s+)(?P<value>[^\s]+)")


def read_timeout_flag(text: str) -> int | None:
    """The ``--timeout`` value in SECONDS carried by a command string.

    String-level sibling of :func:`normalize_timeout_flag` for the carriers
    that hand their flags over as one free-form string (the ``blade_create``
    flags field and the ``kubectl exec ... blade create`` v_args). Last-wins,
    matching blade's pflag precedence — the same value the executor honours.
    ``None`` when the text carries no ``--timeout`` or its value is not an
    integer.
    """
    matches = list(_TIMEOUT_FLAG_RE.finditer(text or ""))
    if not matches:
        return None
    try:
        return int(matches[-1].group("value").strip("\"'").rstrip("sS"))
    except (TypeError, ValueError):
        return None


def set_timeout_flag(text: str, seconds: int) -> str:
    """Rewrite ``text`` so it carries exactly one ``--timeout <seconds>``.

    The FIRST occurrence is rewritten in place and every later spelling is
    dropped, so all other bytes of the command stay untouched — matchers,
    quoting and flag order are never re-rendered. ``--timeout`` is the
    fault's own duration for both blade surfaces, so this function is how a
    command is pinned to the one window the user approved.
    """
    canonical = f"--timeout {int(seconds)}"
    matches = list(_TIMEOUT_FLAG_RE.finditer(text or ""))
    if not matches:
        return f"{text} {canonical}".strip() if text else canonical
    pieces: list[str] = []
    cursor = 0
    for index, match in enumerate(matches):
        pieces.append(text[cursor:match.start()])
        if index == 0:
            pieces.append(canonical)
        cursor = match.end()
    pieces.append(text[cursor:])
    return "".join(pieces)


# Minimum recommended duration per fault type (scope, target, action)
# All values >= 300s per requirement. Based on empirical measurement of
# Layer1 + Layer2 verification latency + ChaosBlade scheduling delay.
_FAULT_TYPE_MIN_DURATION: dict[tuple[str, str, str], int] = {
    # Node-level: high latency (kubectl debug + host-level commands)
    ("node", "disk", "fill"): 300,
    ("node", "network", "drop"): 300,
    ("node", "cpu", "fullload"): 300,
    ("node", "mem", "load"): 300,
    ("node", "disk", "burn"): 300,
    # Pod-level: medium latency (kubectl top/exec/describe)
    ("pod", "cpu", "fullload"): 300,
    ("pod", "mem", "load"): 300,
    ("pod", "network", "drop"): 300,
    ("pod", "disk", "fill"): 300,
    ("pod", "disk", "burn"): 300,
    ("pod", "process", "kill"): 300,
    # Container-level
    ("container", "cpu", "fullload"): 300,
    ("container", "mem", "load"): 300,
    ("container", "network", "drop"): 300,
}

# Fallback used when the operator-configured ``experiment_timeout`` is
# missing, non-positive, or unparseable, and the recommended floor for a
# fault type absent from ``_FAULT_TYPE_MIN_DURATION``. Must be >= 300s per
# requirement.
#
# ADVISORY, not absolute: neither a configured default nor an explicit
# per-injection duration is ever raised to it. A below-floor value is
# honoured verbatim with a warning (l4-contract-faithfulness). The
# empirical basis — Layer1+Layer2 verification latency plus ChaosBlade
# scheduling delay — still explains why a too-tight window tends to exit
# honestly as unverified/partial rather than produce a false verdict.
_DEFAULT_MIN_DURATION = 300


def _configured_experiment_timeout() -> int:
    """Return the operator-configured experiment timeout (seconds).

    Reads ``settings.experiment_timeout`` (config.json / env override).
    Imported lazily so this carrier-agnostic utility module stays
    import-light.

    The configured value is returned verbatim: it *is* the operator's
    stated default, so lifting it to the empirical floor would be the same
    defect as amending a per-injection explicit duration. Only a
    non-positive or unparseable value counts as "not configured" and falls
    back to ``_DEFAULT_MIN_DURATION``.
    """
    try:
        from chaos_agent.config.settings import settings
        configured = int(settings.experiment_timeout)
    except (ImportError, ValueError, TypeError):
        return _DEFAULT_MIN_DURATION
    if configured <= 0:
        logger.warning(
            "experiment_timeout=%ss is not a usable duration (must be "
            "positive); falling back to the %ss default.",
            configured, _DEFAULT_MIN_DURATION,
        )
        return _DEFAULT_MIN_DURATION
    return configured


def get_recommended_duration(scope: str, target: str, action: str) -> int:
    """Return the minimum recommended duration for a fault type."""
    return _FAULT_TYPE_MIN_DURATION.get(
        (scope, target, action),
        _DEFAULT_MIN_DURATION,
    )


def ensure_min_duration(
    timeout_value: int | str | None,
    scope: str | None,
    target: str | None,
    action: str | None,
) -> int:
    """Resolve the effective timeout for a fault type (single source of truth).

    Called from the blade_create tool and CLI.

    When no timeout is specified, the operator-configured
    ``experiment_timeout`` (see settings) is the injected default. A
    configured default and an explicit timeout are treated by one rule:
    both pass through untouched, and a value below the per-fault-type
    empirical floor is honoured verbatim with a warning (the executor must
    not unilaterally amend a stated duration, in either direction).

    Args:
        timeout_value: Current --timeout value (0, None, or a positive int/string).
        scope/target/action: Fault type identifiers.

    Returns:
        The effective timeout value in seconds.
    """
    if scope and target and action:
        floor = get_recommended_duration(scope, target, action)
    else:
        floor = _DEFAULT_MIN_DURATION

    # Parse current value
    try:
        current = int(str(timeout_value).strip()) if timeout_value else 0
    except (ValueError, TypeError):
        current = 0

    if current <= 0:
        # Unspecified: inject the operator-configured default verbatim.
        # One rule for both sources — the floor warns, it does not lift.
        # Silently raising a configured value hides the knob's real range
        # and makes the setting untrustworthy.
        configured = _configured_experiment_timeout()
        if configured < floor:
            logger.warning(
                "Configured experiment_timeout %ss is below the recommended "
                "%ss floor for fault type (%s, %s, %s); applying the "
                "configured %ss verbatim. A window this tight may close "
                "before verification can observe the fault — such a run "
                "exits as unverified/partial rather than being extended.",
                configured, floor, scope, target, action, configured,
            )
        return configured
    if current < floor:
        # Explicit but below floor: honour the caller's value verbatim and
        # make the requested-vs-recommended gap visible. Raising it would
        # be the same defect as downgrading one (DNS-hijack discipline);
        # too-tight windows exit honestly as unverified/partial instead.
        logger.warning(
            "Explicit timeout %ss is below the recommended %ss floor for "
            "fault type (%s, %s, %s); applying the requested %ss verbatim.",
            current, floor, scope, target, action, current,
        )
        return current
    return current


# Fallback grace when settings is unreachable/misconfigured. Mirrors the
# settings.recovery_grace_seconds default so the two never disagree even
# in import-light contexts.
_DEFAULT_RECOVERY_GRACE_SECONDS = 120


def recovery_timer_seconds(duration_seconds: int | str | None) -> int:
    """Return the fault's own recovery-timer seconds (single source of truth).

    Two-number window contract:
    - observation window D = the approved ``duration_seconds`` — the
      framework's presence obligation; hold dispatch is anchored to its end;
    - safety-net timer D + G — what the fault's own timer must be armed with
      (blade ``--timeout`` / carrier ``sleep`` / ``systemd-run --on-active``),
      G = ``settings.recovery_grace_seconds`` (hot-reloadable, read per
      call). The grace makes an actively dispatched framework recovery
      (landing = D + recover latency) fire *before* self-recovery expiry,
      so only the framework-death branch ever touches D+G.

    Non-positive / unparseable durations are rejected: the registry's
    contract rejection still fires first on the injection path, but this
    helper never fabricates a timer out of a broken D. A negative grace is
    clamped to 0 (safety net == observation window) with a warning.
    """
    try:
        duration = int(str(duration_seconds).strip()) if duration_seconds else 0
    except (ValueError, TypeError):
        duration = 0
    if duration <= 0:
        raise ValueError(
            "recovery_timer_seconds requires a positive contract duration, "
            f"got {duration_seconds!r}"
        )
    try:
        from chaos_agent.config.settings import settings
        grace = int(settings.recovery_grace_seconds)
    except (ImportError, ValueError, TypeError):
        grace = _DEFAULT_RECOVERY_GRACE_SECONDS
    if grace < 0:
        logger.warning(
            "recovery_grace_seconds=%s is negative; clamping to 0 (safety "
            "net collapses onto the observation window).", grace,
        )
        grace = 0
    return duration + grace


def extract_fault_type(category: str) -> str:
    """Extract the fault layer/type from a category name.

    Maps category names like 'Pod_Pending', 'workload_xxx',
    '节点容器运行时磁盘使用率过高' to standardized fault types:
    Pod, Workload, Service, Node.

    Priority: Node > Workload > Service > Pod (more specific first).
    """
    cat_lower = category.lower()
    if any(k in cat_lower for k in ("node", "节点", "宿主机")):
        return "Node"
    if any(k in cat_lower for k in ("workload", "副本", "deployment", "扩容", "缩容")):
        return "Workload"
    if any(k in cat_lower for k in ("service", "服务发现", "负载均衡", "endpoints", "conditions")):
        return "Service"
    if any(k in cat_lower for k in ("pod", "容器", "崩溃", "重启", "oom", "cpu", "内存", "镜像", "挂载", "terminating", "initializing", "creating")):
        return "Pod"
    # Fallback: use first segment before underscore or whole string
    return category.split("_")[0] if "_" in category else category


def parse_k8s_memory_to_mb(value: str) -> int | None:
    """Parse a Kubernetes resource quantity string to megabytes.

    Handles binary suffixes (Ki, Mi, Gi, Ti), decimal suffixes (k, M, G),
    and plain integers (treated as bytes). Returns None on any parse failure.

    Examples:
        "200Mi" → 200
        "1Gi"   → 1024
        "131072Ki" → 128
        "1073741824" → 1024
        "" → None
    """
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None

    # Binary suffixes (powers of 1024)
    _BINARY_SUFFIXES: dict[str, int] = {
        "Ki": 1024,
        "Mi": 1024 ** 2,
        "Gi": 1024 ** 3,
        "Ti": 1024 ** 4,
    }
    # Decimal suffixes (powers of 1000, rarely used for memory)
    _DECIMAL_SUFFIXES: dict[str, int] = {
        "k": 1000 ** 1,
        "M": 1000 ** 2,
        "G": 1000 ** 3,
    }

    for suffix, multiplier in _BINARY_SUFFIXES.items():
        if value.endswith(suffix):
            try:
                num = float(value[: -len(suffix)])
                return max(1, int(num * multiplier / (1024 ** 2)))
            except (ValueError, TypeError):
                return None

    for suffix, multiplier in _DECIMAL_SUFFIXES.items():
        if value.endswith(suffix):
            try:
                num = float(value[: -len(suffix)])
                return max(1, int(num * multiplier / (1000 ** 2)))
            except (ValueError, TypeError):
                return None

    # Plain integer → bytes
    try:
        bytes_val = int(value)
        if bytes_val <= 0:
            return None
        return max(1, bytes_val // (1024 ** 2))
    except (ValueError, TypeError):
        return None
