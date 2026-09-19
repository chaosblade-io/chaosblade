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
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from chaos_agent.utils.truncation import (
    build_truncation_notice as _shared_build_notice,
)

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


def _split_key_index(key: str) -> tuple[str, int | None]:
    """Split 'containerStatuses[0]' into ('containerStatuses', 0)."""
    bracket = key.find("[")
    if bracket > 0 and key.endswith("]"):
        return key[:bracket], int(key[bracket + 1:-1])
    return key, None


def _extract_nested(obj: dict, path: str):
    """Extract a value from a nested dict using dot-separated path.

    Supports 'key[N]' combined format (e.g. 'containerStatuses[0].restartCount')
    as well as standalone '[N]' segments.
    Returns None if the path doesn't exist or an intermediate value is not a dict/list.
    """
    keys = path.split(".")
    current = obj
    for key in keys:
        # Handle standalone array index like [0]
        if key.startswith("[") and key.endswith("]"):
            idx = int(key[1:-1])
            if isinstance(current, list) and 0 <= idx < len(current):
                current = current[idx]
            else:
                return None
            continue
        # Handle combined key[N] format (e.g. containerStatuses[0])
        base, arr_idx = _split_key_index(key)
        if isinstance(current, dict) and base in current:
            current = current[base]
        else:
            return None
        if arr_idx is not None:
            if isinstance(current, list) and 0 <= arr_idx < len(current):
                current = current[arr_idx]
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
    """Strip a K8s resource object to only the specified fields.

    Intermediate keys carrying a ``[N]`` index (e.g.
    ``status.containerStatuses[0]``) build a LIST container padded up to
    N, so the stripped output keeps the K8s schema shape — the output of
    ``containerStatuses`` is a list, not a flattened dict (a shape-correct
    output lets LLM-side jsonpath-style reads land instead of silently
    failing). Field paths sharing a prefix merge into the same list
    element naturally. Final keys in the current whitelists are all
    ordinary keys, so the final segment needs no index handling; the
    multi-container "[0]-only" limitation is a field-selection policy,
    preserved as-is.
    """
    result: dict = {}
    for field_path in fields:
        value = _extract_nested(item, field_path)
        if value is None:
            continue
        keys = field_path.split(".")
        target: dict | list = result
        for key in keys[:-1]:
            if key.startswith("["):
                continue  # standalone [N] segment (unused by whitelist fields)
            base, idx = _split_key_index(key)
            if idx is not None:
                container = target.get(base) if isinstance(target, dict) else None
                if not isinstance(container, list):
                    container = []
                    target[base] = container
                while len(container) <= idx:
                    container.append({})
                target = container[idx]
            else:
                nxt = target.get(base) if isinstance(target, dict) else None
                if not isinstance(nxt, dict):
                    nxt = {}
                    target[base] = nxt
                target = nxt
        final_key = keys[-1]
        final_base, _ = _split_key_index(final_key)
        if isinstance(target, dict):
            target[final_base] = value
    return result


def _detect_item_kind(item: dict) -> str:
    """Detect K8s resource kind from an item's structure."""
    kind = item.get("kind", "")
    if kind:
        return kind
    # Heuristic: check structural clues (isinstance-gated alongside
    # the entry gate's field contract — see _k8s_item_contract_ok)
    spec = item.get("spec")
    if isinstance(spec, dict) and "nodeName" in spec:
        return "Pod"
    status = item.get("status")
    if isinstance(status, dict) and "conditions" in status and "capacity" in status:
        return "Node"
    if "involvedObject" in item and "reason" in item and "message" in item:
        return "Event"
    # Check ownerReferences for DaemonSet/ReplicaSet pods. Belt-and-braces
    # type guards: the entry gate already enforces the field contract
    # (_k8s_item_contract_ok), but this helper stays safe when called
    # standalone (ownerReferences may be any JSON value in a hostile
    # payload).
    metadata = item.get("metadata")
    owners = metadata.get("ownerReferences", []) if isinstance(metadata, dict) else []
    if isinstance(owners, list):
        for owner in owners:
            if not isinstance(owner, dict):
                continue
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


