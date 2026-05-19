"""StreamingPrinter — token buffer with throttled rendering via rich.live.

Buffers streamed tokens and updates a Live block at refresh_per_second
(20 Hz). On finalize, stops the Live block and prints the final markdown
in place so it lands in the scrollback as static text.

Visual contract (post-B1):
    - During streaming: bare buffer text in the transient Live block; no
      ┃ left rail, no "⏳ streaming…" status row.
    - On finalize: a single ``⏺`` leader is emitted in front of the
      markdown, then the markdown itself, then nothing — no ``console.rule``
      between turns. The next turn separates itself with a blank line.

PR-E2 — when a ``LiveCoordinator`` is injected, this printer delegates its
Live block to the shared coordinator (owner = ``OWNER_TOKEN_STREAM``)
instead of starting its own. That collapses four sibling Live owners
onto one shared block so handoffs (token → tool spinner → token …) no
longer flicker. Backward-compatible: when no coordinator is passed,
the printer falls back to the original local-Live behaviour and all
existing tests / call sites stay unchanged.
"""

from __future__ import annotations

import random
import re
import threading
import time
from typing import Optional

from rich.live import Live
from rich.text import Text

from chaos_agent.tui import strings
from chaos_agent.tui.console import ChaosConsole
from chaos_agent.tui.live_coordinator import (
    LiveCoordinator,
    OWNER_THINKING,
    OWNER_TOKEN_STREAM,
)
from chaos_agent.tui.theme import BREATHING_DOTS, Colors, Icons, Theme

_REFRESH_HZ = 20
_STREAM_TIMEOUT = 10.0

# Reasoning-leak detection patterns.
# When Qwen enable_thinking puts reasoning directly in content (not
# additional_kwargs.reasoning_content), the text typically starts with
# self-referential planning statements. These patterns are language-neutral
# heuristics — the same kind of leak can happen in English or Chinese.
_REASONING_LEAK_PATTERNS = (
    # "所有必填项已收集完毕，现在应该调用 submit_fault_intent..."
    # Self-referential LLM planning: mentions calling/submitting internal
    # tools like submit_fault_intent, classify_intent, kubectl. Real
    # LLMs never tell the user "I'm about to call submit_fault_intent".
    r"^.{0,6}(?:所有|全部|必填|必要|已收集|已确认|已获).*?(?:应该|需要|现在|接下来|下一步).{0,30}(?:调用|提交|使用|执行|触发).{0,50}(?:submit_fault_intent|classify_intent|kubectl|activate_skill|blade).{0,30}$",
    # "用户提供了节点名称 cms-node-1"
    # The LLM narrating the user's actions (meta-reasoning, NOT user-facing)
    r"^.{0,6}(?:用户|user).{0,30}(?:提供|给出|说|输入|mentioned|provided).{0,40}(?:节点|namespace|pod|参数|标签|label|target).{0,30}$",
)


def _strip_reasoning_leaks(text: str) -> str:
    """Strip reasoning/metacognition paragraphs from streaming buffer.

    LLMs with thinking modes sometimes leak self-referential planning
    statements into the content field. This function splits text into
    paragraphs and removes any whose first line matches a reasoning
    pattern. If all paragraphs are reasoning, returns "".

    Handles both paragraph-separated (\\n\\n) and line-separated (\\n)
    content — streaming tokens often produce single-newline separation
    rather than double-newline paragraphs.
    """
    if not text or not text.strip():
        return text

    # Try double-newline paragraph split first (structured content)
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    if len(paragraphs) > 1:
        # Multiple paragraphs — strip reasoning paragraphs
        clean = []
        for para in paragraphs:
            first_line = para.split("\n")[0].strip()
            is_reasoning = False
            for pattern in _REASONING_LEAK_PATTERNS:
                if re.match(pattern, first_line, re.IGNORECASE):
                    is_reasoning = True
                    break
            if not is_reasoning:
                clean.append(para)
        return "\n\n".join(clean)

    # Single paragraph (or single-line) — strip reasoning lines
    lines = text.split("\n")
    clean_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            clean_lines.append(line)
            continue
        is_reasoning = False
        for pattern in _REASONING_LEAK_PATTERNS:
            if re.match(pattern, stripped, re.IGNORECASE):
                is_reasoning = True
                break
        if not is_reasoning:
            clean_lines.append(line)

    return "\n".join(clean_lines)


