"""Locator allocator — short stable handles for experiments and tool calls.

PR-D4 — every experiment card and tool panel that scrolls past the user
gets a one-letter-plus-number tag (``E1``, ``T3``) the user can refer
back to with ``/show E1``, ``/copy T3`` and ``/rerun E1``. Lives on
``SessionState`` so it survives across multiple turns within a session
but resets per process.

Design intent:

* **Sequential**, monotonically increasing within a session — matches the
  user's reading order so ``E2`` is always after ``E1`` in the
  transcript.
* **Independent counters** for experiments vs tool calls so the user
  doesn't have to remember "tool call #6 == experiment #2" when those
  are interleaved.
* **Snapshots are stored alongside the id** so ``/show`` can re-render
  the panel without re-running the experiment (which would be
  destructive) or asking the LLM to recreate the prose.
* **Display-mode-agnostic** at the allocator layer — calm mode still
  *records* snapshots (so a mid-conversation switch to dense doesn't
  hide locators that already happened); only the *render* of the label
  is gated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Two independent counters keep E and T sequences readable on their own
# without having to mentally subtract the other. The user cares about
# "the third experiment", not "the sixth thing that happened."
_PREFIX_EXPERIMENT = "E"
_PREFIX_TOOL = "T"


@dataclass
class LocatorRecord:
    """A single recorded snapshot keyed by a locator string.

    ``kind`` discriminates the display path: ``experiment`` snapshots are
    re-rendered through the experiment-card builder, ``tool`` snapshots
    through the tool-panel builder. Other future kinds (results, errors)
    can be added without changing the allocator.

    ``payload`` is intentionally a free-form dict — the renderer that
    *recorded* the snapshot is the same renderer that *re-renders* it
    via ``/show``, so the contract is internal to that renderer pair.
    """

    locator: str
    kind: str   # "experiment" | "tool"
    payload: dict = field(default_factory=dict)


class LocatorAllocator:
    """Issues sequential locators and stores the snapshots behind them.

    The class is intentionally tiny — no LRU, no eviction, no persistence.
    A long-running session that allocated thousands of tool calls would
    hold all those payloads in memory; in practice TUI sessions stay in
    the tens of allocations and bouncing the process clears everything.

    Lookup is case-insensitive on the locator (``e1`` matches ``E1``)
    because users will type both, and the prefix is uppercase by
    convention. Returns ``None`` for misses so callers can render a
    helpful "no such locator" message instead of crashing.
    """

    def __init__(self) -> None:
        self._next_e: int = 1
        self._next_t: int = 1
        self._records: dict[str, LocatorRecord] = {}

    def allocate_experiment(self, payload: Optional[dict] = None) -> str:
        loc = f"{_PREFIX_EXPERIMENT}{self._next_e}"
        self._next_e += 1
        self._records[loc] = LocatorRecord(
            locator=loc, kind="experiment", payload=dict(payload or {})
        )
        return loc

    def allocate_tool(self, payload: Optional[dict] = None) -> str:
        loc = f"{_PREFIX_TOOL}{self._next_t}"
        self._next_t += 1
        self._records[loc] = LocatorRecord(
            locator=loc, kind="tool", payload=dict(payload or {})
        )
        return loc

    def update_payload(self, locator: str, **fields) -> None:
        """Merge fields into an existing record's payload.

        Used by tool_panel: a tool call gets its locator allocated at
        ``start`` (so [T#] can render in the title before output exists),
        then the output is merged in via ``update_payload`` once the call
        completes. No-op when the locator is unknown — defensive against
        a tool_end without a matching tool_start.
        """
        rec = self._records.get(locator.upper())
        if rec is None:
            return
        rec.payload.update(fields)

    def get(self, locator: str) -> Optional[LocatorRecord]:
        return self._records.get((locator or "").upper())

    def list_experiments(self) -> list[LocatorRecord]:
        return [r for r in self._records.values() if r.kind == "experiment"]

    def list_tools(self) -> list[LocatorRecord]:
        return [r for r in self._records.values() if r.kind == "tool"]

    def reset(self) -> None:
        """Wipe counters and snapshots — used by tests and ``/clear``."""
        self._next_e = 1
        self._next_t = 1
        self._records.clear()
