"""Tests for the CLI orphan-row signal guard (runner.py).

Covers the SIGTERM cancel guard and the interrupted-row terminal write
introduced to close the CLI-direct orphan gap: a SIGTERM'd / Ctrl-C'd
inject run used to leave its TaskStore row at the last mid-graph upsert
("injecting"), a zombie no later writer can fix.
"""

import ast
import asyncio
import contextlib
import os
import signal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chaos_agent.cli.runner import (
    _install_sigterm_cancel_guard,
    _write_signal_interrupted_row,
)

_RUNNER_PATH = (
    Path(__file__).resolve().parents[2]
    / "src" / "chaos_agent" / "cli" / "runner.py"
)

# The graph-drive calls that make an AgentRunner method an "entry point":
# whatever else an entry does, it must hand control to a compiled graph
# through one of these.
_DRIVE_ATTRS = frozenset({"astream_events", "ainvoke", "astream"})


def _graph_driving_methods() -> dict:
    """Every AgentRunner method that drives a graph, by source scan.

    Derived, never listed by hand: the round-64 census proved a
    hand-maintained list is exactly how an entry point escapes the
    contract (the CLI recover paths were never added to the guard
    census because they were never in any census).
    """
    tree = ast.parse(_RUNNER_PATH.read_text(encoding="utf-8"))
    runner_cls = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "AgentRunner"
    )
    methods = {}
    for node in runner_cls.body:
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        if any(
            isinstance(call, ast.Call)
            and getattr(call.func, "attr", "") in _DRIVE_ATTRS
            for call in ast.walk(node)
        ):
            methods[node.name] = node
    return methods


def _exception_names(node) -> set:
    """The exception class names an ExceptHandler catches (bare or tuple)."""

    handler_type = node.type
    if handler_type is None:
        return set()
    elts = (
        handler_type.elts
        if isinstance(handler_type, ast.Tuple)
        else [handler_type]
    )
    names = set()
    for elt in elts:
        if isinstance(elt, ast.Name):
            names.add(elt.id)
        elif isinstance(elt, ast.Attribute):
            names.add(elt.attr)
    return names


def _is_interrupt_arm(node) -> bool:
    """True for an except arm catching both interrupt shapes and writing
    the interrupted terminal row."""

    if not isinstance(node, ast.ExceptHandler):
        return False
    if not {"KeyboardInterrupt", "CancelledError"} <= _exception_names(node):
        return False
    return any(
        isinstance(call, ast.Call)
        and getattr(call.func, "id", "") == "_write_signal_interrupted_row"
        for call in ast.walk(node)
    )


# The session finalizers and the keyword each takes its classified word
# through. One word per abort event means every writer of a session word
# must be handed the SAME classified value the row surface got.
_SESSION_FINALIZE_PARAMS = {
    "finalize_recover_session": "default_status",
    "finalize_inject_session": "status_override",
    "_finalize_inject_session": "status_override",
}

# Entry points that drive a graph but do NOT own a session's terminal
# state: resuming a paused graph leaves it resumable, so its session
# record stays whatever its owner left it. Listed explicitly so the
# exemption is a decision on the record, not a silent gap — an entry that
# grows a finalize call fails the contract below until it consumes the word.
_NON_SESSION_OWNERS = frozenset(
    {"resume_stream", "lift_dry_run_and_run", "confirm"}
)

# Entries whose interrupt arm classifies the word (abort_word). Both
# surfaces of the abort event — the TaskStore row AND the session record —
# must read from it: the r55 F2 / r64 F1 defect was one event shipping two
# words because only the row writer consumed it.
_INTERRUPT_WORD_PRODUCERS = frozenset(
    {"inject_stream", "inject", "recover", "recover_stream"}
)


def _produces_abort_word(fn) -> bool:
    """True when an interrupt arm classifies the word into ``abort_word``."""

    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign):
            continue
        targets = node.targets
        if not (
            len(targets) == 1
            and isinstance(targets[0], ast.Name)
            and targets[0].id == "abort_word"
        ):
            continue
        if any(
            isinstance(call, ast.Call)
            and getattr(call.func, "id", "") == "_signal_interrupted_word"
            for call in ast.walk(node.value)
        ):
            return True
    return False


def _session_finalize_calls(fn) -> list:
    """``[(call, keyword)]`` for every session finalize inside ``fn``."""

    found = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        fname = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
        keyword = _SESSION_FINALIZE_PARAMS.get(fname)
        if keyword:
            found.append((node, keyword))
    return found


