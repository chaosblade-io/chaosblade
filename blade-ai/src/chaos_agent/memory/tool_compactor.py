"""Tool output two-stage truncation (reference: ReMe).

Recent tool outputs (last N): truncate at 16KB
Historical tool outputs: truncate at 1KB
Oversized outputs are cached to disk with a TTL of 3 days.

When truncating K8s JSON responses, items are intelligently stripped
to essential fields before truncation, preserving valid JSON structure
and providing actionable strategy hints to the LLM.

Also implements time-based micro-compact aligned with Claude Code's
maybeTimeBasedMicrocompact(): when the user has been idle beyond
a threshold, old tool results are replaced with a cleared marker.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Time-based MicroCompact constants (aligned with Claude Code microCompact.ts)
# ---------------------------------------------------------------------------

# Default gap: if last AI message was >5 minutes ago, trigger cleanup
TIME_BASED_MC_GAP_MINUTES = 5.0
# Keep the most recent N tool results even when time-triggered
TIME_BASED_MC_KEEP_RECENT = 3
# Marker replacing cleared tool result content
CLEARED_MARKER = "[Old tool result content cleared]"


def is_tool_message(msg) -> bool:
    """Check if a message is a tool result message."""
    return hasattr(msg, "type") and msg.type == "tool"


def is_ai_message(msg) -> bool:
    """Check if a message is an AI/assistant message."""
    return hasattr(msg, "type") and msg.type == "ai"


def _get_ai_timestamp(msg) -> Optional[datetime]:
    """Extract timestamp from an AI message's additional_kwargs."""
    ts = getattr(msg, "additional_kwargs", {}).get("timestamp")
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts
    if isinstance(ts, str):
        try:
            from chaos_agent.utils.time import parse_iso_timestamp
            return parse_iso_timestamp(ts)
        except (ValueError, TypeError):
            return None
    return None


def maybe_time_based_microcompact(
    messages: list,
    gap_threshold_minutes: float = TIME_BASED_MC_GAP_MINUTES,
    keep_recent: int = TIME_BASED_MC_KEEP_RECENT,
) -> Optional[list]:
    """Time-based tool result cleanup.

    Aligned with Claude Code's maybeTimeBasedMicrocompact().
    When the time since the last AI message exceeds the gap threshold,
    old tool results are replaced with a cleared marker, keeping only
    the most recent N tool results intact.

    This is a "progressive compression" step that runs before full
    compaction — clearing stale kubectl/blade outputs that may no
    longer be relevant after a user pause.

    Args:
        messages: Conversation messages.
        gap_threshold_minutes: Minimum idle minutes to trigger cleanup.
        keep_recent: Number of recent tool results to preserve.

    Returns:
        Modified messages list if cleanup was triggered, or None
        if conditions are not met (no trigger needed).
    """
    # Find the timestamp of the last AI message
    last_ai_time = None
    for msg in reversed(messages):
        if is_ai_message(msg):
            last_ai_time = _get_ai_timestamp(msg)
            break

    if last_ai_time is None:
        # No AI message with timestamp — cannot determine idle gap
        return None

    # Calculate idle gap
    now = datetime.now(timezone.utc)
    # Ensure both datetimes are offset-aware for comparison
    if last_ai_time.tzinfo is None:
        last_ai_time = last_ai_time.replace(tzinfo=timezone.utc)
    gap_minutes = (now - last_ai_time).total_seconds() / 60.0

    if gap_minutes < gap_threshold_minutes:
        return None  # Not idle long enough

    # Collect indices of all tool result messages
    tool_result_indices = []
    for i, msg in enumerate(messages):
        if is_tool_message(msg):
            tool_result_indices.append(i)

    if len(tool_result_indices) <= keep_recent:
        return None  # Not enough tool results to bother cleaning

    # Determine which to clear (all except the last keep_recent)
    keep_set = set(tool_result_indices[-keep_recent:])
    clear_set = set(tool_result_indices) - keep_set

    # Build new message list with cleared markers
    modified = False
    result = list(messages)  # shallow copy
    for i in clear_set:
        msg = result[i]
        content = getattr(msg, "content", "")
        if isinstance(content, str) and content != CLEARED_MARKER:
            # LangChain messages support direct attribute mutation
            msg.content = CLEARED_MARKER
            modified = True

    if not modified:
        return None  # Nothing actually changed

    logger.info(
        f"Time-based micro-compact: cleared {len(clear_set)} old tool results "
        f"(idle {gap_minutes:.1f} min, keeping last {keep_recent})"
    )
    return result


