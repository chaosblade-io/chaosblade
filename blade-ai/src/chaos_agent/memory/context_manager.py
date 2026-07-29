"""Token-aware context manager for Working Memory (Layer 1).

Checks if the conversation context exceeds the token budget
and triggers compaction when needed.

Aligned with Claude Code's autoCompact.ts:
- Multi-level token warning (NORMAL → WARNING → ERROR → AUTO_COMPACT → BLOCKING)
- Dynamic threshold calculation with buffer tokens
- Circuit breaker (MAX_CONSECUTIVE_COMPACT_FAILURES) to prevent infinite retry

Token counting note (E1):
  Token math in this module — both per-string and per-message-list —
  delegates to ``chaos_agent.memory.tokens`` which selects an
  appropriate tokenizer (tiktoken native / family-prefix /
  HuggingFace AutoTokenizer / CJK heuristic) based on
  ``settings.model_name`` and tags the result with a quality grade +
  safety margin. Callers in this file pull ``safe_count`` from the
  returned ``TokenCount`` so the threshold checks automatically widen
  when the quality is HEURISTIC. There is no longer a single global
  fudge factor — each count carries its own appropriate margin.
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from chaos_agent.memory.tokens import (
    ContextUsage,
    TokenCount,
    TokenCountQuality,
    count_tokens,
    count_tokens_messages,
    estimate_context_tokens,
)

logger = logging.getLogger(__name__)


# Re-exports for callers that still want the bare integer view. New
# code should call ``count_tokens(...)`` / ``count_tokens_messages(...)``
# directly to get quality tags.
__all__ = [
    "ContextUsage",
    "count_tokens",
    "count_tokens_messages",
    "estimate_context_tokens",
    "TokenCount",
    "TokenCountQuality",
    "CompactLevel",
    "TokenWarningState",
    "calculate_token_warning_state",
]


# ---------------------------------------------------------------------------
# Auto-compact decision types (aligned with Claude Code autoCompact.ts)
# ---------------------------------------------------------------------------


class CompactLevel(str, Enum):
    """Compaction urgency level, aligned with Claude Code's calculateTokenWarningState()."""

    NORMAL = "normal"                # Context usage is fine
    WARNING = "warning"              # Approaching threshold, prepare to compact
    ERROR = "error"                  # Above threshold, should compact soon
    AUTO_COMPACT = "auto_compact"    # Trigger automatic compaction
    BLOCKING = "blocking"            # Context is full, must compact before continuing


@dataclass
class TokenWarningState:
    """Token usage warning state.

    Aligned with Claude Code's calculateTokenWarningState() return type.
    Provides multi-level warning instead of a simple bool.
    """

    percent_left: int
    level: CompactLevel
    is_above_warning: bool
    is_above_error: bool
    is_above_auto_compact: bool
    is_at_blocking: bool


# Buffer tokens aligned with Claude Code's AUTOCOMPACT_BUFFER_TOKENS etc.
AUTOCOMPACT_BUFFER_TOKENS = 13_000
WARNING_BUFFER_TOKENS = 30_000
ERROR_BUFFER_TOKENS = 20_000
BLOCKING_BUFFER_TOKENS = 3_000

# Circuit breaker: stop retrying after consecutive failures
MAX_CONSECUTIVE_COMPACT_FAILURES = 3

# A compaction that leaves this share of the tokens behind did not accomplish
# anything. It counts against the circuit breaker exactly like an exception
# would: the breaker exists to stop futile retries, and "succeeded but freed
# nothing" is futile in the same way — worse, actually, because each attempt
# also spends an LLM call and appends another summary.
#
# The failure mode this catches was measured, not hypothesised: with every
# ``[Compressed History]`` retained, a 30-summary history sat 40K past the
# window while ``to_compact`` held 42 tokens, so each pass freed 42 and appended
# ~4,600. The bookkeeping made it invisible — the success branch reset
# ``consecutive_failures`` to 0 every time, so the breaker never engaged and the
# session ran until the provider rejected the request.
INEFFECTIVE_COMPACTION_RATIO = 0.95


@dataclass
class CompactTrackingState:
    """Auto-compact tracking state with circuit breaker.

    Aligned with Claude Code's autoCompact tracking.
    Tracks whether compaction has occurred this turn,
    how many turns since last compact, and consecutive failures.
    """

    compacted: bool = False
    turn_count: int = 0
    consecutive_failures: int = 0