class StreamingPrinter:
    """Throttled token streamer with role visual markers.

    Usage:
        printer.append(token)   # buffers; live block shows partial Text
        printer.finalize()      # stops live, prints final Markdown
        printer.maybe_warn_stale()  # shows STREAM_INTERRUPTED if idle >10s

    When ``coordinator`` is provided, the printer routes Live ownership
    through ``LiveCoordinator`` under the ``OWNER_TOKEN_STREAM`` token,
    sharing one Live region with the other three printers (no flicker
    on handoff). When ``coordinator`` is None, the printer runs an
    embedded ``rich.live.Live`` of its own — the pre-PR-E2 behaviour.
    """

    def __init__(
        self,
        console: ChaosConsole,
        *,
        coordinator: Optional[LiveCoordinator] = None,
    ) -> None:
        self._console = console
        self._buffer: str = ""
        self._live: Optional[Live] = None  # used only when no coordinator
        self._coord: Optional[LiveCoordinator] = coordinator
        self._coord_active: bool = False  # do we currently own the coord?
        self._last_token_time: float = 0.0
        self._stale_warned: bool = False
        self._start_time: float = 0.0

    @property
    def is_active(self) -> bool:
        if self._coord is not None:
            return self._coord_active
        return self._live is not None

    def _render_streaming(self) -> Text:
        """Render the current buffer as plain text — no ┃ rail, no status row."""
        return Text(self._buffer)

    def _flush_final(self) -> None:
        """Emit the marker leader + markdown for the buffered text.

        Reasoning leaks are stripped before rendering — when LLMs with
        thinking modes put self-referential planning statements in the
        content field, the TUI should never show those to the user.
        If stripping removes everything, the entire buffer is discarded
        (the caller's fallback mechanism will provide a template reply).
        """
        if not self._buffer:
            return
        # Strip reasoning/metacognition paragraphs from the buffer
        # before rendering to the user.
        clean_buffer = _strip_reasoning_leaks(self._buffer)
        if not clean_buffer.strip():
            # All content was reasoning — discard the whole buffer.
            # The node's _ensure_visible_content fallback will have
            # provided a clean template reply via AIMessage.content,
            # which runner yields as a synthetic token if needed.
            return
        # Vertical breath: 1 blank line before agent response,
        # separating it from preceding user message / tool output.
        self._console.print("")
        self._console.print(Text(f"{Icons.MARKER} ", style=Theme.gradient_bright), end="")
        try:
            self._console.print_markdown(clean_buffer)
        except Exception:
            self._console.print_text(clean_buffer)

    def _reset_buffer_state(self) -> None:
        self._buffer = ""
        self._last_token_time = 0.0
        self._stale_warned = False
        self._start_time = 0.0

    def append(self, content: str) -> None:
        if not content:
            return
        if self._coord is not None:
            if not self._coord_active:
                self._buffer = ""
                self._stale_warned = False
                self._start_time = time.monotonic()
                self._coord.acquire(OWNER_TOKEN_STREAM)
                self._coord_active = True
            self._buffer += content
            self._last_token_time = time.monotonic()
            self._coord.update(OWNER_TOKEN_STREAM, self._render_streaming())
            return

        if self._live is None:
            self._buffer = ""
            self._stale_warned = False
            self._start_time = time.monotonic()
            self._live = Live(
                Text(""),
                console=self._console.console,
                refresh_per_second=_REFRESH_HZ,
                transient=True,
            )
            self._live.start()
        self._buffer += content
        self._last_token_time = time.monotonic()
        try:
            self._live.update(self._render_streaming())
        except Exception:
            pass

    def finalize(self) -> None:
        """Stop the Live block and print the final markdown after a ⏺ leader.

        No trailing ``console.rule`` — the next turn provides separation
        via a blank line, matching Claude Code's calmer rhythm.
        """
        if self._coord is not None:
            if not self._coord_active:
                return
            buf_present = bool(self._buffer)
            self._coord_active = False
            # ``on_release`` runs AFTER the Live region is torn down, so
            # the markdown lands in scrollback without competing with a
            # still-active region.
            self._coord.release(
                OWNER_TOKEN_STREAM,
                on_release=self._flush_final if buf_present else None,
            )
            self._reset_buffer_state()
            return

        if self._live is None:
            return
        try:
            self._live.stop()
        except Exception:
            pass
        self._live = None
        self._flush_final()
        self._reset_buffer_state()

    def discard(self) -> None:
        """Stop the Live block WITHOUT printing the buffer.

        Used when the accumulated tokens are about to be replaced by a
        cleaned version (e.g., chat content with intent markers stripped),
        so the raw stream shouldn't land in scrollback.
        """
        if self._coord is not None:
            if self._coord_active:
                self._coord_active = False
                self._coord.release(OWNER_TOKEN_STREAM)
            self._reset_buffer_state()
            return

        if self._live is not None:
            try:
                self._live.stop()
            except Exception:
                pass
            self._live = None
        self._reset_buffer_state()

    def maybe_warn_stale(self) -> bool:
        """Print STREAM_INTERRUPTED if the stream has been idle too long.

        Returns True if a warning was printed (caller may want to abort).
        """
        if not self.is_active or self._stale_warned:
            return False
        if self._last_token_time <= 0:
            return False
        if (time.monotonic() - self._last_token_time) <= _STREAM_TIMEOUT:
            return False
        self._stale_warned = True

        if self._coord is not None:
            buf_present = bool(self._buffer)
            self._coord_active = False
            self._coord.release(
                OWNER_TOKEN_STREAM,
                on_release=self._flush_final if buf_present else None,
            )
            self._buffer = ""
        else:
            try:
                if self._live is not None:
                    self._live.stop()
            except Exception:
                pass
            self._live = None
            if self._buffer:
                self._flush_final()
                self._buffer = ""

        warn = Text()
        warn.append(f"  {Icons.WARNING} ", style=f"bold {Colors.WARNING}")
        warn.append(strings.STREAM_INTERRUPTED, style=Colors.WARNING)
        self._console.print(warn)
        self._start_time = 0.0
        return True


