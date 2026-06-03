"""Task store: JSON-based per-task conversation persistence.

Each inject and each recover is a separate task with its own file
at `memory/tasks/<task_id>.json`. A recover task cross-references
its originating inject via `parent_task_id`. TUI-level grouping is
written on every task file as `tui_session_id`, and if a global
`TuiSessionStore` is registered, new task_ids are appended to the
corresponding TUI session's forward index automatically.
"""

import json
import logging
import tempfile
from pathlib import Path
from typing import Optional

from langchain_core.messages import RemoveMessage, SystemMessage

from chaos_agent.agent.node_names import INTENT_CLARIFICATION
from chaos_agent.config.settings import settings
from chaos_agent.utils.time import now_iso

logger = logging.getLogger(__name__)

# Global SessionStore reference for direct access from non-hook graph nodes
# (e.g., direct_execute, which bypasses PreReasoningHook and cannot reach
#  the SessionStore through the normal state→hook channel).
_global_session_store: Optional["SessionStore"] = None


def set_global_session_store(store: "SessionStore") -> None:
    """Register the global SessionStore singleton for non-hook node access."""
    global _global_session_store
    _global_session_store = store


def get_global_session_store() -> Optional["SessionStore"]:
    """Retrieve the global SessionStore singleton (None if not yet set)."""
    return _global_session_store


# Marker key for messages that should be visible to the LLM but excluded
# from session recording and observable output.
# Usage: HumanMessage(content=..., additional_kwargs={NO_SESSION_MARKER: True})
NO_SESSION_MARKER = "_no_session"


def _is_intent_dialogue_message(msg) -> bool:
    """Return True if the message belongs to the intent clarification phase.

    The IntentClarificationSummary SystemMessage is the handoff boundary.
    Messages BEFORE it (in the state.messages list order) are intent
    dialogue and should live in the session file, not the task file.
    Messages AFTER it (including the summary itself) are execution content.

    Detection: messages that do NOT contain "[Intent Clarification Summary]"
    and appear before the summary in a linear scan are considered dialogue.
    Since finalize_session receives the full remaining list, we split at
    the first occurrence of the summary.
    """
    # RemoveMessage entries are not "dialogue" — they're housekeeping.
    if isinstance(msg, RemoveMessage):
        return False
    # SystemMessages that start with "[Intent Clarification Summary]" are
    # the handoff boundary itself — they belong to the execution phase.
    content = getattr(msg, "content", "") or ""
    if isinstance(msg, SystemMessage) and content.startswith("[Intent Clarification Summary]"):
        return False
    return True


def _split_at_handoff(messages: list) -> tuple[list, list]:
    """Split a message list at the IntentClarificationSummary boundary.

    Returns (dialogue_messages, execution_messages) where:
    - dialogue_messages: everything BEFORE the summary (intent clarification)
    - execution_messages: the summary + everything AFTER it (execution)

    If no summary is found, returns ([], messages) — treat everything as
    execution content (pre-P0-7-5 behavior, safe fallback).
    """
    summary_idx = None
    for i, msg in enumerate(messages):
        if isinstance(msg, RemoveMessage):
            continue
        content = getattr(msg, "content", "") or ""
        if isinstance(msg, SystemMessage) and content.startswith("[Intent Clarification Summary]"):
            summary_idx = i
            break

    if summary_idx is None:
        # No handoff found — treat all as execution (backward-compatible)
        return [], messages

    # dialogue = messages before the summary (excluding RemoveMessage)
    dialogue = [m for m in messages[:summary_idx] if not isinstance(m, RemoveMessage)]
    # execution = summary + everything after
    execution = messages[summary_idx:]
    return dialogue, execution


