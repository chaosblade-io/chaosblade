"""Fault-type classification utilities."""

import shlex

# ChaosBlade K8s valid scopes
VALID_SCOPES = {"node", "pod", "container"}

# ChaosBlade K8s valid targets per scope
VALID_TARGETS: dict[str, set[str]] = {
    "pod": {"cpu", "network", "disk", "process", "pod", "mem", "file", "script"},
    "node": {"cpu", "network", "disk", "process", "mem"},
    "container": {"cpu", "network", "disk", "process", "mem"},
}


def validate_blade_params(scope: str, target: str, action: str) -> tuple[bool, str]:
    """Validate scope/target/action for ChaosBlade K8s scenarios.

    Returns:
        (is_valid, error_message) — error_message is empty string when valid.
    """
    if scope not in VALID_SCOPES:
        return False, f"Invalid scope '{scope}', must be one of {sorted(VALID_SCOPES)}"
    valid = VALID_TARGETS.get(scope, set())
    if target not in valid:
        return False, f"Invalid target '{target}' for scope '{scope}', must be one of {sorted(valid)}"
    if not action:
        return False, "action is required"
    return True, ""


def build_blade_create_args(
    scope: str,
    target: str,
    action: str,
    namespace: str = "",
    names: str = "",
    labels: str = "",
    kubeconfig: str = "",
    params: dict = None,
    params_flags: list = None,
    duration: int = 0,
) -> dict:
    """Build blade_create.ainvoke() arguments from structured parameters.

    Construction logic:
    1. params key-value pairs → "--key value" in flags
    2. params_flags bare keys → "--key" in flags (boolean flags)
    3. duration > 0 → "--timeout <duration>" appended to flags
    4. evict_count/evict_percent left empty (not needed for direct scenarios)

    Returns:
        Dict matching blade_create tool signature:
        {scope, target, action, namespace, names, labels, kubeconfig,
         evict_count, evict_percent, flags}
    """
    flags_parts = []
    if params:
        for k, v in params.items():
            flags_parts.extend([f"--{k}", str(v)])
    if params_flags:
        for flag in params_flags:
            flags_parts.append(f"--{flag}")
    if duration > 0:
        flags_parts.extend(["--timeout", str(duration)])

    return {
        "scope": scope,
        "target": target,
        "action": action,
        "namespace": namespace,
        "names": names,
        "labels": labels,
        "kubeconfig": kubeconfig,
        "evict_count": "",
        "evict_percent": "",
        "flags": " ".join(flags_parts),
    }


def parse_blade_flags(flags_str: str) -> dict[str, str]:
    """Parse key parameters from blade flags string.

    Extracts structured parameter values from the raw flags string
    for verifier/recover_verifier consumption.

    Returns dict with only the parameters found, e.g.:
      {"path": "/tmp", "percent": "85", "timeout": "600"}
    """
    # Key parameters that affect verification strategy
    KEY_PARAMS = {"path", "percent", "size", "timeout", "time", "cpu-percent", "mem-percent"}
    result: dict[str, str] = {}
    if not flags_str:
        return result
    try:
        tokens = shlex.split(flags_str)
    except ValueError:
        return result
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.startswith("--"):
            key = token[2:]
            if key in KEY_PARAMS and i + 1 < len(tokens):
                result[key] = tokens[i + 1]
                i += 2
                continue
        i += 1
    return result


# Minimum recommended duration per fault type (scope, target, action)
# All values >= 600s per requirement. Based on empirical measurement of
# Layer1 + Layer2 verification latency + ChaosBlade scheduling delay.
_FAULT_TYPE_MIN_DURATION: dict[tuple[str, str, str], int] = {
    # Node-level: high latency (kubectl debug + host-level commands)
    ("node", "disk", "fill"): 600,
    ("node", "network", "delay"): 600,
    ("node", "network", "loss"): 600,
    ("node", "cpu", "fullload"): 600,
    ("node", "mem", "load"): 600,
    ("node", "disk", "burn"): 600,
    ("node", "network", "corrupt"): 600,
    ("node", "network", "duplicate"): 600,
    ("node", "network", "reorder"): 600,
    # Pod-level: medium latency (kubectl top/exec/describe)
    ("pod", "cpu", "fullload"): 600,
    ("pod", "mem", "load"): 600,
    ("pod", "network", "delay"): 600,
    ("pod", "network", "loss"): 600,
    ("pod", "disk", "fill"): 600,
    ("pod", "disk", "burn"): 600,
    ("pod", "network", "corrupt"): 600,
    ("pod", "network", "duplicate"): 600,
    ("pod", "network", "reorder"): 600,
    ("pod", "process", "kill"): 600,
    # Container-level
    ("container", "cpu", "fullload"): 600,
    ("container", "mem", "load"): 600,
    ("container", "network", "delay"): 600,
    ("container", "network", "loss"): 600,
}

# Default minimum duration when fault type is not in the table
# Must be >= 600s per requirement
_DEFAULT_MIN_DURATION = 600


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
    """Ensure timeout meets the minimum recommended duration for the fault type.

    This is the SINGLE source of truth for duration auto-boost logic.
    Called from blade_create tool, CLI, and direct_execute.

    Args:
        timeout_value: Current --timeout value (0, None, or a positive int/string).
        scope/target/action: Fault type identifiers.

    Returns:
        The effective timeout value (at least the recommended minimum).
    """
    if scope and target and action:
        recommended = get_recommended_duration(scope, target, action)
    else:
        recommended = _DEFAULT_MIN_DURATION

    # Parse current value
    try:
        current = int(str(timeout_value).strip()) if timeout_value else 0
    except (ValueError, TypeError):
        current = 0

    if current < recommended:
        return recommended
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