_THINKING_VERB_TTL = 10.0           # rotate the header verb every ~10 s
_THINKING_SPINNER_HZ = 6.0          # breathing spinner cadence (frames/sec)
_JUSTIFICATION_MAX_LEN = 80         # truncate long CoT sentences to fit narrow panels
_JUSTIFICATION_MIN_LEN = 4          # below this, treat the chunk as noise (e.g. " 。")

# Pipeline graph node → stepper phase key, including ``intent_clarification``.
# We re-implement the mapping locally instead of importing ``phase_for_node``
# from phase_timeline because that one deliberately *excludes*
# ``intent_clarification`` (chat turns shouldn't paint the 5-stage stepper).
# For the thinking header we DO want the phase label even on chat turns —
# "意图识别 · 思考中..." beats a bare "思考中..." for telling the user
# what the agent is currently weighing.
_THINKING_NODE_PHASE_MAP = {
    "intent_clarification": "intent",
    "safety_check": "safety",
    "confirmation_gate": "safety",
    "baseline_capture": "inject",
    "agent_loop": "inject",
    "execute_loop": "inject",
    "direct_execute": "inject",
    "verifier_loop": "verify",
    "recover_verifier_loop": "recovery",
    "recover_handler": "recovery",
}

# Sentence terminators we use to split the CoT buffer into "complete" sentences.
# Mixes CJK and ASCII punctuation since reasoning content is often bilingual.
_SENTENCE_SPLIT_RE = re.compile(r"[。！？.!?]+")


