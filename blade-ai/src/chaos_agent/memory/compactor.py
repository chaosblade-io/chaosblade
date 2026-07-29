"""LLM-based structured compaction for Session Memory.

Generates structured summaries from conversation history using the
Claude Code two-step compaction pattern: <analysis> drafting + <summary> output.

Supports three compaction modes (BASE / PARTIAL / UP_TO) and provides
post-compaction context recovery aligned with Claude Code's
createPlanAttachmentIfNeeded / createSkillAttachmentIfNeeded.

Single compaction entry point: ``PreReasoningHook`` (memory/hook.py).
The hook handles both the auto-trigger path (called before every LLM
reasoning step) and the manual ``/compact`` path (called with
``force=True``). This module exposes the LLM summary primitive
``compact_memory()`` plus a few helpers it composes with
(``extract_critical_context``, ``build_post_compact_context_message``,
``format_compact_summary``).
"""

import logging
import re
from enum import Enum
from typing import Optional

from langchain_core.messages import SystemMessage

from chaos_agent.agent.spec.skill_identity import read_active_skill_name
from chaos_agent.config.settings import settings
from chaos_agent.utils.reasoning_replay import replayed_reasoning

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

# Token budgets for skill content preservation.
#
# Ceilings, not the operative values: both are now capped against the configured
# model's compaction threshold by ``resolve_skill_budgets()``. The absolute
# numbers come from Claude Code's POST_COMPACT_MAX_TOKENS_PER_SKILL /
# SKILLS_TOKEN_BUDGET, which are sized for its 200K window — 25,000 is 15% of
# that threshold but 95% of a 32K model's, where restoring the skill text alone
# would refill the context that compaction just emptied.
POST_COMPACT_MAX_TOKENS_PER_SKILL = 5000
POST_COMPACT_SKILLS_TOKEN_BUDGET = 25000

# Largest share of the compaction threshold that recovered skill text may claim.
# The recovered context is PREPENDED to the summary, so it competes directly with
# the room compaction was run to create; a fifth leaves the summary and the
# retained tail the rest.
SKILLS_BUDGET_SHARE_OF_THRESHOLD = 0.2


