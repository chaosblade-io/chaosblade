"""LLM-based structured compaction for Session Memory (Layer 2).

Generates structured summaries from conversation history using the
Claude Code two-step compaction pattern: <analysis> drafting + <summary> output.

Supports three compaction modes (BASE / PARTIAL / UP_TO) and provides
post-compaction context recovery aligned with Claude Code's
createPlanAttachmentIfNeeded / createSkillAttachmentIfNeeded.

Implements layered compaction aligned with Claude Code's sessionMemoryCompact.ts:
- Layer 1: try_lightweight_compact() — trim messages without LLM
- Layer 2: compact_memory() — full LLM-based structured summary
- compact_if_needed() — entry point that tries Layer 1 first, falls back to Layer 2
"""

import logging
import re
from enum import Enum
from typing import Optional

from langchain_core.messages import SystemMessage

from chaos_agent.config.settings import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# NO_TOOLS preamble/trailer — prevent tool calls during compaction
# Aligned with Claude Code's compact/prompt.ts NO_TOOLS_PREAMBLE
# ---------------------------------------------------------------------------

NO_TOOLS_PREAMBLE = """CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.
- You already have all the context you need in the conversation above.
- Tool calls will be rejected and waste your turn.
- Your entire response must be plain text: an <analysis> block followed by a <summary> block.

"""

NO_TOOLS_TRAILER = (
    "\n\nREMINDER: Do NOT call any tools. Respond with plain text only — "
    "an <analysis> block followed by a <summary> block."
)


# ---------------------------------------------------------------------------
# Analysis + Summary prompt templates
# Aligned with Claude Code's compact/prompt.ts DETAILED_ANALYSIS_INSTRUCTION_*
# ---------------------------------------------------------------------------

COMPACTION_ANALYSIS_PROMPT = """Before providing your final summary, wrap your analysis in <analysis> tags.
In your analysis, chronologically identify:
1. What was the user's goal?
2. What skill was activated?
3. What target was selected?
4. What has been done so far? (pre-checks, injection, verification)
5. What critical data was produced? (blade_uid, status codes, errors)
6. What remains to be done?
7. Pay special attention to specific user feedback that you received.

<analysis>
[Your analysis here]
</analysis>
"""

# Legacy single-block prompt (kept for backward compatibility)
COMPACTION_PROMPT = """Summarize the conversation into a structured format for a chaos engineering agent:

## Goal
The user's objective for this chaos engineering task.

## Target
The Kubernetes resource being targeted (namespace, pod/node name, labels).

## Skill & Parameters
The activated skill and fault parameters.

## Progress
What has been accomplished so far (pre-checks, injection, verification).

## Key Results
Critical data: blade UID, status codes, error messages, timing.

## Next Steps
What remains to be done.
"""

# BASE mode: full conversation summary
BASE_COMPACT_PROMPT = """Your task is to create a detailed summary of the conversation so far, paying close attention to the user's explicit requests and your previous actions.
This summary should be thorough in capturing technical details, code patterns, and architectural decisions that would be essential for continuing development work without losing context.

{analysis_instruction}
Your summary should include the following sections:

1. Goal: The user's objective for this chaos engineering task
2. Target: The Kubernetes resource being targeted (namespace, pod/node name, labels)
3. Skill & Parameters: The activated skill and fault parameters
4. Progress: What has been accomplished so far (pre-checks, injection, verification)
5. Key Results: Critical data: blade_uid, status codes, error messages, timing
6. Errors and Fixes: List all errors encountered and how they were resolved
7. Next Steps: What remains to be done

<example>
<analysis>
[Your thought process, ensuring all points are covered thoroughly and accurately]
</analysis>

<summary>
1. Goal:
   [Detailed description]

2. Target:
   [namespace/resource_type/names]

3. Skill & Parameters:
   [skill name and key params]

4. Progress:
   - [x] Pre-checks completed
   - [x] Fault injected (blade_uid: ...)
   - [ ] Verification pending

5. Key Results:
   - blade_uid: ...
   - status: ...

6. Errors and Fixes:
   [Any errors encountered and how they were resolved]

7. Next Steps:
   [What remains]
</summary>
</example>

Please provide your summary based on the conversation so far, following this structure.
"""

