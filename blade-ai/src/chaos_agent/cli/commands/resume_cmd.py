"""``blade-ai resume`` — start the TUI taking over a previous session.

The natural recovery entry: after exiting a TUI session, ONE command
brings it back — no need to boot a fresh TUI and then type
``/resume <sid>`` inside it. Two forms:

  ``blade-ai resume``           → list the sessions that carry an events
                                   jsonl on disk, then exit (list-only —
                                   no interactive prompt holding the
                                   process open on stdin)
  ``blade-ai resume -i <sid>``  → straight to the named session

The listing shares ``TuiSessionStore.list_resumable_sessions`` with
the server's ``GET /api/v1/memory/resumable`` route (single source —
the CLI picker and the TUI's ``/resume`` list cannot drift apart).
Naming a sid via ``-i`` execvp's the TS TUI with ``--resume <sid>``,
which BootRunner hands to core's ``runSessionResume`` — the exact
takeover sequence the ``/resume`` slash command uses (fold, confirm-
gate flush, SESSION_INITIALIZED re-bind).

Remote mode (``BLADE_AI_SERVER`` set): the TUI connects to a server
whose memory dir is NOT this machine's disk, so the local disk
checks below would adjudicate the wrong machine's sessions. ``-i``
skips the local ``has_events`` check and lets the remote server
adjudicate at boot (an unknown sid still fails loud, one stage later);
the bare listing refuses with a hint, since it can only ever read the
local disk.

Exit codes:
  0  terminal handed to the TUI; or bare form (listing printed — an
     empty history is not an error)
  1  ``-i`` names a malformed or event-less sid; bare form attempted
     in remote mode
  TUI bundle/Node failures exit 1 with the same messages as plain
  ``blade-ai`` (see ``_launch_default_tui``).
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

import typer


def _format_last_active(mtime: float) -> str:
    """Minute-resolution stamp for the picker's LAST ACTIVE column.

    ``modified_at`` (events-file mtime) — the SAME key the listing
    sorts by — is what lets the user tell sessions apart;
    ``started_at`` (creation) can sit days earlier and made the old
    column contradict the "newest first" order it sat under.
    """
    dt = datetime.fromtimestamp(mtime)
    if dt.year != datetime.now().year:
        return dt.strftime("%Y-%m-%d")
    return dt.strftime("%m-%d %H:%M")


def _human_size(n: int) -> str:
    """Compact byte size for the picker's SIZE column."""
    if n < 1024:
        return f"{n} B"
    kb = n / 1024.0
    if kb < 10:
        return f"{kb:.1f} KB"
    if kb < 1024:
        return f"{round(kb)} KB"
    return f"{kb / 1024.0:.1f} MB"


def _truncate_display(text: str, budget: int) -> str:
    """截断到 budget 显示宽（CJK 记 2 列），尾缀省略号。

    rich 对 ``no_wrap`` cell 的测量下限是内容全文显示宽：50+ 宽的
    长中文 digest 会把 ratio 列的 floor 抬破档位预算，rich 的压缩
    路径随即吃掉固定列（# 消失、sid 砍残）——与终端过窄同一失效
    形态，仅触发维度不同（内容长 vs 终端窄）。进 Table 前先截，
    测量 floor 才落回预算内。真实 19 会话列表实测复现过。
    """
    from rich.text import Text

    t = Text(text)
    t.truncate(budget, overflow="ellipsis")
    return t.plain