def _is_k8s_list_response(data: dict) -> bool:
    """Structural signature of a K8s APIServer list response.

    ``kubectl -o json`` list output always carries a top-level ``kind``
    ending in ``List`` (PodList/NodeList/EventList/...) — the APIServer's
    serialisation contract. A bare ``items`` key is NOT K8s evidence in
    an open world: it is the most common pagination field name (MCP
    tools and generic REST APIs return ``items`` as string arrays or
    plain dicts), and treating its presence as ownership of the schema
    crashed the agent on string arrays (issue #1347) and silently
    stripped ordinary dict items down to ``{}``. When in doubt, reject:
    the caller falls back to conservative truncation, which loses token
    efficiency but never data.
    """
    kind = data.get("kind")
    return isinstance(kind, str) and kind.endswith("List")


# Fixed-schema envelope fields of a K8s object: always JSON objects in
# a genuine APIServer response. Type-malformed values under a List
# signature mark a contradictory payload.
_K8S_OBJECT_FIELDS = ("metadata", "spec", "status")


def _k8s_item_contract_ok(item: dict) -> bool:
    """True when the item's fixed-schema fields are objects (or absent).

    The strip pipeline navigates these fields (kind detection probes
    spec/status/metadata; field extraction walks into them), so a
    scalar value would crash the detector or silently strip the item
    to ``{}`` — reject the whole document instead (fail-closed
    family, round-35 A1-A3)."""
    return all(
        isinstance(item[field], dict)
        for field in _K8S_OBJECT_FIELDS
        if field in item
    )


def smart_strip_k8s_json(content: str, max_bytes: int) -> Optional[str]:
    """Smart-strip a K8s JSON list response to fit within max_bytes.

    Parses the JSON and requires the K8s list structural signature
    (top-level ``kind`` ending in ``List`` — see
    ``_is_k8s_list_response``) plus all-dict items; then identifies the
    resource type from the first item, strips each item to essential
    fields, and re-serializes. If the stripped result still exceeds
    max_bytes, progressively removes items from the end until it fits.

    Returns:
        Stripped JSON string, or None if parsing fails or content
        is not a K8s list response — the caller then falls back to
        conservative truncation (data fidelity over smart stripping).
    """
    # Round-37 input-domain boundary: this function faces raw output from
    # arbitrary tools, so the WHOLE body is one exception boundary. The
    # full parse/serialize taxonomy maps to "not a usable K8s list" →
    # None → the caller's conservative fallback: JSONDecodeError
    # (unparseable), TypeError (non-str input), RecursionError (nesting
    # deeper than the parser's recursion limit), UnicodeError (lone
    # surrogates: json.loads ACCEPTS the \udXXX escape, but the strict
    # .encode("utf-8") size checks cannot serialize it). Surrogates are
    # rejected, never errors="replace"-serialized — replacing would
    # inject raw surrogate characters into message content that the
    # provider request encoder would then choke on; the fallback's
    # head-cut keeps the ORIGINAL escape-sequence form, which is safe.
    try:
        return _smart_strip_k8s_json_body(content, max_bytes)
    except (json.JSONDecodeError, TypeError, RecursionError, UnicodeError):
        return None