# PARTIAL mode: summarize only recent messages (earlier messages are kept intact)
PARTIAL_COMPACT_PROMPT = """Your task is to create a detailed summary of the RECENT portion of the conversation — the messages that follow earlier retained context. The earlier messages are being kept intact and do NOT need to be summarized. Focus your summary on what was discussed, learned, and accomplished in the recent messages only.

{analysis_instruction}
Your summary should include the following sections:

1. Goal: The user's objective from the recent messages
2. Target: The Kubernetes resource being targeted
3. Skill & Parameters: The activated skill and fault parameters
4. Progress: What has been accomplished recently
5. Key Results: Critical data from recent messages
6. Errors and Fixes: List errors encountered and how they were fixed
7. Next Steps: What remains from the recent messages

Please provide your summary based on the RECENT messages only, following this structure.
"""

# UP_TO mode: summarize messages up to a point (later messages are kept intact)
UP_TO_COMPACT_PROMPT = """Your task is to create a detailed summary of this conversation. This summary will be placed at the start of a continuing session; newer messages that build on this context will follow after your summary (you do not see them here). Summarize thoroughly so that someone reading only your summary and then the newer messages can fully understand what happened and continue the work.

{analysis_instruction}
Your summary should include the following sections:

1. Goal: The user's objective for this chaos engineering task
2. Target: The Kubernetes resource being targeted
3. Skill & Parameters: The activated skill and fault parameters
4. Progress: What has been accomplished
5. Key Results: Critical data
6. Work Completed: Describe what was accomplished by the end of this portion
7. Context for Continuing Work: Key context, decisions, or state needed to continue

Please provide your summary following this structure, ensuring precision and thoroughness.
"""


# ---------------------------------------------------------------------------
# Compaction mode enum
# Aligned with Claude Code's compact/prompt.ts BASE/PARTIAL/UP_TO
# ---------------------------------------------------------------------------

class CompactionMode(str, Enum):
    """Compaction mode selector.

    BASE: Full conversation summary from scratch.
    PARTIAL: Incremental update on top of an existing summary (only summarize recent messages).
    UP_TO: Summarize messages up to a point; later messages are preserved.
    """
    BASE = "base"
    PARTIAL = "partial"
    UP_TO = "up_to"


# ---------------------------------------------------------------------------
# format_compact_summary — strip <analysis> draft, format <summary> output
# Aligned with Claude Code's compact/prompt.ts formatCompactSummary()
# ---------------------------------------------------------------------------

def format_compact_summary(raw_summary: str) -> str:
    """Strip the <analysis> drafting scratchpad and format <summary> tags.

    Aligned with Claude Code's formatCompactSummary().
    The <analysis> block is a drafting scratchpad that improves summary quality
    but has no informational value once the summary is written.

    Args:
        raw_summary: The raw summary string potentially containing
                      <analysis> and <summary> XML tags.

    Returns:
        Formatted summary with analysis stripped and summary tags replaced.
    """
    result = raw_summary

    # Strip analysis section
    result = re.sub(r"<analysis>[\s\S]*?</analysis>", "", result)

    # Extract and format summary section
    summary_match = re.search(r"<summary>([\s\S]*?)</summary>", result)
    if summary_match:
        content = summary_match.group(1).strip()
        result = re.sub(
            r"<summary>[\s\S]*?</summary>",
            f"Summary:\n{content}",
            result,
        )

    # Clean up extra whitespace
    result = re.sub(r"\n\n+", "\n\n", result)
    return result.strip()


