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
import re
import tempfile
import threading
from pathlib import Path
from typing import Optional

from chaos_agent.utils.time import now_iso

logger = logging.getLogger(__name__)

# Whitelist for ``tui_session_id`` values, wherever one is composed
# into a filesystem path (this store's ``_file_path`` /
# ``_events_jsonl_path``, the server's memory/sessions routes).
# Server-side ``createSession`` produces ``sess_<12 hex>``; we accept
# the same shape plus a generous superset (alnum + dash + underscore,
# max 128 chars) so a user-managed deployment can pick its own id
# format. Anything else (slashes, dots, ``..``, percent-encoded path
# separators after FastAPI decodes) must be rejected BEFORE the value
# reaches a Path — routes import this single source so the memory and
# sessions surfaces can never drift apart on the rule.
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")

_global_tui_session_store: Optional["TuiSessionStore"] = None


# ── resumable-listing helpers ─────────────────────────────────────────
#
# Shared by ``TuiSessionStore.list_resumable_sessions`` (single source
# for the /resume picker row shape). Moved here from the memory route so
# the CLI's ``blade-ai resume`` picker uses the exact same assembly.

# How much of the events jsonl ``_first_user_input`` reads while
# hunting for the first ``user_input`` event. In practice user_input is
# the FIRST event of every session (turn_event_stream sidewrites it
# before the intent graph streams), so 64KB is several thousand lines
# of headroom.
_FIRST_INPUT_HEAD_BYTES = 64 * 1024

# Rendering cap for the first-input snippet echoed in the picker row.
_FIRST_INPUT_MAX_CHARS = 60


def _count_events_file_lines(path: Path) -> int:
    """Line count without JSON parsing — a resumable row only needs the
    event total, not the parsed events. Binary mode: counting on bytes
    avoids decode errors mid-file (corrupt tail lines still count)."""
    n = 0
    with open(path, "rb") as f:
        for _ in f:
            n += 1
    return n