def _phase_label_for_node(node: str) -> str:
    """Resolve the structure label for line 1 of the thinking header.

    Falls back to ``"思考"`` when the node isn't tracked or is empty —
    that keeps the rendering useful even for events that arrive before
    the graph has settled on a node, and for any future node we forget
    to map (better a generic verb than the literal node name leaking
    into the UI).
    """
    if not node:
        return strings.ROLE_LABELS.get("thinking", "思考")
    phase_key = _THINKING_NODE_PHASE_MAP.get(node, "")
    if phase_key:
        return strings.PHASE_NAMES.get(phase_key, phase_key)
    return strings.ROLE_LABELS.get("thinking", "思考")


def _extract_last_sentence(buffer: str) -> str:
    """Pick the most recent complete sentence from the CoT stream.

    Returns ``""`` while no terminator has arrived — caller treats that
    as "line 2 has no real content yet" and renders only line 1, per
    §9.4 ("don't show a placeholder"). When several sentences exist we
    return the last one because it represents the *current* line of
    reasoning; earlier ones are stale.

    Truncation to ``_JUSTIFICATION_MAX_LEN`` keeps the panel one line
    wide on narrow terminals; an ellipsis marks the cut so the user
    knows there's more.
    """
    if not buffer:
        return ""
    parts = [p.strip() for p in _SENTENCE_SPLIT_RE.split(buffer) if p and p.strip()]
    # If the buffer ends with content that's NOT followed by a terminator,
    # that trailing chunk is mid-sentence — drop it. We only render
    # *complete* sentences so the line doesn't flicker character-by-character.
    if not _SENTENCE_SPLIT_RE.search(buffer.rstrip()):
        # No terminator at all → nothing complete yet.
        return ""
    if not _SENTENCE_SPLIT_RE.search(buffer[-1:]):
        # Ends mid-sentence: trim the dangling fragment.
        parts = parts[:-1] if parts else parts
    if not parts:
        return ""
    last = parts[-1]
    if len(last) < _JUSTIFICATION_MIN_LEN:
        return ""
    if len(last) > _JUSTIFICATION_MAX_LEN:
        return last[: _JUSTIFICATION_MAX_LEN - 1].rstrip() + "\u2026"
    return last