def truncate_text(text: str, max_bytes: int) -> str:
    """Truncate text to approximately max_bytes, preserving valid UTF-8."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    truncated = encoded[:max_bytes].decode("utf-8", errors="replace")
    return truncated


# ---------------------------------------------------------------------------
# K8s JSON smart stripping — reduce large K8s list responses to key fields
# ---------------------------------------------------------------------------

# Parameters to ignore when generating tool call fingerprints for loop detection
_GLOBAL_PARAMS = {"kubeconfig", "context", "cluster"}


def _extract_nested(obj: dict, path: str):
    """Extract a value from a nested dict using dot-separated path.

    Returns None if the path doesn't exist or an intermediate value is not a dict/list.
    """
    keys = path.split(".")
    current = obj
    for key in keys:
        # Handle array index like [0]
        if key.startswith("[") and key.endswith("]"):
            idx = int(key[1:-1])
            if isinstance(current, list) and 0 <= idx < len(current):
                current = current[idx]
            else:
                return None
        elif isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return None
    return current


# Key fields to preserve per K8s resource kind (detected from items)
_POD_STRIP_FIELDS = [
    "metadata.name", "metadata.namespace", "metadata.deletionTimestamp",
    "spec.nodeName",
    "status.phase", "status.startTime",
    "status.conditions",
    "status.containerStatuses[0].restartCount",
    "status.containerStatuses[0].state",
    "status.containerStatuses[0].image",
]

_NODE_STRIP_FIELDS = [
    "metadata.name",
    "spec.unschedulable", "spec.taints",
    "status.conditions",
    "status.capacity", "status.allocatable",
]

_EVENT_STRIP_FIELDS = [
    "metadata.name", "metadata.namespace",
    "involvedObject", "reason", "message", "type",
    "lastTimestamp", "eventTime", "count",
]

_GENERIC_STRIP_FIELDS = [
    "metadata.name", "metadata.namespace",
    "status.phase", "spec.nodeName",
]


def _strip_item(item: dict, fields: list[str]) -> dict:
    """Strip a K8s resource object to only the specified fields."""
    result = {}
    for field_path in fields:
        value = _extract_nested(item, field_path)
        if value is not None:
            # Reconstruct the nested path in the result
            keys = field_path.split(".")
            target = result
            for key in keys[:-1]:
                if key.startswith("["):
                    continue  # Skip array indices in reconstruction
                if key not in target:
                    target[key] = {}
                target = target[key]
            # Handle the final key
            final_key = keys[-1]
            if isinstance(target, dict):
                target[final_key] = value
    return result


def _detect_item_kind(item: dict) -> str:
    """Detect K8s resource kind from an item's structure."""
    kind = item.get("kind", "")
    if kind:
        return kind
    # Heuristic: check structural clues
    if "spec" in item and "nodeName" in item.get("spec", {}):
        return "Pod"
    if "status" in item and "conditions" in item.get("status", {}) and "capacity" in item.get("status", {}):
        return "Node"
    if "involvedObject" in item and "reason" in item and "message" in item:
        return "Event"
    # Check ownerReferences for DaemonSet/ReplicaSet pods
    owners = item.get("metadata", {}).get("ownerReferences", [])
    if owners:
        for owner in owners:
            owner_kind = owner.get("kind", "")
            if owner_kind in ("DaemonSet", "ReplicaSet", "Deployment", "StatefulSet", "Job"):
                return "Pod"  # Owned by a workload → likely a Pod
    return "Generic"


