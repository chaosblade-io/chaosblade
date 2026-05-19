"""Review panel renderer — `/review [task_id]`.

Shows a single task's full lifecycle: target, phase timeline, verification
(layer1 / layer2), recovery, and runtime stats. Sourced from the envelope
returned by ``AgentRunner.metric(task_id)``.
"""

from __future__ import annotations

import json
from typing import Any

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from chaos_agent.tui.console import ChaosConsole
from chaos_agent.tui.theme import Borders, Colors, Icons


def _short(value: Any, n: int = 80) -> str:
    if value is None:
        return "—"
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
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


def _status_meta(status: str, phase: str) -> tuple[str, str, str]:
    s = (status or "").lower()
    if s in ("failed", "error"):
        return Icons.FAIL, Borders.RESULT_FAIL, "TASK FAILED"
    if s in ("partial", "partial_recovered", "waiting_input"):
        return Icons.WARNING, Borders.RESULT_PARTIAL, f"TASK {s.upper()}"
    if s in ("success", "injected", "recovered", "completed"):
        return Icons.SUCCESS, Borders.RESULT_SUCCESS, f"TASK {s.upper()}"
    return Icons.PENDING, Colors.BRAND, f"TASK {phase.upper() or 'IN PROGRESS'}"


def _meta_table(data: dict) -> Table:
    t = Table.grid(padding=(0, 1), pad_edge=False)
    t.add_column(style="bold", no_wrap=True)
    t.add_column(overflow="fold")

    t.add_row("Task ID", data.get("task_id") or "—")
    t.add_row("Stage", data.get("stage") or "—")
    t.add_row("Phase", data.get("phase") or "—")
    t.add_row("Status", Text(data.get("status") or "—", style=Colors.MUTED))
    t.add_row("Skill", data.get("skill_name") or "—")
    t.add_row("Fault Type", data.get("fault_type") or "—")
    t.add_row("Blade UID", data.get("blade_uid") or "—")
    t.add_row("Safety", data.get("safety_status") or "—")

    target = data.get("target") or {}
    if isinstance(target, dict) and target:
        ns = target.get("namespace") or "—"
        names = target.get("names") or []
        if isinstance(names, list):
            names_str = ", ".join(str(n) for n in names) or "—"
        else:
            names_str = str(names)
        t.add_row("Target", f"{ns}/{names_str}")

    params = data.get("params")
    if params:
        t.add_row("Params", _short(params, 100))

    t.add_row("Created", (data.get("gmt_create") or "—").replace("T", " ")[:19])
    if data.get("finished_at"):
        t.add_row("Finished", data["finished_at"].replace("T", " ")[:19])
    t.add_row("Duration", _duration(int(data.get("duration_ms") or 0)))
    return t


def _spans_table(spans: list[dict]) -> Table | None:
    if not spans:
        return None
    t = Table(
        title="Phase Timeline",
        title_style=f"bold {Colors.BRAND}",
        header_style="bold",
        border_style=Colors.MUTED,
        expand=False,
        pad_edge=False,
    )
    t.add_column("#", justify="right", style=Colors.MUTED)
    t.add_column("Node")
    t.add_column("Duration", justify="right")
    t.add_column("Tokens (in/out)", justify="right")
    t.add_column("Tools")
    t.add_column("Error")

    for i, sp in enumerate(spans, start=1):
        tools = sp.get("tool_calls") or []
        tools_str = ", ".join(tools) if tools else "—"
        err = sp.get("error") or ""
        err_text = Text(_short(err, 40), style=Colors.ERROR) if err else Text("—", style=Colors.MUTED)
        t.add_row(
            str(i),
            sp.get("node_name") or "—",
            _duration(int(sp.get("duration_ms") or 0)),
            f"{sp.get('token_input', 0)} / {sp.get('token_output', 0)}",
            _short(tools_str, 36),
            err_text,
        )
    return t


def _verification_block(label: str, verification: Any) -> Text | None:
    if not verification:
        return None
    body = Text()
    body.append(f"── {label} ", style=f"bold {Colors.BRAND}")
    body.append("─" * 30 + "\n", style=Colors.DIM)
    if isinstance(verification, str):
        for line in verification.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            lo = line.lower()
            if any(kw in lo for kw in ("pass", "ok", "success", "✓")):
                body.append(f"  {Icons.SUCCESS} ", style=Colors.SUCCESS)
            elif any(kw in lo for kw in ("fail", "error", "✗")):
                body.append(f"  {Icons.FAIL} ", style=Colors.ERROR)
            else:
                body.append("  • ")
            body.append(f"{line}\n")
    elif isinstance(verification, dict):
        for k, v in verification.items():
            body.append(f"  • {k}: ", style="bold")
            body.append(_short(v, 100) + "\n")
    return body


def render_review(console: ChaosConsole, envelope: dict) -> None:
    """Render the review panel from a ``runner.metric(task_id)`` envelope."""
    if (envelope or {}).get("code") != 0:
        msg = (envelope or {}).get("message") or "未找到任务"
        console.print_text(f"  {msg}", style=Colors.ERROR)
        return

    data = (envelope or {}).get("data") or {}
    if not data:
        console.print_text("  无任务数据", style=Colors.MUTED)
        return

    icon, border, title_text = _status_meta(data.get("status") or "", data.get("phase") or "")
    title = Text()
    title.append(f" {icon} ", style=f"bold {border}")
    title.append(title_text, style=f"bold {border}")

    parts: list[Any] = [_meta_table(data)]

    if data.get("plan_summary"):
        plan = Text()
        plan.append("\n── Plan ", style=f"bold {Colors.BRAND}")
        plan.append("─" * 30 + "\n", style=Colors.DIM)
        plan.append(_short(data["plan_summary"], 600))
        parts.append(plan)

    layer1 = _verification_block("Verification", data.get("verification"))
    if layer1 is not None:
        parts.append(Text(""))
        parts.append(layer1)

    recover = _verification_block("Recover Verification", data.get("recover_verification"))
    if recover is not None:
        parts.append(Text(""))
        parts.append(recover)

    err = data.get("error")
    if err:
        err_block = Text()
        err_block.append("\n── Error ", style=f"bold {Colors.ERROR}")
        err_block.append("─" * 30 + "\n", style=Colors.DIM)
        err_block.append(_short(err, 600), style=Colors.ERROR)
        parts.append(err_block)

    spans_table = _spans_table(data.get("spans") or [])
    if spans_table is not None:
        parts.append(Text(""))
        parts.append(spans_table)

    summary = data.get("summary") or {}
    if summary:
        s = Text()
        s.append("\n── Summary ", style=f"bold {Colors.BRAND}")
        s.append("─" * 30 + "\n", style=Colors.DIM)
        s.append(
            f"  tokens in/out: {summary.get('total_token_input', 0)} / "
            f"{summary.get('total_token_output', 0)}    "
            f"llm: {summary.get('total_llm_calls', 0)}    "
            f"tools: {summary.get('total_tool_calls', 0)}    "
            f"duration: {_duration(int(summary.get('total_duration_ms') or 0))}\n"
        )
        parts.append(s)

    console.print(
        Panel(
            Group(*parts),
            title=title,
            border_style=border,
            padding=(0, 1),
        )
    )