def _render_session_table(rows: list[dict]) -> None:
    """Column-aligned picker listing (printed to stdout).

    The old single-line ``sid · size · ev · when · digest`` rows had
    no shared visual rhythm — every field a different width, dot
    separators everywhere, the 17-char sid repeated once per row.
    A rich table keeps the columns scannable (number → time →
    volume → digest), measures CJK text at display width for the
    digest column, and truncates it to the terminal width instead
    of wrapping onto a second line. ANSI styling degrades
    automatically on non-TTY streams.

    Width tiers — rich's own squeeze policy is NOT a safe fallback:
    when a table's fixed columns exceed the console width it shrinks
    ratio columns to zero FIRST, then starts chopping ``width``
    columns (an explicit width is a preference, not a floor — see
    rich's Table._calculate_column_widths). On a 60-col terminal
    that eliminated the ``#`` column entirely; with the interactive
    pick gone the SID is what the user copies into ``resume -i``, so
    a silently chopped column is just as broken. The tier is decided
    HERE, by measuring the console and only ever laying out a table
    whose fixed columns fit with margin:

      ≥ 78 cols  full  — # + SESSION + LAST ACTIVE + EVENTS + SIZE
                       + FIRST INPUT(ratio)   [61 fixed + ≥17 digest]
      ≥ 56 cols  slim  — drop EVENTS/SIZE    [42 fixed + ≥14 digest]
      <  56      compact — no table at all: two Text rows per session
                       (id line + indented digest line), every row
                       individually ellipsised to the console width.

    The digest is pre-truncated to the tier budget BEFORE ``add_row``:
    rich measures a ``no_wrap`` cell's floor as its full content width,
    so a long CJK digest breaks the same columns a too-narrow console
    would (see ``_truncate_display``).
    """
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text

    console = Console()
    width = console.width

    def _stamp(r: dict) -> str:
        return _format_last_active(float(r.get("modified_at") or 0.0))

    if width >= 56:
        # Fixed columns carry explicit ``width`` so the ratio column
        # (FIRST INPUT) can never starve them — WITH the tier guard
        # above guaranteeing the fixed sum fits the console, rich's
        # squeeze path (which would chop these columns on narrow
        # terminals) never triggers. An over-long sid truncates
        # in-place — acceptable: server-generated sids are ``sess_``
        # + 12 hex (17 chars) against an 18-wide column, and the
        # hint line after the table carries the takeover affordance.
        table = Table(box=None, show_header=True, header_style="dim", padding=(0, 1))
        table.add_column("#", justify="right", width=4, no_wrap=True)
        table.add_column(
            "SESSION", width=18, no_wrap=True, overflow="ellipsis"
        )
        table.add_column("LAST ACTIVE", width=12, no_wrap=True)
        if width >= 78:
            table.add_column("EVENTS", justify="right", width=7, no_wrap=True)
            table.add_column("SIZE", justify="right", width=8, no_wrap=True)
        table.add_column(
            "FIRST INPUT", ratio=1, no_wrap=True, overflow="ellipsis"
        )
        # Digest budget = console width minus the fixed columns and
        # per-column padding. Truncating BEFORE add_row keeps rich's
        # measured floor inside the budget the tier math promised —
        # see _truncate_display for why an untruncated digest breaks
        # the fixed columns.
        digest_budget = width - (61 if width >= 78 else 42)
        for idx, r in enumerate(rows, 1):
            digest = _truncate_display(
                r.get("first_input") or "—", digest_budget
            )
            if width >= 78:
                table.add_row(
                    str(idx),
                    r["tui_session_id"],
                    _stamp(r),
                    str(r.get("event_count") or 0),
                    _human_size(int(r.get("size_bytes") or 0)),
                    digest,
                )
            else:
                table.add_row(
                    str(idx),
                    r["tui_session_id"],
                    _stamp(r),
                    digest,
                )
        console.print(table)
        return

    # Compact tier (< 56 cols): two rows per session, each a single
    # no_wrap Text ellipsised to the console width — no table layout
    # to squeeze, so the pick number and the sid can never vanish.
    for idx, r in enumerate(rows, 1):
        line = Text(f"{idx:>2}  {r['tui_session_id']}  {_stamp(r)}")
        line.no_wrap = True
        line.overflow = "ellipsis"
        console.print(line)
        digest = Text(f"     {r.get('first_input') or '—'}")
        digest.no_wrap = True
        digest.overflow = "ellipsis"
        digest.stylize("dim")
        console.print(digest)