def _get_strip_fields(kind: str) -> list[str]:
    """Get the field list for stripping based on resource kind."""
    if kind == "Pod":
        return _POD_STRIP_FIELDS
    elif kind == "Node":
        return _NODE_STRIP_FIELDS
    elif kind == "Event":
        return _EVENT_STRIP_FIELDS
    return _GENERIC_STRIP_FIELDS


def smart_strip_k8s_json(content: str, max_bytes: int) -> Optional[str]:
    """Smart-strip a K8s JSON list response to fit within max_bytes.

    Parses the JSON, identifies the resource type from the first item,
    strips each item to essential fields, and re-serializes.
    If the stripped result still exceeds max_bytes, progressively
    removes items from the end until it fits.

    Returns:
        Stripped JSON string, or None if parsing fails or content
        is not a K8s list response.
    """
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(data, dict) or "items" not in data:
        return None

    items = data.get("items", [])
    if not items:
        return None

    # Detect kind from first item
    first_kind = _detect_item_kind(items[0]) if items else "Generic"
    strip_fields = _get_strip_fields(first_kind)

    # Strip each item
    stripped_items = [_strip_item(item, strip_fields) for item in items]

    # Build stripped response
    stripped_data = dict(data)
    stripped_data["items"] = stripped_items
    stripped_data["truncated"] = True
    # Remove verbose annotations/managedFields from metadata if present
    if "metadata" in stripped_data:
        stripped_data["metadata"].pop("annotations", None)
        stripped_data["metadata"].pop("managedFields", None)

    # Serialize and check size
    result = json.dumps(stripped_data, ensure_ascii=False)

    # If still too large, progressively remove items from the end
    if len(result.encode("utf-8")) > max_bytes:
        while len(stripped_items) > 1:
            stripped_items.pop()
            stripped_data["items"] = stripped_items
            result = json.dumps(stripped_data, ensure_ascii=False)
            if len(result.encode("utf-8")) <= max_bytes:
                break

    return result


def truncate_json_at_boundary(content: str, max_bytes: int) -> str:
    """Truncate JSON content at an item boundary, keeping valid structure.

    Searches for a '},' boundary near max_bytes (±20%), then closes
    the JSON with ']}' to keep it structurally valid.

    Falls back to simple truncate_text() if no boundary found.
    """
    # Search range: 80% to 120% of max_bytes
    search_start = int(max_bytes * 0.8)
    search_end = min(len(content), int(max_bytes * 1.2))

    # Look for the last }, or }] boundary within the search range
    best_pos = -1
    for pos in range(search_end, search_start, -1):
        if pos < len(content) - 1:
            # Check for item boundary: }, (item separator in array)
            if content[pos:pos+2] == "}," :
                best_pos = pos + 1  # Include the }
                break
            # Check for end of items array: }]
            if content[pos:pos+2] == "}]" :
                best_pos = pos + 2  # Include the }]
                break

    if best_pos > 0:
        truncated = content[:best_pos]
        # Close the items array and the top-level object
        if truncated.rstrip().endswith(","):
            truncated = truncated.rstrip()[:-1]  # Remove trailing comma
        if not truncated.rstrip().endswith("]"):
            truncated += "\n  ]"
        # Add truncated flag
        if truncated.rstrip().endswith("}"):
            # Insert truncated flag before the final }
            truncated = truncated.rstrip()[:-1] + ',\n  "truncated": true\n}'
        return truncated

    # Fallback: simple byte truncation
    return truncate_text(content, max_bytes)


