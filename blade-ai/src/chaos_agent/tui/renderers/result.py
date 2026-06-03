"""Result renderer — structured result cards for task outcomes.

Renders TaskResult envelopes as visually distinct cards with status-colored
borders, structured metadata, and verification sections.
"""

from __future__ import annotations

from rich.console import Group
from rich.panel import Panel
from rich.text import Text

from chaos_agent.tui.console import ChaosConsole
from chaos_agent.tui.renderers._layout import make_field_table
from chaos_agent.tui.state import DisplayMode
from chaos_agent.tui.theme import Borders, Colors, Icons, Spacing
from chaos_agent.utils.time import parse_iso_timestamp


def _read_diagnostic(data: dict, key: str, default):
    """Read a diagnostic field, tolerating envelope vs flat shapes.

    Stream events arrive as a JSONEnvelope (``{status, data: {...}, ...}``),
    while tests pass a flat dict for simplicity. Try the top level first
    (test path) then fall back to ``envelope["data"]`` (production path).
    Returning the inner value lets callers stay shape-agnostic.
    """
    if key in data:
        value = data[key]
        if value is not None:
            return value
    payload = data.get("data")
    if isinstance(payload, dict):
        return payload.get(key, default)
    return default


def _extract_failure_cause_hint(data: dict) -> tuple[str, str]:
    """Extract (cause, hint) from either structured ``failure_detail`` or legacy ``failure_reason``.

    Prefers structured: ``failure_detail.category`` + ``failure_detail.context`` as cause,
    ``failure_detail.llm_analysis`` as hint.
    Falls back to legacy split on ``" | llm_analysis: "`` separator.
    """
    detail = _read_diagnostic(data, "failure_detail", None)
    if isinstance(detail, dict) and detail.get("category"):
        cause = detail["category"]
        ctx = detail.get("context", "")
        if ctx:
            cause = f"{cause}: {ctx}"
        hint = detail.get("llm_analysis", "") or ""
        return cause, hint

    # Legacy fallback
    reason = _read_diagnostic(data, "failure_reason", "") or ""
    if not reason:
        return "", ""
    sep = " | llm_analysis: "
    if sep in reason:
        base, _, analysis = reason.partition(sep)
        return base.strip(), analysis.strip()
    return reason.strip(), ""


# Cap each replan reason to keep the timeline scannable in narrow terminals.
# Longer strings get truncated with an ellipsis — the full value remains
# in build_status_data for /history and any future expansion view.
_REPLAN_REASON_MAX_LEN = 80


def _truncate_reason(text: str, limit: int = _REPLAN_REASON_MAX_LEN) -> str:
    text = (text or "").strip().splitlines()[0] if text else ""
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "\u2026"
    return text


def _render_side_effects(body: Text, side_effects: dict) -> None:
    """Render all side-effect categories detected by the se_detect node."""
    if not side_effects or not any(
        isinstance(v, list) and v for v in side_effects.values()
    ):
        return

    body.append("\n")
    body.append("  \u2500\u2500 Side Effects ", style=Colors.DIM)
    body.append("\u2500" * 32 + "\n", style=Colors.DIM)

    for key, items in side_effects.items():
        if not isinstance(items, list) or not items:
            continue
        for entry in items:
            if not isinstance(entry, dict):
                continue
            _render_side_effect_entry(body, key, entry)


def _render_side_effect_entry(body: Text, category: str, entry: dict) -> None:
    """Render a single side-effect entry with category-aware formatting."""
    body.append(f"  {Icons.WARNING} ", style=Colors.WARNING)

    if category == "container_restarts":
        pod = entry.get("pod") or "unknown"
        count = entry.get("restart_count") or entry.get("restart_delta")
        reason = entry.get("reason") or entry.get("note") or ""
        body.append(f"{pod} restarted", style=Colors.WARNING)
        if isinstance(count, int) and count > 0:
            body.append(f" {count}\u00d7", style=f"bold {Colors.WARNING}")
        if reason:
            body.append(f" \u2014 {_truncate_reason(reason)}", style=Colors.MUTED)
    else:
        label = category.replace("_", " ").title()
        name = (
            entry.get("pod") or entry.get("service") or
            entry.get("hpa") or entry.get("pattern") or ""
        )
        detail = entry.get("reason") or entry.get("message") or ""
        body.append(f"{label}: {name}", style=Colors.WARNING)
        if detail:
            body.append(f" \u2014 {_truncate_reason(detail)}", style=Colors.MUTED)

    body.append("\n")


