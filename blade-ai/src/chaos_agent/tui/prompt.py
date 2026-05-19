"""PromptSession factory for the TUI.

Self-managed slash menu: prompt_toolkit's built-in completion popup is
disabled. Instead, the bottom toolbar renders the candidate list when the
buffer enters "slash mode" (text starts with `/`, no space yet), and
custom key bindings drive selection (↑/↓), apply (Enter/Tab), and dismiss
(Esc). The status bar (mode/model/tokens/ns/duration) is shown the rest
of the time.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.cursor_shapes import ModalCursorShapeConfig
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style
from wcwidth import wcswidth, wcwidth as _wcwidth

from chaos_agent.tui import strings
from chaos_agent.tui.commands import SlashCommand, SlashCommandRegistry
from chaos_agent.tui.key_bindings import (
    SlashMenuState,
    compute_slash_menu,
    make_key_bindings,
)
from chaos_agent.tui.state import DisplayMode, SessionState

_HISTORY_FILE = "~/.blade-ai/history"

_PROMPT_STYLE = Style.from_dict({
    # Prompt arrow
    "prompt": "#e08855 bold",
    # Bottom rule (input ↔ status-bar separator). Softened from the
    # original ``#666666`` — at full terminal width every turn was
    # printing 200+ chars of mid-grey ─ which read as visual *clutter*
    # rather than a divider. ``#3a3a3a`` is still readable on dark
    # themes but stops fighting the actual content for attention.
    "prompt.rule": "fg:#3a3a3a",
    # Continuation (blank indentation for multi-line)
    "continuation": "",
    # Placeholder text (shown when input is empty)
    "placeholder": "#888888",
    # Hide scrollbar
    "scrollbar.background": "bg:default",
    "scrollbar.button": "bg:default",
    # Bottom toolbar — base
    "bottom-toolbar": "noreverse bg:default",
    "bottom-toolbar.text": "noreverse bg:default",
    "toolbar.rule": "noreverse bg:default fg:#3a3a3a",  # Softer divider — see prompt.rule
    "toolbar.indicator": "noreverse bg:default fg:#e08855 bold",
    "toolbar.mode": "noreverse bg:default fg:#cccccc",
    "toolbar.dry_run": "noreverse bg:default fg:#e08855 bold",
    "toolbar.dim": "noreverse bg:default fg:#666666",
    "toolbar.model": "noreverse bg:default fg:#a78bfa",
    "toolbar.token": "noreverse bg:default fg:#4fc3f7",
    "toolbar.progress": "noreverse bg:default fg:#009E73",  # Okabe-Ito bluish-green (was #66bb6a)
    "toolbar.progress.bg": "noreverse bg:default fg:#333333",
    # Slash-menu rows (rendered inside the toolbar in slash mode)
    "slash.cursor": "noreverse bg:default fg:#e08855 bold",
    "slash.cursor.dim": "noreverse bg:default fg:#666666",
    "slash.name": "noreverse bg:default fg:#e08855 bold",
    "slash.name.sel": "noreverse bg:default fg:ansiwhite bold",
    "slash.desc": "noreverse bg:default fg:#888888",
    "slash.desc.sel": "noreverse bg:default fg:#cccccc",
    "slash.hint": "noreverse bg:default fg:#666666",
})


def _term_width(default: int = 80) -> int:
    try:
        return max(40, shutil.get_terminal_size((default, 20)).columns - 1)
    except Exception:
        return default


def _term_height(default: int = 24) -> int:
    try:
        return max(8, shutil.get_terminal_size((80, default)).lines)
    except Exception:
        return default


def _make_prompt_continuation(width: int, line_number: int, is_soft_wrap: bool):
    """Continuation indent for multi-line input — align with `> `."""
    return FormattedText([("class:continuation", " " * max(0, width))])


def _format_duration(seconds: float) -> str:
    """Format duration as human-readable string."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        return f"{seconds / 60:.0f}m"
    else:
        return f"{seconds / 3600:.1f}h"