def _serialize_message_full(msg) -> dict:
    """Serialize a langchain message to a dict with FULL content (no truncation)."""
    result = {"type": getattr(msg, "type", "unknown")}

    content = getattr(msg, "content", "")
    result["content"] = content if isinstance(content, str) else str(content)

    msg_id = getattr(msg, "id", None)
    if msg_id:
        result["id"] = msg_id

    additional_kwargs = getattr(msg, "additional_kwargs", None)
    if additional_kwargs and isinstance(additional_kwargs, dict):
        reasoning_content = additional_kwargs.get("reasoning_content")
        if reasoning_content:
            result["reasoning_content"] = reasoning_content
        ts = additional_kwargs.get("_ts")
        if ts:
            result["time"] = ts
        node = additional_kwargs.get("_node")
        if node:
            result["node"] = node

    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls:
        result["tool_calls"] = [
            {"name": tc.get("name", ""), "args": tc.get("args", {}), "id": tc.get("id", "")}
            for tc in tool_calls
        ]

    tool_call_id = getattr(msg, "tool_call_id", None)
    if tool_call_id:
        result["tool_call_id"] = tool_call_id

    if "time" not in result:
        result["time"] = now_iso()

    return result


def _message_dedup_key(msg_dict: dict) -> str:
    """Build a deterministic dedup key from a serialized message dict.

    ID-first strategy: when a message has an ``id`` field, use it as
    the sole dedup key. This is robust against content differences
    between raw and filtered versions of the same message (e.g. empty
    content vs fallback text), because the LangChain-assigned id is
    immutable regardless of content mutations.

    Falls back to content-based key only when ``id`` is absent (should
    not normally happen but provides a safety net).

    Round-2 hardening: all dict accesses are defensive (``.get`` with
    fallback, ``str`` coercion before slicing). Bug 2's read-side
    dedup runs this over **every** entry loaded from disk, including
    possibly-corrupt or schema-drifted ones; a KeyError or TypeError
    here would crash the entire ``read_session`` call.
    """
    msg_id = msg_dict.get("id") or ""
    if msg_id:
        return f"id:{msg_id}"
    # No id — fall back to type+content+tool_call_id composite. Coerce
    # everything to str first so multi-modal content (list/dict) or
    # missing 'type' don't blow up the whole read path.
    msg_type = msg_dict.get("type") or "unknown"
    content_raw = msg_dict.get("content") or ""
    content_preview = str(content_raw)[:200]
    tool_call_id = msg_dict.get("tool_call_id") or ""
    return f"{msg_type}|{content_preview}|{tool_call_id}"


def build_result_summary(verification: dict) -> str:
    """Build a human-readable result summary from a verification dict."""
    if not verification or not isinstance(verification, dict):
        return ""
    level = verification.get("level", "unknown")
    l1 = verification.get("layer1", {}).get("status", "unknown")
    l2 = verification.get("layer2", {}).get("status", "unknown")
    bc = verification.get("baseline_confidence", "none")
    return f"{level} - Layer1: {l1}, Layer2: {l2}, Baseline: {bc}"


def build_verification_simple(verification: dict) -> dict | None:
    """Flatten verification dict into a compact format for API responses."""
    if not verification or not isinstance(verification, dict):
        return None
    layer1 = verification.get("layer1", {})
    layer2 = verification.get("layer2", {})
    result = {
        "level": verification.get("level", "unknown"),
        "layer1": {
            "status": layer1.get("status", "unknown"),
        },
        "layer2": {
            "status": layer2.get("status", "unknown"),
        },
        "baseline_confidence": verification.get("baseline_confidence", "none"),
        "baseline_used": verification.get("baseline_used"),
    }
    warnings = verification.get("warnings")
    if warnings:
        result["warnings"] = warnings

    checklist = verification.get("checklist", {})
    items = checklist.get("items", []) if isinstance(checklist, dict) else []
    if items:
        result["evidence"] = [
            {"step": it.get("step"), "status": it.get("status"), "detail": it.get("evidence", "")}
            for it in items if isinstance(it, dict)
        ]

    layer2_details = layer2.get("details", "")
    if layer2_details:
        result["evidence_summary"] = layer2_details

    return result