# ---------------------------------------------------------------------------
# extract_critical_context — preserve key info across compaction
# Aligned with Claude Code's compact.ts createPlanAttachmentIfNeeded /
# createSkillAttachmentIfNeeded
# ---------------------------------------------------------------------------

# Token budgets for skill content preservation
# Aligned with Claude Code's POST_COMPACT_MAX_TOKENS_PER_SKILL / SKILLS_TOKEN_BUDGET
POST_COMPACT_MAX_TOKENS_PER_SKILL = 5000
POST_COMPACT_SKILLS_TOKEN_BUDGET = 25000
POST_COMPACT_TOKEN_BUDGET = 50000

SKILL_TRUNCATION_MARKER = (
    "\n\n[... skill content truncated; "
    "re-activate the skill if you need full instructions]"
)


def truncate_to_tokens(content: str, max_tokens: int) -> str:
    """Truncate content to roughly max_tokens tokens, keeping the head.

    Aligned with Claude Code's token-aware truncation. Uses the CJK-aware
    `estimate_tokens` heuristic; the char budget is calibrated against the
    actual content's chars/token ratio so CJK-heavy text isn't over- or
    under-truncated.

    Args:
        content: Text content to potentially truncate.
        max_tokens: Maximum tokens to allow.

    Returns:
        Content truncated to the token budget with a truncation marker
        if it exceeded the budget.
    """
    from chaos_agent.memory.context_manager import estimate_tokens

    actual_tokens = estimate_tokens(content)
    if actual_tokens <= max_tokens:
        return content
    # Calibrate chars/token from this content (mixed CJK/ASCII safe).
    chars_per_token = len(content) / actual_tokens if actual_tokens else 4
    marker_tokens = estimate_tokens(SKILL_TRUNCATION_MARKER)
    available_tokens = max(0, max_tokens - marker_tokens)
    char_budget = max(0, int(available_tokens * chars_per_token))
    return content[:char_budget] + SKILL_TRUNCATION_MARKER


def _extract_skill_content_from_messages(
    messages: list, skill_name: str
) -> str:
    """Extract skill instruction content from tool_result messages.

    Aligned with Claude Code's createSkillAttachmentIfNeeded().
    Scans messages in reverse for tool results that contain the
    activated skill's instructions (e.g., from activate_skill or
    read_skill_resource tool calls).

    Args:
        messages: Conversation messages to scan.
        skill_name: Name of the skill to find content for.

    Returns:
        Skill instruction content string, or empty string if not found.
    """
    for msg in reversed(messages):
        content = getattr(msg, "content", "")
        if not isinstance(content, str):
            continue
        # Skill content typically appears in activate_skill results
        # or system messages containing the skill's instruction text
        if skill_name in content and (
            "instruction" in content.lower()
            or "pre-check" in content.lower()
            or "injection procedure" in content.lower()
            or "skill" in content.lower()
        ):
            return content
    return ""