def _format_tokens(count: int) -> str:
    """Format token count as compact string."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    elif count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


def _display_width(s: str) -> int:
    """Terminal display width, treating unknown chars as width 1."""
    w = wcswidth(s)
    if w >= 0:
        return w
    total = 0
    for ch in s:
        cw = _wcwidth(ch)
        total += 1 if cw < 0 else cw
    return total


def _truncate_to_width(s: str, max_width: int) -> str:
    """Truncate s so its terminal display width is at most max_width.

    If truncation occurs, the result ends with U+2026 (ellipsis). The
    ellipsis itself reserves one column. Returns "" if max_width <= 0.
    """
    if max_width <= 0:
        return ""
    if _display_width(s) <= max_width:
        return s
    if max_width == 1:
        return "\u2026"
    budget = max_width - 1
    used = 0
    out: list[str] = []
    for ch in s:
        cw = _wcwidth(ch)
        if cw < 0:
            cw = 1
        if used + cw > budget:
            break
        out.append(ch)
        used += cw
    return "".join(out) + "\u2026"


def _ljust_display(s: str, width: int) -> str:
    """Right-pad with spaces so the rendered display width is exactly `width`."""
    actual = _display_width(s)
    if actual >= width:
        return s
    return s + " " * (width - actual)


def _render_progress_bar(percent: float, width: int = 10) -> list[tuple[str, str]]:
    """Render a text-based progress bar as FormattedText parts."""
    filled = int(percent / 100 * width)
    empty = width - filled
    parts = []
    if filled > 0:
        parts.append(("class:toolbar.progress", "\u2593" * filled))
    if empty > 0:
        parts.append(("class:toolbar.progress.bg", "\u2591" * empty))
    return parts


def _windowed_indices(
    n: int, idx: int, max_visible: int
) -> tuple[int, int, int, int]:
    """Compute a window of [start, end) around ``idx`` of size up to ``max_visible``.

    Returns ``(start, end, more_above, more_below)`` where the latter two
    are the count of items hidden above/below the visible slice.
    """
    if n <= max_visible:
        return 0, n, 0, 0
    half = max_visible // 2
    start = max(0, min(idx - half, n - max_visible))
    end = start + max_visible
    return start, end, start, n - end


def _render_slash_menu(
    state: SessionState,
    menu: SlashMenuState,
    width: int,
    height: int,
) -> list[tuple[str, str]]:
    """Render the slash-command picker as FormattedText parts.

    In root mode the candidates are rendered with group headers
    ("通用 / 业务 / 技能"). In sub mode a single header naming
    the parent root is shown. When the candidate count exceeds the
    available height, the view is windowed around the cursor and
    "↑/↓ N more" sentinels mark the hidden rows.
    """
    candidates = menu.candidates
    n = len(candidates)
    parts: list[tuple[str, str]] = []
    if n == 0:
        return parts

    # Clamp selection.
    idx = state.slash_selected_index
    if idx < 0 or idx >= n:
        idx = 0
        state.slash_selected_index = 0

    # Layout columns (display widths).
    name_col = 14
    fixed = 4 + name_col + 2
    desc_col = max(8, width - fixed)

    # Reserve rows for: top rule (1) + hint (1) + group headers (variable).
    # height already excludes the top rule (caller passes `height - 1`).
    overhead = 1  # the trailing hint row at the bottom
    if menu.mode == "sub" and menu.root is not None:
        overhead += 1  # one parent header row
    elif menu.mode == "root":
        # Will compute per-group header count below; reserve at most 4.
        overhead += 4
    max_visible = max(3, height - overhead)
    if max_visible > n:
        max_visible = n

    start, end, more_above, more_below = _windowed_indices(n, idx, max_visible)

    if menu.mode == "sub" and menu.root is not None:
        parts.append((
            "class:toolbar.dim",
            f"  {menu.root.name} 子命令\n",
        ))
        if more_above:
            parts.append(("class:toolbar.dim", f"    \u2191 {more_above} more\n"))
        for i in range(start, end):
            parts.extend(_render_row(candidates[i], i == idx, name_col, desc_col))
        if more_below:
            parts.append(("class:toolbar.dim", f"    \u2193 {more_below} more\n"))
    else:
        # Root mode — group rendering when the full list fits, else flat window.
        if n <= max_visible:
            parts.extend(_render_grouped(candidates, idx, name_col, desc_col))
        else:
            if more_above:
                parts.append(("class:toolbar.dim", f"    \u2191 {more_above} more\n"))
            for i in range(start, end):
                parts.extend(_render_row(candidates[i], i == idx, name_col, desc_col))
            if more_below:
                parts.append(("class:toolbar.dim", f"    \u2193 {more_below} more\n"))

    parts.append((
        "class:slash.hint",
        "  ↑↓ 选择   Enter/Tab 选中   Esc 取消",
    ))
    return parts


def _render_grouped(
    candidates: list[SlashCommand],
    idx: int,
    name_col: int,
    desc_col: int,
) -> list[tuple[str, str]]:
    """Render candidates with group headers in display order."""
    from chaos_agent.tui.commands import SlashCommandRegistry

    parts: list[tuple[str, str]] = []
    by_group: dict[str, list[tuple[int, SlashCommand]]] = {}
    for i, cmd in enumerate(candidates):
        by_group.setdefault(cmd.group, []).append((i, cmd))

    last_label: str | None = None
    for group in SlashCommandRegistry.group_order():
        group_items = by_group.get(group)
        if not group_items:
            continue
        label = SlashCommandRegistry.group_label(group)
        # Adjacent groups sharing a label (e.g. skills + dynamic both
        # render under "技能") collapse into a single header.
        if label != last_label:
            parts.append(("class:toolbar.dim", f"  [{label}]\n"))
            last_label = label
        for i, cmd in group_items:
            parts.extend(_render_row(cmd, i == idx, name_col, desc_col))
    return parts


def _render_row(
    cmd: SlashCommand,
    is_selected: bool,
    name_col: int,
    desc_col: int,
) -> list[tuple[str, str]]:
    """Render a single picker row."""
    cursor_cls = "class:slash.cursor" if is_selected else "class:slash.cursor.dim"
    name_cls = "class:slash.name.sel" if is_selected else "class:slash.name"
    desc_cls = "class:slash.desc.sel" if is_selected else "class:slash.desc"
    cursor_glyph = "\u276f " if is_selected else "  "
    return [
        (cursor_cls, "  " + cursor_glyph),
        (name_cls, _ljust_display(cmd.name, name_col)),
        ("class:toolbar.dim", "  "),
        (desc_cls, _truncate_to_width(cmd.description, desc_col)),
        ("", "\n"),
    ]


def _render_usage_hint(menu: SlashMenuState, width: int) -> list[tuple[str, str]]:
    """Render the single-line ``usage:`` row when past the candidate set."""
    text = _truncate_to_width("  " + menu.hint, width)
    return [("class:slash.hint", text)]


def _render_keymap_footer(state: SessionState) -> list[tuple[str, str]]:
    """Render the context-sensitive keymap footer (PR-D6).

    Three branches that compose with ``display_mode`` (PR-D1 §17.1):

    * ``calm`` — empty list. The user has explicitly opted out of UI
      scaffolding; the status bar's mode/ns/tokens stays, but the
      keymap row goes away.
    * ``working`` — compact: ``shift+tab · ctrl+g``. The two essentials
      that change globally usable state. Anything else is one keystroke
      away via /help.
    * ``dense`` — verbose with explanations: ``shift+tab mode · ctrl+g
      密度 · /help`` so power users can read the legend without leaving
      the input line.

    A streaming task overrides all three modes and shows ``ctrl+c
    中断`` regardless — that's the one shortcut you might actually
    *need* mid-stream and it doesn't appear anywhere else.

    Returns the FormattedText parts to append to the status bar (each
    branch already prefixes the leading ``│`` separator). Returns an
    empty list to hide the footer entirely (calm mode, idle).
    """
    if getattr(state, "is_streaming", False):
        return [
            ("class:toolbar.dim", " \u00b7 "),
            ("class:toolbar.indicator", "ctrl+c"),
            ("class:toolbar.dim", " \u4e2d\u65ad"),
        ]

    mode = getattr(state, "display_mode", DisplayMode.WORKING)
    if mode == DisplayMode.CALM:
        return []
    if mode == DisplayMode.DENSE:
        return [
            ("class:toolbar.dim", " \u00b7 "),
            ("class:toolbar.dim", "shift+tab "),
            ("class:toolbar.dim", "mode \u00b7 "),
            ("class:toolbar.dim", "ctrl+g "),
            ("class:toolbar.dim", "\u5bc6\u5ea6 \u00b7 "),
            ("class:toolbar.dim", "/help"),
        ]
    # working (default)
    return [
        ("class:toolbar.dim", " \u00b7 "),
        ("class:toolbar.dim", "shift+tab \u00b7 ctrl+g"),
    ]


def _render_status_bar(state: SessionState) -> list[tuple[str, str]]:
    """Render the default status bar (mode/model/tokens/ns/duration)."""
    parts: list[tuple[str, str]] = []

    icon, label, _ = strings.MODE_CONFIG.get(
        state.permission_mode.value, ("\U0001f512", "\u786e\u8ba4", "")
    )
    parts.extend([
        ("class:toolbar.indicator", "\u23f5\u23f5 "),
        ("class:toolbar.mode", f"{icon} {label}"),
    ])

    if getattr(state, "is_dry_run", False):
        parts.append(("class:toolbar.dim", " \u00b7 "))
        parts.append(("class:toolbar.dry_run", "\U0001f17f Dry-Run"))

    model_name = getattr(state, "model_name", "")
    if model_name:
        parts.append(("class:toolbar.dim", " \u00b7 "))
        parts.append(("class:toolbar.model", model_name))

    token_in = getattr(state, "token_count_input", 0)
    token_out = getattr(state, "token_count_output", 0)
    total_tokens = token_in + token_out
    if total_tokens > 0:
        parts.append(("class:toolbar.dim", " \u00b7 "))
        parts.append(("class:toolbar.token", _format_tokens(total_tokens)))
        parts.append(("class:toolbar.dim", " tok"))

        ctx_limit = getattr(state, "context_limit", 128000)
        if ctx_limit > 0:
            percent = min(100, (total_tokens / ctx_limit) * 100)
            parts.append(("class:toolbar.dim", " "))
            parts.extend(_render_progress_bar(percent, 8))
            parts.append(("class:toolbar.dim", f" {percent:.0f}%"))

    parts.append(("class:toolbar.dim", " \u00b7 "))
    parts.append(("class:toolbar.dim", f"ns:{state.namespace}"))

    # PR-D1 §17.1 — surface the active density mode. calm hides
    # the experiment card / risk meter etc., so making the mode visible
    # in the footer is what tells the user "you're not seeing those by
    # design." working is the default and is omitted (no signal needed
    # for the default) so the footer stays narrow on small terminals.
    display_mode = getattr(state, "display_mode", DisplayMode.WORKING)
    if display_mode != DisplayMode.WORKING:
        label_text = strings.DISPLAY_MODE_LABELS.get(
            display_mode.value, display_mode.value
        )
        parts.append(("class:toolbar.dim", " \u00b7 "))
        parts.append(("class:toolbar.dim", f"\u5bc6\u5ea6:{label_text}"))

    # PR-E8 — running USD cost + p95 turn latency. Show cost only after
    # the session has spent at least a cent (otherwise "$0.00" steals
    # column width from things that matter); show p95 only after we
    # have at least one completed turn so the number is meaningful.
    cost = getattr(state, "usd_cost", 0.0)
    if cost >= 0.01:
        parts.append(("class:toolbar.dim", " \u00b7 "))
        parts.append(("class:toolbar.dim", f"${cost:.2f}"))

    p95_ms = 0
    try:
        p95_ms = state.latency_p95_ms() if hasattr(state, "latency_p95_ms") else 0
    except Exception:
        p95_ms = 0
    if p95_ms > 0:
        parts.append(("class:toolbar.dim", " \u00b7 "))
        # Render seconds with one decimal up to 60s, then minutes —
        # matches _format_duration's grain so the eye doesn't context
        # switch between widgets.
        if p95_ms < 60_000:
            parts.append(("class:toolbar.dim", f"p95 {p95_ms / 1000:.1f}s"))
        else:
            parts.append(("class:toolbar.dim", f"p95 {p95_ms / 60_000:.0f}m"))

    elapsed = time.time() - state.session_start_ts
    parts.append(("class:toolbar.dim", " \u00b7 "))
    parts.append(("class:toolbar.dim", _format_duration(elapsed)))

    # PR-D6 — keymap footer. May return empty list (calm mode) or a
    # streaming-specific override; otherwise compact/verbose by mode.
    parts.extend(_render_keymap_footer(state))
    return parts


def make_session(
    registry: SlashCommandRegistry,
    state: SessionState,
) -> PromptSession:
    """Build a Claude Code style PromptSession.

    Layout:
        ─────────────────────────────────────────  <- top rule
        > <input>
        ─────────────────────────────────────────  <- bottom rule
        <status bar>   OR   <slash menu rows + hint>
    """
    history_path = Path(os.path.expanduser(_HISTORY_FILE))
    try:
        history_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    def _message() -> FormattedText:
        # Removed the full-width top rule (was 200+ \u2500 chars on every
        # prompt) \u2014 it doubled the visual weight of every turn boundary
        # and made the conversation read as a stack of cards rather
        # than a flowing dialogue. The bottom rule alone is enough to
        # separate the input from the toolbar; turns separate themselves
        # via the blank line each renderer already prints. This is the
        # rhythm Claude Code / Qwen Code use and is what the doc \u00a73 P2#11
        # marginTop=1 paragraph spacing was always meant to deliver.
        return FormattedText([
            ("class:prompt", "\u276f "),
        ])

    def _current_buffer_text() -> str:
        try:
            from prompt_toolkit.application import get_app
            return get_app().current_buffer.text
        except Exception:
            return ""

    def _bottom_toolbar() -> FormattedText:
        w = _term_width()
        h = _term_height()
        bottom = "\u2500" * w
        parts: list[tuple[str, str]] = [("class:toolbar.rule", bottom + "\n")]

        menu = compute_slash_menu(_current_buffer_text(), registry, state)
        # Reserve rows for the prompt input area + top rule + bottom rule.
        # Cap menu rows so it never overflows the visible terminal.
        menu_height_cap = max(4, h - 6)

        if menu.mode in ("root", "sub"):
            parts.extend(_render_slash_menu(state, menu, w, menu_height_cap))
        elif menu.mode == "hint":
            parts.extend(_render_usage_hint(menu, w))
        else:
            parts.extend(_render_status_bar(state))

        return FormattedText(parts)

    session: PromptSession = PromptSession(
        history=FileHistory(str(history_path)),
        # No completer: we render the slash menu ourselves in the toolbar
        # and drive it with custom key bindings, so prompt_toolkit's popup
        # never gets a chance to overflow above the input.
        complete_while_typing=False,
        cursor=ModalCursorShapeConfig(),
        key_bindings=make_key_bindings(state=state, registry=registry),
        multiline=True,
        bottom_toolbar=_bottom_toolbar,
        mouse_support=False,
        message=_message,
        prompt_continuation=_make_prompt_continuation,
        placeholder=FormattedText([("class:placeholder", strings.INPUT_PLACEHOLDER)]),
        style=_PROMPT_STYLE,
        reserve_space_for_menu=0,
    )

    # PromptSession defaults the buffer Window to dont_extend_height=False,
    # which lets HSplit's "fill remaining space" pass grow it to consume the
    # whole terminal — manifesting as N empty lines between the input and
    # the bottom rule. Force the buffer to size strictly to its content
    # (1 line when empty, N+1 lines after N newlines).
    from prompt_toolkit.filters import to_filter
    from prompt_toolkit.layout.controls import BufferControl as _BufferControl
    for _w in session.app.layout.find_all_windows():
        if (
            isinstance(_w.content, _BufferControl)
            and _w.content.buffer.name == "DEFAULT_BUFFER"
        ):
            _w.dont_extend_height = to_filter(True)
            break

    return session
