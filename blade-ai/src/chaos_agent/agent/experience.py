"""EXPERIENCE.md experience accumulation system.

Provides load/append/truncate functions for ~/.blade-ai/EXPERIENCE.md,
enabling the Agent to learn from past operations.

Borrowed from Claude Code's CLAUDE.md and OpenClaw's context file budgeting.
"""

import os
import warnings
from pathlib import Path

from chaos_agent.agent.spec.fault_spec import fault_type_from_state
from chaos_agent.agent.result.operation_outcome import read_inject_verification, read_operation_outcome
from chaos_agent.agent.prompts.constants import MAX_EXPERIENCE_MD_BYTES
from chaos_agent.utils.truncation import build_truncation_notice

EXPERIENCE_MD_PATH = Path(os.path.expanduser("~/.blade-ai/EXPERIENCE.md"))
MAX_EXPERIENCE_MD_LINES = 200


def load_agent_experience() -> str:
    """Load ~/.blade-ai/EXPERIENCE.md if exists, with size budgeting.

    - If file exceeds MAX_EXPERIENCE_MD_BYTES, truncate preserving head (75%) and tail (25%)
      (borrowed from OpenClaw's context file budgeting pattern)
    - Both truncation paths (byte budget, line budget) speak the shared
      truncation dialect: quantified elision markers + ONE state-evidence
      notice appended at the end (honest original size + read_file
      retrieval guidance)
    - Returns empty string if file doesn't exist (no warning for missing file)
    """
    if not EXPERIENCE_MD_PATH.is_file():
        return ""

    try:
        content = EXPERIENCE_MD_PATH.read_text(encoding="utf-8").strip()
    except Exception as exc:
        warnings.warn(f"Failed to read EXPERIENCE.md: {exc}", RuntimeWarning, stacklevel=2)
        return ""

    if not content:
        return ""

    # Size budgeting — the notice counts against the byte budget (the
    # returned text stays within MAX_EXPERIENCE_MD_BYTES).
    original_bytes = len(content.encode("utf-8"))
    notice = ""
    if original_bytes > MAX_EXPERIENCE_MD_BYTES:
        notice = _experience_truncation_notice(original_bytes)
        content = _truncate_with_budget(content, notice)

    lines = content.split("\n")
    if len(lines) > MAX_EXPERIENCE_MD_LINES:
        # Keep head (75%) and tail (25%) with a quantified elision marker
        # (line-level keeps Markdown paragraph integrity).
        head_count = int(MAX_EXPERIENCE_MD_LINES * 0.75)
        tail_count = MAX_EXPERIENCE_MD_LINES - head_count
        elided_lines = len(lines) - head_count - tail_count
        content = (
            "\n".join(lines[:head_count])
            + f"\n...[{elided_lines} lines elided]...\n"
            + "\n".join(lines[-tail_count:])
        )
        if not notice:
            # The byte-budget notice already rides the tail above (it is
            # the last line and survives the line cut); only the
            # line-budget-only path appends it here.
            content += _experience_truncation_notice(original_bytes)
    return content


def _experience_truncation_notice(original_bytes: int) -> str:
    """State-evidence notice for the EXPERIENCE.md injection truncation.

    The full file stays on disk and read_file's 50KB cap exceeds the 25KB
    injection budget, so the retrieval hint is a promise this loader keeps.
    """
    return build_truncation_notice(
        "state-evidence",
        original_bytes,
        state_hint="Full EXPERIENCE.md is re-readable via the read_file tool",
        unit="bytes",
    )


def _truncate_with_budget(content: str, notice: str = "") -> str:
    """Truncate content to fit byte budget, preserving head 75% and tail 25%.

    The quantified elision marker and the caller's state-evidence notice
    both count against MAX_EXPERIENCE_MD_BYTES: the returned text stays
    within the ceiling instead of silently returning budget + notice.
    """
    encoded = content.encode("utf-8")
    if len(encoded) <= MAX_EXPERIENCE_MD_BYTES:
        return content

    # Reserve for the marker (worst-case numeric width) + the notice.
    reserved = 64 + len(notice.encode("utf-8"))
    head_bytes = int((MAX_EXPERIENCE_MD_BYTES - reserved) * 0.75)
    tail_bytes = MAX_EXPERIENCE_MD_BYTES - reserved - head_bytes

    head = encoded[:head_bytes].decode("utf-8", errors="ignore")
    tail = (
        encoded[len(encoded) - tail_bytes:].decode("utf-8", errors="ignore")
        if tail_bytes > 0
        else ""
    )
    omitted = len(encoded) - len(head.encode("utf-8")) - len(tail.encode("utf-8"))
    return f"{head}\n...[{omitted} bytes elided]...\n{tail}{notice}"


