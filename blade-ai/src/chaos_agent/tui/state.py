"""Reactive session state — single source of truth for TUI state.

All widgets watch these reactive properties instead of querying each other.
Controllers mutate state; widgets observe and render.
"""

from __future__ import annotations

import time
import uuid
from enum import Enum


class PermissionMode(Enum):
    CONFIRM = "confirm"
    AUTO = "auto"


class ConversationMode(Enum):
    DIRECT = "direct"
    EXPLORATION = "exploration"
    AMBIGUOUS = "ambiguous"


class DisplayMode(Enum):
    """Information density mode (PR-D1 / §17.1).

    Three Pareto points on the cognitive-load curve:

    - ``CALM``    — newcomers, demos, recordings. Hides the experiment
      card, blast meter, locator, sparkline and keymap footer; keeps
      decision summary, tool inline, final result.
    - ``WORKING`` — daily driver (the default). Adds the experiment
      card header, ``failure_reason`` and confirm risk meter on top of
      calm; replan history and locators stay collapsed.
    - ``DENSE``   — power users, postmortems. Everything on: full
      sparklines, locators, replan timeline, side_effects.

    Renderers branch on ``state.display_mode``; the cycle order matches
    the on-ramp ``calm → working → dense`` so a Ctrl-G tap walks the
    user up the density ladder.
    """

    CALM = "calm"
    WORKING = "working"
    DENSE = "dense"