class TestWriteSignalInterruptedRow:
    @pytest.mark.asyncio
    async def test_writes_cancelled_through_shared_taxonomy(self):
        """user_cancel classifies to "cancelled" via abort_row_word — the
        same single source the server abort paths use."""
        with patch(
            "chaos_agent.server.routes.stream_abort.write_aborted_task_row",
            new_callable=AsyncMock,
        ) as mock_write:
            await _write_signal_interrupted_row("inject-abc123")
        mock_write.assert_awaited_once_with("inject-abc123", "cancelled")

    @pytest.mark.asyncio
    async def test_propagates_store_error(self):
        """The helper is a thin pass-through: fail-soft is the runner's
        except-branch responsibility (its try/except logs the LOUD
        warning), so store errors must propagate to it unchanged."""
        with patch(
            "chaos_agent.server.routes.stream_abort.write_aborted_task_row",
            new=AsyncMock(side_effect=RuntimeError("store down")),
        ):
            with pytest.raises(RuntimeError, match="store down"):
                await _write_signal_interrupted_row("inject-abc123")


@pytest.mark.skipif(
    not hasattr(signal, "SIGTERM") or os.name == "nt",
    reason="add_signal_handler needs a Unix main-thread event loop",
)
class TestSigtermCancelGuard:
    @pytest.mark.asyncio
    async def test_sigterm_cancels_driving_task(self):
        """SIGTERM must cancel the driving task (guard installed inside
        it), not kill the process — the except branch around the graph
        run turns that cancel into the terminal row write."""
        cleanup = None
        interrupted = asyncio.Event()

        async def victim():
            nonlocal cleanup
            cleanup = _install_sigterm_cancel_guard()
            assert cleanup is not None  # fail BEFORE sending the signal
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                interrupted.set()
                raise

        task = asyncio.create_task(victim())
        try:
            await asyncio.sleep(0.05)  # let the guard install
            assert cleanup is not None
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.wait_for(interrupted.wait(), timeout=5)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            if cleanup is not None:
                cleanup()

    @pytest.mark.asyncio
    async def test_cleanup_removes_handler(self):
        cleanup = _install_sigterm_cancel_guard()
        assert cleanup is not None
        cleanup()
        # No handler left: removing again reports False.
        assert asyncio.get_running_loop().remove_signal_handler(signal.SIGTERM) is False

    @pytest.mark.asyncio
    async def test_degrades_to_none_when_unsupported(self):
        """Non-main-thread / unsupported platform: degrade to the
        pre-guard behavior (None), never fail the run."""
        mock_loop = MagicMock()
        mock_loop.add_signal_handler.side_effect = NotImplementedError
        with patch(
            "chaos_agent.cli.runner.asyncio.get_running_loop",
            return_value=mock_loop,
        ):
            assert _install_sigterm_cancel_guard() is None