def resolve_skill_budgets() -> tuple[int, int]:
    """``(per_skill, total)`` skill-content budgets for the configured model.

    Scaling by the threshold rather than trusting the absolute ceilings is what
    keeps a small-window model usable: at 32,768 tokens the threshold is 26,214,
    so the unscaled 25,000 total would hand back 95% of it immediately after
    compaction. The per-skill cap is kept proportional to the total (1:5, as in
    the original constants) so a single skill cannot crowd out the others.
    """
    from chaos_agent.config.settings import settings as _settings
    from chaos_agent.memory.context_manager import resolve_auto_compact_threshold

    max_tokens, ratio = _settings.resolve_context_budget()
    threshold = resolve_auto_compact_threshold(max_tokens, ratio)
    total = min(POST_COMPACT_SKILLS_TOKEN_BUDGET,
                int(threshold * SKILLS_BUDGET_SHARE_OF_THRESHOLD))
    per_skill = min(POST_COMPACT_MAX_TOKENS_PER_SKILL, max(1, total // 5))
    return per_skill, total

SKILL_TRUNCATION_MARKER = (
    "\n\n[... skill content truncated; "
    "re-activate the skill if you need full instructions]"
)


def truncate_to_tokens(content: str, max_tokens: int) -> str:
    """Truncate content to roughly max_tokens tokens, keeping the head.

    Aligned with Claude Code's token-aware truncation. Uses the model-
    aware ``chaos_agent.memory.tokens.count_tokens`` (tiktoken when the
    configured model is recognised, CJK heuristic when not); the char
    budget is calibrated against the content's actual chars/token
    ratio so CJK-heavy text isn't over- or under-truncated.

    Args:
        content: Text content to potentially truncate.
        max_tokens: Maximum tokens to allow.

    Returns:
        Content truncated to the token budget with a truncation marker
        if it exceeded the budget.
    """
    from chaos_agent.memory.tokens import count_tokens

    # Single-text count — use raw .count, not .safe_count: budgeting char
    # truncation off an inflated value would over-trim. The downstream
    # consumer cares about LLM-side accuracy, not threshold-direction
    # safety, so the tighter number is correct here.
    actual_tokens = count_tokens(content).count
    if actual_tokens <= max_tokens:
        return content
    # Calibrate chars/token from this content (mixed CJK/ASCII safe).
    chars_per_token = len(content) / actual_tokens if actual_tokens else 4
    marker_tokens = count_tokens(SKILL_TRUNCATION_MARKER).count
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
    - Each skill's content and the total are capped by
      ``resolve_skill_budgets()``, which scales the POST_COMPACT_* ceilings down
      to a share of this model's compaction threshold

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
    #    Aligned with Claude Code's createSkillAttachmentIfNeeded(), but the
    #    budgets are resolved against THIS model's compaction threshold rather
    #    than used as absolutes: the recovered text is prepended to the summary,
    #    so on a 32K window the raw 25,000 ceiling would hand back 95% of the
    #    room compaction had just freed.
    per_skill_budget, skills_budget = resolve_skill_budgets()
    total_skill_tokens = 0
    skill_names = []

    # Collect all active skill names (current + any from state history)
    active_skill_name = read_active_skill_name(state)
    if active_skill_name:
        skill_names.append(active_skill_name)
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
            truncated = truncate_to_tokens(skill_content, per_skill_budget)

            # Then, check total budget. Single-text count, raw value —
            # we're summing toward a hard budget cap, not making a
            # threshold-trigger decision, so the more accurate count
            # is the right one.
            from chaos_agent.memory.tokens import count_tokens
            truncated_tokens = count_tokens(truncated).count
            if total_skill_tokens + truncated_tokens > skills_budget:
                # Truncate further to remaining budget
                remaining = skills_budget - total_skill_tokens
                if remaining > 0:
                    truncated = truncate_to_tokens(skill_content, remaining)
                    skill_contents.append(truncated)
                    total_skill_tokens = skills_budget
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

    # 4-6. Fault context — read from FaultSpec, project to the keys
    # post-compact context consumers expect (target dict / blade_scope /
    # blade_target / blade_action). This is read-only projection;
    # state.fault_spec remains the single source of truth.
    from chaos_agent.agent.spec.fault_spec import read_fault_spec
    spec = read_fault_spec(state)
    if spec:
        context["target"] = {
            "namespace": spec.namespace,
            "names": list(spec.names),
            "labels": dict(spec.labels),
            "resource_type": spec.scope,
        }
        if spec.scope:
            context["blade_scope"] = spec.scope
        if spec.blade_target:
            context["blade_target"] = spec.blade_target
        if spec.blade_action:
            context["blade_action"] = spec.blade_action

    # 5. Plan info
    if state.get("plan_path"):
        context["plan_path"] = state["plan_path"]
    if state.get("plan"):
        context["plan"] = state["plan"]

    if state.get("injection_method"):
        context["injection_method"] = state["injection_method"]

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

# Ceiling on the compaction request's input, in CHARACTERS.
#
# A ceiling only — ``resolve_compaction_input_chars()`` also caps it against the
# model's window, because 100,000 characters of CJK is roughly 68,000 tokens
# (0.68 tok/char measured on this project's traffic), which alone overflows a
# 32K or 64K window. Compaction runs precisely when the context is fullest, so a
# budget that lets its own request overflow fails at the only moment it matters,
# and the failure path is ``_simple_compact``.
MAX_COMPACTION_INPUT_CHARS = 100_000

# Tokens reserved for the summary the compaction call has to write back.
# Claude Code reserves 20,000 on the same reasoning (p99.99 of its summary output
# is 17,387 tokens); the input budget must leave room for it or the request fits
# while the response cannot.
COMPACTION_OUTPUT_RESERVE_TOKENS = 8_000

# Characters-per-token used to convert a token budget into the character budget
# this module works in.
#
# Must be the WORST case, not the average. CJK measures ~0.68 tok/char on this
# project's traffic, i.e. only ~1.47 chars/token, while ASCII prose reaches ~4.
# Picking anything above the CJK figure over-fills: at 1.5 the budget for a 32K
# model came out at 37,152 chars = 25,263 CJK tokens, which together with the
# output reserve exceeded the window by 495 tokens — the exact overflow this
# budget exists to prevent, on the exact traffic this project sends.
_CHARS_PER_TOKEN = 1.4


def resolve_compaction_input_chars() -> int:
    """Character budget for the compaction request on the configured model.

    The compaction call goes through a BARE llm — no tools are bound to it — so
    it carries only the compaction prompt plus the messages. That is why this
    budget is derived from the raw window rather than from the provider-anchored
    overhead the main agent sees: none of that overhead is present here.
    """
    from chaos_agent.config.settings import settings as _settings

    max_tokens, _ = _settings.resolve_context_budget()
    usable = max(1_000, max_tokens - COMPACTION_OUTPUT_RESERVE_TOKENS)
    return min(MAX_COMPACTION_INPUT_CHARS, int(usable * _CHARS_PER_TOKEN))

# Rules attached whenever a previous summary is carried forward.
#
# The bare "Previous summary to build upon:" header this replaces was adequate
# while every ``[Compressed History]`` message was retained verbatim — losing
# detail cost nothing because the original summary stayed in context. That is no
# longer true: older summaries are now folded into each compaction
# (``SUMMARIES_KEPT_VERBATIM``), so whatever the model omits here is gone for
# good. The instructions therefore have to be explicit about carrying facts
# forward rather than trusting "build upon" to imply it.
#
# ``blade_uid`` is called out by name because it is the one value the recovery
# path cannot reconstruct: without it an injected experiment can no longer be
# destroyed, turning a summarisation slip into a fault left running on a
# cluster.
INCREMENTAL_SUMMARY_RULES = """

## Building on the previous summary

The summary below covers everything that happened BEFORE the messages you were
given. Your output REPLACES it, so it is the only record that survives — treat
every fact in it as if it had appeared in the conversation itself.

Rules:
- PRESERVE every fact from the previous summary that is still true. Do not drop
  a detail merely because the recent messages did not mention it again.
- PRESERVE identifiers and literals EXACTLY: blade_uid, namespace, pod and node
  names, labels, file paths, command lines, error text. An altered or missing
  blade_uid makes the experiment unrecoverable.
- UPDATE Progress by moving finished items from pending to done, and refresh
  Next Steps to reflect what is now outstanding.
- MERGE rather than append: one coherent state of the drill, not the old summary
  with new notes stapled on.
- REMOVE only what is genuinely obsolete (e.g. an error that has been fixed, a
  step that has been superseded). When unsure, keep it.

Previous summary:
"""


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

    if llm is None:
        # Fallback: simple concatenation summary
        summary = _simple_compact(messages_to_compact, previous_summary)
    else:
        prompt = _get_compact_prompt(mode)
        if previous_summary:
            prompt += INCREMENTAL_SUMMARY_RULES + previous_summary

        # Prepare messages, truncating if too long
        compact_msgs = _prepare_compaction_messages(messages_to_compact)

        response = await llm.ainvoke(
            [SystemMessage(content=prompt)] + compact_msgs
        )
        additional_kwargs = getattr(response, "additional_kwargs", {}) or {}
        reasoning_content = additional_kwargs.get("reasoning_content", "")
        if reasoning_content and settings.is_debug:
            text = reasoning_content[:300] + ("..." if len(reasoning_content) > 300 else "")
            logger.debug(f"💭 compaction thinking: {text}")
        summary = response.content
        summary = format_compact_summary(summary)

    # Prepend context-recovery message if critical context was extracted
    context_msg = build_post_compact_context_message(critical_context)
    if context_msg:
        summary = context_msg + "\n\n" + summary

    return summary


def _compaction_input_chars(msg) -> int:
    """Characters this message contributes to the compaction LLM's input.

    Counts BOTH ``content`` and any replayed thinking trace, because
    ``compact_memory`` hands the ORIGINAL message objects to ``llm.ainvoke``
    and the outbound patch serialises ``reasoning_content`` alongside
    ``content``. Sizing the budget on ``content`` alone is the same blind spot
    the token counter had: a 40-turn thinking history measures 80 chars while
    the request it produces is ~37k tokens, so the budget check passes and the
    compaction call itself can exceed the context window — precisely when the
    context is fullest and compaction matters most. Worse, that failure falls
    back to ``_simple_compact``.
    """
    content = getattr(msg, "content", "")
    total = len(content) if isinstance(content, str) else 0
    return total + len(replayed_reasoning(msg))


def _prepare_compaction_messages(messages: list) -> list:
    """Truncate messages list to fit within compaction input budget."""
    budget = resolve_compaction_input_chars()
    total_chars = sum(_compaction_input_chars(msg) for msg in messages)
    if total_chars <= budget:
        return messages

    result = []
    kept_chars = 0
    for msg in reversed(messages):
        msg_chars = _compaction_input_chars(msg)
        if kept_chars + msg_chars > budget:
            break
        result.append(msg)
        kept_chars += msg_chars

    result.reverse()

    dropped = len(messages) - len(result)
    logger.warning(
        f"Compaction input truncated: dropped {dropped} oldest of "
        f"{len(messages)} messages ({total_chars - kept_chars} chars over "
        f"{budget} budget)"
    )
    return result


# Character budget for the LLM-free fallback summary.
#
# The fallback's output IS the summary that lands in state while the originals are
# deleted by ``RemoveMessage`` — anything it omits is gone from the live context.
# It used to take ``messages[-10:]`` and ``previous_summary[:500]``: measured on
# an 81-message input that summarised the last 10 and dropped 71, and cut a
# 1,625-character carried-forward summary down by 69%. Both are fixed counts,
# blind to how much room is actually available, and they contradict the rules the
# LLM path is held to (``INCREMENTAL_SUMMARY_RULES``: preserve every fact, keep
# blade_uid exactly, "when unsure, keep it").
#
# This path cannot be lossless — without an LLM there is no real summarisation,
# only selection. The budget makes the loss bounded and explainable instead of
# arbitrary, and it spends the room on the two things that cannot be recovered:
# the carried-forward summary first, then the most recent turns.
FALLBACK_SUMMARY_BUDGET_CHARS = 8_000

# Share of that budget reserved for the carried-forward summary before recent
# messages get any. It is the ONLY record of everything compacted in earlier
# rounds, so starving it loses the whole early history, permanently and
# cumulatively — each degraded round would re-truncate what the last one left.
FALLBACK_PREVIOUS_SUMMARY_SHARE = 0.5


def _simple_compact(messages: list, previous_summary: str = "") -> str:
    """Simple fallback compaction without LLM.

    Falls back to the thinking trace when ``content`` is empty. For a thinking
    model that is the NORMAL shape — the rationale goes to ``reasoning_content``
    and ``content`` comes back blank — so reading ``content`` alone produced a
    summary consisting of the ``[Compressed History]`` header and nothing else.
    Since this path also serves as the recovery route when the LLM compaction
    call fails, an empty summary there means the whole history is discarded with
    nothing put back: the model loses every trace of what it already did, which
    is the non-convergence the replay exists to prevent.

    Selection is budgeted rather than counted: the carried-forward summary gets
    first claim (see ``FALLBACK_PREVIOUS_SUMMARY_SHARE``), then messages are
    taken newest-first until the remaining room runs out.
    """
    lines = ["[Compressed History]"]
    remaining = FALLBACK_SUMMARY_BUDGET_CHARS

    if previous_summary:
        # First claim on the budget, and keep its TAIL: a cumulative summary ends
        # with the most recent state ("Next Steps"), and that is what the next
        # turn needs.
        allowance = int(FALLBACK_SUMMARY_BUDGET_CHARS * FALLBACK_PREVIOUS_SUMMARY_SHARE)
        carried = (
            previous_summary
            if len(previous_summary) <= allowance
            else previous_summary[-allowance:]
        )
        lines.append(f"Previous context: {carried}")
        remaining -= len(carried)

    # Newest-first so the turns closest to now survive, then reversed back into
    # chronological order. No fixed count: a run of small messages keeps far more
    # than ten, and a few huge ones keep fewer, which is the correct trade in both
    # directions.
    picked: list[str] = []
    for msg in reversed(messages):
        if remaining <= 0:
            break
        content = getattr(msg, "content", "")
        if isinstance(content, str) and content:
            entry = f"- {content[:200]}"
        else:
            # No content — keep the thinking TAIL, where a trace states its
            # conclusion ("...so the next step is X"), matching how the replay
            # truncates. The head is scene-setting and least useful in a summary.
            reasoning = replayed_reasoning(msg)
            if not reasoning:
                continue
            entry = f"- [thinking] {reasoning[-200:]}"
        picked.append(entry)
        remaining -= len(entry)
    lines.extend(reversed(picked))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Note: ``compact_if_needed`` and ``try_lightweight_compact`` were removed
# when the manual ``/compact`` path was unified onto ``PreReasoningHook``
# (called with ``force=True``). The hook is now the single source of truth
# for compaction logic across both auto-trigger and user-initiated paths.
# Manual callers (TUI ``commands._compact_thread`` / server
# ``/api/v1/sessions/{sid}/compact``) reach the hook via
# ``agents["pre_reason_hook"]``.
# ---------------------------------------------------------------------------
