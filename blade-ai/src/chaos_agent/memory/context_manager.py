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
    TokenCount,
    TokenCountQuality,
    count_tokens,
    count_tokens_messages,
)

logger = logging.getLogger(__name__)


# Re-exports for callers that still want the bare integer view. New
# code should call ``count_tokens(...)`` / ``count_tokens_messages(...)``
# directly to get quality tags.
__all__ = [
    "count_tokens",
    "count_tokens_messages",
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
WARNING_BUFFER_TOKENS = 20_000
ERROR_BUFFER_TOKENS = 20_000
BLOCKING_BUFFER_TOKENS = 3_000

# Circuit breaker: stop retrying after consecutive failures
MAX_CONSECUTIVE_COMPACT_FAILURES = 3


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
        # Threshold = the EARLIER of two triggers (min, not max):
        #
        #   1. ``max_tokens - AUTOCOMPACT_BUFFER_TOKENS``  — buffer ceiling.
        #      Never let context get within 13K of the absolute max,
        #      otherwise the next user message could push past the
        #      provider's hard limit before compaction has a chance to
        #      run.
        #
        #   2. ``max_tokens * compact_ratio``  — user setting. Lets
        #      the operator pull the trigger earlier (e.g. 0.5 ratio
        #      on a 128K window = 64K trigger) for tighter sessions
        #      or when testing the compaction path.
        #
        # Take the SMALLER → "whichever fires first". With defaults
        # (0.85 × 128K = 108_800 vs 128K - 13K = 115_000), the ratio
        # wins and we trigger at 108_800. Bumping ratio to 0.95 would
        # let the buffer floor (115_000) win — the ratio can never
        # delay compaction past the buffer's safety ceiling.
        #
        # PREVIOUS BUG: this was ``max()``, which made the buffer
        # ALWAYS win because subtracting 13K is almost always more
        # restrictive than ratio multiplication. The ``compact_ratio``
        # setting was effectively dead — operators changing it saw
        # no behavior change at the trigger threshold.
        auto_compact_threshold = min(
            max_tokens - AUTOCOMPACT_BUFFER_TOKENS,
            int(max_tokens * compact_ratio),
        )
        # Defensive floor: never go below 50% of the window even if
        # the user typo'd a tiny ratio. Below this the system would
        # spin trying to compact every message.
        auto_compact_threshold = max(
            auto_compact_threshold,
            int(max_tokens * 0.5),
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
        #   the user setting actually shapes the trigger (see the
        #   ``min()`` formula in that function).
        # - threshold is the legacy "rough" budget some callers still
        #   compare against directly (e.g. the hook's post-strip
        #   "still over budget?" check). Both must agree on intent so
        #   the operator's BLADE_AI_CONTEXT_COMPACT_RATIO knob behaves
        #   consistently across all consumers.
        self.compact_ratio = compact_ratio
        self.compact_threshold = int(max_tokens * compact_ratio)
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
        # safe_count = count × per-quality safety margin (1.0 for tiktoken
        # native, 1.05 for cross-family BPE, 1.20 for legacy heuristic).
        # The threshold check thus auto-widens when the underlying tokenizer
        # is less accurate, replacing the old global SAFETY_MARGIN=1.2.
        # No ``model=`` arg needed — count_tokens_messages reads
        # ``settings.model_name`` by default (see _resolve_model in tokens.py).
        total_tokens = count_tokens_messages(messages).safe_count
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
        # Incremental compaction: always keep [Compressed History] summaries
        # so they are never re-compressed, preventing information loss.
        messages_to_keep = []
        kept_tokens = 0

        # First pass: pull out all [Compressed History] summaries into to_keep
        # This ensures previous compression results are never re-compressed.
        # Use raw ``count`` here (not ``safe_count``) — we're accumulating
        # toward ``reserve_tokens`` to decide how much room is left for
        # tail-keeping, not making a threshold decision; over-counting at
        # this step would under-keep useful recent context.
        summary_indices = set()
        for i, msg in enumerate(messages):
            if _is_compressed_history(msg):
                messages_to_keep.append(msg)
                kept_tokens += count_tokens_messages([msg]).count
                summary_indices.add(i)

        # Second pass: reserve recent messages (skipping summaries already kept)
        recent_keep = []
        for msg in reversed(messages):
            if _is_compressed_history(msg):
                continue  # Already added above
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
