"""Tasks table renderer — `/tasks [active|failed|all]`.

Reads the JSONEnvelope returned by ``AgentRunner.metric()`` (no task_id)
and renders a Rich Table grouped by recency. Filtering is performed
in Python because TaskStore.list takes only ``task_state`` (DB-level
filter), but the user-facing slices are based on inferred status/phase
which mix multiple columns.
"""

from __future__ import annotations

from typing import Sequence

from rich.box import ROUNDED
from rich.table import Table
from rich.text import Text

from chaos_agent.tui.console import ChaosConsole
from chaos_agent.tui.theme import Colors

# Phases the user thinks of as "running" — task is mid-pipeline.
_ACTIVE_PHASES = frozenset({"planning", "executing", "verifying", "dry_run_planned"})
# Statuses the user thinks of as "failed".
_FAILED_STATUSES = frozenset({"failed", "error"})

_FILTERS = ("active", "failed", "all")


def _short(s: str, n: int) -> str:
    if not s:
        return "—"
    return s if len(s) <= n else s[: n - 1] + "…"


def _duration(ms: int) -> str:
    if not ms:
        return "—"
    s = ms / 1000
    if s < 60:
        return f"{s:.1f}s"
    m, s = divmod(int(s), 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def _status_style(status: str) -> str:
    s = (status or "").lower()
    if s in _FAILED_STATUSES:
        return Colors.ERROR
    if s in ("success", "injected", "recovered", "completed"):
        return Colors.SUCCESS
    if s in ("partial", "partial_recovered", "waiting_input"):
        return Colors.WARNING
    return Colors.MUTED


def _passes_filter(task: dict, flt: str) -> bool:
    if flt == "all":
        return True
    if flt == "active":
        return (task.get("phase") or "") in _ACTIVE_PHASES and (
            task.get("status") or ""
        ).lower() not in _FAILED_STATUSES
    if flt == "failed":
        return (task.get("status") or "").lower() in _FAILED_STATUSES
    return True


def render_tasks(
    console: ChaosConsole,
    envelope: dict,
    *,
    filter_: str = "all",
) -> None:
    """Render the tasks table from a ``runner.metric()`` envelope."""
    if (envelope or {}).get("code") != 0:
        msg = (envelope or {}).get("message") or "查询任务失败"
        console.print_text(f"  {msg}", style=Colors.ERROR)
        return

    data = (envelope or {}).get("data") or {}
    tasks: Sequence[dict] = data.get("tasks") or []
    total = data.get("total") or len(tasks)

    flt = filter_ if filter_ in _FILTERS else "all"
    visible = [t for t in tasks if _passes_filter(t, flt)]

    if not visible:
        if total:
            console.print_text(
                f"  共 {total} 条任务，无符合 [{flt}] 的项",
                style=Colors.MUTED,
            )
        else:
            console.print_text("  暂无任务记录", style=Colors.MUTED)
        return

    # Status icons
    _STATUS_ICONS = {
        "injected": "✓", "success": "✓", "recovered": "✓", "completed": "✓",
        "failed": "✗", "error": "✗",
        "partial": "◐", "partial_recovered": "◐", "waiting_input": "◐",
    }

    table = Table(
        title=f"  ✻ Tasks  ·  {flt}  ·  {len(visible)}/{total}",
        title_style=f"bold {Colors.BRAND}",
        title_justify="left",
        header_style=f"bold {Colors.MUTED}",
        border_style=Colors.BRAND,
        box=ROUNDED,
        expand=True,
        padding=(0, 1),
        show_lines=False,
    )
    table.add_column("", width=2, no_wrap=True)
    table.add_column("Task ID", min_width=14, no_wrap=True)
    table.add_column("Fault", min_width=20, overflow="fold")
    table.add_column("Target", min_width=16, overflow="fold")
    table.add_column("Status", min_width=8, no_wrap=True)
    table.add_column("Verify", min_width=8, no_wrap=True)
    table.add_column("Duration", justify="right", min_width=7, no_wrap=True)
    table.add_column("Created", min_width=16, no_wrap=True)

    for t in visible:
        status = (t.get("status") or "").lower() or "unknown"
        icon = _STATUS_ICONS.get(status, "·")
        icon_style = _status_style(status)

        # Fault type: prefer scope-target-action, fallback to skill_name
        fault_type = "—"
        params = t.get("params") or {}
        if isinstance(params, dict) and params:
            scope = params.get("scope", "")
            target = params.get("target", "")
            action = params.get("action", "")
            joined = "-".join(p for p in (scope, target, action) if p)
            if joined:
                fault_type = joined
        if fault_type == "—":
            fault_type = t.get("skill_name") or "—"

        # Target: namespace/name
        targets = (t.get("summary") or {}).get("targets") or []
        if targets and isinstance(targets, list):
            first = targets[0] if isinstance(targets[0], dict) else {}
            ns = first.get("namespace", "")
            name = first.get("name", "")
            target_str = f"{ns}/{name}" if ns and name else (name or ns or "—")
        else:
            target_str = "—"

        # Verification level
        verification = (t.get("summary") or {}).get("verification") or {}
        if isinstance(verification, dict):
            verify_level = verification.get("level", "—")
        else:
            verify_level = "—"
        verify_style = (
            Colors.SUCCESS if verify_level == "verified"
            else Colors.WARNING if verify_level == "partial"
            else Colors.ERROR if verify_level == "unverified"
            else Colors.MUTED
        )

        duration = _duration(int((t.get("summary") or {}).get("total_duration_ms") or 0))
        created = (t.get("gmt_create") or "").replace("T", " ")[:16] or "—"

        table.add_row(
            Text(icon, style=icon_style),
            Text(_short(t.get("task_id") or "", 18), style=Colors.MUTED),
            Text(_short(fault_type, 24)),
            Text(_short(target_str, 24)),
            Text(status, style=_status_style(status)),
            Text(verify_level, style=verify_style),
            Text(duration, style=Colors.MUTED),
            Text(created, style=Colors.MUTED),
        )

    console.print(table)


def parse_filter(args: str) -> str:
    """Validate the filter argument; default to ``all``."""
    arg = (args or "").strip().lower()
    if not arg:
        return "all"
    if arg in _FILTERS:
        return arg
    return "all"