def _render_replan_history(
    body: Text,
    history: list,
    final_status: str,
) -> None:
    """Render the agent's self-improvement loop as a versioned timeline.

    Each ``replan_history`` entry records the **previous** plan's failure
    that triggered the next attempt — i.e., entry with ``attempt=N`` means
    "v(N-1) failed, agent generated v(N)". We render it as
    ``v1 ✗ <reason> → v2`` so users can see the convergence story.

    Skipped entirely when there were no replans (single-shot success).
    Why surface it: the value pitch of an agent over a static playbook is
    "it learns mid-flight". When that loop fires we should show it.
    """
    if not history:
        return

    body.append("\n")
    body.append("  \u2500\u2500 Replan History ", style=Colors.DIM)
    body.append("\u2500" * 31 + "\n", style=Colors.DIM)

    for entry in history:
        if not isinstance(entry, dict):
            continue
        attempt = int(entry.get("attempt") or 0)
        if attempt <= 0:
            continue
        # entry.attempt=N means "the Nth replan event was just triggered" —
        # i.e., plan v{N} (1-indexed: v1 = original) is the version that failed
        # and the agent is about to run v{N+1}. We surface the failed version.
        prev_v = attempt
        reason = _truncate_reason(entry.get("original_error") or "(no reason recorded)")
        body.append(f"  v{prev_v} ", style=f"bold {Colors.MUTED}")
        body.append(f"{Icons.FAIL} ", style=Colors.ERROR)
        body.append(f"{reason}\n", style=Colors.MUTED)

    final_v = max(int(e.get("attempt") or 0) for e in history if isinstance(e, dict)) + 1
    body.append(f"  v{final_v} ", style=f"bold {Colors.MUTED}")
    if final_status == "success":
        body.append(f"{Icons.SUCCESS} ", style=Colors.SUCCESS)
        body.append("agent improved the plan and succeeded\n", style=Colors.SUCCESS)
    else:
        body.append(f"{Icons.FAIL} ", style=Colors.ERROR)
        body.append("final attempt also failed\n", style=Colors.ERROR)


def _format_offset(seconds: float) -> str:
    """Render a wall-clock offset as ``T+M:SS`` (or ``T+S.s`` when sub-minute).

    Used by the physical timeline so the eye reads "this many seconds
    after the run started" without doing the timestamp math itself.
    Negative offsets degrade to ``T+0`` rather than printing ``T-…`` —
    a clock skew between the task store and the local box shouldn't
    surface as a confusing sentinel.
    """
    if seconds <= 0:
        return "T+0"
    if seconds < 60:
        return f"T+{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"T+{minutes}:{secs:02d}"


def _build_timeline_events(data: dict) -> list[tuple[str, float, str]]:
    """Pull ``(label, offset_seconds, glyph_color)`` rows from the result envelope.

    All timestamps are already collected by the agent; we only re-arrange
    them. We deliberately don't fabricate replan-event timestamps — the
    `replan_history` rows lack their own ``at`` field and an interpolated
    midpoint would be misleading. Returns an empty list when ``created_at``
    is missing (everything else is anchored against it).
    """
    created_at = data.get("created_at") or _read_diagnostic(data, "created_at", "")
    if not created_at:
        return []
    try:
        anchor = parse_iso_timestamp(created_at)
    except (ValueError, TypeError):
        return []

    events: list[tuple[str, float, str]] = [
        ("\u4efb\u52a1\u542f\u52a8", 0.0, Colors.MUTED),  # 任务启动
    ]

    injection_start = data.get("injection_start_time") or _read_diagnostic(
        data, "injection_start_time", ""
    )
    if injection_start:
        try:
            offset = (parse_iso_timestamp(injection_start) - anchor).total_seconds()
            events.append(("\u6545\u969c\u6ce8\u5165", offset, Colors.WARNING))  # 故障注入
        except (ValueError, TypeError):
            pass

    finished_at = data.get("finished_at") or _read_diagnostic(data, "finished_at", "")
    if finished_at:
        try:
            offset = (parse_iso_timestamp(finished_at) - anchor).total_seconds()
            status = data.get("status", "")
            label = (
                "\u4efb\u52a1\u5b8c\u6210" if status == "success"
                else "\u4efb\u52a1\u7ed3\u675f"
            )  # 任务完成 / 任务结束
            color = Colors.SUCCESS if status == "success" else Colors.ERROR
            events.append((label, offset, color))
        except (ValueError, TypeError):
            pass

    return events


