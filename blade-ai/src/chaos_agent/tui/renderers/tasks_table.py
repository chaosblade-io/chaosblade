"""Tasks table renderer — `/tasks [active|failed|all]`.

Reads the JSONEnvelope returned by ``AgentRunner.metric()`` (no task_id)
and renders a Rich Table grouped by recency. Filtering is performed
in Python because TaskStore.list takes only ``task_state`` (DB-level
filter), but the user-facing slices are based on inferred status/phase
which mix multiple columns.
"""

from __future__ import annotations

from typing import Sequence

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

    table = Table(
        title=f"任务列表  •  filter={flt}  •  {len(visible)}/{total}",
        title_style=f"bold {Colors.BRAND}",
        header_style="bold",
        border_style=Colors.BRAND,
        expand=False,
        pad_edge=False,
    )
    table.add_column("Task ID", overflow="fold", no_wrap=False)
    table.add_column("Fault Type", overflow="fold")
    table.add_column("Phase")
    table.add_column("Status")
    table.add_column("Duration", justify="right")
    table.add_column("Created", overflow="fold")

    for t in visible:
        status = (t.get("status") or "").lower() or "unknown"
        fault_type = t.get("skill_name") or "—"
        params = t.get("params") or {}
        if isinstance(params, dict) and params:
            scope = params.get("scope", "")
            target = params.get("target", "")
            action = params.get("action", "")
            joined = "-".join(p for p in (scope, target, action) if p)
            if joined:
                fault_type = joined
        phase = t.get("phase") or "—"
        duration = _duration(int((t.get("summary") or {}).get("total_duration_ms") or 0))
        created = (t.get("gmt_create") or "").replace("T", " ")[:19] or "—"

        status_text = Text(status, style=_status_style(status))
        table.add_row(
            _short(t.get("task_id") or "", 36),
            _short(fault_type, 36),
            _short(phase, 18),
            status_text,
            duration,
            created,
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