class SessionState:
    """Observable session state shared across all TUI components.

    Note: Textual reactive descriptors only work on Widget subclasses.
    This plain class stores state and controllers call widget-level
    update methods when state changes. This avoids coupling state to
    any single widget.
    """

    def __init__(self) -> None:
        self.permission_mode: PermissionMode = PermissionMode.CONFIRM
        self.conversation_mode: ConversationMode = ConversationMode.DIRECT
        # PR-D1 §17.1: default to "working" so the daily driver keeps the
        # experiment card / failure_reason / confirm risk meter that
        # distinguish blade-ai from a generic chat. Newcomers can drop to
        # ``calm`` via Ctrl-G or ``/mode calm``.
        self.display_mode: DisplayMode = DisplayMode.WORKING
        self.namespace: str = "default"
        self.cluster_name: str = ""
        self.active_task_id: str = ""
        self.current_phase: str = ""
        self.is_streaming: bool = False
        self.is_dry_run: bool = False
        self.connection_status: str = "unknown"  # connected / disconnected / checking
        self.config_complete: bool = False
        self.onboarding_skipped: bool = False
        self.active_task_count: int = 0

        # TUI session identity: minted once per process; each inject/recover
        # task is indexed under this id in memory/sessions/<tui_session_id>.json.
        self.tui_session_id: str = f"ses-{uuid.uuid4()}"
        self.session_start_ts: float = time.time()
        self.message_count: int = 0
        self.injection_count: int = 0
        self.injection_success: int = 0
        self.injection_fail: int = 0
        self.recovery_count: int = 0

        # Token/context tracking (for status bar display)
        self.token_count_input: int = 0
        self.token_count_output: int = 0
        self.context_limit: int = 128000  # Default context window size
        self.model_name: str = ""

        # PR-E8 — cumulative session cost (USD) and a rolling buffer of
        # per-turn wall-clock latencies. Cost is computed from token
        # counts using a default pricing table; the buffer feeds a p95
        # so the footer can show "p95 12.3s" rather than every spike.
        self.usd_cost: float = 0.0
        self.latencies_ms: list[int] = []
        # Cap the buffer so a long session can't grow unbounded; p95 over
        # the most recent 50 turns is what users actually compare against.
        self._latency_max_samples: int = 50

        # Turn timing
        self.current_turn_start: float = 0.0

        # PR-D4 — locator allocator for [E#] / [T#] short handles.
        # Lives on the session so /show / /copy / /rerun can resolve a
        # locator across turns. Reset only on /clear or process restart.
        from chaos_agent.tui.locators import LocatorAllocator
        self.locators: LocatorAllocator = LocatorAllocator()

        # Slash-command popup (rendered by the bottom toolbar; ↑/↓
        # bindings update this index and Enter applies the selection).
        self.slash_selected_index: int = 0
        # Signature of the most recently computed menu (mode + root + count).
        # Used by compute_slash_menu to reset the cursor when the candidate
        # list is materially replaced (e.g. switching from root to sub mode).
        self.slash_menu_signature: str = ""

        self._listeners: list = []

    def add_listener(self, callback) -> None:
        """Register a callback(state, field_name) for any state change."""
        self._listeners.append(callback)

    def remove_listener(self, callback) -> None:
        self._listeners = [cb for cb in self._listeners if cb is not callback]

    def _notify(self, field: str) -> None:
        for cb in self._listeners:
            try:
                cb(self, field)
            except Exception:
                pass

    def set_permission_mode(self, mode: PermissionMode) -> None:
        self.permission_mode = mode
        self._notify("permission_mode")

    def set_namespace(self, ns: str) -> None:
        self.namespace = ns
        self._notify("namespace")

    def set_cluster_name(self, name: str) -> None:
        self.cluster_name = name
        self._notify("cluster_name")

    def set_active_task(self, task_id: str) -> None:
        self.active_task_id = task_id
        self._notify("active_task_id")

    def set_current_phase(self, phase: str) -> None:
        self.current_phase = phase
        self._notify("current_phase")

    def set_streaming(self, streaming: bool) -> None:
        self.is_streaming = streaming
        self._notify("is_streaming")

    def set_dry_run(self, value: bool) -> None:
        if self.is_dry_run == value:
            return
        self.is_dry_run = value
        self._notify("is_dry_run")

    def set_connection_status(self, status: str) -> None:
        self.connection_status = status
        self._notify("connection_status")

    def set_config_complete(self, complete: bool) -> None:
        self.config_complete = complete
        self._notify("config_complete")

    def set_active_task_count(self, count: int) -> None:
        self.active_task_count = count
        self._notify("active_task_count")

    def cycle_permission_mode(self) -> PermissionMode:
        """Cycle through permission modes and return the new mode."""
        modes = list(PermissionMode)
        idx = modes.index(self.permission_mode)
        new_mode = modes[(idx + 1) % len(modes)]
        self.set_permission_mode(new_mode)
        return new_mode

    def set_display_mode(self, mode: DisplayMode) -> None:
        """Set the information-density mode and notify listeners."""
        if self.display_mode == mode:
            return
        self.display_mode = mode
        self._notify("display_mode")

    def cycle_display_mode(self) -> DisplayMode:
        """Walk calm → working → dense → calm and return the new mode.

        The order matches the cognitive-load ramp so a Ctrl-G tap from
        the default ``working`` lands on ``dense`` (the next step up),
        and a second tap rolls back to ``calm`` (the simplest view).
        """
        order = [DisplayMode.CALM, DisplayMode.WORKING, DisplayMode.DENSE]
        try:
            idx = order.index(self.display_mode)
        except ValueError:
            idx = order.index(DisplayMode.WORKING)
        new_mode = order[(idx + 1) % len(order)]
        self.set_display_mode(new_mode)
        return new_mode

    def set_model_name(self, name: str) -> None:
        self.model_name = name
        self._notify("model_name")

    def set_context_limit(self, limit: int) -> None:
        self.context_limit = limit
        self._notify("context_limit")

    def add_tokens(self, input_tokens: int = 0, output_tokens: int = 0) -> None:
        """Accumulate token usage from a model response.

        Side effect (PR-E8): rolls the same counts into the USD cost
        accumulator using the active model's pricing. Decoupling cost
        accounting from token accounting would let the two drift —
        every input_tokens delta has a corresponding cost delta, so we
        co-locate them.
        """
        self.token_count_input += input_tokens
        self.token_count_output += output_tokens
        self._notify("token_count")
        if input_tokens > 0 or output_tokens > 0:
            self.add_cost(input_tokens, output_tokens)

    def start_turn(self) -> None:
        """Mark the start of an agent turn for timing."""
        self.current_turn_start = time.time()

    def end_turn(self) -> float:
        """Mark end of turn and return elapsed seconds."""
        if self.current_turn_start > 0:
            elapsed = time.time() - self.current_turn_start
            self.current_turn_start = 0.0
            self.record_turn_latency_ms(int(elapsed * 1000))
            return elapsed
        return 0.0

    # -- PR-E8: cost + latency accounting ---------------------------------

    def add_cost(self, input_tokens: int, output_tokens: int, model: str = "") -> None:
        """Accumulate USD cost using the default pricing table.

        Pricing is approximate by design — the goal is "is this turn
        roughly cheap or expensive" rather than billing-accurate accounting.
        Unknown models fall back to a mid-tier rate so the display still
        moves; users with their own contracts can override via env later.
        """
        from chaos_agent.tui.pricing import resolve_pricing

        in_per_1k, out_per_1k = resolve_pricing(model or self.model_name)
        delta = (input_tokens / 1000.0) * in_per_1k + (output_tokens / 1000.0) * out_per_1k
        if delta > 0:
            self.usd_cost += delta
            self._notify("usd_cost")

    def record_turn_latency_ms(self, ms: int) -> None:
        """Append a turn's wall-clock latency; trim to the rolling window."""
        if ms <= 0:
            return
        self.latencies_ms.append(ms)
        if len(self.latencies_ms) > self._latency_max_samples:
            del self.latencies_ms[: len(self.latencies_ms) - self._latency_max_samples]
        self._notify("latencies_ms")

    def latency_p95_ms(self) -> int:
        """p95 over the rolling buffer; 0 when no samples yet.

        Uses nearest-rank rather than interpolation — with only ~50
        samples interpolation is overkill, and integer ms reads cleanly
        in the toolbar.
        """
        if not self.latencies_ms:
            return 0
        ordered = sorted(self.latencies_ms)
        idx = max(0, int(round(0.95 * len(ordered))) - 1)
        return ordered[min(idx, len(ordered) - 1)]
