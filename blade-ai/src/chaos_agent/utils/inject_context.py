"""Inject-context construction for recovery verification.

When a recovery task starts, it needs to know what fault was injected during
the parent inject task.  The inject_context string carries this information.

**Design rationale (first-principles — causal chain illusion prevention)**:

Inject-phase kubectl tool results (df -h overlay 12%, kubectl describe pod,
etc.) must NOT be provided in reusable raw-output format.  Recovery verifier
LLMs suffer from "causal chain illusion": when same-format, same-command,
same-target data from the inject phase is available in context, the LLM
perceives the causal chain as complete ("I already have kubectl results →
no need to call kubectl again") and skips tool calls entirely — using stale
inject-phase data as "current" post-recovery evidence.

Root cause (5 axioms):
  1. LLM is a causal reasoning engine — tool-call behavior follows causal
     chain completeness perception, not external rule compliance.
  2. "MUST call kubectl" rules are rationally ignored when causal chain
     is perceived as complete.
  3. LLMs judge data by SOURCE credibility (tool-obtained = credible), not
     by TIME semantics (injection-phase vs recovery-phase).
  4. Same-format data triggers "data reusability illusion" — LLM reuses
     inject-phase outputs without recognizing they represent FAULT-STATE,
     not CURRENT-STATE.
  5. Rejection messages cannot break causal chain illusion because LLM's
     internal causal chain remains self-consistent.

Solution: physically break the causal chain by removing reusable raw kubectl
output from inject_context.  Only provide:
  - AI reasoning summaries (LLM conclusions, not raw data)
  - Tool result ABSTRACTS (tool type + 1-line summary, no raw output)
  - EXPIRED marker + MUST-re-execute instruction

This forces the LLM to perceive "I lack current data → causal chain broken
→ must call kubectl" instead of "I have data → causal chain complete → no
need to call kubectl".

Reference: task-b6cce4c1 recovery verifier failure analysis.
"""

from __future__ import annotations

import logging
import re

from langchain_core.messages import AIMessage as _AIMsg, ToolMessage as _ToolMsg

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_INJECT_CONTEXT_MAX_LEN = 4000

# Tool-output truncation: short enough that LLM cannot "reuse" the content
# as current-state evidence (no specific metric values extractable), but
# long enough to convey what the tool did and whether it succeeded.
_TOOL_ABSTRACT_MAX_LEN = 80

# AI-content truncation: keep LLM reasoning/conclusions (these are abstract
# interpretations, not raw data, so they don't trigger reuse illusion).
_AI_CONTENT_MAX_LEN = 500


# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------

def build_inject_context(inject_messages: list) -> str:
    """Build the inject_context string from inject-phase messages.

    Reformatted to prevent causal chain illusion: raw kubectl tool outputs
    are replaced with short abstracts (tool name + success/fail + 1-line
    summary), preventing LLM from reusing stale injection-phase data as
    "current" recovery-state evidence.

    Args:
        inject_messages: List of LangChain messages from the inject-phase task.

    Returns:
        inject_context string (max ~4000 chars), or empty string if no data.
    """
    if not inject_messages:
        return ""

    parts: list[str] = []

    # ── Phase 1: AI reasoning summaries ──
    # These are LLM's own conclusions/interpretations — they describe what
    # the LLM *decided* (e.g., "the disk burn is verified"), not raw data
    # that can be reused as "current evidence".  Low reuse risk.
    for _m in inject_messages:
        if isinstance(_m, _AIMsg) and _m.content and _m.content.strip():
            parts.append(_m.content.strip()[:_AI_CONTENT_MAX_LEN])

    # ── Phase 2: Tool result abstracts (NOT raw output) ──
    # Each tool result is reduced to: [tool_type] abstract_summary
    # The abstract contains: what the tool did, whether it succeeded,
    # and a 1-line content summary — but NOT the raw kubectl output
    # that LLM could reuse as "current df -h 12%" evidence.
    for _m in inject_messages:
        if isinstance(_m, _ToolMsg) and _m.content:
            _name = getattr(_m, "name", "unknown")
            _content = _m.content if isinstance(_m.content, str) else ""
            if not _content.strip():
                continue
            abstract = _abstract_tool_result(_name, _content)
            parts.append(abstract)

    if not parts:
        return ""

    raw_context = "\n\n---\n\n".join(parts)[:_INJECT_CONTEXT_MAX_LEN]

    # ── Phase 3: EXPIRED marker + MUST-re-execute instruction ──
    # This creates a causal chain break: "inject-phase data is stale →
    # I must re-execute kubectl to get current data".
    expired_prefix = (
        "⚠️ EXPIRED DATA — captured during fault injection (NOT current state)\n"
        "The observations below were made WHILE THE FAULT WAS ACTIVE. "
        "They represent the FAULT STATE, not the CURRENT (post-recovery) state.\n"
        "You MUST re-execute the same kubectl commands NOW to obtain CURRENT data. "
        "Using these injection-phase observations as 'current evidence' is INVALID.\n"
    )

    return expired_prefix + raw_context


