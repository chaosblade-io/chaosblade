"""Key bindings + slash menu state computation for the TUI prompt.

Submission vs newline:
- Enter         submit the buffer; in slash-menu mode, apply the highlighted
                command into the buffer instead of submitting
- Shift+Enter   insert newline (only on terminals that send a distinct
                CSI-u sequence, e.g. kitty/WezTerm/Ghostty/iTerm2-CSI-u)
- Alt+Enter     insert newline (Esc+Enter — universal fallback)
- Ctrl+J        insert newline (legacy LF — works on every terminal)
- Up / Down     navigate the slash-command menu when active; otherwise
                fall through to default cursor movement
- Tab           apply the highlighted slash command (slash-menu mode only)
- Shift+Tab     emit a sentinel string the REPL handles (cycle permission mode)
- Ctrl+C        let prompt_toolkit's default raise KeyboardInterrupt

Slash menu modes (returned by :func:`compute_slash_menu`):
- ``"root"``  — typing a root token, candidates are root commands by prefix
- ``"sub"``   — committed to a root with subcommands, candidates are subs
- ``"hint"``  — committed past the candidate set; show ``usage:`` line
- ``"none"``  — not in slash mode; show the regular status bar
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings

if TYPE_CHECKING:
    from chaos_agent.tui.commands import SlashCommand, SlashCommandRegistry
    from chaos_agent.tui.state import SessionState

CYCLE_MODE_SENTINEL = "\x00cycle-permission-mode"
CYCLE_DISPLAY_MODE_SENTINEL = "\x00cycle-display-mode"

SlashMenuMode = Literal["root", "sub", "hint", "none"]


@dataclass
class SlashMenuState:
    """Result of inspecting the current buffer against the registry."""

    mode: SlashMenuMode = "none"
    candidates: list["SlashCommand"] = field(default_factory=list)
    root: "SlashCommand | None" = None  # parent in sub mode (for apply prefix)
    hint: str = ""

    @property
    def is_selectable(self) -> bool:
        return self.mode in ("root", "sub") and bool(self.candidates)


def _insert_newline(event) -> None:
    """Insert a literal newline at the cursor."""
    event.current_buffer.insert_text("\n")


def compute_slash_menu(
    buffer_text: str,
    registry: "SlashCommandRegistry",
    state: "SessionState | None" = None,
) -> SlashMenuState:
    """Inspect ``buffer_text`` and decide what to render.

    The caller is expected to invoke this on every render; if the
    underlying mode/root/count signature changes between calls,
    ``state.slash_selected_index`` is reset to 0 so the highlight
    doesn't dangle onto an unrelated row.
    """
    text = buffer_text.lstrip()
    if not text.startswith("/") or "\n" in text:
        return _finish(SlashMenuState(mode="none"), state)

    parts = text.split(" ", 1)
    first = parts[0].lower()
    rest: str | None = parts[1] if len(parts) > 1 else None

    if rest is None:
        # Still typing the root token → root candidates by prefix.
        # Sort in visual display order (group first, then name) so the
        # cursor moves through the rendered rows in sequence — otherwise
        # ↓ from /exit jumps to the alphabetically-next /experiments
        # which is rendered far below in a different group section.
        cands = [c for c in registry.list_commands() if c.name.startswith(first)]
        cands.sort(key=registry.display_order_key)
        if not cands:
            return _finish(SlashMenuState(mode="none"), state)
        return _finish(SlashMenuState(mode="root", candidates=cands), state)

    # We have a space → committed to a root.
    root = registry.get(first)
    if root is None:
        return _finish(SlashMenuState(mode="none"), state)

    if root.subcommands:
        return _finish(_compute_sub_menu(root, rest), state)

    # Root has no subs, we're past it → usage hint.
    if root.usage:
        return _finish(
            SlashMenuState(mode="hint", root=root, hint=f"usage: {root.name} {root.usage}"),
            state,
        )
    return _finish(SlashMenuState(mode="none"), state)


def _compute_sub_menu(root: "SlashCommand", rest: str) -> SlashMenuState:
    sub_parts = rest.split(" ", 1)
    sub_token = sub_parts[0].lower() if rest.strip() else ""
    sub_rest: str | None = sub_parts[1] if len(sub_parts) > 1 else None

    if sub_rest is None:
        # Still typing the sub token (no further space yet).
        cands = [
            sub for name, sub in root.subcommands.items() if name.startswith(sub_token)
        ]
        cands.sort(key=lambda c: c.name)
        if cands:
            return SlashMenuState(mode="sub", candidates=cands, root=root)
        # Unknown sub prefix — fall back to a usage hint listing valid subs.
        valid = ", ".join(sorted(root.subcommands.keys()))
        return SlashMenuState(
            mode="hint",
            root=root,
            hint=f"usage: {root.name} <{valid}>",
        )

    # Past the sub token: show the matched sub's own usage hint, if any.
    sub = root.subcommands.get(sub_token)
    if sub is not None and sub.usage:
        return SlashMenuState(
            mode="hint",
            root=root,
            hint=f"usage: {root.name} {sub.name} {sub.usage}",
        )
    return SlashMenuState(mode="none")


def _finish(menu: SlashMenuState, state: "SessionState | None") -> SlashMenuState:
    """Reset ``slash_selected_index`` when the menu signature changes."""
    if state is None:
        return menu
    sig = _signature(menu)
    if sig != getattr(state, "slash_menu_signature", ""):
        state.slash_menu_signature = sig
        state.slash_selected_index = 0
    return menu


def _signature(menu: SlashMenuState) -> str:
    root_name = menu.root.name if menu.root else ""
    return f"{menu.mode}:{root_name}:{len(menu.candidates)}"


# ── Backward-compat thin wrapper ──────────────────────────────────


def slash_candidates(
    buffer_text: str, registry: "SlashCommandRegistry"
) -> list:
    """Legacy helper — returns the selectable items for the current buffer.

    Newer callers should use :func:`compute_slash_menu` for full state
    (including hint mode and the parent root in sub mode).
    """
    return compute_slash_menu(buffer_text, registry).candidates


# ── Key bindings ──────────────────────────────────────────────────


def make_key_bindings(
    state: "SessionState | None" = None,
    registry: "SlashCommandRegistry | None" = None,
) -> KeyBindings:
    bindings = KeyBindings()

    def _menu_now() -> SlashMenuState:
        if registry is None:
            return SlashMenuState(mode="none")
        try:
            from prompt_toolkit.application import get_app
            text = get_app().current_buffer.text
        except Exception:
            return SlashMenuState(mode="none")
        return compute_slash_menu(text, registry, state)

    @Condition
    def _in_slash_menu() -> bool:
        return _menu_now().is_selectable

    def _clamp_selected(n: int) -> int:
        if state is None or n <= 0:
            return 0
        idx = state.slash_selected_index
        if idx < 0 or idx >= n:
            idx = 0
            state.slash_selected_index = 0
        return idx

    def _apply_selection(buf) -> None:
        menu = _menu_now()
        if not menu.is_selectable:
            return
        idx = _clamp_selected(len(menu.candidates))
        chosen = menu.candidates[idx]
        if menu.mode == "sub" and menu.root is not None:
            buf.text = f"{menu.root.name} {chosen.name} "
        else:
            buf.text = chosen.name + " "
        buf.cursor_position = len(buf.text)
        if state is not None:
            state.slash_selected_index = 0
            state.slash_menu_signature = ""

    @bindings.add("enter")
    def _(event):
        buf = event.current_buffer
        if _menu_now().is_selectable:
            _apply_selection(buf)
            return
        if buf.complete_state:
            current = buf.complete_state.current_completion
            if current is not None:
                buf.apply_completion(current)
            buf.cancel_completion()
            return
        if not buf.text.strip():
            return
        buf.validate_and_handle()

    @bindings.add("up", filter=_in_slash_menu)
    def _(event):
        if state is None:
            return
        n = len(_menu_now().candidates)
        if n == 0:
            return
        state.slash_selected_index = (state.slash_selected_index - 1) % n

    @bindings.add("down", filter=_in_slash_menu)
    def _(event):
        if state is None:
            return
        n = len(_menu_now().candidates)
        if n == 0:
            return
        state.slash_selected_index = (state.slash_selected_index + 1) % n

    @bindings.add("tab", filter=_in_slash_menu)
    def _(event):
        _apply_selection(event.current_buffer)

    @bindings.add("escape", filter=_in_slash_menu, eager=True)
    def _(event):
        # Clear the buffer to dismiss the slash menu without submitting.
        event.current_buffer.text = ""
        if state is not None:
            state.slash_selected_index = 0
            state.slash_menu_signature = ""

    @bindings.add("escape", "enter")
    def _(event):
        _insert_newline(event)

    try:
        @bindings.add("s-enter")
        def _(event):
            _insert_newline(event)
    except Exception:
        pass

    try:
        @bindings.add("c-j")
        def _(event):
            _insert_newline(event)
    except Exception:
        pass

    @bindings.add("backspace")
    def _(event):
        count = getattr(event, "arg", 1) or 1
        event.current_buffer.delete_before_cursor(count=count)
        if state is not None:
            state.slash_selected_index = 0

    try:
        @bindings.add("s-tab")
        def _(event):
            buf = event.current_buffer
            buf.text = CYCLE_MODE_SENTINEL
            buf.validate_and_handle()
    except Exception:
        pass

    # PR-D1 §17.1 — Ctrl-G cycles the display-density mode (calm →
    # working → dense). Mirrors the Shift-Tab pattern: stuff a sentinel
    # into the buffer, let the REPL switch on it. Done as a binding (not
    # a slash) so it works without releasing focus on the input line.
    try:
        @bindings.add("c-g")
        def _(event):
            buf = event.current_buffer
            buf.text = CYCLE_DISPLAY_MODE_SENTINEL
            buf.validate_and_handle()
    except Exception:
        pass

    return bindings