# ---------------------------------------------------------------------------
# Token warning calculation
# ---------------------------------------------------------------------------


def resolve_auto_compact_threshold(max_tokens: int, compact_ratio: float) -> int:
    """The token count at which auto-compaction fires.

    Single source of truth for BOTH consumers — ``calculate_token_warning_state``
    (which decides the warning level) and ``ContextManager.compact_threshold``
    (which the hook compares against after stripping, and reports as
    ``trigger_tokens`` in the UI). They used to compute it separately: the
    former as ``min(max_tokens - buffer, max_tokens * ratio)``, the latter as a
    bare ``max_tokens * ratio``. Identical at the default ratio, so the split
    stayed invisible — but at ``ratio >= 0.93`` on a 131,072 window the bare
    form returned 124,518 against the guarded 118,072, letting the post-strip
    check accept a context the trigger had already rejected. The constructor
    comment already declared "Both must agree on intent"; this makes it
    structural instead of a convention.

    The buffer ceiling keeps context from getting within
    ``AUTOCOMPACT_BUFFER_TOKENS`` of the hard maximum, so the next user message
    cannot push past the provider limit before compaction gets a chance to run.
    The ratio lets an operator trigger EARLIER, never later. The 50% floor
    protects against a typo'd tiny ratio (below it the system would try to
    compact every message) and also keeps the result positive on the small
    windows used in tests, where the absolute buffer alone would go negative.
    """
    threshold = min(
        max_tokens - AUTOCOMPACT_BUFFER_TOKENS,
        int(max_tokens * compact_ratio),
    )
    return max(threshold, int(max_tokens * 0.5))


def calculate_token_warning_state(
    token_usage: int,
    max_tokens: int,
    auto_compact_enabled: bool = True,
    compact_ratio: float = 0.85,
) -> TokenWarningState:
    """Calculate token warning level.

    Aligned with Claude Code's calculateTokenWarningState().
    Returns multi-level warning state instead of a simple bool,
    enabling progressive escalation from warning → error → auto-compact → blocking.

    Args:
        token_usage: Current token usage.
        max_tokens: Maximum context window size.
        auto_compact_enabled: Whether auto-compact is allowed.
        compact_ratio: User-tunable trigger ratio (default 0.85). See the
            threshold note below for how this interacts with the buffer
            floor.

    Returns:
        TokenWarningState with level and boolean flags.
    """
    effective_window = max_tokens
    if auto_compact_enabled:
        # Threshold = the EARLIER of two triggers (buffer ceiling vs the
        # operator's ratio), with a 50% floor. See
        # ``resolve_auto_compact_threshold`` for the full rationale, including
        # the PREVIOUS BUG where this was ``max()`` — that made the buffer
        # always win and left ``compact_ratio`` effectively dead.
        auto_compact_threshold = resolve_auto_compact_threshold(
            max_tokens, compact_ratio
        )
    else:
        auto_compact_threshold = max_tokens

    percent_left = max(
        0,
        round(((auto_compact_threshold - token_usage) / auto_compact_threshold) * 100),
    )

    warning_threshold = auto_compact_threshold - WARNING_BUFFER_TOKENS
    error_threshold = auto_compact_threshold - ERROR_BUFFER_TOKENS
    blocking_limit = effective_window - BLOCKING_BUFFER_TOKENS

    # Ensure thresholds don't go below 0 for small windows
    warning_threshold = max(warning_threshold, 0)
    error_threshold = max(error_threshold, 0)
    blocking_limit = max(blocking_limit, int(effective_window * 0.95))

    return TokenWarningState(
        percent_left=percent_left,
        level=(
            CompactLevel.BLOCKING
            if token_usage >= blocking_limit
            else CompactLevel.AUTO_COMPACT
            if auto_compact_enabled and token_usage >= auto_compact_threshold
            else CompactLevel.ERROR
            if token_usage >= error_threshold
            else CompactLevel.WARNING
            if token_usage >= warning_threshold
            else CompactLevel.NORMAL
        ),
        is_above_warning=token_usage >= warning_threshold,
        is_above_error=token_usage >= error_threshold,
        is_above_auto_compact=auto_compact_enabled
        and token_usage >= auto_compact_threshold,
        is_at_blocking=token_usage >= blocking_limit,
    )