# ---------------------------------------------------------------------------
# Tool-result abstracting
# ---------------------------------------------------------------------------

def _abstract_tool_result(tool_name: str, content: str) -> str:
    """Reduce a tool result to a short abstract that conveys what was done
    without providing reusable raw data.

    Strategy by tool type:
      - kubectl: Extract success/fail + 1-line summary. Strip all specific
        metric values (percentages, sizes, counts) from the summary to
        prevent reuse as "current state" evidence.
      - blade_create/blade_destroy: Keep structured JSON result (code, success)
        — this is not reusable as kubectl evidence.
      - blade_status: Keep structured result — same as above.
      - Other: Generic abstract with content truncated to 80 chars.
    """
    if tool_name == "kubectl" or tool_name == "":
        return _abstract_kubectl_result(content)
    elif tool_name.startswith("blade_"):
        return _abstract_blade_result(tool_name, content)
    else:
        # Generic: truncate to abstract length, strip metric-like values
        _c = content.strip()[:_TOOL_ABSTRACT_MAX_LEN]
        _c = _strip_metric_values(_c)
        return f"[{tool_name}] {_c}"


def _abstract_kubectl_result(content: str) -> str:
    """Abstract a kubectl tool result.

    Detects the kubectl subcommand from content patterns and produces
    a 1-line summary that describes WHAT was observed but NOT the
    specific values that could be reused.

    Examples:
      Input:  "Filesystem 116G 13G 97G 12% overlay / ..."
      Output: "[kubectl] df -h: disk usage report for 1 filesystem (values expired — re-execute df -h now)"

      Input:  "Name: accounting-6fbdb464c7-qn2vr\nNamespace: cms-demo\n..."
      Output: "[kubectl] describe pod: pod accounting in cms-demo (status/details expired — re-execute now)"

      Input:  "NAME READY STATUS RESTARTS AGE\notel-c-tool..."
      Output: "[kubectl] get pods: listed pods in namespace (details expired — re-execute now)"
    """
    _c = content.strip()

    # Detect subcommand type from content pattern
    if _looks_like_df_output(_c):
        # df -h output: count filesystems, but strip all values
        fs_count = _count_filesystems_in_df(_c)
        return (
            f"[kubectl] df -h: disk usage report for {fs_count} filesystem(s) "
            f"(values expired — re-execute df -h now)"
        )

    if _looks_like_describe_output(_c):
        # describe pod/node output: extract name and namespace if possible
        pod_name = _extract_pod_name_from_describe(_c)
        ns = _extract_namespace_from_describe(_c)
        name_part = f" {pod_name}" if pod_name else ""
        ns_part = f" in {ns}" if ns else ""
        return (
            f"[kubectl] describe pod{name_part}{ns_part} "
            f"(status/details expired — re-execute now)"
        )

    if _looks_like_get_output(_c):
        # get pods/nodes output: count rows
        row_count = _count_table_rows(_c)
        return (
            f"[kubectl] get: resource listing with {row_count} entries "
            f"(details expired — re-execute now)"
        )

    if _looks_like_blade_json(_c):
        # blade create/destroy/status JSON wrapped in kubectl exec
        # (when blade commands are executed via kubectl exec, the ToolMessage
        # name is "kubectl", not "blade_*").  This is structural info about
        # ChaosBlade operation success/failure — safe to include because
        # it can't be reused as "current pod state" evidence.
        return _abstract_blade_in_kubectl(_c)

    if _looks_like_exec_output(_c):
        # exec output (ls, du, top, etc.): describe what was checked
        cmd_hint = _extract_exec_command_hint(_c)
        return (
            f"[kubectl] exec {cmd_hint}: "
            f"output observed during injection (expired — re-execute now)"
        )

    if _looks_like_events_output(_c):
        return (
            "[kubectl] get events: event listing from injection phase "
            "(expired — re-execute now)"
        )

    # Fallback: generic abstract with metric values stripped
    _c_abstract = _strip_metric_values(_c[:_TOOL_ABSTRACT_MAX_LEN])
    return f"[kubectl] {_c_abstract} (expired — re-execute now)"