def _smart_strip_k8s_json_body(content: str, max_bytes: int) -> Optional[str]:
    data = json.loads(content)

    if not isinstance(data, dict) or not _is_k8s_list_response(data):
        return None

    items = data.get("items", [])
    if not items:
        return None

    # Fail-closed shape check: the K8s contract says ``items`` is an
    # array of objects whose fixed-schema fields are objects. A
    # non-list value (scalar/dict), a non-dict member (string arrays,
    # mixed arrays, fabricated payloads), a type-malformed field value
    # (scalar/null spec/status/metadata — round-35 A1-A3), or a
    # null/scalar envelope metadata (round-35 A4) rejects the WHOLE
    # document to the generic fallback — never a raise, never a partial
    # strip. Keeps the entry contract self-contained: non-K8s → None,
    # for every input shape.
    if "metadata" in data and not isinstance(data.get("metadata"), dict):
        return None
    if not isinstance(items, list) or not all(
        isinstance(item, dict) and _k8s_item_contract_ok(item) for item in items
    ):
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
    # (the entry gate rejects null/scalar envelope metadata; the
    # isinstance guard keeps this body standalone-safe regardless).
    if isinstance(stripped_data.get("metadata"), dict):
        stripped_data["metadata"].pop("annotations", None)
        stripped_data["metadata"].pop("managedFields", None)

    # Serialize and check size
    result = json.dumps(stripped_data, ensure_ascii=False)
    if len(result.encode("utf-8")) <= max_bytes:
        return result

    # Still too large: size the longest fitting prefix ARITHMETICALLY.
    # The old pop-and-re-dumps loop re-serialized the whole document once
    # per removed item (O(n²) — measured 20.8s for a realistic 5000-pod
    # PodList at the 16KB recent budget; the compactor runs before every
    # LLM turn, so the stall is user-visible). A dict-wrapped list has
    # no closed-form prefix (``items`` sits between sibling keys), but
    # one dumps of the empty-items shell measures everything except the
    # item bodies, and the bodies serialize independently:
    #   total(k) = shell_bytes + Σ sizes[:k] + 2·(k-1)   [", " separators]
    # — the same identity truncate_json_at_boundary relies on for bare
    # arrays. A single final dumps verifies the arithmetic; the guard
    # converts any sizing bug into the honest plain-text fallback below,
    # never a silent over-budget or malformed result.
    shell_bytes = len(
        json.dumps(stripped_data | {"items": []}, ensure_ascii=False).encode("utf-8")
    )
    sizes = [
        len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
        for item in stripped_items
    ]
    best_k = 0
    running = shell_bytes
    for k, size in enumerate(sizes, start=1):
        running += size + (2 if k > 1 else 0)  # ", " separator
        if running > max_bytes:
            break
        best_k = k
    if best_k >= 1:
        stripped_data["items"] = stripped_items[:best_k]
        result = json.dumps(stripped_data, ensure_ascii=False)
        if len(result.encode("utf-8")) <= max_bytes:
            return result
    # best_k == 0 (no single item fits) or the verification dumps came
    # out over budget: fall through to the honest plain-text cut.

    # Budget-contract floor: a single stripped item may still exceed the
    # budget (an abnormally giant pod, or the notice bytes squeezing the
    # budget near its half floor). JSON-awareness is best-effort, not a
    # hard guarantee — an honest plain-text head cut of the ORIGINAL
    # content wins over silently returning over-budget JSON (the
    # original is already cached to disk; the notice carries retrieval).
    return truncate_text(content, max_bytes)


# Bloat keys dropped during JSON-aware reduction (mirrors smart_strip's
# metadata pruning): managedFields/annotations routinely dominate a
# single-object response's byte count while carrying near-zero value.
_BLOAT_KEYS = ("managedFields", "annotations")


def _prune_bloat(obj):
    """Recursively drop bloat keys (managedFields/annotations)."""
    if isinstance(obj, dict):
        return {k: _prune_bloat(v) for k, v in obj.items() if k not in _BLOAT_KEYS}
    if isinstance(obj, list):
        return [_prune_bloat(v) for v in obj]
    return obj


def truncate_json_at_boundary(content: str, max_bytes: int) -> str:
    """Truncate JSON content under max_bytes WITHOUT fabricating structure.

    JSON-aware reduction for parseable content:
    * dict-shaped → drop bloat keys everywhere, re-serialize, revive the
      top-level "truncated": true marker (previously dead code);
    * list-shaped → drop bloat keys, then progressively drop whole items
      from the end until within budget (a bare array cannot carry a
      marker key; the compactor notice appended by the caller already
      announces the truncation);
    * either shape still over budget after reduction → honest
      plain-text head cut of the ORIGINAL content.

    Content that does not parse (pseudo-JSON, e.g. merged error output)
    gets a plain-text cut directly. The previous implementation cut at a
    textual '},' position and appended fabricated closing brackets — it
    could not tell an item-level boundary from a nested-field one, so
    real K8s objects (which nest metadata/spec/status) came out as
    invalid JSON with an unclosed '{'. NEVER fabricate closers again.
    """
    # Round-37 input-domain boundary (same family taxonomy as
    # smart_strip_k8s_json): deep nesting blows the parser's recursion
    # limit and lone surrogates pass json.loads but break the strict
    # .encode("utf-8") size checks — every family member lands on the
    # honest plain-text head-cut of the ORIGINAL content.
    try:
        return _truncate_json_at_boundary_body(content, max_bytes)
    except (json.JSONDecodeError, TypeError, RecursionError, UnicodeError):
        return truncate_text(content, max_bytes)


