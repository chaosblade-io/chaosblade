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

    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls:
        result["tool_calls"] = [
            {"name": tc.get("name", ""), "args": tc.get("args", {})}
            for tc in tool_calls
        ]

    tool_call_id = getattr(msg, "tool_call_id", None)
    if tool_call_id:
        result["tool_call_id"] = tool_call_id

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
    """
    msg_id = msg_dict.get("id", "")
    if msg_id:
        return f"id:{msg_id}"
    # No id — fall back to type+content+tool_call_id composite
    content_preview = msg_dict.get("content", "")[:200]
    tool_call_id = msg_dict.get("tool_call_id", "")
    return f"{msg_dict['type']}|{content_preview}|{tool_call_id}"


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
    return result


class SessionStore:
    """Persist per-task conversation messages using JSONL + periodic snapshots.

    File layout for an active task:
      - ``<task_dir>/<task_id>.json``  — Snapshot (compaction checkpoint).
      - ``<task_dir>/<task_id>.jsonl`` — Append-only increment log.

    On append, new messages are written to the ``.jsonl`` file (O(K) I/O
    instead of rewriting the full JSON).  When the JSONL exceeds a
    configurable threshold, a full snapshot is written atomically and
    the JSONL is truncated.

    On finalization, a single complete ``.json`` is written atomically
    and the ``.jsonl`` is deleted.  Legacy ``.json``-only files from
    before this change are still readable by ``read_session()``.
    """

    def __init__(self, task_dir: Path, compaction_threshold: int = 500):
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

        # Write initial_messages as the FIRST entries in the task file.
        # This is the P0-7-6 handoff: IntentClarificationSummary marks
        # the boundary between intent dialogue (session file) and
        # execution content (stored in task file).
        if initial_messages:
            self.append_messages(task_id, initial_messages)

        # Flush the initial state (including any initial_messages) to disk.
        self._write_json(task_id)

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

    def append_messages(self, task_id: str, messages: list) -> None:
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

            entry = _serialize_message_full(msg)
            key = _message_dedup_key(entry)

            if key not in existing_keys:
                new_entries.append(entry)
                existing_keys.add(key)

        if new_entries:
            session["messages"].extend(new_entries)
            self._append_to_jsonl(task_id, new_entries)
            if self._needs_compaction(task_id):
                self._compact(task_id)

    def append_raw_message(self, task_id: str, msg_dict: dict) -> None:
        """Append a pre-serialized message dict directly to the task record."""
        session = self._active_sessions.get(task_id)
        if session is None:
            logger.warning(f"Task {task_id} not found, skipping raw message append")
            return

        session["messages"].append(msg_dict)
        self._append_to_jsonl(task_id, [msg_dict])
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
            logger.warning(f"Task {task_id} not found for finalization")
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

        self._atomic_write_json(task_id)

        # Clean up the JSONL increment log — the final .json is the
        # complete archival record and no further appends are expected.
        jsonl_path = self._jsonl_path(task_id)
        try:
            if jsonl_path.exists():
                jsonl_path.unlink()
        except OSError as e:
            logger.warning(f"Failed to delete JSONL for task {task_id}: {e}")
        self._jsonl_counts.pop(task_id, None)

        del self._active_sessions[task_id]
        logger.info(f"Task {task_id} finalized with status={status}")

    def read_session(self, task_id: str) -> Optional[dict]:
        """Read a task session from disk, reconstructing from snapshot + JSONL.

        Handles three cases:
        1. Finalized / legacy task (only .json exists) — read directly.
        2. Active task (.json snapshot + .jsonl increments) — replay JSONL.
        3. Corrupt JSONL lines — skip with a warning.
        """
        json_path = self._file_path(task_id)
        jsonl_path = self._jsonl_path(task_id)

        if not json_path.exists():
            return None

        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to read task {task_id}: {e}")
            return None

        # No JSONL — finalized or legacy format, return as-is.
        if not jsonl_path.exists():
            return data

        # Replay JSONL increments onto the snapshot.
        try:
            incremental_messages = []
            with open(jsonl_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        incremental_messages.append(json.loads(line))
                    except json.JSONDecodeError:
                        logger.warning(
                            f"Corrupt JSONL line in task {task_id}, skipping"
                        )
            data["messages"] = data.get("messages", []) + incremental_messages
        except OSError as e:
            logger.warning(f"Failed to read JSONL for task {task_id}: {e}")

        return data

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
        """
        jsonl_path = self._jsonl_path(task_id)
        try:
            with open(jsonl_path, "a", encoding="utf-8") as f:
                for entry in entries:
                    f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            self._jsonl_counts[task_id] = self._jsonl_counts.get(task_id, 0) + len(entries)
        except OSError as e:
            logger.warning(f"Failed to append JSONL for task {task_id}: {e}")

    def _needs_compaction(self, task_id: str) -> bool:
        """Check whether the JSONL line count exceeds the compaction threshold.

        Uses the in-memory counter instead of scanning the file — we know
        exactly how many lines we've written since the last compaction.
        """
        return self._jsonl_counts.get(task_id, 0) >= self._compaction_threshold

    def _compact(self, task_id: str) -> None:
        """Write a full snapshot atomically and truncate the JSONL log.

        Uses atomic write (tempfile + rename) for the snapshot so the
        checkpoint is always valid.  Truncates (not deletes) the JSONL
        to avoid orphaned file-descriptor writes from concurrent appends.
        """
        self._atomic_write_json(task_id)
        jsonl_path = self._jsonl_path(task_id)
        try:
            jsonl_path.write_text("", encoding="utf-8")
        except OSError as e:
            logger.warning(f"Failed to truncate JSONL for task {task_id}: {e}")
        self._jsonl_counts[task_id] = 0

    def _write_json(self, task_id: str) -> None:
        """Write task data to JSON file (non-atomic, for initial skeleton only).

        Used exclusively by ``create_session`` to write the empty-messages
        skeleton.  All subsequent appends go through ``_append_to_jsonl``.
        """
        session = self._active_sessions.get(task_id)
        if session is None:
            return
        file_path = self._file_path(task_id)
        try:
            data = self._serialize_for_write(session)
            file_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning(f"Failed to write task {task_id}: {e}")

    def _atomic_write_json(self, task_id: str) -> None:
        """Write task data using atomic tempfile + rename (for finalization)."""
        session = self._active_sessions.get(task_id)
        if session is None:
            return
        file_path = self._file_path(task_id)
        try:
            data = self._serialize_for_write(session)
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self.task_dir), suffix=".json.tmp"
            )
            try:
                with open(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2, default=str)
            except Exception:
                import os
                os.unlink(tmp_path)
                raise
            import os
            os.replace(tmp_path, str(file_path))
        except OSError as e:
            logger.warning(f"Failed to atomic-write task {task_id}: {e}")