def _abstract_blade_result(tool_name: str, content: str) -> str:
    """Abstract blade create/destroy/status results.

    These are structured JSON (code, success, result) — they describe
    whether the ChaosBlade operation succeeded, not kubectl observations.
    Safe to include because they can't be reused as "current pod state".
    """
    _c = content.strip()[:200]
    # blade results are short JSON — keep them mostly intact
    # but strip any embedded kubectl-like metric data
    _c = _strip_metric_values(_c)
    return f"[{tool_name}] {_c}"


# ---------------------------------------------------------------------------
# Content pattern detectors
# ---------------------------------------------------------------------------

def _looks_like_df_output(content: str) -> bool:
    """Detect df -h output: lines with 'Filesystem' header and percentage usage."""
    return bool(re.search(r"^Filesystem\s+", content, re.MULTILINE))


def _looks_like_describe_output(content: str) -> bool:
    """Detect kubectl describe output: 'Name:' header with resource details."""
    return bool(re.search(r"^Name:\s+", content, re.MULTILINE))


def _looks_like_get_output(content: str) -> bool:
    """Detect kubectl get output: table with 'NAME' header."""
    return bool(re.search(r"^NAME\s+", content, re.MULTILINE))


def _looks_like_exec_output(content: str) -> bool:
    """Detect kubectl exec output: various formats (ls, du, top, cat, etc.)."""
    # blade JSON outputs should be caught first (by _looks_like_blade_json)
    # before falling through to exec classification
    if _looks_like_blade_json(content):
        return False
    # exec outputs are diverse — check for common patterns:
    # ls output: total X, filenames
    # du output: size + path
    # top output: PID, %CPU, %MEM
    # cat/echo output: plain text
    # Don't match if it's already classified as df/describe/get/events
    if (_looks_like_df_output(content) or _looks_like_describe_output(content)
            or _looks_like_get_output(content) or _looks_like_events_output(content)):
        return False
    # exec outputs are short-ish and don't have the above headers
    return len(content) < 5000 and len(content.strip()) > 0


def _looks_like_events_output(content: str) -> bool:
    """Detect kubectl get events output."""
    return bool(re.search(r"^LAST\s+SEEN\s+", content, re.MULTILINE))


# ---------------------------------------------------------------------------
# Blade JSON detection (when wrapped in kubectl exec)
# ---------------------------------------------------------------------------

def _looks_like_blade_json(content: str) -> bool:
    """Detect ChaosBlade JSON output wrapped in kubectl exec ToolMessage.

    When blade commands are executed via `kubectl exec pod -- blade create/destroy/status`,
    the ToolMessage name is "kubectl" (not "blade_*"). The content is structured JSON
    with "code" and "success" fields. This is structural info about ChaosBlade operation
    success, not pod state data — safe to include with minimal abstraction.
    """
    _c = content.strip()
    # Must start with { and contain "code" and "success" keys
    if not _c.startswith("{"):
        return False
    return '"code"' in _c and '"success"' in _c


