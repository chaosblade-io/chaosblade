"""Shared bounded-yet-complete pagination contract for list-shaped tools.

Root cause IV (presentation contract lacked a first-class "bounded but
completely visible" citizen): a list tool that can exceed the context
budget must page — but paging has to stay TRANSPARENT, never a silent
``[:N]`` cap. A silent cap lets the program decide which facts the model
may see and can drop the very row the user means with no signal it ever
existed. Transparent paging keeps the model in charge: the header always
states the TRUE total, exactly how many rows are withheld, and the offset
that fetches the next page — the real target is only ever DEFERRED to a
later page, never lost.

Before this module the geometry (total / ``showing rows X-Y`` / ``N not
shown`` / next offset / past-end explanation) was hand-rolled inside
``query_active_experiments``. It is the ONE consumer today, but the
geometry is universal while only the domain nouns vary, so the structure —
the part that must not drift between list tools — lives here and each
caller injects its wording through :class:`PageLabels`. This is the
"bounded-yet-complete" output contract as a reusable primitive.

Two boundaries this MUST NOT cross:

* **Never sinks into the store.** Paging happens strictly ABOVE the data
  layer: the caller fetches the FULL set (``store.query_active`` with no
  limit/offset — memory_nodes.load_memory and cli/runner both depend on
  the complete set) and hands it to :func:`paginate`, which slices a view.
* **Never silent.** The withheld count and next offset are load-bearing;
  a page that drops rows without saying so is a contract break.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TypeVar

__all__ = ["PageLabels", "paginate"]

T = TypeVar("T")


@dataclass(frozen=True)
class PageLabels:
    """Domain wording for the transparent-pagination header/footer.

    The pagination GEOMETRY (total / ``showing rows X-Y`` / ``N withheld``
    / next offset / past-end) is universal and lives in :func:`paginate`;
    these fields inject only the domain nouns so the structure — the thing
    that must not drift between list tools — is shared, not re-derived per
    caller.

    Attributes:
        summary: ``total ->`` the leading clause naming the collection,
            e.g. ``"There are 25 recoverable active experiment(s)"``. Used
            verbatim in BOTH the normal header and the past-end message, so
            the two cannot disagree about what is being counted.
        withheld: ``remaining ->`` the clause naming the hidden tail, e.g.
            ``"5 older experiment(s) not shown"``. The ``; call again with
            offset=N`` cursor hint is appended by :func:`paginate`.
        ordering: parenthetical sort note placed after ``summary`` in the
            header (e.g. ``" (most recently injected first)"``); empty when
            the order needs no explanation.
        footer: trailing how-to-use hint appended as its own paragraph;
            empty to omit.
        past_end_hint: the recovery instruction appended when ``offset``
            lands past the last row (e.g. ``"pass offset=0 to start from
            the most recently injected."``).
    """

    summary: Callable[[int], str]
    withheld: Callable[[int], str]
    ordering: str = ""
    footer: str = ""
    past_end_hint: str = "pass offset=0 to start from the top."


def paginate(
    rows: Sequence[T],
    *,
    limit: int,
    offset: int,
    renderer: Callable[[int, T], str],
    labels: PageLabels,
) -> str:
    """Render one transparent page of ``rows`` (the bounded-yet-complete form).

    Args:
        rows: the FULL ordered collection (the caller already fetched and
            sorted it — this function never queries anything, so paging
            cannot sink into the store).
        limit: page size; clamped to ``>= 1`` so a non-positive value can
            never produce a degenerate empty slice on in-range offsets.
        offset: rows to skip; clamped to ``>= 0``.
        renderer: ``(continuous_1_based_index, row) -> line``. The index
            continues across pages (page 2 starts at ``offset + 1``), never
            restarts at 1 — the model must be able to correlate a row with
            its absolute position.
        labels: the domain wording bundle (see :class:`PageLabels`).

    Returns:
        The composed page text. When ``offset`` lands past the last row an
        explanatory empty page is returned (total + why + how to restart)
        rather than a bare empty string — a silent blank would look like
        "nothing exists" when the truth is "you paged too far".
    """
    total = len(rows)
    start = max(0, offset)
    step = max(1, limit)
    page = list(rows[start : start + step])
    if not page:
        return (
            f"{labels.summary(total)}, but offset={offset} is past the end; "
            f"{labels.past_end_hint}"
        )
    lines = [
        f"{labels.summary(total)}{labels.ordering}; "
        f"showing rows {start + 1}-{start + len(page)}."
    ]
    for i, row in enumerate(page, start + 1):
        lines.append(renderer(i, row))
    remaining = total - (start + len(page))
    if remaining > 0:
        lines.append(
            f"\n{labels.withheld(remaining)}; call again with "
            f"offset={start + len(page)} for the next page."
        )
    if labels.footer:
        lines.append(f"\n{labels.footer}")
    return "\n".join(lines)