def build_truncation_notice(
    original_size: int,
    max_bytes: int,
    is_recent: bool,
    cache_path: str = "",
) -> str:
    """Build a truncation notice with strategy hints.

    Uses a detailed notice for recent outputs (16KB budget) and a
    compact notice for old outputs (1KB budget).
    """
    original_kb = original_size // 1024

    if is_recent:
        notice = (
            f"\n\n⚠️ OUTPUT_TRUNCATED: 输出已精简或截断（原始 {original_kb}KB），仅保留关键字段。"
        )
        if cache_path:
            notice += f"\n完整输出缓存于: {cache_path}"
        notice += (
            "\n不要重复相同查询！如需完整数据，请使用以下策略："
            "\n- 使用 --field-selector 缩小范围（如 kubectl subcommand=\"get\" --field-selector spec.nodeName=<node>）"
            "\n- 使用 -o name 获取精简列表"
            "\n- 使用 kubectl 工具的 -o jsonpath 提取特定字段"
            "\n- 按名称查询单个资源而非列出全部"
        )
    else:
        notice = "\n⚠️ TRUNCATED. Use field_selector or output_format='name'."
        if cache_path:
            notice += f" Cache: {cache_path}"

    return notice


class ToolResultCompactor:
    """Two-stage tool output truncation with time-based micro-compact."""

    RECENT_MAX_BYTES = 16 * 1024  # 16KB for recent tool outputs (aligned with OpenClaw)
    OLD_MAX_BYTES = 1024  # 1KB for historical tool outputs
    KEEP_RECENT_N = 3  # Keep last 3 tool results at high limit

    def __init__(self, cache_dir: Optional[Path] = None):
        self.cache_dir = cache_dir

    def _cache_to_disk(self, content: str, task_id: str = "") -> str:
        """Cache oversized output to disk. Returns the cache path."""
        if self.cache_dir is None:
            return ""

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_id = uuid.uuid4().hex[:8]
        cache_path = self.cache_dir / f"{cache_id}.txt"
        cache_path.write_text(content, encoding="utf-8")
        logger.debug(f"Cached oversized output to {cache_path}")
        return str(cache_path)

    def compact(
        self,
        messages: list,
        task_id: str = "",
    ) -> list:
        """Apply time-based micro-compact and two-stage truncation.

        Aligned with Claude Code's progressive compression:
        1. First, try time-based micro-compact (clear stale tool results).
        2. Then, apply size-based truncation (two-stage: recent vs old).

        Args:
            messages: List of langchain message objects
            task_id: Task ID for disk cache naming

        Returns:
            Messages with tool outputs compacted as needed
        """
        # Step 1: Time-based micro-compact (clear stale tool results)
        time_result = maybe_time_based_microcompact(messages)
        if time_result is not None:
            messages = time_result

        # Step 2: Size-based two-stage truncation
        tool_results = [
            (i, msg) for i, msg in enumerate(messages) if is_tool_message(msg)
        ]

        for idx, (i, msg) in enumerate(tool_results):
            # Skill case content is the primary authority for downstream
            # nodes (baseline, execute, verifier) — never truncate it.
            if getattr(msg, "name", "") == "read_skill_resource":
                continue

            is_recent = idx >= len(tool_results) - self.KEEP_RECENT_N
            max_bytes = self.RECENT_MAX_BYTES if is_recent else self.OLD_MAX_BYTES

            content = getattr(msg, "content", "")
            if not isinstance(content, str):
                continue

            if len(content.encode("utf-8", errors="replace")) > max_bytes:
                original_size = len(content.encode("utf-8", errors="replace"))
                cache_path = self._cache_to_disk(content, task_id)

                # Try smart strip for K8s JSON responses first
                stripped = smart_strip_k8s_json(content, max_bytes)
                if stripped is not None:
                    msg.content = stripped
                else:
                    # Not a K8s list JSON — try boundary-aware JSON truncation
                    if content.lstrip().startswith("{") or content.lstrip().startswith("["):
                        msg.content = truncate_json_at_boundary(content, max_bytes)
                    else:
                        msg.content = truncate_text(content, max_bytes)

                # Append actionable truncation notice
                msg.content += build_truncation_notice(
                    original_size, max_bytes, is_recent, cache_path
                )

        return messages
