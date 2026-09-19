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

# Default minimum duration when fault type is not in the table
# Must be >= 300s per requirement. This is the ABSOLUTE safety floor:
# the operator-configured ``experiment_timeout`` is clamped up to it.
_DEFAULT_MIN_DURATION = 300


def _configured_experiment_timeout() -> int:
    """Return the operator-configured experiment timeout (seconds).

    Reads ``settings.experiment_timeout`` (config.json / env override).
    Imported lazily so this carrier-agnostic utility module stays
    import-light. Values below ``_DEFAULT_MIN_DURATION`` are clamped
    up: the empirical verification-latency floor always wins.
    """
    try:
        from chaos_agent.config.settings import settings
        configured = int(settings.experiment_timeout)
    except (ImportError, ValueError, TypeError):
        configured = _DEFAULT_MIN_DURATION
    return max(configured, _DEFAULT_MIN_DURATION)


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
    ``experiment_timeout`` (see settings) is the injected default,
    never below the per-fault-type empirical floor. Explicit timeouts
    pass through untouched — an explicitly requested duration below the
    floor is honoured verbatim with a warning (the executor must not
    unilaterally amend a contract-stated duration, in either direction).

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
        # Unspecified: inject the configured default, clamped to the floor.
        return max(_configured_experiment_timeout(), floor)
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