# ---------------------------------------------------------------------------
# Message pair integrity
# ---------------------------------------------------------------------------


def ensure_pair_integrity(
    to_compact: list,
    to_keep: list,
) -> tuple[list, list]:
    """Ensure tool_call and tool_result messages are not split across boundary.

    Moves incomplete pairs from to_compact to to_keep.

    .. deprecated:: Use group_messages_by_round() for more robust grouping.
    """
    if not to_compact:
        return to_compact, to_keep

    # Check if the last message in to_compact is a tool_call (AI message with tool_calls)
    # If so, we need to move it and its response to to_keep
    last = to_compact[-1]
    if hasattr(last, "tool_calls") and last.tool_calls:
        # AI message with tool calls - must keep the response too
        to_keep.insert(0, to_compact.pop())

    return to_compact, to_keep


def group_messages_by_round(messages: list) -> list[list]:
    """Group messages by API round, ensuring tool_call/tool_result pairs are not split.

    Aligned with Claude Code's groupMessagesByApiRound().
    Each group starts with an AI (assistant) message and includes all subsequent
    tool result messages until the next AI message. This ensures that when
    compaction splits messages, tool_use/tool_result pairs always stay together.

    Args:
        messages: Conversation messages to group.

    Returns:
        List of message groups (each group is a list of messages).
    """
    if not messages:
        return []

    groups: list[list] = []
    current: list = []

    for msg in messages:
        is_ai = hasattr(msg, "type") and msg.type == "ai"
        has_tool_calls = bool(getattr(msg, "tool_calls", None))

        # Start a new group when we encounter an AI message that:
        # 1. Has tool calls (beginning of a new API round), AND
        # 2. The current group is non-empty
        if is_ai and has_tool_calls and current:
            groups.append(current)
            current = [msg]
        elif is_ai and not has_tool_calls and current:
            # AI message without tool calls is a natural boundary
            # (e.g., final text response) — start a new group
            groups.append(current)
            current = [msg]
        else:
            current.append(msg)

    if current:
        groups.append(current)

    return groups


# ---------------------------------------------------------------------------
# ContextManager
# ---------------------------------------------------------------------------


COMPRESSED_HISTORY_PREFIX = "[Compressed History]"

# How many of the newest [Compressed History] summaries are kept verbatim
# instead of being folded into the next compaction.
#
# 1, not 0: the newest summary is the safety net for the one thing this design
# cannot verify statically — whether the model actually honoured "build upon the
# previous summary". If it silently drops earlier facts, the latest checkpoint
# still survives untouched. Keeping it costs one summary's worth of tokens.
#
# 1, not more: every extra retained summary restores the monotonic growth this
# limit exists to stop, only slower. Older summaries are already represented
# twice in the compaction request (cumulative ``previous_summary`` + the
# messages themselves), so retaining them verbatim buys no additional safety.
SUMMARIES_KEPT_VERBATIM = 1

# Largest share of ``reserve_tokens`` the retained summary may occupy.
#
# Retaining a summary is only worthwhile if recent context still fits beside it.
# The second reservation pass stops as soon as ``kept_tokens`` exceeds
# ``reserve_tokens``, so an oversized summary does not merely crowd recent
# messages out — it leaves ZERO of them, handing the model a stale checkpoint
# with no idea what just happened.
#
# This is reachable today, not a hypothetical: ``compact_memory`` prepends the
# recovered critical context to the summary text, and its skill-content budget
# alone (``POST_COMPACT_SKILLS_TOKEN_BUDGET`` = 25,000) already exceeds the
# 20,000-token reservation, with plan/target/summary prose on top and no cap of
# their own.
#
# When a summary breaches this share it is recycled like the older ones: it goes
# into ``to_compact``, so its content is re-summarised (smaller) rather than
# dropped. 0.5 keeps at least half the reservation for actual recent turns.
MAX_SUMMARY_SHARE_OF_RESERVE = 0.5


def _is_compressed_history(msg) -> bool:
    """Check if a message is a compressed history summary."""
    content = getattr(msg, "content", "")
    return isinstance(content, str) and content.startswith(COMPRESSED_HISTORY_PREFIX)


