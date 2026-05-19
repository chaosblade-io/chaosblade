"""Experiment history store: JSONL-based CRUD for past experiments (Layer 3).

.. deprecated::
    This module is deprecated. Use :class:`TaskStore` (SQLite-backed) instead.
    It is kept only for backward-compatible reading of legacy JSONL files.
"""

import json
import logging
import warnings
from pathlib import Path
from typing import Optional

from chaos_agent.utils.time import now_iso

logger = logging.getLogger(__name__)


class ExperimentStore:
    """CRUD operations for experiment history records.

    .. deprecated:: Use :class:`TaskStore` instead.

    Each record is a JSON line in history.jsonl:
    {
        "task_id": "...",
        "timestamp": "ISO8601",
        "operation": "inject|recover",
        "skill": "...",
        "target": {"namespace": "...", "resource_type": "...", "names": [...]},
        "params": {...},
        "blade_uid": "...",
        "status": "success|failed",
        "duration_seconds": 12.5,
        "error": null
    }
    """

    def __init__(self, history_path: Path):
        warnings.warn(
            "ExperimentStore is deprecated. Use TaskStore instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        self.history_path = history_path
        self.history_path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: dict) -> None:
        """Append an experiment record."""
        record.setdefault("timestamp", now_iso())
        try:
            with open(self.history_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except Exception as e:
            logger.warning(f"Failed to append experiment record: {e}")

    def query_by_task_id(self, task_id: str) -> Optional[dict]:
        """Find the most recent record for a task_id."""
        if not self.history_path.exists():
            return None

        result = None
        with open(self.history_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    if record.get("task_id") == task_id:
                        result = record
                except json.JSONDecodeError:
                    pass
        return result

    def query_active_experiments(self, namespace: str = "", target_name: str = "") -> list[dict]:
        """Find active (non-recovered) experiments, optionally filtered."""
        if not self.history_path.exists():
            return []

        results = []
        recovered_task_ids = set()

        # First pass: collect recovered task IDs
        with open(self.history_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    if record.get("operation") == "recover" and record.get("status") == "success":
                        recovered_task_ids.add(record.get("task_id", ""))
                except json.JSONDecodeError:
                    pass

        # Second pass: find active inject records
        with open(self.history_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    if (
                        record.get("operation") == "inject"
                        and record.get("status") == "success"
                        and record.get("task_id") not in recovered_task_ids
                    ):
                        # Apply filters
                        if namespace:
                            target = record.get("target", {})
                            if target.get("namespace") != namespace:
                                continue
                        if target_name:
                            target = record.get("target", {})
                            names = target.get("names", [])
                            if target_name not in names:
                                continue
                        results.append(record)
                except json.JSONDecodeError:
                    pass

        return results

    def query_recent(self, limit: int = 20) -> list[dict]:
        """Get the N most recent experiment records."""
        if not self.history_path.exists():
            return []

        records = []
        with open(self.history_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

        return records[-limit:]
