"""Unit tests for the shared bounded-yet-complete pagination contract.

The ``query_active_experiments`` behaviour is pinned end-to-end in
``test_intent_handlers.py``; these tests pin the WRAPPER itself and — via a
second, differently-shaped consumer — prove the geometry is genuinely
generic rather than experiment-specific (root cause IV: the contract is a
first-class citizen any list tool reuses, not a one-off retrofit).
"""

from __future__ import annotations

from chaos_agent.utils.pagination import PageLabels, paginate

# Consumer A: the real experiment-listing wording.
_EXP_LABELS = PageLabels(
    summary=lambda n: f"There are {n} recoverable active experiment(s)",
    withheld=lambda n: f"{n} older experiment(s) not shown",
    ordering=" (most recently injected first)",
    footer='then call recover_task(task_id="...").',
    past_end_hint="pass offset=0 to start from the most recently injected.",
)


def _exp_renderer(i: int, row: dict) -> str:
    return f"{i}. {row['id']}"


# Consumer B: a completely different collection shape and wording — the
# reuse proof. Same geometry, different nouns, no ordering note, no footer.
_NODE_LABELS = PageLabels(
    summary=lambda n: f"{n} node(s) matched",
    withheld=lambda n: f"{n} more node(s) beyond this page",
    past_end_hint="pass offset=0 to restart from the first node.",
)


def _node_renderer(i: int, row: str) -> str:
    return f"[{i}] {row}"


class TestGeometry:
    def test_header_is_transparent_not_silent(self):
        rows = [{"id": f"task-{k:03d}"} for k in range(25)]
        out = paginate(rows, limit=20, offset=0, renderer=_exp_renderer,
                       labels=_EXP_LABELS)
        # True total (25, not the page size), the exact withheld count, and
        # the offset that fetches the next page — the three facts that turn a
        # bounded view into a bounded-yet-COMPLETE one.
        assert "There are 25 recoverable active experiment(s)" in out
        assert "showing rows 1-20" in out
        assert "5 older experiment(s) not shown" in out
        assert "offset=20" in out

    def test_row_numbering_is_continuous_across_pages(self):
        rows = [{"id": f"task-{k:03d}"} for k in range(25)]
        out = paginate(rows, limit=20, offset=20, renderer=_exp_renderer,
                       labels=_EXP_LABELS)
        assert "showing rows 21-25" in out
        assert "21. task-020" in out
        # Last page: no further-page hint.
        assert "older experiment(s) not shown" not in out

    def test_offset_past_end_explains_rather_than_blank(self):
        rows = [{"id": f"task-{k:03d}"} for k in range(5)]
        out = paginate(rows, limit=20, offset=999, renderer=_exp_renderer,
                       labels=_EXP_LABELS)
        assert "past the end" in out
        assert "pass offset=0 to start from the most recently injected." in out
        # Never a bare empty string — that would read as "nothing exists".
        assert out.strip()

    def test_bounds_are_guarded(self):
        rows = [{"id": f"task-{k:03d}"} for k in range(5)]
        # limit clamps to >=1: a non-positive limit still yields one row,
        # never a degenerate empty slice on an in-range offset.
        out = paginate(rows, limit=0, offset=0, renderer=_exp_renderer,
                       labels=_EXP_LABELS)
        assert "showing rows 1-1" in out
        # offset clamps to >=0.
        out2 = paginate(rows, limit=2, offset=-5, renderer=_exp_renderer,
                        labels=_EXP_LABELS)
        assert "showing rows 1-2" in out2

    def test_walking_pages_loses_no_row(self):
        rows = [{"id": f"task-{k:03d}"} for k in range(45)]
        seen, offset = set(), 0
        while True:
            out = paginate(rows, limit=20, offset=offset, renderer=_exp_renderer,
                           labels=_EXP_LABELS)
            for k in range(45):
                if f"task-{k:03d}" in out:
                    seen.add(f"task-{k:03d}")
            if f"offset={offset + 20}" not in out:
                break
            offset += 20
        assert seen == {f"task-{k:03d}" for k in range(45)}


class TestReusability:
    """The load-bearing answer to 'is a one-consumer wrapper over-engineered?':
    a second, unrelated collection renders through the SAME geometry with only
    its nouns swapped — the structure is shared, not re-derived."""

    def test_second_consumer_gets_same_geometry_different_nouns(self):
        rows = [f"node-{k}" for k in range(7)]
        out = paginate(rows, limit=3, offset=0, renderer=_node_renderer,
                       labels=_NODE_LABELS)
        assert "7 node(s) matched" in out
        assert "showing rows 1-3" in out
        assert "[1] node-0" in out
        assert "4 more node(s) beyond this page" in out
        assert "offset=3" in out
        # No ordering note / footer for this consumer — the optional labels
        # are genuinely optional, not experiment-specific requirements.
        assert "most recently injected" not in out
        assert "recover_task" not in out

    def test_second_consumer_past_end_uses_its_own_hint(self):
        rows = [f"node-{k}" for k in range(2)]
        out = paginate(rows, limit=5, offset=50, renderer=_node_renderer,
                       labels=_NODE_LABELS)
        assert "2 node(s) matched" in out
        assert "past the end" in out
        assert "pass offset=0 to restart from the first node." in out

    def test_wrapper_is_pure_over_the_given_rows(self):
        # paginate never queries anything: identical inputs give identical
        # output, and the source rows are not mutated (the caller's full set —
        # the store-boundary contract — is left intact for other consumers).
        rows = [{"id": f"task-{k:03d}"} for k in range(10)]
        snapshot = [dict(r) for r in rows]
        a = paginate(rows, limit=4, offset=0, renderer=_exp_renderer,
                     labels=_EXP_LABELS)
        b = paginate(rows, limit=4, offset=0, renderer=_exp_renderer,
                     labels=_EXP_LABELS)
        assert a == b
        assert rows == snapshot