def _render_physical_timeline(
    body: Text, data: dict, display_mode: DisplayMode
) -> None:
    """Render the wall-clock timeline (PR-D5).

    Three branches by density:

    * ``calm``    — silent. The user opted out of the differentiating UI.
    * ``working`` — single line: total duration + a one-line summary
      "故障注入 → 完成". Compact enough to fit beside the status fields.
    * ``dense``   — full ``T+0 / T+inject / T+done`` table so a postmortem
      reader can lift the timeline into a doc without reformatting.

    The data is *already* in the envelope (``created_at``,
    ``injection_start_time``, ``finished_at``, ``duration_ms``); this
    function only recomposes it.
    """
    if display_mode == DisplayMode.CALM:
        return
    events = _build_timeline_events(data)
    if not events:
        return

    body.append("\n")
    body.append("  \u2500\u2500 Timeline ", style=Colors.DIM)
    body.append("\u2500" * 33 + "\n", style=Colors.DIM)

    if display_mode == DisplayMode.DENSE:
        for label, offset, color in events:
            body.append(f"  {_format_offset(offset)} ", style=Colors.DIM)
            body.append(f"{label}\n", style=color)
        return

    # working — collapse to a single line: total duration first, then a
    # short arrow-joined summary so the user sees both "how long" and
    # "what fired" without reading three rows.
    duration_ms = data.get("duration_ms") or _read_diagnostic(data, "duration_ms", 0)
    total_seconds = (duration_ms or 0) / 1000.0
    if total_seconds <= 0 and len(events) >= 2:
        total_seconds = events[-1][1] - events[0][1]

    summary_labels = [label for label, _offset, _color in events]
    summary = " \u2192 ".join(summary_labels)
    body.append(
        f"  {_format_offset(total_seconds)} \u00b7 ",
        style=Colors.DIM,
    )
    body.append(f"{summary}\n", style=Colors.MUTED)


def _latest_experiment_locator(state) -> str:
    """Return the most recently allocated experiment locator (e.g. 'E1'), or ''."""
    if state is None:
        return ""
    locators = getattr(state, "locators", None)
    if locators is None:
        return ""
    experiments = locators.list_experiments()
    if not experiments:
        return ""
    return experiments[-1].locator