def _first_user_input(path: Path) -> str:
    """First ``user_input`` content from the head of the events file,
    truncated for display. Empty string when the head window doesn't
    contain one (session crashed before the first turn, or the first
    input landed past the window — the latter doesn't happen today)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(_FIRST_INPUT_HEAD_BYTES)
    except OSError:
        return ""
    for line in head.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("event_type") == "user_input":
            content = (rec.get("data") or {}).get("content") or ""
            if isinstance(content, str):
                snippet = " ".join(content.split())
                if len(snippet) > _FIRST_INPUT_MAX_CHARS:
                    return snippet[: _FIRST_INPUT_MAX_CHARS - 1] + "…"
                return snippet
    return ""


def set_global_tui_session_store(store: "TuiSessionStore") -> None:
    """Register the global TuiSessionStore singleton."""
    global _global_tui_session_store
    _global_tui_session_store = store


def get_global_tui_session_store() -> Optional["TuiSessionStore"]:
    """Retrieve the global TuiSessionStore singleton (None if not yet set)."""
    return _global_tui_session_store


def persist_node_dialogue(tui_session_id: str, messages: list) -> None:
    """Append node-authored messages to the TUI session file, best-effort.

    Nodes that end a conversational turn with their own text — rather than a
    streamed LLM reply — have to write it themselves. The turn-level fallback in
    ``session_finalizer`` only reaches the session file when the caller passes a
    ``tui_session_store``, which the CLI runner does and the server route does
    not; the server path is additionally gated on an operational ``task_id`` that
    a pure dialogue turn never has. So on the server a node's text lands in graph
    state and on the TUI, and nowhere on disk.

    Never raises: an audit write must not fail a turn that already decided what
    to do. Silent when there is no session (non-TUI callers) or no store.
    """
    if not tui_session_id or not messages:
        return
    try:
        store = get_global_tui_session_store()
        if store is not None:
            store.append_dialogue(tui_session_id, list(messages))
    except Exception as e:  # noqa: BLE001
        logger.debug(f"Node dialogue persistence skipped: {e}")


class TuiSessionStore:
    # Maximum number of sessions to keep in memory. When exceeded, the oldest
    # sessions (by started_at) are evicted from in-memory buffers. Disk files
    # are NOT deleted — only the in-memory cache is trimmed. Subsequent access
    # to evicted sessions will reload from disk via _load_from_disk().
    _MAX_ACTIVE_SESSIONS = 200

    def __init__(self, session_dir: Path, compaction_threshold: int = 50):
        self.session_dir = session_dir.expanduser()
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._events_dir = self.session_dir.parent / "tui"
        self._events_dir.mkdir(parents=True, exist_ok=True)
        self._compaction_threshold = compaction_threshold
        # Thread lock for protecting compound operations on _active_sessions.
        # RLock (reentrant) because add_task() -> create() nests lock acquisition.
        # In TUI (single-thread) this is effectively a no-op.
        self._lock = threading.RLock()
        # Token/thinking stream buffers for event coalescing.
        # Keyed by tui_session_id. Flushed on non-streamable event.
        self._token_buf: dict[str, str] = {}
        self._thinking_buf: dict[str, str] = {}
        self._buf_meta: dict[str, dict] = {}  # ts, source, task_id from first chunk
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

    def _events_jsonl_path(self, tui_session_id: str) -> Path:
        """Return the events JSONL path (full StreamEvent audit trail).

        Stored under ``<memory_dir>/tui/`` (sibling of sessions/ and tasks/).
        """
        return self._events_dir / f"{tui_session_id}.events.jsonl"

    def _maybe_evict(self) -> None:
        """Evict oldest sessions from in-memory cache when limit is exceeded.

        MUST be called while self._lock is held. Disk files are NOT deleted —
        only the in-memory buffers are trimmed. Subsequent access to evicted
        sessions will transparently reload from disk via _load_from_disk().
        """
        if len(self._active_sessions) < self._MAX_ACTIVE_SESSIONS:
            return
        # Sort by started_at, evict oldest 20%
        evict_count = max(1, len(self._active_sessions) // 5)
        sorted_ids = sorted(
            self._active_sessions.keys(),
            key=lambda sid: self._active_sessions[sid].get("started_at", ""),
        )
        for sid in sorted_ids[:evict_count]:
            # Flush to disk before evicting (best-effort)
            try:
                self._write_json(sid)
            except Exception:
                pass
            self._active_sessions.pop(sid, None)
            self._existing_keys.pop(sid, None)
            self._jsonl_counts.pop(sid, None)
        logger.debug(
            "TuiSessionStore: evicted %d sessions from memory (remaining: %d)",
            evict_count, len(self._active_sessions),
        )

    def create(
        self,
        tui_session_id: str,
        cluster_name: str = "",
        namespace: str = "",
        conversation_thread_id: str = "",
    ) -> None:
        """Initialize a new TUI session file.

        ``conversation_thread_id``: the LangGraph thread the session's
        dialogue checkpoints under. Written at create time so a session
        resume after a server restart can recover the thread binding
        from disk (see ``update_thread_id`` for the later-rebind path —
        empty here means the caller hasn't allocated a thread yet and
        turn.py will mint + backfill one on the first turn).
        """
        with self._lock:
            # Double-check inside lock to prevent duplicate create
            if tui_session_id in self._active_sessions:
                return
            self._maybe_evict()
            _ts = now_iso()
            data = {
                "tui_session_id": tui_session_id,
                "started_at": _ts,
                "finished_at": None,
                "status": "active",
                "cluster_name": cluster_name,
                "namespace": namespace,
                "conversation_thread_id": conversation_thread_id,
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

    def update_thread_id(self, tui_session_id: str, thread_id: str) -> None:
        """Persist the session's LangGraph conversation thread binding.

        Called when turn.py mints a thread for a session whose stored
        binding is empty — the resume-after-restart path where the
        in-memory SessionStore was rebuilt from disk without a thread
        (legacy files predate the field). Not called for the normal
        create flow: create() writes the binding up-front.

        Follows update_stats' load-else-create pattern so a server that
        never loaded this session into memory (fresh process, resume
        request arrives) still persists the field.
        """
        with self._lock:
            session = self._active_sessions.get(tui_session_id)
            if session is None:
                session = self._load_from_disk(tui_session_id)
                if session is None:
                    logger.warning(
                        f"update_thread_id: session {tui_session_id} missing; skipping"
                    )
                    return
            session["conversation_thread_id"] = thread_id
            self._write_json(tui_session_id)

    def add_task(self, tui_session_id: str, task_id: str) -> None:
        """Append a task_id to the session's task list (no-op if already present)."""
        with self._lock:
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
        with self._lock:
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
        self._flush_event_buffers(tui_session_id)
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

    def update_status(self, tui_session_id: str, status: str) -> None:
        """Patch ONLY the status field, keeping the session live.

        The resume path uses this to flip a finalized/completed record
        back to ``active`` — unlike finalize() it must NOT stamp
        finished_at, drop the .jsonl increment log, or evict the
        in-memory buffers (the session is about to keep appending).
        """
        with self._lock:
            session = self._active_sessions.get(tui_session_id)
            if session is None:
                session = self._load_from_disk(tui_session_id)
                if session is None:
                    logger.warning(
                        f"update_status: session {tui_session_id} missing; skipping"
                    )
                    return
            session["status"] = status
            self._write_json(tui_session_id)

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

        # Replay JSONL increments onto the snapshot, deduplicating entries
        # that already exist in the snapshot (append_dialogue writes both
        # .json and .jsonl, so they overlap in the normal non-crash path).
        try:
            from chaos_agent.memory.session_store import _message_dedup_key

            existing_keys = {_message_dedup_key(m) for m in data.get("messages", [])}
            incremental_messages = []
            with open(jsonl_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning(
                            f"Corrupt JSONL line in session {tui_session_id}, skipping"
                        )
                        continue
                    key = _message_dedup_key(entry)
                    if key not in existing_keys:
                        incremental_messages.append(entry)
                        existing_keys.add(key)
            if incremental_messages:
                data["messages"] = data.get("messages", []) + incremental_messages
        except OSError as e:
            logger.warning(f"Failed to read JSONL for session {tui_session_id}: {e}")

        return data

    # ------------------------------------------------------------------
    # Event audit trail (Display Store)
    # ------------------------------------------------------------------

    _STREAMABLE = frozenset(("token", "thinking"))
    _SKIP = frozenset(("node_start", "node_end"))

    def append_event(self, tui_session_id: str, event_dict: dict) -> None:
        """Append a StreamEvent record to the events JSONL.

        Token and thinking events are coalesced into a single record
        per contiguous run (60 thinking chunks → 1 merged record).
        node_start/node_end are dropped (internal signals, not needed
        for TUI recovery). All other events are written immediately.
        """
        event_type = event_dict.get("event_type", "")

        if event_type in self._SKIP:
            return

        if event_type in self._STREAMABLE:
            content = (event_dict.get("data") or {}).get("content", "")
            buf_key = f"_{'token' if event_type == 'token' else 'thinking'}_buf"
            buf = getattr(self, buf_key)
            buf[tui_session_id] = buf.get(tui_session_id, "") + content
            if tui_session_id not in self._buf_meta:
                self._buf_meta[tui_session_id] = {
                    "ts": event_dict.get("ts", ""),
                    "source": event_dict.get("source", ""),
                    "task_id": event_dict.get("task_id", ""),
                }
            return

        # Non-streamable event: flush buffers first, then write this event
        self._flush_event_buffers(tui_session_id)
        self._write_event(tui_session_id, event_dict)

    def flush_events(self, tui_session_id: str) -> None:
        """Flush any pending token/thinking buffers to disk."""
        self._flush_event_buffers(tui_session_id)

    def _flush_event_buffers(self, tui_session_id: str) -> None:
        meta = self._buf_meta.pop(tui_session_id, None)
        if meta is None:
            return
        for event_type, buf_dict in (
            ("thinking", self._thinking_buf),
            ("token", self._token_buf),
        ):
            content = buf_dict.pop(tui_session_id, "")
            if content:
                self._write_event(tui_session_id, {
                    "ts": meta.get("ts", ""),
                    "source": meta.get("source", ""),
                    "task_id": meta.get("task_id", ""),
                    "event_type": event_type,
                    "data": {"content": content},
                })

    def _write_event(self, tui_session_id: str, event_dict: dict) -> None:
        path = self._events_jsonl_path(tui_session_id)
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(
                    json.dumps(event_dict, ensure_ascii=False, default=str)
                    + "\n"
                )
        except OSError as e:
            logger.warning(
                f"Failed to append event for session {tui_session_id}: {e}"
            )

    def read_events(self, tui_session_id: str) -> list[dict]:
        """Read the full event audit trail for a session.

        Returns an empty list when the file doesn't exist or is
        unreadable. Corrupt lines are skipped with a warning.
        """
        path = self._events_jsonl_path(tui_session_id)
        if not path.exists():
            return []
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
                            f"Corrupt event line in session {tui_session_id}, skipping"
                        )
        except OSError as e:
            logger.warning(
                f"Failed to read events for session {tui_session_id}: {e}"
            )
        return out

    def has_events(self, tui_session_id: str) -> bool:
        """Whether the session carries an events jsonl on disk — the
        resume source of truth (the session JSON is decoration). Used
        by the CLI's ``blade-ai resume -i <sid>`` to fail before the
        TUI/server boot when the sid names nothing resumable."""
        return self._events_jsonl_path(tui_session_id).exists()

    def list_resumable_sessions(self, limit: int = 50) -> list[dict]:
        """Sessions that carry an events jsonl on disk, newest first.

        Single source for the ``/resume`` picker row shape — the
        ``GET /api/v1/memory/resumable`` route (TUI's ``/resume`` list)
        and the CLI's ``blade-ai resume`` bare listing both call
        this, so the two surfaces cannot drift apart. Row shape (all
        display-oriented; no conversation internals):

          tui_session_id, size_bytes, event_count, started_at,
          modified_at, first_input

        Raises OSError when the events directory cannot be scanned —
        callers decide whether that is a fail envelope (route) or a
        stderr message (CLI).
        """
        candidates = sorted(
            (
                p
                for p in self._events_dir.glob("*.events.jsonl")
                if p.is_file()
            ),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:limit]
        rows: list[dict] = []
        for path in candidates:
            sid = path.name[: -len(".events.jsonl")]
            try:
                st = path.stat()
            except OSError:
                continue
            # started_at from the session JSON when present; file mtime is
            # the fallback for sessions whose JSON was deleted (the events
            # file is the resume source of truth, the JSON is decoration).
            started_at = ""
            session_json = self.session_dir / f"{sid}.json"
            if session_json.exists():
                try:
                    data = json.loads(
                        session_json.read_text(encoding="utf-8")
                    )
                    started_at = data.get("started_at") or ""
                except (OSError, ValueError):
                    started_at = ""
            rows.append(
                {
                    "tui_session_id": sid,
                    "size_bytes": st.st_size,
                    "event_count": _count_events_file_lines(path),
                    "started_at": started_at,
                    "modified_at": st.st_mtime,
                    "first_input": _first_user_input(path),
                }
            )
        return rows

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