def extract_critical_context(messages: list, state: dict) -> dict:
    """Extract critical context that must survive compaction.

    Aligned with Claude Code's createPlanAttachmentIfNeeded and
    createSkillAttachmentIfNeeded. After compaction, the conversation
    history is replaced by a summary — this function captures the
    operational state (blade UIDs, active skills, targets, plans)
    and skill instruction content that the summary may miss.

    Enhanced with skill content preservation and token budget
    (aligned with Claude Code's createSkillAttachmentIfNeeded):
    - Each skill's content is truncated to POST_COMPACT_MAX_TOKENS_PER_SKILL
    - Total skill content stays within POST_COMPACT_SKILLS_TOKEN_BUDGET

    Args:
        messages: Conversation messages to scan for critical data.
        state: AgentState dict containing current task state.

    Returns:
        Dict of critical context key-value pairs.
    """
    context = {}

    # 1. Active blade_uid (from tool_result / ToolMessage content)
    for msg in reversed(messages):
        content = getattr(msg, "content", "")
        if isinstance(content, str) and "blade_uid" in content:
            # Match blade_uid followed by separators and a hex/hyphen value
            match = re.search(r'blade_uid[":\s]+([0-9a-fA-F\-]+)', content)
            if match:
                context["active_blade_uid"] = match.group(1)
                break
        # Also check for UID in JSON-format tool results
        # blade create returns: {"code":200,"success":true,"result":"<uid>"}
        if isinstance(content, str) and '"result"' in content:
            match = re.search(r'"result"\s*:\s*"([0-9a-fA-F\-]+)"', content)
            if match:
                context["active_blade_uid"] = match.group(1)
                break

    # 2. Blade UID from state (direct field) — only used as fallback
    #    if not already found from message content
    if state.get("blade_uid") and "active_blade_uid" not in context:
        context["active_blade_uid"] = state["blade_uid"]

    # 3. Active skill info (from state) — with content preservation
    #    Aligned with Claude Code's createSkillAttachmentIfNeeded():
    #    - Each skill truncated to POST_COMPACT_MAX_TOKENS_PER_SKILL
    #    - Total skill content stays within POST_COMPACT_SKILLS_TOKEN_BUDGET
    total_skill_tokens = 0
    skill_names = []

    # Collect all active skill names (current + any from state history)
    if state.get("skill_name"):
        skill_names.append(state["skill_name"])
    if state.get("active_skills"):
        for s in state["active_skills"]:
            if s not in skill_names:
                skill_names.append(s)

    if skill_names:
        # Store primary skill name
        context["active_skill"] = skill_names[0]

        # Extract and truncate content for each skill within total budget
        skill_contents = []
        for skill_name in skill_names:
            skill_content = _extract_skill_content_from_messages(
                messages, skill_name
            )
            if not skill_content:
                continue

            # First, truncate to per-skill budget
            truncated = truncate_to_tokens(
                skill_content, POST_COMPACT_MAX_TOKENS_PER_SKILL
            )

            # Then, check total budget
            from chaos_agent.memory.context_manager import estimate_tokens
            truncated_tokens = estimate_tokens(truncated)
            if total_skill_tokens + truncated_tokens > POST_COMPACT_SKILLS_TOKEN_BUDGET:
                # Truncate further to remaining budget
                remaining = POST_COMPACT_SKILLS_TOKEN_BUDGET - total_skill_tokens
                if remaining > 0:
                    truncated = truncate_to_tokens(skill_content, remaining)
                    skill_contents.append(truncated)
                    total_skill_tokens = POST_COMPACT_SKILLS_TOKEN_BUDGET
                break  # Budget exhausted

            skill_contents.append(truncated)
            total_skill_tokens += truncated_tokens

        if skill_contents:
            # Single skill: store directly for backward compat
            if len(skill_contents) == 1:
                context["active_skill_content"] = skill_contents[0]
            else:
                # Multiple skills: join with separator
                context["active_skill_content"] = "\n---\n".join(skill_contents)

    # 4. Current target info
    if state.get("target"):
        context["target"] = state["target"]

    # 5. Plan info
    if state.get("plan_path"):
        context["plan_path"] = state["plan_path"]
    if state.get("plan"):
        context["plan"] = state["plan"]

    # 6. Injection method and blade metadata (needed by verifier across iterations)
    if state.get("injection_method"):
        context["injection_method"] = state["injection_method"]
    if state.get("blade_scope"):
        context["blade_scope"] = state["blade_scope"]
    if state.get("blade_target"):
        context["blade_target"] = state["blade_target"]
    if state.get("blade_action"):
        context["blade_action"] = state["blade_action"]

    return context