class SessionStore:
    """Persist per-task conversation messages using JSONL + periodic snapshots.

    ─── File layout ─────────────────────────────────────────────────
    Active task (mid-conversation):
      ``<task_dir>/<task_id>.json``            ← Snapshot checkpoint
      ``<task_dir>/<task_id>.jsonl``           ← Append-only increment log
      ``<task_dir>/<task_id>.jsonl.compacted`` ← Transient (only during
                                                  crashed _compact recovery)

    Finalized task:
      ``<task_dir>/<task_id>.json``  — single complete archival record
      (``.jsonl`` is deleted by ``finalize_session``)

    ─── Why two files? ──────────────────────────────────────────────
    ``append_messages`` is O(K) (K = new messages) instead of O(N)
    (N = full conversation history) that ``.json`` rewrite would cost.

    ─── Reading the complete history ────────────────────────────────
    ALWAYS go through ``read_session(task_id)`` — it combines snapshot
    + increments and dedupes. Direct ``cat <task_id>.json`` will look
    "stale" or "almost empty" because between compactions the snapshot
    lags behind. Direct ``cat <task_id>.jsonl`` shows only the increments
    since the last snapshot, not the full history. Both are
    intentional, not a bug.

    ─── Invariant ───────────────────────────────────────────────────
    ``.json["messages"]`` and ``.jsonl`` are **disjoint sets** of
    messages by design. The previous version of ``create_session``
    violated this for ``initial_messages`` (Bug 1, fixed); the
    ``_compact`` crash window can also violate it temporarily (Bug 3,
    handled by orphan ``.jsonl.compacted`` recovery + Bug 2 read-time
    dedup as defense-in-depth).

    ─── Compaction ──────────────────────────────────────────────────
    When ``.jsonl`` accumulates ``compaction_threshold`` entries
    (default 50), ``_compact`` rotates ``.jsonl`` → ``.jsonl.compacted``,
    writes a fresh ``.json`` snapshot atomically (tempfile + rename),
    then deletes the orphan. Any crash during this sequence leaves
    ``.jsonl.compacted`` on disk; ``read_session`` recovers by
    replaying it and deduping (id-based) against the snapshot.

    ─── Legacy ──────────────────────────────────────────────────────
    Files written by prior versions (snapshot-only ``.json``) remain
    readable through ``read_session``.
    """

    def __init__(self, task_dir: Path, compaction_threshold: int = 50):
        self.task_dir = task_dir.expanduser()
        self.task_dir.mkdir(parents=True, exist_ok=True)
        # In-memory buffer for active tasks, keyed by task_id.
        self._active_sessions: dict[str, dict] = {}
        self._compaction_threshold = compaction_threshold
        # In-memory JSONL line counts, keyed by task_id.  Avoids scanning
        # the file on every append — we know exactly how many lines we wrote.
        self._jsonl_counts: dict[str, int] = {}
        logger.info(f"SessionStore initialized at {self.task_dir}")

    def has_active(self, task_id: str) -> bool:
        """Return True iff ``task_id`` has an in-memory active session.

        Public counterpart to the ``_active_sessions`` dict so callers
        outside this module don't reach into a leading-underscore
        attribute. ``True`` means ``create_session`` has registered
        the task and ``finalize_session`` has not yet released it; in
        that window ``append_messages`` / ``append_raw_message`` will
        actually persist to disk. ``False`` means the task is either
        not yet bootstrapped, already finalized, or lost across a
        process restart (in-memory state is empty until a future
        recovery hook reloads from disk).
        """
        if not isinstance(task_id, str) or not task_id:
            return False
        return task_id in self._active_sessions

    def _file_path(self, task_id: str) -> Path:
        return self.task_dir / f"{task_id}.json"

    def _jsonl_path(self, task_id: str) -> Path:
        """Return the JSONL increment-log path for a task."""
        return self.task_dir / f"{task_id}.jsonl"

    def _compacted_path(self, task_id: str) -> Path:
        """Sentinel name used by ``_compact`` to atomically rename
        ``.jsonl`` aside while writing the snapshot. Recovery on next
        ``read_session`` replays this file and relies on Bug 2 dedup
        in ``read_session`` to drop duplicates if the snapshot already
        absorbed it (Bug 3)."""
        return self.task_dir / f"{task_id}.jsonl.compacted"

    def create_session(
        self,
        task_id: str,
        operation: str,
        tui_session_id: str = "",
        parent_task_id: str = "",
        baseline_messages: list = None,
        initial_messages: list = None,
    ) -> None:
        """Initialize a new task record and write the initial JSON skeleton.

        Args:
            task_id: Task identifier (also the file key).
            operation: Operation type ("inject" or "recover").
            tui_session_id: Owning TUI session id (empty for non-TUI callers).
            parent_task_id: For recover, the inject task_id it recovers.
            baseline_messages: Optional pre-existing messages whose dedup keys
                should be recorded so `append_messages` skips them (used by
                recover to drop inherited inject messages).
            initial_messages: Optional messages to write as the FIRST entries
                in the task file. This is the P0-7-6 "handoff" mechanism —
                the IntentClarificationSummary SystemMessage that marks the
                boundary between intent dialogue (stored in session file) and
                execution content (stored in task file).
        """
        _ts = now_iso()
        session_data = {
            "taskId": task_id,
            "tui_session_id": tui_session_id,
            "parent_task_id": parent_task_id,
            "operation": operation,
            "created_at": _ts,
            "finished_at": None,
            "status": "active",
            "messages": [],
            "result_summary": None,
        }
        baseline_keys = set()
        if baseline_messages:
            save_system = getattr(settings, "save_system_message", True)
            for msg in baseline_messages:
                if isinstance(msg, RemoveMessage):
                    continue
                if isinstance(msg, SystemMessage) and not save_system:
                    continue
                _msg_kwargs = getattr(msg, "additional_kwargs", None) or {}
                if _msg_kwargs.get(NO_SESSION_MARKER):
                    continue
                entry = _serialize_message_full(msg)
                baseline_keys.add(_message_dedup_key(entry))
        session_data["_baseline_keys"] = baseline_keys

        # Register session BEFORE writing initial_messages so append_messages
        # can find the task in _active_sessions.
        self._active_sessions[task_id] = session_data

        # Bug 1: write the empty skeleton FIRST, then append initial_messages
        # via the normal jsonl path. The old order (append-then-write)
        # caused both files to hold initial_messages — read_session()
        # concat would then double-count them. With this order the
        # ``.json`` is a true skeleton (messages=[]) and the ``.jsonl``
        # holds all increments including initial_messages, preserving
        # the invariant "snapshot + increments are disjoint sets".
        #
        # Round-5: use ``_atomic_write_json`` only (the old non-atomic
        # ``_write_json`` was deleted). It used to silently log +
        # return on OSError, hiding disk failures and leaving an
        # in-memory-only registration that subsequent ``read_session``
        # calls returned as None. Catching + rolling back the
        # registration on failure makes the failure explicit (raise
        # propagates to caller) and prevents the inconsistent
        # half-registered state.
        try:
            self._atomic_write_json(task_id)
        except OSError:
            # Roll back in-memory registration so subsequent
            # append_messages doesn't write a jsonl with no matching
            # .json snapshot (which would make read_session return None
            # despite the data existing on disk).
            self._active_sessions.pop(task_id, None)
            raise

        # Write initial_messages as the FIRST entries in the jsonl log.
        # This is the P0-7-6 handoff: IntentClarificationSummary marks
        # the boundary between intent dialogue (session file) and
        # execution content (stored in task file).
        if initial_messages:
            self.append_messages(task_id, initial_messages, node_name=INTENT_CLARIFICATION)

        # Opportunistically index into the TUI session forward list.
        if tui_session_id:
            try:
                from chaos_agent.memory.tui_session_store import (
                    get_global_tui_session_store,
                )
                tui_store = get_global_tui_session_store()
                if tui_store is not None:
                    tui_store.add_task(tui_session_id, task_id)
            except Exception as e:
                logger.debug(f"TUI session forward-index update skipped: {e}")

    def append_messages(self, task_id: str, messages: list, node_name: str = "") -> None:
        """Append serialized messages to the task record."""
        session = self._active_sessions.get(task_id)
        if session is None:
            logger.warning(f"Task {task_id} not found, skipping append")
            return

        baseline_keys = session.get("_baseline_keys", set())
        existing_keys = {_message_dedup_key(m) for m in session["messages"]}
        existing_keys.update(baseline_keys)

        save_system = getattr(settings, "save_system_message", True)

        new_entries = []
        for msg in messages:
            if isinstance(msg, RemoveMessage):
                continue

            if isinstance(msg, SystemMessage) and not save_system:
                continue

            _msg_kwargs = getattr(msg, "additional_kwargs", None) or {}
            if _msg_kwargs.get(NO_SESSION_MARKER):
                continue

            if node_name:
                _real_kwargs = getattr(msg, "additional_kwargs", None)
                if isinstance(_real_kwargs, dict):
                    _real_kwargs.setdefault("_node", node_name)

            entry = _serialize_message_full(msg)
            key = _message_dedup_key(entry)

            if key not in existing_keys:
                new_entries.append(entry)
                existing_keys.add(key)

        if new_entries:
            # Bug A: write disk FIRST, then update in-memory. If the
            # disk write fails, in-memory remains untouched so the next
            # append_messages call will retry (these new_entries will
            # not be in existing_keys → re-classified as new). This
            # makes disk the source of truth and prevents silent data
            # loss when the next read_session() comes from disk.
            #
            # Round-5 silent-fail audit: this catch is INTENTIONALLY
            # silent (does not raise) — unlike ``_atomic_write_json``
            # which now propagates OSError. Rationale:
            #   - Callers (memory/hook.py PreReasoningHook,
            #     tools/shell.py command-execution recorder) are
            #     fire-and-forget ARCHIVAL paths. A graph node must
            #     not fail mid-inject because the session file write
            #     glitched.
            #   - The retry-via-dedup behaviour above (in-memory
            #     unchanged → next append re-classifies as new)
            #     gives transparent self-healing for transient errors.
            #   - Persistent disk failure is still observable through
            #     the WARNING log + the read_session()-returns-stale
            #     symptom.
            try:
                self._append_to_jsonl(task_id, new_entries)
            except OSError as e:
                logger.warning(
                    f"Failed to append JSONL for task {task_id} "
                    f"(in-memory unchanged; will retry on next append): {e}"
                )
                return
            session["messages"].extend(new_entries)
            if self._needs_compaction(task_id):
                self._compact(task_id)

    def append_raw_message(self, task_id: str, msg_dict: dict) -> None:
        """Append a pre-serialized message dict directly to the task record."""
        session = self._active_sessions.get(task_id)
        if session is None:
            logger.warning(f"Task {task_id} not found, skipping raw message append")
            return

        if "time" not in msg_dict:
            msg_dict["time"] = now_iso()

        # Bug A: same write-disk-first ordering as append_messages.
        # Round-5 silent-fail audit: see append_messages for rationale
        # — this is an INTENTIONALLY-silent archival path (raise here
        # would crash the calling tool execution recorder mid-stream).
        try:
            self._append_to_jsonl(task_id, [msg_dict])
        except OSError as e:
            logger.warning(
                f"Failed to append raw message for task {task_id} "
                f"(in-memory unchanged): {e}"
            )
            return
        session["messages"].append(msg_dict)
        if self._needs_compaction(task_id):
            self._compact(task_id)

    def finalize_session(
        self,
        task_id: str,
        remaining_messages: Optional[list] = None,
        result_summary: str | dict = "",
        status: str = "completed",
    ) -> None:
        """Finalize a task record: append remaining messages, set timestamps, flush atomically.

        P0-7-5: When ``remaining_messages`` includes intent clarification
        dialogue messages (before the IntentClarificationSummary), those
        are filtered out and routed to the TUI session file instead of
        the task file. Only execution-phase messages (summary + everything
        after it) are written to the task file.
        """
        session = self._active_sessions.get(task_id)
        if session is None:
            logger.debug(f"Task {task_id} not found for finalization")
            return

        if remaining_messages:
            # P0-7-5: split at IntentClarificationSummary handoff boundary.
            # Dialogue messages → session file; execution messages → task file.
            dialogue, execution = _split_at_handoff(remaining_messages)

            # Persist dialogue messages to session file (if TUI session available)
            if dialogue:
                tui_session_id = session.get("tui_session_id", "")
                if tui_session_id:
                    try:
                        from chaos_agent.memory.tui_session_store import (
                            get_global_tui_session_store,
                        )
                        tui_store = get_global_tui_session_store()
                        if tui_store is not None:
                            tui_store.append_dialogue(tui_session_id, dialogue)
                    except Exception as e:
                        logger.debug(
                            f"Dialogue routing to session file skipped: {e}"
                        )

            # Write only execution-phase messages to the task file
            self.append_messages(task_id, execution)

        session["finished_at"] = now_iso()
        session["status"] = status
        session["result_summary"] = result_summary or None

        # Round-3: _atomic_write_json now raises on disk failure.
        # finalize is called from graph nodes via fire-and-forget /
        # try-except patterns; we catch here to avoid propagating an
        # OSError to LangGraph and aborting the surrounding flow.
        # If snapshot write fails, the data is still in ``.jsonl``
        # (which we deliberately DON'T unlink below in that case)
        # and read_session reconstructs from it.
        snapshot_ok = True
        try:
            self._atomic_write_json(task_id)
        except OSError as e:
            snapshot_ok = False
            logger.warning(
                f"Snapshot write failed during finalize for task {task_id} "
                f"(leaving .jsonl/.jsonl.compacted intact so data is still "
                f"recoverable via read_session): {e}"
            )

        # Clean up the JSONL increment log + any leftover compact
        # orphan — the final .json is the complete archival record
        # and no further appends are expected. Skip cleanup if
        # snapshot write failed (the jsonl is now the source of truth).
        if snapshot_ok:
            jsonl_path = self._jsonl_path(task_id)
            compacted_path = self._compacted_path(task_id)
            for p in (jsonl_path, compacted_path):
                try:
                    if p.exists():
                        p.unlink()
                except OSError as e:
                    logger.warning(f"Failed to delete {p.name} for task {task_id}: {e}")
        self._jsonl_counts.pop(task_id, None)

        del self._active_sessions[task_id]
        logger.info(f"Task {task_id} finalized with status={status}")

    def read_session(self, task_id: str) -> Optional[dict]:
        """Read a task session from disk, reconstructing from snapshot + JSONL.

        Reconstruction sources (in priority order):
          1. ``.json`` — snapshot (required).
          2. ``.jsonl.compacted`` — orphan from crashed _compact (Bug 3
             recovery); replay then unlink.
          3. ``.jsonl`` — live increment log.

        After concat, run Bug 2 dedup on the full list (id-based
        ``_message_dedup_key``) so any duplicates from Bug 1 / Bug 3
        crash windows are dropped instead of being returned.
        """
        json_path = self._file_path(task_id)
        jsonl_path = self._jsonl_path(task_id)
        compacted_path = self._compacted_path(task_id)

        if not json_path.exists():
            return None

        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to read task {task_id}: {e}")
            return None

        all_messages = list(data.get("messages", []))

        # Bug 3 recovery — orphan ``.jsonl.compacted`` means _compact
        # crashed mid-rotation. Replay it; Bug 2 dedup at the end drops
        # duplicates if the snapshot already absorbed this content.
        if compacted_path.exists():
            all_messages.extend(self._replay_jsonl_file(compacted_path, task_id))
            try:
                compacted_path.unlink()
            except OSError as e:
                logger.warning(f"Failed to clean orphan .jsonl.compacted: {e}")

        # Replay live JSONL increments.
        if jsonl_path.exists():
            all_messages.extend(self._replay_jsonl_file(jsonl_path, task_id))

        # Bug 2 — id-based dedup, preserving first-seen order. Saves us
        # from Bug 1 / Bug 3 crash duplicates, also defends against any
        # future write-path bug that double-writes a message.
        seen: set[str] = set()
        deduped: list[dict] = []
        for msg in all_messages:
            key = _message_dedup_key(msg)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(msg)
        data["messages"] = deduped

        return data

    @staticmethod
    def _replay_jsonl_file(path: Path, task_id: str) -> list[dict]:
        """Read all valid JSON lines from a jsonl file. Skips corrupt
        lines with a warning. Returns [] on OSError."""
        out: list[dict] = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        logger.warning(
                            f"Corrupt JSONL line in {path.name} for task {task_id}, skipping"
                        )
        except OSError as e:
            logger.warning(f"Failed to read {path.name} for task {task_id}: {e}")
        return out

    def list_tasks(self) -> list[str]:
        """List all task IDs from files on disk."""
        tasks = []
        for f in sorted(self.task_dir.glob("task-*.json")):
            tasks.append(f.stem)
        return tasks

    def _serialize_for_write(self, session: dict) -> dict:
        """Prepare session data for JSON persistence (exclude internal fields)."""
        return {k: v for k, v in session.items() if k != "_baseline_keys"}

    def _append_to_jsonl(self, task_id: str, entries: list[dict]) -> None:
        """Append serialized message entries to the JSONL increment log.

        This is the core I/O optimization: instead of rewriting the entire
        JSON file on every append, we only write new entries to a JSONL file.

        Raises ``OSError`` on disk failure so callers can decide whether to
        roll back their in-memory state. Bug A regression: previously this
        method swallowed OSError + logged warning, leaving in-memory
        ``session["messages"]`` updated while disk state was not — a
        subsequent ``read_session()`` from disk silently dropped the
        missing messages.
        """
        jsonl_path = self._jsonl_path(task_id)
        with open(jsonl_path, "a", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        self._jsonl_counts[task_id] = self._jsonl_counts.get(task_id, 0) + len(entries)

    def _needs_compaction(self, task_id: str) -> bool:
        """Check whether the JSONL line count exceeds the compaction threshold.

        Uses the in-memory counter instead of scanning the file — we know
        exactly how many lines we've written since the last compaction.
        """
        return self._jsonl_counts.get(task_id, 0) >= self._compaction_threshold

    def _compact(self, task_id: str) -> None:
        """Write a full snapshot atomically and rotate the JSONL log.

        Bug 3 — crash-safe ordering:
          0. If a previous compact crashed and left an orphan
             ``.jsonl.compacted``, MERGE it into the current ``.jsonl``
             before rotation. POSIX ``os.replace`` would otherwise
             silently overwrite the orphan and lose data — the orphan
             may be the only on-disk copy of those messages if the
             previous crash happened before ``_atomic_write_json``
             finished.
          1. Atomically rename ``.jsonl`` → ``.jsonl.compacted``.
          2. Write the full snapshot to ``.json`` via tempfile+rename.
          3. ``unlink`` ``.jsonl.compacted``.

        Any crash between steps leaves ``.jsonl.compacted`` on disk;
        ``read_session`` replays it and dedups (Bug 2).

        Bug B: only reset ``_jsonl_counts`` after the full sequence
        succeeds. If unlink fails the counter stays, next compact
        will retry the cleanup.
        """
        import os
        jsonl_path = self._jsonl_path(task_id)
        compacted_path = self._compacted_path(task_id)

        # Step 0 — orphan reclaim. If an orphan .jsonl.compacted exists
        # from a previous crashed compact, prepend its content to the
        # current .jsonl so the upcoming rename doesn't overwrite it.
        # The orphan may hold messages that aren't yet in any snapshot.
        if compacted_path.exists():
            try:
                logger.info(
                    f"Found orphan .jsonl.compacted for task {task_id}; "
                    f"merging into live jsonl before rotation"
                )
                orphan_content = compacted_path.read_text(encoding="utf-8")
                # Prepend orphan to live jsonl by rewriting it
                live_content = (
                    jsonl_path.read_text(encoding="utf-8")
                    if jsonl_path.exists()
                    else ""
                )
                merged = orphan_content
                if merged and not merged.endswith("\n"):
                    merged += "\n"
                merged += live_content
                jsonl_path.write_text(merged, encoding="utf-8")
                compacted_path.unlink()
            except OSError as e:
                logger.warning(
                    f"Failed to merge orphan .jsonl.compacted for {task_id}: {e}; "
                    f"aborting compact to avoid data loss"
                )
                return

        # Step 1: atomic rename. Skip if .jsonl doesn't exist (compact
        # called on freshly-created session with no appends).
        if jsonl_path.exists():
            try:
                os.replace(str(jsonl_path), str(compacted_path))
            except OSError as e:
                logger.warning(f"Failed to rename JSONL for compact ({task_id}): {e}")
                return

        # Step 2: write snapshot atomically. If this fails the orphan
        # .jsonl.compacted still holds the rotated content — MUST NOT
        # proceed to step 3 (would unlink the only on-disk copy).
        # Round-3 fix: _atomic_write_json now raises; without this
        # catch+return, unconditional unlink in step 3 caused true
        # data loss when snapshot write failed.
        try:
            self._atomic_write_json(task_id)
        except OSError as e:
            logger.warning(
                f"Snapshot write failed during compact for task {task_id} "
                f"(orphan .jsonl.compacted preserved, counter not reset; "
                f"read_session will replay orphan, next append retries compact): {e}"
            )
            return

        # Step 3: delete the orphan. If this fails leave it — the next
        # read_session replays + dedups (Bug 2), no data loss.
        try:
            if compacted_path.exists():
                compacted_path.unlink()
        except OSError as e:
            logger.warning(
                f"Compact cleanup failed for task {task_id} "
                f"(snapshot OK, orphan .jsonl.compacted will be "
                f"replayed and deduped on next read): {e}"
            )
            return

        self._jsonl_counts[task_id] = 0

    def _atomic_write_json(self, task_id: str) -> None:
        """Write task data using atomic tempfile + rename.

        Round-3 fix: raises ``OSError`` on disk failure (was silently
        log + return). Callers MUST catch and decide what to do — in
        particular ``_compact`` must NOT proceed to unlink the orphan
        ``.jsonl.compacted`` if the snapshot write failed.

        Round-4 fix: ``os.replace`` failure (e.g. cross-device boundary
        on NFS / overlayfs, target locked on Windows, target dir
        permission flip) used to leak the ``.json.tmp`` file forever.
        Now any raise path through this function cleans up the tempfile
        via try/finally + success flag.
        """
        session = self._active_sessions.get(task_id)
        if session is None:
            return
        file_path = self._file_path(task_id)
        data = self._serialize_for_write(session)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self.task_dir), suffix=".json.tmp"
        )
        import os
        success = False
        try:
            with open(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
            os.replace(tmp_path, str(file_path))
            success = True
        finally:
            if not success:
                # Any raise path (json.dump error, os.replace error,
                # disk full mid-write) lands here. Clean the tempfile
                # so a partial / orphaned ``.json.tmp`` doesn't
                # accumulate in task_dir across retries.
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