class ThinkingPrinter:
    """Two-line thinking display — structure + justification (PR-B2 / §9.4).

    Behaviour contract:
      - During reasoning: a transient block whose first line is
        ``✻ <phase_label> · <verb>...`` (structure: which graph phase
        the agent is in, plus a rotating gerund). When the LLM has
        produced at least one complete sentence in its CoT, a second
        line ``└─ <last sentence>`` (justification: why) appears
        beneath it. Until then we render only line 1, so the user
        doesn't see a stub or placeholder.
      - The raw chain-of-thought as a whole is still **not** spilled to
        scrollback — only the latest complete sentence appears, and it
        lives only inside the transient Live block. On ``finalize`` the
        block disappears entirely.

    The reason we even keep the spinner running mid-stream is that a model
    can pause for several seconds between reasoning tokens, and a frozen
    line reads as a hang. The 6 Hz tick keeps the line alive without
    drowning the terminal in updates.

    PR-E2 — when a ``LiveCoordinator`` is injected, this printer paints
    into the coord's **region slot** under ``OWNER_THINKING``. That
    lets thinking → token / tool transitions rotate region ownership
    without the stop-start flicker the standalone Live block would
    cause. The thinking content is still discarded on finalize (per
    §9.4); we just route the *during-render* paints through the
    shared region.
    """

    def __init__(
        self,
        console: ChaosConsole,
        inflight=None,
        *,
        coordinator: Optional[LiveCoordinator] = None,
    ) -> None:
        self._console = console
        self._buffer: str = ""
        self._live: Optional[Live] = None  # used only when no coordinator
        # PR-E2 — coord routing. When set, region slot ownership is the
        # source of truth for "is the thinking block painting?"; the
        # local ``_coord_active`` mirror lets us tear down internal
        # state (tick thread, buffer, verb cache) even when ownership
        # has already been rotated away by another region owner.
        self._coord: Optional[LiveCoordinator] = coordinator
        self._coord_active: bool = False
        self._last_token_time: float = 0.0
        self._verb: str = ""
        self._verb_picked_at: float = 0.0
        self._stop_event: Optional[threading.Event] = None
        self._tick_thread: Optional[threading.Thread] = None
        self._node: str = ""
        # PR-E5 — when present, the printer prefers the tracker's verb
        # hint (e.g. "调用 kubectl") over the random pool. Optional so
        # standalone tests / older callers still work.
        self._inflight = inflight

    @property
    def is_active(self) -> bool:
        if self._coord is not None:
            return self._coord_active
        return self._live is not None

    def _pick_verb(self) -> str:
        """Sample a verb; called once at start and every TTL seconds after.

        PR-E5 — when the in-flight tracker has a concrete hint ("调用
        kubectl" / "生成回复"), use it verbatim and skip the TTL check
        in the caller. The hint already mirrors live state, so refreshing
        it on each tick is the correct cadence.
        """
        if self._inflight is not None:
            try:
                hint = self._inflight.verb_hint()
            except Exception:
                hint = None
            if hint:
                return hint
        return random.choice(strings.THINKING_VERBS)

    def _render(self) -> Text:
        """Render the structure (line 1) and optional justification (line 2)."""
        now = time.monotonic()
        # When the tracker offers a live hint, refresh on every render so
        # the verb tracks events. Otherwise fall back to the TTL-gated
        # random pool to avoid jittering between samples.
        live_hint = None
        if self._inflight is not None:
            try:
                live_hint = self._inflight.verb_hint()
            except Exception:
                live_hint = None
        if live_hint:
            self._verb = live_hint
            self._verb_picked_at = now
        elif not self._verb_picked_at or (now - self._verb_picked_at) >= _THINKING_VERB_TTL:
            self._verb = self._pick_verb()
            self._verb_picked_at = now
        frame_idx = int((now * _THINKING_SPINNER_HZ)) % len(BREATHING_DOTS)
        glyph = BREATHING_DOTS[frame_idx]

        phase_label = _phase_label_for_node(self._node)

        text = Text()
        # Glyph: gradient bright — stands out from the grey body
        text.append(f" {glyph} ", style=Theme.gradient_bright)
        # Phase label: gradient mid — bold makes it pop, natural bright→mid→dim flow
        text.append(f"{phase_label} \u00b7 ", style=f"bold {Theme.gradient_mid}")
        # Verb: muted grey — recedes from the label, creating a natural
        # visual gradient (accent → muted) across the single line
        text.append(f"{self._verb}...", style=f"italic {Theme.text_muted}")

        justification = _extract_last_sentence(self._buffer)
        if justification:
            text.append("\n")
            text.append(" \u23bf ", style=f"italic {Theme.gradient_mid}")
            text.append(justification, style=f"italic {Theme.gradient_dim}")
        return text

    def _tick_loop(self) -> None:
        """Background driver — re-renders at spinner cadence so the glyph
        animates even when no new tokens arrive. Lives for the duration of
        the active block (coord region or local Live); exits as soon as
        the stop_event is set.

        Coord path: the loop calls ``coord.update(OWNER_THINKING, ...)``.
        If ownership has been rotated to another region owner, the
        coordinator silently drops the update (returns False) — that's
        the contract pinned by ``test_live_coordinator.py``. The loop
        keeps running until ``stop_event`` is set in finalize.
        """
        interval = 1.0 / _THINKING_SPINNER_HZ
        while self._stop_event is not None and not self._stop_event.is_set():
            if self._coord is not None:
                try:
                    self._coord.update(OWNER_THINKING, self._render())
                except Exception:
                    return
            elif self._live is not None:
                try:
                    self._live.update(self._render())
                except Exception:
                    return
            time.sleep(interval)

    def append(self, content: str, node: str = "") -> None:
        if not content:
            return

        if self._coord is not None:
            if not self._coord_active:
                self._buffer = ""
                self._verb = self._pick_verb()
                self._verb_picked_at = time.monotonic()
                self._node = node
                # NOTE: no scrollback blank line here. ThinkingPrinter
                # content is transient (discarded on finalize). A blank
                # line written now would remain in scrollback after the
                # thinking block is torn down, creating a phantom gap
                # that stacks with StreamingPrinter's own vertical breath.
                # The separator before agent text comes from
                # StreamingPrinter._flush_final() instead.
                self._coord.acquire(OWNER_THINKING)
                self._coord_active = True
                self._coord.update(OWNER_THINKING, self._render())
                self._stop_event = threading.Event()
                self._tick_thread = threading.Thread(
                    target=self._tick_loop, daemon=True
                )
                self._tick_thread.start()
            if node:
                self._node = node
            self._buffer += content
            self._last_token_time = time.monotonic()
            return

        # Legacy path — own Live block.
        if self._live is None:
            self._buffer = ""
            self._verb = self._pick_verb()
            self._verb_picked_at = time.monotonic()
            self._node = node
            self._live = Live(
                self._render(),
                console=self._console.console,
                refresh_per_second=_THINKING_SPINNER_HZ,
                transient=True,
            )
            self._live.start()
            self._stop_event = threading.Event()
            self._tick_thread = threading.Thread(target=self._tick_loop, daemon=True)
            self._tick_thread.start()
        # A later event can carry a more specific node than the first
        # token (e.g. the first chunk arrived before the graph entered
        # the node). Always prefer the latest non-empty node.
        if node:
            self._node = node
        self._buffer += content
        self._last_token_time = time.monotonic()

    def finalize(self) -> None:
        """Stop the active block; deliberately emit nothing to scrollback.

        The thinking content is intentionally discarded — chaos-eng users
        want decisions and outcomes, not a chain-of-thought transcript.
        Surfacing this via ``/review`` or ``memory/decisions/<task>.json``
        is a future PR.

        Coord path: ``release(OWNER_THINKING)`` without an
        ``on_release`` callback. If thinking has already lost
        ownership via rotation, release is a no-op on the coord side
        (owner-scoped) and we just clear local state. The tick thread
        exits on the next iteration via ``stop_event``.
        """
        if self._coord is not None:
            if not self._coord_active:
                return
            if self._stop_event is not None:
                self._stop_event.set()
            self._coord_active = False
            # No on_release — thinking content is discarded, not flushed
            # to scrollback. Release is owner-scoped; if rotation already
            # happened it's a clean no-op on the coordinator side.
            self._coord.release(OWNER_THINKING)
            self._tick_thread = None
            self._stop_event = None
            self._buffer = ""
            self._last_token_time = 0.0
            self._verb = ""
            self._verb_picked_at = 0.0
            self._node = ""
            return

        # Legacy path
        if self._live is None:
            return
        if self._stop_event is not None:
            self._stop_event.set()
        try:
            self._live.stop()
        except Exception:
            pass
        self._live = None
        self._tick_thread = None
        self._stop_event = None
        self._buffer = ""
        self._last_token_time = 0.0
        self._verb = ""
        self._verb_picked_at = 0.0
        self._node = ""