def build_post_compact_context_message(critical_context: dict) -> str:
    """Build a context-recovery message to prepend after compaction.

    Aligned with Claude Code's CompactionResult.summaryMessages + attachments.
    After compaction replaces the conversation with a summary, this message
    injects the critical operational state (blade UIDs, active skills,
    skill instruction content, targets, plans) so the next agent_loop
    iteration can continue without re-discovering this information.

    Enhanced with skill content and plan content preservation,
    aligned with Claude Code's createSkillAttachmentIfNeeded().

    Args:
        critical_context: Dict from extract_critical_context().

    Returns:
        Formatted context-recovery message string, or empty string
        if critical_context is empty.
    """
    if not critical_context:
        return ""

    parts = ["[Context preserved after compaction]"]

    if "active_blade_uid" in critical_context:
        parts.append(
            f"Active experiment blade_uid: {critical_context['active_blade_uid']}"
        )
    if "active_skill" in critical_context:
        parts.append(f"Active skill: {critical_context['active_skill']}")
    if "active_skill_content" in critical_context:
        parts.append(
            f"Skill instructions (preserved):\n{critical_context['active_skill_content']}"
        )
    if "target" in critical_context:
        target = critical_context["target"]
        if isinstance(target, dict):
            parts.append(
                f"Target: namespace={target.get('namespace', '?')} "
                f"type={target.get('resource_type', '?')} "
                f"names={target.get('names', [])}"
            )
        else:
            parts.append(f"Target: {target}")
    if "plan_path" in critical_context:
        parts.append(f"Plan file: {critical_context['plan_path']}")
    if "plan" in critical_context:
        plan = critical_context["plan"]
        plan_preview = plan[:500] + "..." if len(plan) > 500 else plan
        parts.append(f"Plan content:\n{plan_preview}")

    # Injection metadata
    metadata_parts = []
    if "injection_method" in critical_context:
        metadata_parts.append(f"method={critical_context['injection_method']}")
    if "blade_scope" in critical_context:
        metadata_parts.append(f"scope={critical_context['blade_scope']}")
    if "blade_target" in critical_context:
        metadata_parts.append(f"target={critical_context['blade_target']}")
    if "blade_action" in critical_context:
        metadata_parts.append(f"action={critical_context['blade_action']}")
    if metadata_parts:
        parts.append(f"Injection: {' | '.join(metadata_parts)}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Core compaction functions
# ---------------------------------------------------------------------------

# Maximum length for the compaction prompt + messages to avoid recursive overflow
MAX_COMPACTION_INPUT_CHARS = 100_000

# Large tool output stripping config (simplified microCompact)
_STRIP_TOOL_HEAD_CHARS = 500
_STRIP_TOOL_TAIL_CHARS = 500
_STRIP_TOOL_THRESHOLD_CHARS = 2000
_STRIP_TOOL_MARKER = "\n... [tool output truncated] ...\n"


def _strip_large_tool_outputs(messages: list) -> list:
    """Progressively compress large tool outputs before full compaction.

    Aligned with Claude Code's microCompact.ts: before sending messages to
    the LLM for compaction, truncate oversized tool result content to reduce
    token usage. This is a simpler alternative to the full microCompact that
    works at the message level rather than per-tool granularity.

    Args:
        messages: Conversation messages to strip.

    Returns:
        New message list with oversized tool outputs truncated.
    """
    result = []
    modified = False

    for msg in messages:
        content = getattr(msg, "content", "")
        if not isinstance(content, str):
            result.append(msg)
            continue

        # Only strip tool result messages
        is_tool = hasattr(msg, "type") and msg.type == "tool"
        if not is_tool or len(content) <= _STRIP_TOOL_THRESHOLD_CHARS:
            result.append(msg)
            continue

        # Truncate: keep head + marker + tail
        head = content[:_STRIP_TOOL_HEAD_CHARS]
        tail = content[-_STRIP_TOOL_TAIL_CHARS:]
        truncated = head + _STRIP_TOOL_MARKER + tail

        if hasattr(msg, "model_copy") and hasattr(msg, "__fields__"):
            # LangChain BaseModel subclass — use model_copy for immutable update
            new_msg = msg.model_copy(update={"content": truncated})
        else:
            # Mutable mock or plain object — set directly
            msg.content = truncated
            new_msg = msg
        result.append(new_msg)
        modified = True

    if modified:
        logger.debug("Stripped large tool outputs before compaction")

    return result


def _get_compact_prompt(mode: CompactionMode = CompactionMode.BASE) -> str:
    """Build the full compaction prompt for the given mode.

    Assembles NO_TOOLS_PREAMBLE + mode-specific prompt + NO_TOOLS_TRAILER.
    """
    analysis_instruction = COMPACTION_ANALYSIS_PROMPT

    if mode == CompactionMode.BASE:
        template = BASE_COMPACT_PROMPT
    elif mode == CompactionMode.PARTIAL:
        template = PARTIAL_COMPACT_PROMPT
    elif mode == CompactionMode.UP_TO:
        template = UP_TO_COMPACT_PROMPT
    else:
        template = BASE_COMPACT_PROMPT

    prompt = NO_TOOLS_PREAMBLE + template.format(analysis_instruction=analysis_instruction)
    prompt += NO_TOOLS_TRAILER
    return prompt


async def compact_memory(
    messages_to_compact: list,
    previous_summary: str = "",
    llm=None,
    mode: CompactionMode = CompactionMode.BASE,
    state: Optional[dict] = None,
) -> str:
    """Use LLM to compress old messages into a structured summary.

    Supports three compaction modes aligned with Claude Code:
    - BASE: Full conversation summary from scratch.
    - PARTIAL: Incremental update (only summarize recent messages).
    - UP_TO: Summarize up to a point (later messages are preserved).

    When state is provided, extracts critical context (blade_uid, skill,
    target, plan) before compaction and prepends a recovery message
    after compaction.

    Args:
        messages_to_compact: Old messages to compress.
        previous_summary: Previous compressed summary to build upon.
        llm: LangChain LLM instance.
        mode: Compaction mode (BASE/PARTIAL/UP_TO).
        state: Optional AgentState dict for context recovery.

    Returns:
        Structured summary text, optionally prefixed with context recovery.
    """
    # Extract critical context before compaction (if state provided)
    critical_context = {}
    if state is not None:
        critical_context = extract_critical_context(messages_to_compact, state)

    # Progressive compression: strip large tool outputs before compaction
    # (aligned with Claude Code's microCompact — reduce token usage before LLM call)
    messages_to_compact = _strip_large_tool_outputs(messages_to_compact)

    if llm is None:
        # Fallback: simple concatenation summary
        summary = _simple_compact(messages_to_compact, previous_summary)
    else:
        prompt = _get_compact_prompt(mode)
        if previous_summary:
            prompt += f"\n\nPrevious summary to build upon:\n{previous_summary}"

        # Prepare messages, truncating if too long
        compact_msgs = _prepare_compaction_messages(messages_to_compact)

        try:
            response = await llm.ainvoke(
                [SystemMessage(content=prompt)] + compact_msgs
            )
            # Log reasoning_content in debug mode (enable_thinking)
            additional_kwargs = getattr(response, "additional_kwargs", {}) or {}
            reasoning_content = additional_kwargs.get("reasoning_content", "")
            if reasoning_content and settings.is_debug:
                text = reasoning_content[:300] + ("..." if len(reasoning_content) > 300 else "")
                logger.debug(f"💭 compaction thinking: {text}")
            summary = response.content
            # Format the two-step summary (strip <analysis>, format <summary>)
            summary = format_compact_summary(summary)
        except Exception as e:
            logger.warning(f"LLM compaction failed, falling back to simple compact: {e}")
            summary = _simple_compact(messages_to_compact, previous_summary)

    # Prepend context-recovery message if critical context was extracted
    context_msg = build_post_compact_context_message(critical_context)
    if context_msg:
        summary = context_msg + "\n\n" + summary

    return summary


def _prepare_compaction_messages(messages: list) -> list:
    """Truncate messages list to fit within compaction input budget."""
    total_chars = 0
    result = []
    for msg in messages:
        content = getattr(msg, "content", "")
        if isinstance(content, str):
            total_chars += len(content)
        if total_chars > MAX_COMPACTION_INPUT_CHARS:
            break
        result.append(msg)
    return result


def _simple_compact(messages: list, previous_summary: str = "") -> str:
    """Simple fallback compaction without LLM."""
    lines = ["[Compressed History]"]
    if previous_summary:
        lines.append(f"Previous context: {previous_summary[:500]}")

    # Extract key information from messages
    for msg in messages[-10:]:  # Last 10 messages
        content = getattr(msg, "content", "")
        if isinstance(content, str) and content:
            lines.append(f"- {content[:200]}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Layered Compaction (Migration Point 13)
# Aligned with Claude Code's sessionMemoryCompact.ts + autoCompact.ts
# ---------------------------------------------------------------------------

# Lightweight compaction config — aligned with Claude Code's DEFAULT_SM_COMPACT_CONFIG
LIGHTWEIGHT_COMPACT_MIN_TOKENS = 10_000
LIGHTWEIGHT_COMPACT_MIN_MESSAGES = 5
LIGHTWEIGHT_COMPACT_MAX_TOKENS = 40_000

# Marker added when lightweight compaction drops messages
LIGHTWEIGHT_DROPPED_MARKER = (
    "[Earlier conversation messages were trimmed to save context space. "
    "Key information has been preserved above.]"
)


def try_lightweight_compact(
    messages: list,
    max_tokens: int,
    min_keep_messages: int = LIGHTWEIGHT_COMPACT_MIN_MESSAGES,
) -> Optional[tuple[list, list]]:
    """Try lightweight message trimming without calling LLM.

    Aligned with Claude Code's trySessionMemoryCompaction() from
    sessionMemoryCompact.ts. This is Layer 1 of the layered compaction
    strategy — it simply drops old messages and keeps recent ones,
    which is much cheaper than a full LLM summary.

    Use this when the context is only slightly over budget and a
    full LLM summary would be overkill (e.g., only one extra round
    of conversation pushed the context over the limit).

    Args:
        messages: All conversation messages.
        max_tokens: Maximum allowed token budget.
        min_keep_messages: Minimum number of recent messages to keep.

    Returns:
        (messages_to_drop, messages_to_keep) tuple if lightweight compaction
        is suitable, or None if the context is too large for trimming alone
        (a full LLM summary is needed instead).
    """
    from chaos_agent.memory.context_manager import count_tokens_approx

    total_tokens = count_tokens_approx(messages)

    # If even after trimming to the max budget we'd still be over max_tokens,
    # lightweight compaction isn't sufficient — need full LLM summary
    if total_tokens - LIGHTWEIGHT_COMPACT_MAX_TOKENS > max_tokens:
        logger.debug(
            f"Lightweight compact not suitable: "
            f"total={total_tokens}, even trimming {LIGHTWEIGHT_COMPACT_MAX_TOKENS} "
            f"tokens would leave {total_tokens - LIGHTWEIGHT_COMPACT_MAX_TOKENS} "
            f"which exceeds max={max_tokens}"
        )
        return None

    # If total is already within budget, no compaction needed
    if total_tokens <= max_tokens:
        return None

    # Walk backwards from the most recent messages, accumulating tokens
    # until we stay within LIGHTWEIGHT_COMPACT_MAX_TOKENS
    kept_tokens = 0
    keep_from = len(messages)
    for i in range(len(messages) - 1, -1, -1):
        msg_tokens = count_tokens_approx([messages[i]])
        if kept_tokens + msg_tokens > LIGHTWEIGHT_COMPACT_MAX_TOKENS:
            break
        kept_tokens += msg_tokens
        keep_from = i

    # Ensure we keep at least min_keep_messages
    min_keep_from = len(messages) - min_keep_messages
    if min_keep_from < 0:
        min_keep_from = 0
    keep_from = min(keep_from, min_keep_from)

    # If we'd keep everything, no trimming needed
    if keep_from == 0:
        return None

    messages_to_drop = messages[:keep_from]
    messages_to_keep = messages[keep_from:]

    # Ensure we don't split tool_call/tool_result pairs
    # If the first message in to_keep is a tool result, move it back
    if messages_to_keep:
        first_keep = messages_to_keep[0]
        # Tool result without preceding tool_call → move to drop
        if hasattr(first_keep, "type") and first_keep.type == "tool":
            messages_to_drop.append(messages_to_keep.pop(0))

    logger.info(
        f"Lightweight compact: dropping {len(messages_to_drop)} messages "
        f"({count_tokens_approx(messages_to_drop)} tokens), "
        f"keeping {len(messages_to_keep)} messages "
        f"({count_tokens_approx(messages_to_keep)} tokens)"
    )

    return messages_to_drop, messages_to_keep


async def compact_if_needed(
    messages: list,
    max_tokens: int,
    previous_summary: str = "",
    state: Optional[dict] = None,
    llm=None,
    mode: CompactionMode = CompactionMode.BASE,
) -> tuple[list, bool]:
    """Layered compaction entry point: try lightweight trim first, then LLM summary.

    Aligned with Claude Code's autoCompactIfNeeded() which first calls
    trySessionMemoryCompaction() (lightweight trim) and only falls back
    to compactConversation() (full LLM summary) if trimming is insufficient.

    This two-layer approach saves LLM tokens and latency for simple cases
    where the context is only slightly over budget.

    Args:
        messages: All conversation messages.
        max_tokens: Maximum allowed token budget.
        previous_summary: Previous compressed summary to build upon.
        state: Optional AgentState dict for context recovery.
        llm: LangChain LLM instance.
        mode: Compaction mode for LLM summary (BASE/PARTIAL/UP_TO).

    Returns:
        (compacted_messages, used_lightweight) tuple:
        - compacted_messages: The resulting message list after compaction.
        - used_lightweight: True if lightweight trim was used, False if LLM summary.
    """
    from langchain_core.messages import HumanMessage
    from chaos_agent.memory.context_manager import count_tokens_approx

    total_tokens = count_tokens_approx(messages)
    if total_tokens <= max_tokens:
        # No compaction needed
        return messages, False

    # Layer 1: Try lightweight trim (no LLM needed)
    lightweight_result = try_lightweight_compact(messages, max_tokens)
    if lightweight_result is not None:
        dropped, kept = lightweight_result

        # Extract critical context from dropped messages
        critical_context = {}
        if state is not None:
            critical_context = extract_critical_context(dropped, state)

        # Build context-recovery message from dropped messages
        context_msg = build_post_compact_context_message(critical_context)

        # Assemble result: context recovery + lightweight marker + kept messages
        result = []
        if context_msg:
            result.append(HumanMessage(content=context_msg))
        result.append(HumanMessage(content=LIGHTWEIGHT_DROPPED_MARKER))
        result.extend(kept)

        logger.info(
            f"Lightweight compaction applied: "
            f"{count_tokens_approx(result)} tokens in result "
            f"(was {total_tokens})"
        )
        return result, True

    # Layer 2: Full LLM summary (fallback)
    logger.info(
        f"Lightweight compaction not suitable, "
        f"falling back to LLM summary ({total_tokens} tokens)"
    )

    # Split messages: compact the old ones, keep the recent ones
    from chaos_agent.memory.context_manager import ContextManager
    cm = ContextManager(max_tokens=max_tokens)
    to_compact, to_keep, is_valid = cm.check_context(messages)

    if not to_compact:
        # ContextManager didn't find anything to compact
        return messages, False

    summary = await compact_memory(
        to_compact,
        previous_summary=previous_summary,
        llm=llm,
        mode=mode,
        state=state,
    )

    # Build result: summary message + kept messages
    result = [HumanMessage(content=summary)]
    result.extend(to_keep)

    return result, False
