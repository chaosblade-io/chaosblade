"""GET /api/v1/recordings/{task_id} — return recorded TUIEvent jsonl.

The legacy Python TUI's ``EventRecorder`` writes one JSON object per
line under ``<memory_dir>/recordings/<task_id>.jsonl``. Each line is
``{"ts": ..., "type": <TUIEvent class name>, "data": {...}}``. The TS
TUI's ``/replay`` command consumes this endpoint and re-dispatches the
events through its reducer to reconstruct the conversation.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request

from chaos_agent.config.settings import settings
from chaos_agent.models.schemas import JSONEnvelope, ResponseCode

logger = logging.getLogger(__name__)

recordings_router = APIRouter(prefix="/api/v1/recordings", tags=["recordings"])

# Allowed task_id charset. Real ids are ``task-<uuid>`` shaped:
# letters / digits / hyphens / underscores up to 80 chars. Anything
# else (especially ``/``, ``..`` or NUL) is rejected before we touch
# the filesystem.
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def _safe_recording_path(task_id: str) -> Path | None:
    """Resolve a task_id to its recording file safely.

    Returns ``None`` when the id fails validation OR the resolved
    path escapes the recordings directory (path-traversal guard).
    Both signals collapse to a single 404 at the route layer so we
    don't leak existence information.
    """
    if not _TASK_ID_RE.match(task_id):
        return None
    rec_dir = (settings.resolved_memory_dir / "recordings").resolve()
    candidate = (rec_dir / f"{task_id}.jsonl").resolve()
    # str() + os.sep prefix is the standard ``Path.is_relative_to``
    # equivalent that also works on 3.8 (we're on 3.11+ but the idiom
    # is robust to symlinks resolving outside the dir).
    rec_dir_str = str(rec_dir)
    cand_str = str(candidate)
    if cand_str != rec_dir_str and not cand_str.startswith(rec_dir_str + "/"):
        logger.warning(
            f"recording path traversal blocked: task_id={task_id!r} → {candidate}"
        )
        return None
    return candidate


@recordings_router.get("/{task_id}")
async def get_recording(task_id: str, req: Request) -> Any:
    """Return the recorded events for ``task_id`` as a JSON array.

    The on-disk format is one JSON object per line; we parse them
    eagerly here so the TS client doesn't need its own jsonl parser.
    Lines that fail to decode are skipped silently — a corrupt record
    shouldn't abort the whole read.
    """
    req_id = getattr(req.state, "request_id", "")
    path = _safe_recording_path(task_id)
    if path is None or not path.exists() or not path.is_file():
        return JSONEnvelope.fail(
            code=ResponseCode.TASK_NOT_FOUND,
            message=f"recording not found: {task_id}",
            request_id=req_id,
        )

    events: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.debug(f"skipping malformed recording line in {task_id}")
                    continue
    except OSError as e:
        return JSONEnvelope.fail(
            code=ResponseCode.SERVER_SHUTTING_DOWN,
            message=f"failed to read recording: {e}",
            request_id=req_id,
        )

    return JSONEnvelope.ok(
        data={"task_id": task_id, "events": events, "total": len(events)},
        request_id=req_id,
    )


@recordings_router.get("")
async def list_recordings(req: Request) -> Any:
    """List task_ids that have a recording available on disk."""
    req_id = getattr(req.state, "request_id", "")
    rec_dir = (settings.resolved_memory_dir / "recordings").resolve()
    if not rec_dir.exists():
        return JSONEnvelope.ok(
            data={"recordings": [], "total": 0}, request_id=req_id
        )
    items: list[dict[str, Any]] = []
    for entry in sorted(rec_dir.glob("*.jsonl")):
        # Defense-in-depth: skip entries whose stem has somehow ended
        # up violating the id charset (e.g. file copied in manually
        # with weird chars). Same predicate the read endpoint uses.
        if not _TASK_ID_RE.match(entry.stem):
            continue
        try:
            stat = entry.stat()
            items.append(
                {
                    "task_id": entry.stem,
                    "size_bytes": stat.st_size,
                    # ISO 8601 UTC matches how every other route reports
                    # timestamps (created_at / finished_at). epoch
                    # seconds was a one-off and confused TS clients.
                    "modified_at": datetime.fromtimestamp(
                        stat.st_mtime, tz=timezone.utc
                    ).isoformat(),
                    "_mtime_secs": stat.st_mtime,  # used for sort below
                }
            )
        except OSError:
            continue
    # Most recent first; sort on the numeric helper, then drop it
    # so the public payload only carries ISO strings.
    items.sort(key=lambda r: r["_mtime_secs"], reverse=True)
    for item in items:
        item.pop("_mtime_secs", None)
    return JSONEnvelope.ok(
        data={"recordings": items, "total": len(items)}, request_id=req_id
    )