class ContextManager:
    """Manages the context window budget for Working Memory.

    Enhanced with multi-level token warning and circuit breaker,
    aligned with Claude Code's autoCompact decision flow.
    """

    def __init__(
        self,
        max_tokens: int = 128000,
        compact_ratio: float = 0.8,
    ):
        self.max_tokens = max_tokens
        # Keep the raw ratio AND the precomputed threshold:
        # - ratio is what we hand to calculate_token_warning_state so
        #   the user setting actually shapes the trigger.
        # - threshold is what callers compare against directly (the hook's
        #   post-strip "still over budget?" check, and the ``trigger_tokens``
        #   reported to the UI).
        # Both now come from ``resolve_auto_compact_threshold``, so they cannot
        # disagree — previously this line was a bare ``max_tokens * ratio``,
        # which drifted above the guarded trigger once the ratio passed ~0.93.
        self.compact_ratio = compact_ratio
        self.compact_threshold = resolve_auto_compact_threshold(
            max_tokens, compact_ratio
        )
        self.reserve_tokens = 20000

    def check_context(
        self,
        messages: list,
        tracking: Optional[CompactTrackingState] = None,
        force: bool = False,
    ) -> tuple[list, list, bool]:
        """Check if context needs compaction with multi-level decision.

        Aligned with Claude Code's shouldAutoCompact() + check_context().
        Uses calculate_token_warning_state() for multi-level assessment
        and respects circuit breaker from tracking state.

        Args:
            messages: Conversation messages.
            tracking: Optional CompactTrackingState for circuit breaker.
            force: When True (user-initiated /compact), bypass BOTH the
                auto-trigger threshold check AND the circuit breaker —
                the user explicitly asked for compaction now, even if
                we're below the threshold or have failed recently. Only
                the split (reserve_tokens + [Compressed History]
                preservation) runs. Default False (auto-trigger path).

        Returns:
            (messages_to_compact, messages_to_keep, is_valid)
            is_valid=False means context is blocked (circuit breaker tripped
            or at blocking level).
        """
        # "How full is the window?" is not "how big is this message list?".
        # A request also carries the system prompt (assembled skills, knowledge)
        # and every tool's JSON schema, none of which appear in ``messages``. On
        # real checkpoint data from this project the first call of a drill
        # reported ``input_tokens=6,936`` against a message list worth 11 tokens,
        # and the gap grows as skills load. Measured over nine consecutive calls,
        # anchoring on provider usage cut the mean absolute error from 77% to 8%,
        # and the old figure was low EVERY time (-67% to -95%) — the direction
        # that delays compaction.
        #
        # ``safe_tokens`` applies the tokenizer's margin to the estimated tail
        # only; the provider's own number needs no padding.
        usage = estimate_context_tokens(messages)
        total_tokens = usage.safe_tokens
        # Pass the instance's configured ratio so the operator's
        # ``BLADE_AI_CONTEXT_COMPACT_RATIO`` setting actually influences
        # the trigger. Before this fix the function silently used its
        # 0.85 default no matter what the user configured.
        warning_state = calculate_token_warning_state(
            total_tokens,
            self.max_tokens,
            compact_ratio=self.compact_ratio,
        )

        # Circuit breaker: stop retrying after too many consecutive failures.
        # SKIPPED on force=True — the breaker exists to protect the
        # auto-trigger loop from hammering a broken LLM. When a user
        # presses /compact, they want a retry; the breaker would just
        # frustrate them and they can always wait/retry themselves.
        if (
            not force
            and tracking
            and tracking.consecutive_failures >= MAX_CONSECUTIVE_COMPACT_FAILURES
        ):
            logger.warning(
                f"Auto-compact circuit breaker: "
                f"{tracking.consecutive_failures} consecutive failures, "
                f"not attempting compaction"
            )
            return [], messages, False

        # Below auto-compact threshold — no action needed.
        # SKIPPED on force=True so manual /compact always splits and
        # produces a summary even when usage is well below the trigger.
        if not force and not warning_state.is_above_auto_compact:
            if warning_state.is_above_warning:
                logger.info(
                    f"Context at {total_tokens} tokens "
                    f"({warning_state.level.value} level, "
                    f"{warning_state.percent_left}% remaining)"
                )
            return [], messages, True

        logger.info(
            f"Context at {total_tokens} tokens, "
            f"level={warning_state.level.value}, "
            f"triggering compaction "
            f"(threshold≈{self.max_tokens - AUTOCOMPACT_BUFFER_TOKENS})"
        )

        # Reserve recent messages
        messages_to_keep = []
        kept_tokens = 0

        # First pass: decide which [Compressed History] summaries survive.
        #
        # Keeping ALL of them (the previous behaviour) made context grow
        # monotonically and eventually INVERTED compaction. Measured on the
        # real 131,072-token window: at 30 summaries the history was 145K —
        # past both the threshold and the window — yet everything but 42
        # tokens sat in to_keep, so a "compaction" removed 42 tokens and
        # appended a fresh ~4.6K summary. Each pass grew the context.
        #
        # Recycling the older ones is lossless because their content reaches
        # the summarising LLM through TWO independent paths:
        #   1. ``state["compressed_summary"]`` → ``previous_summary`` in the
        #      prompt ("Previous summary to build upon"), which is itself
        #      cumulative — each summary was built on its predecessor;
        #   2. the summary messages themselves land in ``to_compact`` and are
        #      passed to ``llm.ainvoke`` as part of the conversation.
        # The newest summary is still KEPT verbatim as a safety net: if the
        # model ever fails to carry forward what it was told to preserve, the
        # most recent checkpoint survives untouched rather than being replaced
        # by a lossy re-summary.
        #
        # Summaries are SystemMessages with no tool_calls, so moving them
        # between the two lists cannot split a tool_call/tool_result pair.
        summary_indices = [
            i for i, msg in enumerate(messages) if _is_compressed_history(msg)
        ]
        # An oversized summary is worse than no summary: it consumes the whole
        # reservation and leaves no room for the recent turns the model needs to
        # know where it is. Recycle it instead — it lands in ``to_compact`` and
        # gets re-summarised, so the content survives in a smaller form.
        #
        # Walk NEWEST-first. The share is consumed in visit order, so iterating
        # oldest-first would let an older summary claim the budget and push the
        # newest one out — inverting the very priority
        # ``SUMMARIES_KEPT_VERBATIM`` exists to express. Unreachable while it is
        # 1, but its own comment weighs raising it, and a config change must not
        # silently reverse which checkpoint survives.
        summary_budget = int(self.reserve_tokens * MAX_SUMMARY_SHARE_OF_RESERVE)
        for i in reversed(summary_indices[-SUMMARIES_KEPT_VERBATIM:]):
            # Use raw ``count`` here (not ``safe_count``) — we're accumulating
            # toward ``reserve_tokens`` to decide how much room is left for
            # tail-keeping, not making a threshold decision; over-counting at
            # this step would under-keep useful recent context.
            summary_tokens = count_tokens_messages([messages[i]]).count
            if kept_tokens + summary_tokens > summary_budget:
                logger.info(
                    f"Recycling the newest [Compressed History] too: it needs "
                    f"{summary_tokens} tokens against a {summary_budget}-token "
                    f"share of the {self.reserve_tokens}-token reservation, "
                    f"which would leave no room for recent messages"
                )
                continue
            messages_to_keep.append(messages[i])
            kept_tokens += summary_tokens
        recycled_summaries = len(summary_indices) - sum(
            1 for m in messages_to_keep if _is_compressed_history(m)
        )
        if recycled_summaries:
            logger.info(
                f"Recycling {recycled_summaries} older [Compressed History] "
                f"summaries into this compaction "
                f"(keeping the newest {SUMMARIES_KEPT_VERBATIM})"
            )

        # Second pass: reserve recent messages (skipping summaries entirely —
        # the kept ones are already in, the recycled ones must fall through to
        # to_compact even though they sit late in the list).
        recent_keep = []
        for msg in reversed(messages):
            if _is_compressed_history(msg):
                continue
            msg_tokens = count_tokens_messages([msg]).count
            if kept_tokens + msg_tokens > self.reserve_tokens:
                break
            recent_keep.insert(0, msg)
            kept_tokens += msg_tokens

        # Merge: summaries first, then recent messages
        # Rebuild in original order by sorting by position in messages list
        messages_to_keep = messages_to_keep + recent_keep
        # Stable sort by original position to preserve order
        msg_index_map = {id(msg): i for i, msg in enumerate(messages)}
        messages_to_keep.sort(key=lambda m: msg_index_map.get(id(m), 0))

        # to_compact = everything NOT in to_keep
        keep_ids = {id(m) for m in messages_to_keep}
        messages_to_compact = [m for m in messages if id(m) not in keep_ids]

        # Ensure tool_call/tool_result pairs are not split.
        # Scan to_keep from the start, skipping [Compressed History]
        # summaries: any ToolMessage whose AI caller is in to_compact
        # is an orphan and must move back so the pair stays together.
        if messages_to_keep and messages_to_compact:
            i = 0
            while i < len(messages_to_keep):
                if _is_compressed_history(messages_to_keep[i]):
                    i += 1
                    continue
                msg = messages_to_keep[i]
                if not (hasattr(msg, "type") and msg.type == "tool"):
                    break
                tc_id = getattr(msg, "tool_call_id", None)
                caller_in_compact = False
                if tc_id:
                    for cm in messages_to_compact:
                        for tc in getattr(cm, "tool_calls", []):
                            if tc.get("id") == tc_id:
                                caller_in_compact = True
                                break
                        if caller_in_compact:
                            break
                if not caller_in_compact:
                    break
                messages_to_compact.append(messages_to_keep.pop(i))

        # Additional safety: if the last message in to_compact is an AI
        # with tool_calls, its results may be in to_keep — pull it over.
        messages_to_compact, messages_to_keep = ensure_pair_integrity(
            messages_to_compact, messages_to_keep
        )

        is_valid = not warning_state.is_at_blocking
        return messages_to_compact, messages_to_keep, is_valid