def _abstract_blade_in_kubectl(content: str) -> str:
    """Abstract blade JSON output that was wrapped in kubectl exec.

    Preserves the operation result (success/failure, command description)
    but strips any embedded kubectl-like metric data. The "code" and "success"
    fields tell the LLM whether blade destroy succeeded — this is structural
    info, not reusable as pod state evidence.
    """
    import json as _json
    _c = content.strip()
    try:
        data = _json.loads(_c)
        code = data.get("code", "unknown")
        success = data.get("success", "unknown")
        result = data.get("result", "")
        # Extract just the command portion from the result string
        # e.g. "command: k8s pod-disk burn --path=/tmp, destroy time: ..."
        # We keep only the command description, not any metric values
        result_abstract = _strip_metric_values(result[:150])
        return (
            f"[kubectl] blade operation result: "
            f"code={code}, success={success}, "
            f"{result_abstract}"
        )
    except (_json.JSONDecodeError, ValueError):
        # Not valid JSON — fallback to generic abstract
        _c_abstract = _strip_metric_values(_c[:_TOOL_ABSTRACT_MAX_LEN])
        return f"[kubectl] {_c_abstract} (expired — re-execute now)"


# ---------------------------------------------------------------------------
# Content extractors (for abstract building)
# ---------------------------------------------------------------------------

def _count_filesystems_in_df(content: str) -> int:
    """Count filesystem entries in df -h output (excluding header line)."""
    lines = content.strip().split("\n")
    # Skip header line (starts with 'Filesystem')
    data_lines = [line for line in lines[1:] if line.strip() and not line.startswith("Filesystem")]
    return len(data_lines)


def _extract_pod_name_from_describe(content: str) -> str:
    """Extract pod name from describe output 'Name: <pod-name>' line."""
    match = re.search(r"^Name:\s+(\S+)", content, re.MULTILINE)
    return match.group(1) if match else ""


def _extract_namespace_from_describe(content: str) -> str:
    """Extract namespace from describe output 'Namespace: <ns>' line."""
    match = re.search(r"^Namespace:\s+(\S+)", content, re.MULTILINE)
    return match.group(1) if match else ""


def _count_table_rows(content: str) -> int:
    """Count data rows in kubectl get table output."""
    lines = content.strip().split("\n")
    # Skip header line
    data_lines = [line for line in lines[1:] if line.strip() and not line.startswith("NAME")]
    return len(data_lines)


def _extract_exec_command_hint(content: str) -> str:
    """Extract a short hint about what exec command produced this output."""
    _c = content.strip()
    # Check for common exec output patterns
    if _c.startswith("total ") or "drwx" in _c[:20]:
        return "ls/dir listing"
    if re.search(r"^\d+[KMG]?\s+", _c):
        return "du/size check"
    if re.search(r"^\s*PID\s+", _c):
        return "top/process stats"
    if "Memory" in _c[:50] or "CPU" in _c[:50]:
        return "resource stats"
    return "command output"


# ---------------------------------------------------------------------------
# Metric-value stripping
# ---------------------------------------------------------------------------

# Patterns for metric values that LLM could reuse as "current state" evidence
_METRIC_VALUE_PATTERNS = [
    # Percentage values: "12%", "84%", "0.5%"
    re.compile(r"\b\d+(?:\.\d+)?%"),
    # Size values with units: "13G", "100M", "4.0K", "2Gi"
    re.compile(r"\b\d+(?:\.\d+)?[KMG]i?\b"),
    # Restart counts: "Restart Count: 7", "RESTARTS 3"
    re.compile(r"(?:Restart\s+Count|RESTARTS)[:\s]+\d+"),
    # CPU/memory percentages: "CPU: 800m", "Memory: 512Mi"
    re.compile(r"\b(?:CPU|Memory|Mem|Cpu)[:\s]+\d+[mMiKg]+\b"),
]


def _strip_metric_values(text: str) -> str:
    """Remove specific metric values from text to prevent reuse as current-state evidence.

    Replaces percentage values, size values, restart counts, and resource metrics
    with '[expired]' markers, keeping the structural context (what was measured)
    but removing the specific values.
    """
    result = text
    for pattern in _METRIC_VALUE_PATTERNS:
        result = pattern.sub("[expired]", result)
    return result