def ensure_experience_md_dir() -> None:
    """Ensure ~/.blade-ai/ directory exists."""
    EXPERIENCE_MD_PATH.parent.mkdir(parents=True, exist_ok=True)


def append_experience(task_summary: str, state: dict) -> dict:
    """Append a learned experience to EXPERIENCE.md (called when self_evolution=True).

    Uses LLM to extract a structured Rule+Why+How entry from the task outcome.
    Only appends if the task produced non-trivial learnings (failures, workarounds, etc.).

    Args:
        task_summary: Human-readable summary of the completed task.
        state: Agent state dict containing skill_name, fault_type, verification_result, errors, etc.

    Returns:
        dict with keys:
          - status: "appended" | "skipped" — whether an entry was written
          - reason: human-readable explanation for the status
          - category: the EXPERIENCE.md section targeted (e.g. "Verification")
          - entry_preview: first 120 chars of the appended entry (empty if skipped)

    Content filtering rules — only record:
    - Workarounds found after fault injection failure
    - Unexpected behavior discovered during verification
    - Cluster/environment-specific pitfalls
    - Safety-related new findings

    Skip recording:
    - Routine tasks where everything went smoothly
    - Tasks cancelled by the user
    - Experiences that duplicate existing rules
    """
    # 1. 从 state 中提取关键信息
    fault_type = fault_type_from_state(state)
    verification = read_inject_verification(state) or {}
    errors = read_operation_outcome(state).error
    l2_status = verification.get("layer2", {}).get("status", "") if isinstance(verification, dict) else ""

    # 2. 判断是否有值得记录的经验
    has_failure = bool(errors)
    has_verification_issue = l2_status in ("failed", "skipped", "unknown")
    if not has_failure and not has_verification_issue:
        # 一切顺利的常规任务，跳过记录
        return {
            "status": "skipped",
            "reason": "Routine task — no failure or verification issue to record",
            "category": "",
            "entry_preview": "",
        }

    # 3. 格式化为 Rule+Why+How 三段式
    # Determine the category based on what happened
    if has_failure and "safety" in str(errors).lower():
        category = "Safety Rules"
    elif has_failure and fault_type:
        category = "Fault Injection"
    elif has_verification_issue:
        category = "Verification"
    else:
        # Environment-specific pitfalls are filed under the active profile:
        # host faults land in "Host", cluster faults in "K8s Cluster".
        from chaos_agent.transports.registry import is_host_scope_channel
        category = "Host" if is_host_scope_channel(state) else "K8s Cluster"

    # Build the experience entry
    rule_text = task_summary[:200] if task_summary else f"Issue with {fault_type}"
    why_text = str(errors)[:300] if errors else f"Verification {l2_status}"
    how_text = "Apply caution when encountering similar scenarios."

    entry = (
        f"- Rule: {rule_text}\n"
        f"  Why: {why_text}\n"
        f"  How: {how_text}\n"
    )

    # 4. 追加到 EXPERIENCE.md 对应分类下
    ensure_experience_md_dir()

    if EXPERIENCE_MD_PATH.is_file():
        content = EXPERIENCE_MD_PATH.read_text(encoding="utf-8")
    else:
        content = _default_agent_md_template()

    # Find the category section and append
    category_header = f"## {category}"
    if category_header in content:
        # Insert after the category header (and optional comment)
        idx = content.index(category_header) + len(category_header)
        # Skip past any comment line
        rest = content[idx:]
        if rest.startswith("\n<!--"):
            comment_end = rest.find("-->")
            if comment_end >= 0:
                idx += comment_end + 3
        content = content[:idx] + "\n" + entry + content[idx:]
    else:
        # Category doesn't exist, add at the end
        content = content.rstrip() + f"\n\n{category_header}\n{entry}"

    # 5. Check budget and truncate if needed
    if len(content.encode("utf-8")) > MAX_EXPERIENCE_MD_BYTES:
        content = _truncate_with_budget(content)

    EXPERIENCE_MD_PATH.write_text(content, encoding="utf-8")

    return {
        "status": "appended",
        "reason": f"Non-trivial outcome: {'error' if has_failure else 'verification_issue'}",
        "category": category,
        "entry_preview": entry[:120],
    }


def _default_agent_md_template() -> str:
    """Default template for a new EXPERIENCE.md file."""
    return """# Blade AI Experience Log

## Safety Rules
<!-- Safety lessons: which operations demand extra caution -->

## Fault Injection
<!-- Injection lessons: command construction, parameter choice, common traps -->

## Verification
<!-- Verification lessons: strategies, common misjudgements, minimal container environments -->

## Recovery
<!-- Recovery lessons: handling failed recovery, cascading faults, leftover cleanup -->

## K8s Cluster
<!-- Cluster-specific lessons: config differences, permission issues, network policies -->

## Host
<!-- Host-specific lessons: host connectivity, native command carriers, reverse-recovery traps -->
"""
