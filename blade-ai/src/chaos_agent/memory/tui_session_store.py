"""TUI session store: JSON + JSONL per TUI process.

Tracks the lifecycle of a single TUI session: start/finish timestamps,
environment snapshot (cluster/namespace), the task IDs that ran inside
the session, intent clarification dialogue messages, and aggregate stats.

File layout for an active session:
  - ``<session_dir>/<tui_session_id>.json``  — Snapshot (compaction checkpoint).
  - ``<session_dir>/<tui_session_id>.jsonl`` — Append-only increment log.

On append, new messages are written to the ``.jsonl`` file (O(K) I/O
instead of rewriting the full JSON).  When the JSONL exceeds a
configurable threshold, a full snapshot is written atomically and
the JSONL is truncated.

On finalization, a single complete ``.json`` is written atomically
and the ``.jsonl`` is deleted.  Legacy ``.json``-only files from
before this change are still readable by ``read()``.

Intent clarification dialogue messages are stored in the ``messages``
field of the session file. Per-task execution content (agent_loop,
execute_loop, verifier, recover) lives under
`memory/tasks/<task_id>.json` (see `SessionStore`). This separation
ensures that dialogue and execution content don't get mixed together.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

from chaos_agent.utils.time import now_iso

logger = logging.getLogger(__name__)

_global_tui_session_store: Optional["TuiSessionStore"] = None


def set_global_tui_session_store(store: "TuiSessionStore") -> None:
    """Register the global TuiSessionStore singleton."""
    global _global_tui_session_store
    _global_tui_session_store = store


def get_global_tui_session_store() -> Optional["TuiSessionStore"]:
    """Retrieve the global TuiSessionStore singleton (None if not yet set)."""
    return _global_tui_session_store


class TuiSessionStore:
    def __init__(self, session_dir: Path, compaction_threshold: int = 50):
        self.session_dir = session_dir.expanduser()
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._compaction_threshold = compaction_threshold
        # In-memory buffers for active sessions
        self._active_sessions: dict[str, dict] = {}
        # In-memory dedup key sets, keyed by tui_session_id.
        # Avoids re-reading the file on every append_dialogue call.
        self._existing_keys: dict[str, set] = {}
        # In-memory JSONL line counts, keyed by tui_session_id.
        self._jsonl_counts: dict[str, int] = {}
        logger.info(f"TuiSessionStore initialized at {self.session_dir}")

    def _file_path(self, tui_session_id: str) -> Path:
        return self.session_dir / f"{tui_session_id}.json"

    def _jsonl_path(self, tui_session_id: str) -> Path:
        """Return the JSONL increment-log path for a session."""
        return self.session_dir / f"{tui_session_id}.jsonl"

    def create(
        self,
        tui_session_id: str,
        cluster_name: str = "",
        namespace: str = "",
    ) -> None:
        """Initialize a new TUI session file."""
        _ts = now_iso()
        data = {
            "tui_session_id": tui_session_id,
            "started_at": _ts,
            "finished_at": None,
            "status": "active",
            "cluster_name": cluster_name,
            "namespace": namespace,
            "task_ids": [],
            "messages": [],
            "stats": {
                "message_count": 0,
                "injection_count": 0,
                "injection_success": 0,
                "injection_fail": 0,
                "recovery_count": 0,
            },
        }
        # Register in-memory before writing
        self._active_sessions[tui_session_id] = data
        self._existing_keys[tui_session_id] = set()
        self._jsonl_counts[tui_session_id] = 0
        self._write_json(tui_session_id)

    def add_task(self, tui_session_id: str, task_id: str) -> None:
        """Append a task_id to the session's task list (no-op if already present)."""
        session = self._active_sessions.get(tui_session_id)
        if session is None:
            # Load from disk if not in memory
            session = self._load_from_disk(tui_session_id)
            if session is None:
                logger.warning(
                    f"add_task: session {tui_session_id} missing; creating fresh"
                )
                self.create(tui_session_id)
                session = self._active_sessions[tui_session_id]

        task_ids = session.setdefault("task_ids", [])
        if task_id and task_id not in task_ids:
            task_ids.append(task_id)
            self._write_json(tui_session_id)

    def append_dialogue(self, tui_session_id: str, messages: list) -> None:
        """Append intent clarification dialogue messages to the session file.

        Messages are added via JSONL incremental append (O(K) I/O) instead
        of rewriting the full JSON. Deduplication uses ID-based keys —
        messages whose dedup key already exists in the in-memory set are
        silently skipped. This is the sole write source for session files
        during intent clarification (PreReasoningHook no longer writes
        here).
        """
        session = self._active_sessions.get(tui_session_id)
        if session is None:
            # Load from disk if not in memory
            session = self._load_from_disk(tui_session_id)
            if session is None:
                logger.warning(
                    f"append_dialogue: session {tui_session_id} missing; skipping"
                )
                return

        existing_keys = self._existing_keys.get(tui_session_id, set())
        from chaos_agent.memory.session_store import _message_dedup_key

        new_entries = []
        for msg in messages:
            serialized = self._serialize_dialogue_message(msg)
            key = _message_dedup_key(serialized)
            if key in existing_keys:
                continue
            new_entries.append(serialized)
            existing_keys.add(key)

        if new_entries:
            session.setdefault("messages", []).extend(new_entries)
            session["stats"]["message_count"] = len(session["messages"])
            self._existing_keys[tui_session_id] = existing_keys
            self._append_to_jsonl(tui_session_id, new_entries)
            # Flush the full snapshot to ``.json`` after every append so
            # the snapshot is never out-of-sync with ``.jsonl``. Why this
            # matters: a reader of ``~/.blade-ai/memory/sessions/<sid>.json``
            # mid-session (the user opening the file in an editor, or
            # any tool that consumes the audit trail) was previously
            # seeing a snapshot frozen at the last ``add_task`` /
            # ``update_stats`` call — which on a chat-only turn means
            # nothing, so the just-streamed dialogue lived only in
            # ``.jsonl``. The cost here is one ~50 KB JSON write per
            # turn (~1 ms on SSD); cheap enough that the perf savings
            # of a JSONL-only mid-session path don't justify the UX
            # surprise of "I asked the agent and the file didn't
            # update". Atomic-write is reserved for finalize where
            # crash-safety actually matters.
            self._write_json(tui_session_id)
            if self._needs_compaction(tui_session_id):
                self._compact(tui_session_id)

    def read_dialogue(self, tui_session_id: str) -> list[dict]:
        """Read all intent clarification dialogue messages from session file.

        Returns the full messages list (empty list if session doesn't exist).
        Useful for /history command, crash recovery, and offline auditing.
        """
        data = self.read(tui_session_id)
        if data is None:
            return []
        return data.get("messages", [])

    def _serialize_dialogue_message(self, msg) -> dict:
        """Serialize a LangChain message to JSON for session storage.

        Reuses the module-level ``_serialize_message_full`` format for
        consistency so that messages in session files and task files
        share the same schema.
        """
        from chaos_agent.memory.session_store import _serialize_message_full

        return _serialize_message_full(msg)

    def update_stats(self, tui_session_id: str, stats: dict) -> None:
        """Merge a stats dict into the session file's stats block."""
        session = self._active_sessions.get(tui_session_id)
        if session is None:
            session = self._load_from_disk(tui_session_id)
            if session is None:
                logger.warning(
                    f"update_stats: session {tui_session_id} missing; creating fresh"
                )
                self.create(tui_session_id)
                session = self._active_sessions[tui_session_id]

        cur = session.setdefault("stats", {})
        cur.update(stats)
        self._write_json(tui_session_id)

    def update_env(
        self,
        tui_session_id: str,
        cluster_name: Optional[str] = None,
        namespace: Optional[str] = None,
    ) -> None:
        """Patch cluster_name / namespace after create (values may be set later)."""
        session = self._active_sessions.get(tui_session_id)
        if session is None:
            session = self._load_from_disk(tui_session_id)
            if session is None:
                return
        if cluster_name is not None:
            session["cluster_name"] = cluster_name
        if namespace is not None:
            session["namespace"] = namespace
        self._write_json(tui_session_id)

    def list_active(self) -> list[str]:
        """Return a snapshot of session IDs currently in the in-memory
        active set. Returned as a fresh list so callers can iterate +
        mutate (e.g. finalize each) without ``RuntimeError: dictionary
        changed size during iteration``. Used by the FastAPI server's
        lifespan shutdown sweep to finalize any abandoned sessions.
        """
        return list(self._active_sessions.keys())

    def finalize(self, tui_session_id: str, status: str = "completed") -> None:
        """Mark the session as finished and atomically flush."""
        session = self._active_sessions.get(tui_session_id)
        if session is None:
            session = self._load_from_disk(tui_session_id)
            if session is None:
                return
        session["finished_at"] = now_iso()
        session["status"] = status
        self._atomic_write_json(tui_session_id)

        # Clean up JSONL — the final .json is the complete archival record
        jsonl_path = self._jsonl_path(tui_session_id)
        try:
            if jsonl_path.exists():
                jsonl_path.unlink()
        except OSError as e:
            logger.warning(f"Failed to delete JSONL for session {tui_session_id}: {e}")

        # Remove from in-memory buffers
        self._active_sessions.pop(tui_session_id, None)
        self._existing_keys.pop(tui_session_id, None)
        self._jsonl_counts.pop(tui_session_id, None)

    def read(self, tui_session_id: str) -> Optional[dict]:
        """Read a session from disk, reconstructing from snapshot + JSONL.

        Handles three cases:
        1. Finalized / legacy session (only .json exists) — read directly.
        2. Active session (.json snapshot + .jsonl increments) — replay JSONL.
        3. In-memory active session — return cached data.
        """
        # Fast path: in-memory buffer
        session = self._active_sessions.get(tui_session_id)
        if session is not None:
            return session

        json_path = self._file_path(tui_session_id)
        jsonl_path = self._jsonl_path(tui_session_id)

        if not json_path.exists():
            return None

        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to read session {tui_session_id}: {e}")
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
                            f"Corrupt JSONL line in session {tui_session_id}, skipping"
                        )
            data["messages"] = data.get("messages", []) + incremental_messages
        except OSError as e:
            logger.warning(f"Failed to read JSONL for session {tui_session_id}: {e}")

        return data

    # ------------------------------------------------------------------
    # Private: JSONL incremental write
    # ------------------------------------------------------------------

    def _append_to_jsonl(self, tui_session_id: str, entries: list[dict]) -> None:
        """Append serialized message entries to the JSONL increment log."""
        jsonl_path = self._jsonl_path(tui_session_id)
        try:
            with open(jsonl_path, "a", encoding="utf-8") as f:
                for entry in entries:
                    f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            self._jsonl_counts[tui_session_id] = (
                self._jsonl_counts.get(tui_session_id, 0) + len(entries)
            )
        except OSError as e:
            logger.warning(f"Failed to append JSONL for session {tui_session_id}: {e}")

    def _needs_compaction(self, tui_session_id: str) -> bool:
        """Check whether the JSONL line count exceeds the compaction threshold."""
        return self._jsonl_counts.get(tui_session_id, 0) >= self._compaction_threshold

    def _compact(self, tui_session_id: str) -> None:
        """Write a full snapshot atomically and truncate the JSONL log."""
        self._atomic_write_json(tui_session_id)
        jsonl_path = self._jsonl_path(tui_session_id)
        try:
            jsonl_path.write_text("", encoding="utf-8")
        except OSError as e:
            logger.warning(f"Failed to truncate JSONL for session {tui_session_id}: {e}")
        self._jsonl_counts[tui_session_id] = 0

    def _load_from_disk(self, tui_session_id: str) -> Optional[dict]:
        """Load session data from disk and populate in-memory buffers."""
        data = self.read(tui_session_id)
        if data is None:
            return None

        self._active_sessions[tui_session_id] = data

        # Rebuild existing_keys from the loaded messages
        from chaos_agent.memory.session_store import _message_dedup_key
        msg_list = data.get("messages", [])
        existing_keys = {_message_dedup_key(m) for m in msg_list}
        self._existing_keys[tui_session_id] = existing_keys
        self._jsonl_counts[tui_session_id] = 0

        return data

    def _write_json(self, tui_session_id: str) -> None:
        """Write session data to JSON file (non-atomic, for skeleton/updates)."""
        session = self._active_sessions.get(tui_session_id)
        if session is None:
            return
        file_path = self._file_path(tui_session_id)
        try:
            file_path.write_text(
                json.dumps(session, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning(f"Failed to write session {tui_session_id}: {e}")

    def _atomic_write_json(self, tui_session_id: str) -> None:
        """Write session data using atomic tempfile + rename (for finalization)."""
        session = self._active_sessions.get(tui_session_id)
        if session is None:
            return
        file_path = self._file_path(tui_session_id)
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self.session_dir), suffix=".json.tmp"
            )
            try:
                with open(fd, "w", encoding="utf-8") as f:
                    json.dump(session, f, ensure_ascii=False, indent=2, default=str)
            except Exception:
                os.unlink(tmp_path)
                raise
            os.replace(tmp_path, str(file_path))
        except OSError as e:
            logger.warning(f"Failed to atomic-write session {tui_session_id}: {e}")