# ---------------------------------------------------------------------------
# Large output stripping (aligned with Claude Code's microCompact.ts)
# ---------------------------------------------------------------------------

# Maximum characters to keep from oversized tool outputs before compaction
STRIP_HEAD_CHARS = 500
STRIP_TAIL_CHARS = 500
STRIP_THRESHOLD_CHARS = 2000
STRIP_MARKER = "\n... [output truncated] ...\n"


def strip_large_outputs(messages: list, threshold: int = STRIP_THRESHOLD_CHARS) -> list:
    """Truncate oversized tool outputs in messages before compaction.

    Aligned with Claude Code's microCompact.ts: before full compaction,
    progressively compress tool outputs by truncating content that exceeds
    the threshold, keeping head and tail portions.

    This reduces token usage without losing critical information,
    making the compaction input smaller and cheaper.

    Args:
        messages: Conversation messages to strip.
        threshold: Character threshold above which content is truncated.

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

        # Only strip tool result messages (kubectl/blade outputs)
        is_tool = hasattr(msg, "type") and msg.type == "tool"
        if not is_tool or len(content) <= threshold:
            result.append(msg)
            continue

        # Truncate: keep head + marker + tail
        head = content[:STRIP_HEAD_CHARS]
        tail = content[-STRIP_TAIL_CHARS:]
        truncated = head + STRIP_MARKER + tail

        # Create a copy with truncated content
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
        logger.debug(
            f"Stripped large outputs: {sum(1 for m in messages if getattr(m, 'type', None) == 'tool')} "
            f"tool messages processed"
        )

    return result


# ---------------------------------------------------------------------------
# Post-compaction cleanup (aligned with Claude Code's postCompactCleanup.ts)
# ---------------------------------------------------------------------------

def post_compact_cleanup(state: dict) -> dict:
    """Clean up cached state after compaction.

    Aligned with Claude Code's postCompactCleanup.ts which clears
    classifierApprovals, speculativeChecks, sessionMessagesCache, etc.
    after a compaction event.

    In this project, the primary cleanup is:
    - Clear the environment info cache so it's re-collected on next loop
    - Reset any compaction-related tracking flags

    Args:
        state: AgentState dict to clean up.

    Returns:
        Dict of state updates to apply.
    """
    updates = {}

    # Clear env info cache so next agent_loop rebuilds it
    try:
        from chaos_agent.agent.env_info import clear_env_cache
        task_id = state.get("task_id", "")
        if task_id:
            clear_env_cache(task_id)
            logger.debug(f"Cleared env cache for task {task_id} after compaction")
    except ImportError:
        pass

    # Mark that compaction has occurred this turn
    updates["_compacted_this_turn"] = True

    return updates