def render_result(
    console: ChaosConsole,
    data: dict,
    task_id: str = "",
    display_mode: DisplayMode = DisplayMode.WORKING,
    state=None,
) -> None:
    if not isinstance(data, dict):
        console.print_text(f"  Result: {data}", style=Colors.SUCCESS)
        return

    # Non-injection completions from intent_clarification (TUI mode).
    # The LLM response was already streamed as tokens — skip the result panel.
    payload = data.get("data") if isinstance(data.get("data"), dict) else {}
    if payload.get("confirmed_intent") in ("chat", "recover"):
        return

    status = data.get("status", "unknown")
    task_state = data.get("task_state", "")

    # Determine visual style based on status
    if status == "success" or task_state in ("injected", "recovered"):
        icon = Icons.SUCCESS
        border_color = Borders.RESULT_SUCCESS
        title_text = "INJECTION SUCCESS" if task_state == "injected" else "TASK SUCCESS"
        title_style = Colors.SUCCESS
    elif status == "partial" or task_state == "partial_recovered":
        icon = Icons.WARNING
        border_color = Borders.RESULT_PARTIAL
        title_text = "PARTIAL SUCCESS"
        title_style = Colors.WARNING
    else:
        icon = Icons.FAIL
        border_color = Borders.RESULT_FAIL
        title_text = "INJECTION FAILED" if task_state else "TASK FAILED"
        title_style = Colors.ERROR

    # Build title
    title = Text()
    locator = _latest_experiment_locator(state)
    if locator:
        title.append(f" [{locator}]", style=f"bold {title_style}")
    title.append(f" {icon} ", style=f"bold {title_style}")
    title.append(title_text, style=f"bold {title_style}")

    # Build body with structured metadata.
    #
    # PR-C2: the metadata block was a hand-padded ``label:`` + ``ljust(16)``
    # loop. ``ljust`` aligns by Python char count, but in a CJK locale a
    # Han glyph occupies 2 terminal cells, so a Chinese fault_type used to
    # shove the value into the next column. We now render the metadata via
    # ``make_field_table`` (Rich's column-width logic is cell-aware) and
    # keep the rest of the panel as Text via a Group composition.
    meta_fields: list[tuple[str, object]] = []
    if task_id:
        meta_fields.append(("Task ID", task_id))
    if data.get("fault_type"):
        meta_fields.append(("Fault Type", data["fault_type"]))
    if data.get("blade_uid"):
        meta_fields.append(("Blade UID", data["blade_uid"]))
    if task_state:
        meta_fields.append(("State", task_state))

    meta_renderable = (
        make_field_table(
            meta_fields,
            label_min_width=10,
            value_min_width=30,
            boxed=False,
            indent=2,
        )
        if meta_fields
        else None
    )

    body = Text()

    # Replan history — agent self-improvement timeline.
    # _read_diagnostic so the renderer works for both flat-dict tests and
    # production envelopes where the field lives at envelope["data"][key].
    replan_history = _read_diagnostic(data, "replan_history", []) or []
    if replan_history:
        _render_replan_history(body, replan_history, status)

    # Verification section
    verification = data.get("verification")
    if verification:
        body.append("  \u2500\u2500 Verification ", style=Colors.DIM)
        body.append("\u2500" * 30 + "\n", style=Colors.DIM)
        if isinstance(verification, str):
            for line in verification.strip().splitlines():
                line = line.strip()
                if line:
                    # Color code verification lines
                    if any(kw in line.lower() for kw in ("pass", "ok", "success", "\u2713")):
                        body.append(f"  {Icons.SUCCESS} ", style=Colors.SUCCESS)
                    elif any(kw in line.lower() for kw in ("fail", "error", "\u2717")):
                        body.append(f"  {Icons.FAIL} ", style=Colors.ERROR)
                    else:
                        body.append("  \u2022 ")
                    body.append(f"{line}\n")
        elif isinstance(verification, dict):
            for k, v in verification.items():
                body.append(f"  {Icons.SUCCESS} ", style=Colors.SUCCESS)
                body.append(f"{k}: {v}\n")

    # Side effects — chaos-eng-specific value signal.
    # ``container_restarts`` means the fault triggered a real pod restart
    # (evidence got destroyed but the experiment was effective). The data
    # has always been recorded by the verifier; the user just never saw it.
    side_effects = _read_diagnostic(data, "side_effects", {}) or {}
    if isinstance(side_effects, dict):
        _render_side_effects(body, side_effects)

    # PR-D5 physical timeline. Renders working/dense; calm is silent.
    # Data is pre-existing in the result envelope (created_at /
    # injection_start_time / finished_at), we only re-shape it.
    _render_physical_timeline(body, data, display_mode)

    # Error details for failed results.
    # Prefer the structured failure_detail (category + context + hint) over
    # the raw merged_error so failed runs show *why* on one line and the
    # LLM diagnosis on a second, instead of a single ungrokable blob.
    if status not in ("success",):
        cause, hint = _extract_failure_cause_hint(data)
        if cause:
            body.append("  Cause: ", style=f"bold {Colors.ERROR}")
            body.append(f"{cause}\n", style=Colors.ERROR)
            if hint:
                body.append("  Hint:  ", style=f"bold {Colors.MUTED}")
                body.append(f"{hint}\n", style=Colors.MUTED)
        else:
            error_msg = _read_diagnostic(data, "error", None) or data.get("message")
            if error_msg:
                body.append(f"  Error: {error_msg}\n", style=Colors.ERROR)

    # Recovery info
    recovery = data.get("recovery")
    if recovery:
        body.append("\n")
        body.append("  \u2500\u2500 Recovery ", style=Colors.DIM)
        body.append("\u2500" * 33 + "\n", style=Colors.DIM)
        if isinstance(recovery, str):
            body.append(f"  {Icons.SUCCESS} ", style=Colors.SUCCESS)
            body.append(f"{recovery}\n")
        elif isinstance(recovery, dict):
            for k, v in recovery.items():
                body.append(f"  {k}: {v}\n")

    panel_body = (
        Group(meta_renderable, body) if meta_renderable is not None else body
    )
    # Vertical breath: 1 blank line before result card
    console.print("")
    console.print(
        Panel(
            panel_body,
            title=title,
            border_style=border_color,
            padding=Spacing.RESULT_PADDING,
        )
    )
    # Bell notification on completion
    console.bell()