def _list_resumable_sessions() -> None:
    """Print the resumable sessions (stdout only) and return.

    Bare ``blade-ai resume`` is a LISTING, not an interactive picker:
    the old ``typer.prompt`` number-pick held the process open on
    stdin — and hung outright wherever stdin never delivered input.
    The number added nothing the sid itself doesn't: the table is the
    copy source for a follow-up ``resume -i <sid>``.
    """
    from chaos_agent.config.settings import settings
    from chaos_agent.memory.tui_session_store import TuiSessionStore

    store = TuiSessionStore(settings.resolved_memory_dir / "sessions")
    try:
        rows = store.list_resumable_sessions()
    except OSError as e:
        typer.echo(f"blade-ai: failed to scan the sessions dir: {e}", err=True)
        raise typer.Exit(1) from e
    if not rows:
        typer.echo("No resumable sessions found (no events files on disk).")
        typer.echo("Run `blade-ai` to start a fresh session.")
        return

    typer.echo(f"Resumable sessions (newest first, {len(rows)} total):\n")
    _render_session_table(rows)
    typer.echo("")
    typer.echo("Run `blade-ai resume -i <session_id>` to take over a session.")


def resume_command(
    i: Optional[str] = typer.Option(
        None,
        "--session-id",
        "-i",
        help="TUI session id to take over (omit to pick interactively).",
    ),
) -> None:
    """Start the TUI taking over a previous session."""
    from chaos_agent.memory.tui_session_store import SESSION_ID_PATTERN

    # Remote mode: BLADE_AI_SERVER points the TUI (which inherits our
    # environment through execvp) at a server whose memory dir is not
    # this machine's disk — local-disk checks would adjudicate the
    # wrong machine's sessions.
    remote = bool((os.environ.get("BLADE_AI_SERVER") or "").strip())

    sid = i
    if sid is None:
        if remote:
            # The listing can only ever read the LOCAL disk; showing
            # it here would present the wrong machine's history.
            typer.echo(
                "blade-ai: BLADE_AI_SERVER is set (remote mode) — the "
                "bare listing only reads sessions on the local disk.",
                err=True,
            )
            typer.echo(
                "  Use `blade-ai resume -i <session_id>` (find sids via "
                "/resume inside a running TUI session), or unset "
                "BLADE_AI_SERVER to manage local sessions.",
                err=True,
            )
            raise typer.Exit(1)
        # List-only: print and exit 0 — the process never waits on
        # stdin (see module docstring).
        _list_resumable_sessions()
        raise typer.Exit(0)

    # Same whitelist the server routes enforce (single source);
    # failing here keeps a typo'd sid from booting the whole
    # server first. Pure format validation — it applies in remote
    # mode too.
    if not SESSION_ID_PATTERN.match(sid):
        typer.echo(
            f"blade-ai: invalid session id '{sid}' — must be 1–128 "
            "characters of [A-Za-z0-9_-].",
            err=True,
        )
        raise typer.Exit(1)
    if not remote:
        from chaos_agent.config.settings import settings
        from chaos_agent.memory.tui_session_store import TuiSessionStore

        # Local mode: fail before the boot when the sid names
        # nothing on THIS machine's disk. Remote mode skips this —
        # the remote server adjudicates at boot (still fail-loud,
        # one stage later).
        store = TuiSessionStore(settings.resolved_memory_dir / "sessions")
        if not store.has_events(sid):
            typer.echo(
                f"blade-ai: no events file for session '{sid}'.", err=True
            )
            typer.echo(
                "Run `blade-ai resume` (no args) to list resumable "
                "sessions.",
                err=True,
            )
            raise typer.Exit(1)

    # Runtime import: main.py imports this module at registration time,
    # so a top-level `from chaos_agent.cli.main import ...` here would
    # hit a half-initialised module (circular). By handler time main
    # is fully loaded.
    from chaos_agent.cli.main import _launch_default_tui

    _launch_default_tui(["--resume", sid])