class TestRunnerWiringContract:
    """Source-level wiring guard: every CLI graph-run entry must install
    the guard and route interrupt exceptions through the shared write —
    a new entry point (or an accidental revert) fails here.

    Round-64 F1 upgrade: the former contract counted occurrences
    (``count(...) == 3``) and named the three inject-era entries. That
    shape cannot fail for an entry point the census never listed — and
    the round-64 census found three more (recover / recover_stream /
    lift_dry_run_and_run), each driving a graph with NEITHER half of the
    pair. The consequence was not cosmetic: the CLI recover paths
    recorded a mid-flight Ctrl-C as "completed" on the session record
    (see test_session_finalizer.py's no-word contract). The scan now
    derives the entry-point set from the source itself.
    """

    def test_every_graph_driving_entry_wires_the_interrupt_pair(self):
        methods = _graph_driving_methods()
        known = {
            "inject_stream",
            "inject",
            "resume_stream",
            "confirm",
            "recover",
            "recover_stream",
            "lift_dry_run_and_run",
        }
        assert known <= set(methods), (
            "the census must see every known graph-driving entry point "
            "(a rename or a restructured drive call silently shrinks the "
            f"scan domain): missing {sorted(known - set(methods))}"
        )

        missing_guard, missing_cleanup, missing_arm = [], [], []
        for name, fn in methods.items():
            guard_names = {
                node.targets[0].id
                for node in ast.walk(fn)
                if isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Call)
                and getattr(node.value.func, "id", "") == "_install_sigterm_cancel_guard"
            }
            if not guard_names:
                missing_guard.append(name)
            else:
                finally_stmts = [
                    stmt
                    for node in ast.walk(fn)
                    if isinstance(node, ast.Try)
                    for stmt in node.finalbody
                ]
                released = any(
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id in guard_names
                    for stmt in finally_stmts
                    for call in ast.walk(stmt)
                )
                if not released:
                    missing_cleanup.append(name)

            if not any(_is_interrupt_arm(node) for node in ast.walk(fn)):
                missing_arm.append(name)

        assert not missing_guard, (
            "every CLI graph-driving entry must install the SIGTERM guard "
            "(_sigterm_cleanup = _install_sigterm_cancel_guard()) — without "
            "it a SIGTERM kills the process before any terminal word is "
            f"written: {missing_guard}"
        )
        assert not missing_cleanup, (
            "every guard must be released in the entry's finally block — a "
            f"leaked handler cancels the NEXT run's driving task: {missing_cleanup}"
        )
        assert not missing_arm, (
            "every CLI graph-driving entry must catch "
            "(KeyboardInterrupt, asyncio.CancelledError) and write the "
            "interrupted terminal row through _write_signal_interrupted_row "
            f"— a BaseException bypasses `except Exception` entirely: {missing_arm}"
        )

    def test_every_interrupt_word_producer_feeds_its_session_surface(self):
        """One abort event, ONE word — on BOTH surfaces it is visible on.

        The row writer always got the classified word; the session record
        got whatever its finalize computed from a bare boolean, so a
        mid-flight Ctrl-C shipped "cancelled" to the row and "completed"
        to the session (recover) / "failed" to the session (inject) — the
        r55 F2 split, CLI edition, surviving into round 64 because the
        session-finalize family was never in any census.

        The contract is structural, not per-entry: an entry whose
        interrupt arm classifies a word must hand that SAME value to every
        session finalize it calls. An entry that owns no session must not
        grow a finalize call without also consuming the word.
        """
        methods = _graph_driving_methods()
        producers = {
            name for name, fn in methods.items() if _produces_abort_word(fn)
        }
        assert producers == _INTERRUPT_WORD_PRODUCERS, (
            "the set of entries classifying an interrupt word changed — "
            "every producer must also feed the session surface (update the "
            "contract only together with the wiring): "
            f"produced={sorted(producers)} "
            f"expected={sorted(_INTERRUPT_WORD_PRODUCERS)}"
        )

        for name in sorted(producers | _NON_SESSION_OWNERS):
            calls = _session_finalize_calls(methods[name])
            if name in _NON_SESSION_OWNERS:
                assert not calls, (
                    f"{name} is declared a non-owner of session state but "
                    "now calls a session finalize — either it owns the "
                    "session (then it must consume abort_word) or the "
                    "exemption must be re-judged"
                )
                continue

            assert calls, (
                f"{name} produces the classified interrupt word but never "
                "finalizes a session — the session surface would fall back "
                "to its own inference (the word split)"
            )
            for call, keyword in calls:
                value = next(
                    (kw.value for kw in call.keywords if kw.arg == keyword),
                    None,
                )
                assert value is not None, (
                    f"{name} must pass {keyword} to its session finalize — "
                    "without it the interrupt is spelled by inference"
                )
                referenced = {
                    node.id for node in ast.walk(value) if isinstance(node, ast.Name)
                }
                assert "abort_word" in referenced, (
                    f"{name}'s {keyword} must consume the classified "
                    "interrupt word (abort_word, the same value its row "
                    f"write uses); found references {sorted(referenced)}"
                )


class TestInterruptWordSingleSource:
    """One abort event, ONE word: the CLI's row write and its session
    record must resolve through the same taxonomy the server uses."""

    def test_interrupt_word_is_the_shared_taxonomy_word(self):
        from chaos_agent.cli.runner import _signal_interrupted_word
        from chaos_agent.server.routes.stream_abort import abort_row_word

        assert _signal_interrupted_word() == abort_row_word("user_cancel")
        assert _signal_interrupted_word() == "cancelled"

    def test_interrupt_word_helper_is_the_row_writer_source(self):
        """The row write must NOT re-declare the cause: it resolves the
        word through the helper, so the two CLI surfaces can never drift."""
        src = _RUNNER_PATH.read_text(encoding="utf-8")
        assert 'abort_row_word("user_cancel")' not in src.split(
            "async def _write_signal_interrupted_row"
        )[1].split("class AgentRunner")[0], (
            "_write_signal_interrupted_row must consume _signal_interrupted_word "
            "instead of classifying its own cause — a private mapping at the "
            "write site is the r54 G4 / r56 F2 defect shape"
        )