def _truncate_json_at_boundary_body(content: str, max_bytes: int) -> str:
    data = json.loads(content)

    if isinstance(data, dict):
        candidate = _prune_bloat(data)
        # FORCE the marker: bloat removal IS a truncation, and an original
        # "truncated": false riding along unchanged would report "nothing
        # was lost" on reduced content — a lie that suppresses the very
        # caution this notice exists to trigger.
        candidate["truncated"] = True
        out = json.dumps(candidate, ensure_ascii=False)
        if len(out.encode("utf-8")) <= max_bytes:
            return out
        # Still over budget after pruning: honest plain-text fallback.
        return truncate_text(content, max_bytes)

    if isinstance(data, list):
        pruned = _prune_bloat(data)
        out = json.dumps(pruned, ensure_ascii=False)
        if len(out.encode("utf-8")) <= max_bytes:
            return out
        # Same floor as smart_strip's item-removal loop (> 1, never 0):
        # popping the LAST item would leave "[]" — 2 bytes, in-budget, and
        # ZERO information (the model reads an empty list + notice as
        # "the list was empty", losing even the shape of the giant item).
        # Sizing is arithmetic, not pop-and-re-dumps: each pop iteration
        # re-serializes the whole array (O(n²) — measured ~520ms for a
        # realistic 2000-item bare array at the 1KB tier; the compactor
        # runs every turn, so per-message stalls are user-visible). A
        # bare array serializes exactly as "[" + ", ".join(items) + "]",
        # so the longest fitting prefix follows from one pass of item
        # sizes; a single dumps verifies the result.
        if len(pruned) > 1:
            sizes = [
                len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
                for item in pruned
            ]
            best_k = 0
            running = 2  # the "[" and "]" brackets
            for k, size in enumerate(sizes, start=1):
                running += size + (2 if k > 1 else 0)  # ", " separator
                if running > max_bytes:
                    break
                best_k = k
            if best_k >= 1:
                out = json.dumps(pruned[:best_k], ensure_ascii=False)
                if len(out.encode("utf-8")) <= max_bytes:
                    return out
        # A single (giant or still-over-budget) item: honest fallback.
        return truncate_text(content, max_bytes)

    # Scalar JSON (number/string/bool) — plain-text cut.
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

    Thin delegation to the shared truncation contract
    (chaos_agent.utils.truncation): markers, guidance semantics, and
    cache wordings are preserved verbatim — the shared module is the
    single home of the notice family (new call sites import from
    there). The historical variant gains the original size field
    (three-field invariant skeleton: marker + size + retrieval path).
    """
    if is_recent:
        return _shared_build_notice(
            "success-output", original_size // 1024, unit="KB",
            retrieve_path=cache_path,
        )
    return _shared_build_notice(
        "historical", original_size, unit="bytes",
        retrieve_path=cache_path,
    )


# Eviction sweep candidates are identified by the compactor's OWN naming
# signature (uuid4().hex[:8] + ".txt"), never by extension alone — the
# directory is shared infrastructure in some deployments (a working_dir
# collision used to co-locate the skill catalog cache here — round-42
# O1), so an extension glob would evict tenants by coincidence; the
# signature makes "ours" a property of what we write, not of what
# others happen to name their files.
_HEX_DIGITS = frozenset("0123456789abcdef")


def _is_cache_artifact_name(name: str) -> bool:
    """True iff name matches the compactor's naming signature exactly.

    A set-membership test, not a hex-class regex — deliberately outside
    the UID-shape legislation: this is a filename ownership signature,
    not a UID shape (uuid4().hex[:8] is lowercase by construction, and
    the lowercase-only set encodes that contract more precisely than a
    character class would).
    """
    stem, dot, suffix = name.partition(".")
    return (
        dot == "."
        and suffix == "txt"
        and len(stem) == 8
        and all(c in _HEX_DIGITS for c in stem)
    )


class ToolResultCompactor:
    """Two-stage tool output truncation with time-based micro-compact."""

    RECENT_MAX_BYTES = 16 * 1024  # 16KB for recent tool outputs (aligned with OpenClaw)
    OLD_MAX_BYTES = 1024  # 1KB for historical tool outputs
    # Keep last 5 tool results at high limit. 3→5 (2026-09-18, #13-R):
    # the agent plans in parallel batches of 4-5 tool calls, so a 3-slot
    # window demotes the OLDEST result of the very batch that just
    # returned — the #13-R pods enumeration (9785B) was demoted 78s after
    # arrival, and the notice-directed `read_file` cache re-read was
    # demoted again 0.3s later (same-batch oldest). 5 slots cover the
    # observed batch shape; cost ceiling +32KB (~8k tokens worst case).
    KEEP_RECENT_N = 5
    # Retention for cached artifacts — the module docstring's "TTL of 3
    # days" promise: swept best-effort on each successful write.
    CACHE_TTL_DAYS = 3

    def __init__(self, cache_dir: Optional[Path] = None):
        self.cache_dir = cache_dir

    def _cache_to_disk(self, content: str, task_id: str = "") -> str:
        """Cache oversized output to disk. Returns the cache path.

        Best-effort by the same contract the truncation boundaries obey:
        the cache is a retrieval artifact, not message content, so raw
        lone surrogates are written with replacement characters instead
        of failing the write, and an IO failure (unwritable cache_dir)
        downgrades to an empty path — the truncation notice then simply
        omits the retrieval path. Either family raising here used to
        abort compaction wholesale on compact()'s mandatory path (the
        cache write happens BEFORE the wrapped truncation boundaries):
        message never truncated, retried every turn, orphaned cache
        files accumulating per failed attempt. On success, artifacts
        past CACHE_TTL_DAYS are swept (see _evict_expired_cache).
        """
        if self.cache_dir is None:
            return ""

        cache_id = uuid.uuid4().hex[:8]
        cache_path = self.cache_dir / f"{cache_id}.txt"
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(content, encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning(
                f"Failed to cache oversized output for task {task_id} to "
                f"{cache_path} ({exc!r}); truncating without a retrieval path"
            )
            return ""
        logger.debug(f"Cached oversized output to {cache_path}")
        self._evict_expired_cache()
        return str(cache_path)

    def _evict_expired_cache(self) -> None:
        """Sweep cache artifacts past CACHE_TTL_DAYS. Best-effort: never raises.

        Write-side piggyback, no timers or background threads: every
        successful cache write sweeps the directory it just wrote to, so
        an active cache_dir stays bounded while a quiet one is inert
        (nothing new arrives, nothing grows). Eviction failures are
        loud warnings, not aborts — the same never-fail contract the
        write itself obeys. The sweep's mandate is disk boundedness
        alone: since the round-41 bridge retirement these artifacts are
        write-only (the recover-side reader was removed with the bridge
        — it parsed workload-spoofable stdout for file paths), so a
        vanished artifact breaks nothing downstream, and no read path
        may grow back without re-earning a security review.
        """
        if self.cache_dir is None:
            return
        cutoff = time.time() - self.CACHE_TTL_DAYS * 86400
        try:
            # Signature-matched, not extension-matched — see
            # _is_cache_artifact_name: only files the compactor itself
            # names are sweep candidates, foreign tenants are spared.
            artifacts = [
                p for p in self.cache_dir.glob("*.txt")
                if _is_cache_artifact_name(p.name)
            ]
        except OSError as exc:
            logger.warning(
                f"Cache eviction scan failed in {self.cache_dir}: {exc!r}"
            )
            return
        for artifact in artifacts:
            try:
                if artifact.stat().st_mtime < cutoff:
                    artifact.unlink()
                    logger.debug(f"Evicted expired cache artifact: {artifact}")
            except OSError as exc:
                logger.warning(
                    f"Failed to evict expired cache artifact "
                    f"{artifact}: {exc!r}"
                )

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
        # Exclude read_skill_resource from the list so it doesn't
        # inflate len(tool_results) and shift the recency window.
        tool_results = [
            (i, msg)
            for i, msg in enumerate(messages)
            if is_tool_message(msg) and getattr(msg, "name", "") != "read_skill_resource"
        ]

        for idx, (i, msg) in enumerate(tool_results):
            is_recent = idx >= len(tool_results) - self.KEEP_RECENT_N
            max_bytes = self.RECENT_MAX_BYTES if is_recent else self.OLD_MAX_BYTES

            content = getattr(msg, "content", "")
            if not isinstance(content, str):
                continue

            akw = getattr(msg, "additional_kwargs", None) or {}
            if akw.get("_truncated_budget") == max_bytes:
                continue

            if len(content.encode("utf-8", errors="replace")) > max_bytes:
                original_size = len(content.encode("utf-8", errors="replace"))
                cache_path = self._cache_to_disk(content, task_id)

                notice = build_truncation_notice(
                    original_size, max_bytes, is_recent, cache_path
                )
                notice_bytes = len(notice.encode("utf-8", errors="replace"))
                truncate_budget = max(max_bytes - notice_bytes, max_bytes // 2)

                stripped = smart_strip_k8s_json(content, truncate_budget)
                if stripped is not None:
                    msg.content = stripped
                else:
                    if content.lstrip().startswith("{") or content.lstrip().startswith("["):
                        msg.content = truncate_json_at_boundary(content, truncate_budget)
                    else:
                        msg.content = truncate_text(content, truncate_budget)

                msg.content += notice

                if isinstance(akw, dict):
                    akw["_truncated_budget"] = max_bytes

        return messages
