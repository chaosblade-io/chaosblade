"""Round-50/51/52 legislation: abort-exit cleanup invariants, pinned by AST.

Structural invariants ended the enumerate-by-memory era (rounds 47-52
each found a defect of the same class: a rule that existed at one point
with nothing generalizing or enforcing it):

  I1 — every ``await`` inside ``event_generator``'s except handlers either
       sits under ``anyio.CancelScope(shield=True)`` or is a call to the
       single-source ``_abort_turn_cleanup``. A bare await in a handler dies
       at its first suspension under a real disconnect (level-based scope
       cancellation, round-48), and an exception raised inside one handler
       escapes the whole try block — sibling handlers cannot catch it — so
       every unshielded handler is a bypass gate around the cancel exit's
       whole shielded chain (round-50's fifth site).

  I2 — inside EVERY registered single-source cleanup function, every
       ``await`` sits under the shield: the single sources must not
       silently grow unshielded arms.

  I3 — each abort exit of the turn stream actually calls
       ``_abort_turn_cleanup`` — per-exit wiring, not per-memory.

  I4 — PACKAGE-WIDE (round-51 generalized the walk off event_generator's
       handlers; round-52 took it off the single module): every ``await``
       — AND every ``async with`` / ``async for`` (implicit ``__aexit__`` /
       ``__anext__`` awaits are NOT ast.Await nodes — round-52's shared
       census blind spot, currently zero live instances) — in ANY finally
       block or abort-ish except handler of ANY async function in ANY
       module under ``server/`` is shielded, routed through a registered
       single source, or carries an explicit ``# abort-safe:`` marker
       whose (function, target) pair is registered in that module's
       allowlist with its justification. Round-52 proved the module
       boundary was itself an arbitrary cut: recover_stream.py carried
       the ENTIRE round-48 defect family (asyncio.shield outer await,
       bare handler await, bare finally finalize) one module over from
       the legislation's reach. The package-wide census keeps future
       stream modules inside the law by default.

  I5 — the turn single source carries the gated dispatched-state clear
       (``pipeline_task_id`` + non-dry-run), ordered after the rollback.

  I6 — every registered single source actually carries its named arm (an
       empty single source would satisfy I2/I4 vacuously while every
       exit silently cleaned up nothing).

  I7 (round-54) — the I4 escape hatches are EXPLICIT, not anonymous: a
       shield-direct abort await (inside a bare CancelScope(shield=True)
       that does NOT route through a registered single source) must have
       its (module, function) pair in SHIELD_DIRECT_ALLOWLIST with a
       justification. Round-53's E found the anonymous form: safe against
       the round-48 cancellation family but invisible to the
       decision-table pattern. The polarity inversion: single-source
       routing is the default, shield-direct is the audited exception.

  I8 (round-54) — every abort-path shield in ``server/`` carries a
       ``fail_after`` with a non-None bound on every arm. The r49
       "millisecond local-SQLite" no-ceiling ruling was
       checkpointer-shaped (AsyncSqliteSaver, verified r53) and the
       shields carry aget_state — a checkpointer READ — so an unbounded
       shield re-opens the r49 hang surface the moment the checkpointer
       is remote. Bounded abandon everywhere.

  I9 (round-54) — every ``is_disconnected`` await in a stream module
       RAISES on a true poll (ClientDisconnected or an abort exception).
       The silent ``break`` form is the G1/G2 defect: the poll-loser path
       fell into the NORMAL completion path, and which semantics a
       disconnect got was decided by the poll-vs-cancel race (r48: the
       cancel usually wins, but "the race picks the record" is a defect,
       not a coin flip). status_stream is exempt: a passive watcher
       whose cleanup is sync-only.

  I10 (round-54) — the abort paths of all three stream modules write
       the TaskStore row's terminal word through the shared guarded
       helper (``write_aborted_task_row``): the r53 triad was wired for
       the cancel exits only — crashed and poll-aborted runs left their
       rows zombie at the last mid-graph upsert. The helper itself is
       the guarded form (skip_if_terminal: a row that reached its OWN
       terminal word keeps its verdict).

  CLOSED BOUNDARY (round-53 F6 → round-54 I7): the anonymous shield-direct
  form is no longer legal — see I7 and SHIELD_DIRECT_ALLOWLIST below.

AST is immune to the failure modes that repeatedly broke source-string
assertions in the probe lineage (r45 indentation drift, r46 renames, r50
slice boundaries): it reads structure, not text.
"""

import ast
from pathlib import Path

from chaos_agent.server.routes import turn_event_stream

SERVER_DIR = Path(turn_event_stream.__file__).resolve().parent.parent

SOURCE = Path(turn_event_stream.__file__).read_text(encoding="utf-8")

_MODULE = ast.parse(SOURCE)

_ABORT_FN = "_abort_turn_cleanup"
# ast.unparse renders `except asyncio.CancelledError:` with its module
# prefix; match on the type name's tail so the set stays plain names.
_ABORT_CAUSES = {"ClientDisconnected", "ConfirmTimeout", "CancelledError", "Exception"}

# ---------------------------------------------------------------------------
# Package-wide module registry (I4): every module under server/ is scanned;
# the specs below only add per-module exemptions and single sources. A
# module with no entry is scanned with zero exemptions — new stream
# modules enter the legislation's jurisdiction the moment they are created.
# ---------------------------------------------------------------------------

_MODULE_SPECS: dict[str, dict] = {
    "routes/turn_event_stream.py": {
        "source_fn": "_abort_turn_cleanup",
        "allowlist": {
            ("_graph_pump", "unified.put"): (
                "unbounded asyncio.Queue.put never suspends, so there is no "
                "cancellation delivery point — the put completes even inside "
                "a cancelled frame; losing graph_done only matters to a "
                "consumer the scope cancel is already killing"
            ),
            ("_merged_stream", "s_task"): (
                "collection of a task the scope cancel already killed; "
                "losing the collection costs an unretrieved-exception "
                "warning at worst, and on the disconnect-poll path (no "
                "cancel active) this finally runs uncancelled via asyncgen "
                "finalization"
            ),
            ("_merged_stream", "g_task"): (
                "same rationale as _merged_stream/s_task"
            ),
            ("_merged_stream", "h_task"): (
                "same rationale as _merged_stream/s_task"
            ),
            ("_run_inject_pipeline", "_clear_dispatched_inject_intent_state"): (
                "normal-path backstop only: on plain-exception unwinds (no "
                "cancel active) it runs uncancelled; the abort path's "
                "remover is the gated clear inside _abort_turn_cleanup "
                "(I5) — an idempotent all-fields-None update, so the "
                "double write is harmless"
            ),
            ("_run_batch_pipeline", "_clear_dispatched_inject_intent_state"): (
                "same rationale as "
                "_run_inject_pipeline/_clear_dispatched_inject_intent_state"
            ),
        },
    },
    "routes/stream_abort.py": {
        "source_fn": None,
        "allowlist": {
            ("write_aborted_task_row", "asyncio.sleep"): (
                "the retry backoff between guarded row-write attempts: "
                "cancellation here loses nothing durable — the previous "
                "attempt already failed, the next one would only retry "
                "what the loud-warning path logs on give-up, and the "
                "helper itself runs inside the callers' shields on the "
                "stream abort paths"
            ),
        },
    },
    "routes/recover_stream.py": {
        "source_fn": "_abort_recover_cleanup",
        "allowlist": {},
    },
    "routes/inject_stream.py": {
        "source_fn": "_abort_inject_stream_cleanup",
        "allowlist": {},
    },
    "routes/inject.py": {
        "source_fn": None,
        "allowlist": {
            ("_run_inject", "auto_rollback"): (
                "background-task form: _run_inject runs in its OWN asyncio "
                "task (asyncio.create_task), not the Starlette task group — "
                "a client disconnect never cancels it, and graceful "
                "shutdown DRAINS these tasks (TaskTracker.drain awaits "
                "them, 30s timeout) rather than cancelling; the only "
                "cancellation it can ever see is event-loop teardown, "
                "where no await of any form completes anyway"
            ),
            ("_run_inject", "finalize_inject_session"): (
                "same rationale as _run_inject/auto_rollback"
            ),
        },
    },
    "routes/sessions.py": {
        "source_fn": None,
        "allowlist": {
            ("event_generator", "hook_task"): (
                "collection of a hook task the code just cancelled itself "
                "(compaction hook teardown); the hook handles "
                "CancelledError internally and its output has no consumer "
                "left — losing the collection is an asyncio warning at "
                "worst, and the surrounding comment documents this"
            ),
        },
    },
    "web/__init__.py": {
        "source_fn": None,
        "allowlist": {
            ("get_response", "super().get_response"): (
                "SPA 404 fallback — response generation, not cleanup "
                "semantics: serving index.html to a disconnected client "
                "is moot, and no durable state is touched here"
            ),
        },
    },
}

# I7 (round-54): shield-direct abort sites — awaits inside a bare
# CancelScope(shield=True) that do NOT route through a registered single
# source. Each entry: (module relpath, enclosing function) → justification.
# These are the r53-era "intentional turn-parity shapes" plus the r54
# additions, now explicit and audited: a new shield-direct site is RED
# until it registers a single source or argues its way in here.
SHIELD_DIRECT_ALLOWLIST: dict[tuple[str, str], str] = {
    ("routes/turn_event_stream.py", "event_generator"): (
        "the finally-block terminal finalize + the turn-dispatched recover "
        "abort fallback (r54 G5): the turn stream's OWN finally semantics — "
        "decision-table review lives with _abort_turn_cleanup's docstring "
        "plus the r48/r53 lineage; the recover fallback mirrors "
        "_run_recover's normal-path finalize shape"
    ),
    ("routes/inject_stream.py", "event_generator"): (
        "the finally-block terminal triad (row write via the shared "
        "write_aborted_task_row + finalize with status_override): the "
        "r53 fix's shape, kept as the inject twin's own finally semantics; "
        "its rollback arm (the heavier vehicle-class chain) DOES route "
        "through the registered single source _abort_inject_stream_cleanup"
    ),
    ("routes/recover_stream.py", "event_generator"): (
        "the finally-block terminal finalize (status=failed default): the "
        "r52 fix's shape; every abort-exit record/row arm routes through "
        "the registered single source _abort_recover_cleanup — the "
        "finally shield is the terminal session-finalize twin"
    ),
}

# Every registered single-source cleanup function (I2).
_SOURCE_FNS = [
    (relpath, spec["source_fn"])
    for relpath, spec in _MODULE_SPECS.items()
    if spec["source_fn"]
]


def _handler_name(handler: ast.ExceptHandler) -> str:
    desc = ast.unparse(handler.type) if handler.type else ""
    return desc.rsplit(".", 1)[-1]


def _fn_def(name: str) -> ast.AsyncFunctionDef:
    for node in ast.walk(_MODULE):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in module")


def _shielded_node_ids(fn: ast.AST) -> set[int]:
    """IDs of every node nested inside a CancelScope(shield=True) with-block."""
    shielded: set[int] = set()

    class V(ast.NodeVisitor):
        def visit_With(self, node: ast.With) -> None:
            expr = node.items[0].context_expr
            is_shield = (
                isinstance(expr, ast.Call)
                and isinstance(expr.func, ast.Attribute)
                and expr.func.attr == "CancelScope"
                and any(
                    kw.arg == "shield" and getattr(kw.value, "value", None) is True
                    for kw in expr.keywords
                )
            )
            if is_shield:
                for sub in ast.walk(node):
                    shielded.add(id(sub))
            self.generic_visit(node)

    V().visit(fn)
    return shielded


def _awaits(fn: ast.AST):
    for node in ast.walk(fn):
        if isinstance(node, ast.Await):
            yield node


def _abort_position_awaits(fn: ast.AsyncFunctionDef):
    """Yield (where, node) for awaits in finally blocks and except handlers."""
    for node in ast.walk(fn):
        if isinstance(node, ast.ExceptHandler):
            where = f"except {ast.unparse(node.type) if node.type else '<bare>'}"
            for sub in _awaits(node):
                yield where, sub
        elif isinstance(node, ast.Try) and node.finalbody:
            for stmt in node.finalbody:
                for sub in ast.walk(stmt):
                    if isinstance(sub, ast.Await):
                        yield "finally", sub


def _abort_position_implicit_awaits(fn: ast.AsyncFunctionDef):
    """Yield (where, node) for async-with/async-for in abort positions.

    An ``async with`` runs ``__aexit__`` and an ``async for`` runs
    ``__anext__`` through suspension points, but neither is an ast.Await
    node — a shared blind spot of the round-51 census and legislation,
    closed round-52 (zero live instances at the time of writing).
    """
    for node in ast.walk(fn):
        if isinstance(node, ast.ExceptHandler):
            where = f"except {ast.unparse(node.type) if node.type else '<bare>'}"
            for sub in ast.walk(node):
                if isinstance(sub, (ast.AsyncWith, ast.AsyncFor)):
                    yield where, sub
        elif isinstance(node, ast.Try) and node.finalbody:
            for stmt in node.finalbody:
                for sub in ast.walk(stmt):
                    if isinstance(sub, (ast.AsyncWith, ast.AsyncFor)):
                        yield "finally", sub


def _parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    parent: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[id(child)] = node
    return parent


def _fn_depth(fn: ast.AsyncFunctionDef, parent: dict[int, ast.AST]) -> int:
    depth = 0
    node = parent.get(id(fn))
    while node is not None:
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            depth += 1
        node = parent.get(id(node))
    return depth


def _await_target(sub: ast.Await) -> str:
    call = sub.value
    if isinstance(call, ast.Call):
        return ast.unparse(call.func)
    return ast.unparse(sub.value)


def _marker_window_hit(lines: list[str], lineno: int) -> bool:
    window = lines[max(0, lineno - 5):lineno]
    return any("# abort-safe:" in ln for ln in window)


def test_abort_handlers_have_no_bare_awaits():
    """I1: every await in event_generator's except handlers is shielded
    or routed through _abort_turn_cleanup."""
    eg = _fn_def("event_generator")
    shielded = _shielded_node_ids(eg)
    violations = []

    for node in ast.walk(eg):
        if not isinstance(node, ast.ExceptHandler):
            continue
        handler_desc = ast.unparse(node.type) if node.type else "<bare>"
        for sub in _awaits(node):
            if id(sub) in shielded:
                continue
            # The sanctioned escape: the single-source abort-cleanup call
            # (whose own body is fully shielded — I2).
            call = sub.value
            is_abort_call = (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == _ABORT_FN
            )
            if is_abort_call:
                continue
            violations.append(
                f"line {sub.lineno} in except {handler_desc}: "
                f"await {ast.unparse(sub.value)[:70]}"
            )

    assert not violations, (
        "bare awaits in abort handlers (must be shielded or routed through "
        f"{_ABORT_FN}): " + "; ".join(violations)
    )


def test_single_source_fns_are_fully_shielded():
    """I2: every await inside EVERY registered single-source cleanup
    function sits under the shield."""
    for relpath, fn_name in _SOURCE_FNS:
        path = SERVER_DIR / relpath
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        fn = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.AsyncFunctionDef) and n.name == fn_name),
            None,
        )
        assert fn is not None, f"{relpath}:{fn_name} not found"
        shielded = _shielded_node_ids(fn)
        bare = [a.lineno for a in _awaits(fn) if id(a) not in shielded]
        assert not bare, (
            f"{relpath}:{fn_name} grew unshielded awaits at lines {bare} — "
            "a single source must keep its whole chain inside "
            "CancelScope(shield=True)"
        )


def test_every_abort_exit_routes_through_the_single_source():
    """I3: each abort exit actually calls _abort_turn_cleanup — the wiring
    that made ConfirmTimeout the round-48 sweep's missed fifth site is now
    pinned per-exit, not per-memory."""
    eg = _fn_def("event_generator")
    wired: set[str] = set()

    for node in ast.walk(eg):
        if not isinstance(node, ast.ExceptHandler) or node.type is None:
            continue
        desc = _handler_name(node)
        if desc not in _ABORT_CAUSES:
            continue
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Await)
                and isinstance(sub.value, ast.Call)
                and isinstance(sub.value.func, ast.Name)
                and sub.value.func.id == _ABORT_FN
            ):
                wired.add(desc)

    missing = _ABORT_CAUSES - wired
    assert not missing, (
        f"abort exits not routing through {_ABORT_FN}: {sorted(missing)} — "
        "a hand-written await chain in any exit reintroduces the "
        "site-by-site enumeration the round-50 single source retired"
    )


def test_server_package_abort_positions_are_shielded_marked_or_sourced():
    """I4: no silent unshielded awaits (or implicit async-with/for awaits)
    in finally blocks or abort-ish handlers anywhere under ``server/``.

    Round-51 mutation: a bare await injected into event_generator's finally
    (the block type of round-48's fourth site) passed I1-I3 green. Round-52
    proved the module boundary was just as arbitrary: recover_stream.py,
    one file over, carried the entire round-48 defect family. The domain is
    now the whole package; exemptions are explicit allowlist entries.
    """
    violations = []

    for path in sorted(SERVER_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(SERVER_DIR))
        spec = _MODULE_SPECS.get(rel, {"source_fn": None, "allowlist": {}})
        source_fn = spec["source_fn"]
        allowlist: dict = spec["allowlist"]

        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        lines = source.splitlines()
        parent = _parent_map(tree)

        fns = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]
        if not fns:
            continue
        fn_by_id = {id(fn): fn for fn in fns}
        shielded_by_fn = {id(fn): _shielded_node_ids(fn) for fn in fns}
        depth_by_fn = {id(fn): _fn_depth(fn, parent) for fn in fns}

        # Attribute each abort-position await to its INNERMOST containing
        # function (nested defs beat their parents on depth ties).
        claims: dict[int, tuple[int, int, str, ast.Await]] = {}
        for fn in fns:
            for where, sub in _abort_position_awaits(fn):
                prev = claims.get(id(sub))
                if prev is None or depth_by_fn[id(fn)] > prev[0]:
                    claims[id(sub)] = (depth_by_fn[id(fn)], id(fn), where, sub)

        for _depth, fnid, where, sub in claims.values():
            fn = fn_by_id[fnid]
            if id(sub) in shielded_by_fn[fnid]:
                continue
            call = sub.value
            if (
                source_fn
                and isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == source_fn
            ):
                continue
            target = _await_target(sub)
            if _marker_window_hit(lines, sub.lineno):
                if (fn.name, target) not in allowlist:
                    violations.append(
                        f"{rel} line {sub.lineno} ({fn.name} {where}): marker "
                        f"present but ({fn.name!r}, {target!r}) missing from "
                        "the allowlist — add a justification or remove the "
                        "marker"
                    )
            else:
                violations.append(
                    f"{rel} line {sub.lineno} ({fn.name} {where}): unshielded "
                    f"await {ast.unparse(sub.value)[:55]} — shield it, route "
                    f"through the single source, or justify it with "
                    "'# abort-safe:' plus an allowlist entry"
                )

        # F4: implicit awaits (async with __aexit__ / async for __anext__)
        # in abort positions must be shielded too.
        implicit_claims: dict[int, tuple[int, int, str, ast.AST]] = {}
        for fn in fns:
            for where, sub in _abort_position_implicit_awaits(fn):
                prev = implicit_claims.get(id(sub))
                if prev is None or depth_by_fn[id(fn)] > prev[0]:
                    implicit_claims[id(sub)] = (depth_by_fn[id(fn)], id(fn), where, sub)

        for _depth, fnid, where, sub in implicit_claims.values():
            fn = fn_by_id[fnid]
            if id(sub) in shielded_by_fn[fnid]:
                continue
            kind = "async with" if isinstance(sub, ast.AsyncWith) else "async for"
            violations.append(
                f"{rel} line {sub.lineno} ({fn.name} {where}): implicit "
                f"{kind} await is unshielded — its __aexit__/__anext__ "
                "suspension dies under level-based cancellation; shield it "
                "or restructure"
            )

    assert not violations, (
        "abort-position awaits outside the legislation (I4 package-wide):\n  "
        + "\n  ".join(violations)
    )


def test_single_source_clears_dispatched_state_after_rollback():
    """I5: the turn single source carries the gated dispatched-state clear.

    The pipeline subgenerators' own finally-clears run inline inside the
    cancelled task on the scope-cancel path and die at their first
    suspension (round-51 two-path experiment) — this arm is the one the
    shield guarantees. Pin its presence, its gate, and its ordering.
    """
    fn = _fn_def(_ABORT_FN)

    call_lines = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in (
                "_rollback_intent_checkpoint_for_turn",
                "_clear_dispatched_inject_intent_state",
                "_write_interrupted_record",
            ):
                call_lines[node.func.id] = node.lineno

    missing = {
        "_rollback_intent_checkpoint_for_turn",
        "_clear_dispatched_inject_intent_state",
        "_write_interrupted_record",
    } - set(call_lines)
    assert not missing, (
        f"{_ABORT_FN} lost cleanup arms: {sorted(missing)} — every arm the "
        "decision table describes must actually be wired"
    )

    assert (
        call_lines["_rollback_intent_checkpoint_for_turn"]
        < call_lines["_clear_dispatched_inject_intent_state"]
        < call_lines["_write_interrupted_record"]
    ), (
        "dispatched-state clear must run AFTER the rollback (a successful "
        "fork already discards the dispatched fields — this arm matters "
        "exactly when the rollback is skipped) and BEFORE the record"
    )

    fn_source = ast.get_source_segment(SOURCE, fn) or ""
    assert "if ctx.pipeline_task_id and not ctx.dry_run:" in fn_source, (
        "the dispatched-state clear must stay gated on pipeline_task_id "
        "(a handoff happened this turn) and dry_run (matching the "
        "pipeline finallys' own gating)"
    )


def test_single_source_fns_carry_their_decision_table_arms():
    """I6: every registered single source actually carries its named arm.

    An empty (or arm-less) single source satisfies I2 and I4 vacuously
    while every abort exit silently cleans up nothing — the arms the
    decision tables name must be wired, same discipline as I5.
    """
    # relpath -> (single-source fn, required call inside its body)
    required_arms = {
        "routes/recover_stream.py": (
            "_abort_recover_cleanup", "_write_recover_interrupted",
        ),
        "routes/inject_stream.py": (
            "_abort_inject_stream_cleanup", "auto_rollback",
        ),
    }
    for relpath, (fn_name, arm) in required_arms.items():
        path = SERVER_DIR / relpath
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        fn = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.AsyncFunctionDef) and n.name == fn_name),
            None,
        )
        assert fn is not None, f"{relpath}:{fn_name} not found"
        fn_source = ast.get_source_segment(source, fn) or ""
        assert arm in fn_source, (
            f"{relpath}:{fn_name} must call {arm} — without it the shield "
            "carries an empty shell"
        )


# ---------------------------------------------------------------------------
# Round-54 legislation: I7-I10
# ---------------------------------------------------------------------------

def test_shield_direct_abort_awaits_are_allowlisted():
    """I7: a shield-direct abort await (not routed through a registered
    single source) must be an explicit, justified exception.

    Round-53 E: the bypass construction — a bare CancelScope(shield=True)
    wrapping the cleanup awaits with NO single source — passed I4 as
    "shielded", invisible to the decision-table pattern. The polarity is
    now inverted: shield-direct is the audited exception, single-source
    routing the default.

    Exemptions (NOT shield-direct):
      * a shield whose body awaits the module's registered single source
        (that is exactly the legislated routing form), and
      * a shield nested inside the registered single source ITSELF — that
        shape is I2's jurisdiction (the single source is where the shield
        is REQUIRED, not a bypass of it). The r54 first cut mis-flagged
        all three single sources here: its attribution pass rescanned
        every shield-with-await site without re-applying the routing
        exemption, so _abort_turn_cleanup's own shield read as a bypass
        of itself.
    """
    detailed = []
    for path in sorted(SERVER_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(SERVER_DIR))
        spec = _MODULE_SPECS.get(rel, {"source_fn": None})
        source_fn = spec["source_fn"]
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parent = _parent_map(tree)

        for node in ast.walk(tree):
            if not isinstance(node, ast.With):
                continue
            expr = node.items[0].context_expr
            is_shield = (
                isinstance(expr, ast.Call)
                and isinstance(expr.func, ast.Attribute)
                and expr.func.attr == "CancelScope"
                and any(
                    k.arg == "shield" and getattr(k.value, "value", None) is True
                    for k in expr.keywords
                )
            )
            if not is_shield:
                continue
            shield_awaits = [
                a for a in ast.walk(node) if isinstance(a, ast.Await)
            ]
            if not shield_awaits:
                continue
            # Exemption 1: the shield routes through the module's
            # registered single source (the legislated routing form).
            if source_fn and any(
                isinstance(a.value, ast.Call)
                and isinstance(a.value.func, ast.Name)
                and a.value.func.id == source_fn
                for a in shield_awaits
            ):
                continue
            # Attribute the site to its innermost enclosing function.
            owner = parent.get(id(node))
            fname = "<module>"
            while owner is not None:
                if isinstance(owner, (ast.AsyncFunctionDef, ast.FunctionDef)):
                    fname = owner.name
                    break
                owner = parent.get(id(owner))
            # Exemption 2: the shield IS the registered single source's
            # own body — I2's jurisdiction, not a bypass.
            if source_fn and fname == source_fn:
                continue
            if (rel, fname) not in SHIELD_DIRECT_ALLOWLIST:
                detailed.append(f"{rel}:{fname}")
    assert not detailed, (
        "shield-direct abort shields outside the allowlist (I7): "
        + "; ".join(detailed)
        + " — register a single source (_abort_*_cleanup) or argue "
        "the site into SHIELD_DIRECT_ALLOWLIST"
    )


def test_every_abort_shield_carries_a_ceiling():
    """I8: every CancelScope(shield=True) in server/ that carries awaits
    also carries a fail_after with a non-None bound on every arm."""
    ceilingless = []
    for path in sorted(SERVER_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(SERVER_DIR))
        tree = ast.parse(path.read_text(encoding="utf-8"))

        class V(ast.NodeVisitor):
            def visit_With(self, node):
                expr = node.items[0].context_expr
                is_shield = (
                    isinstance(expr, ast.Call)
                    and isinstance(expr.func, ast.Attribute)
                    and expr.func.attr == "CancelScope"
                    and any(
                        k.arg == "shield" and getattr(k.value, "value", None) is True
                        for k in expr.keywords
                    )
                )
                if is_shield and any(
                    isinstance(a, ast.Await) for a in ast.walk(node)
                ):
                    fail_afters = [
                        n for n in ast.walk(node)
                        if isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Attribute)
                        and n.func.attr == "fail_after"
                    ]
                    fully_bounded = [
                        n for n in fail_afters
                        if n.args
                        and not any(
                            isinstance(sub, ast.Constant) and sub.value is None
                            for sub in ast.walk(n.args[0])
                        )
                    ]
                    if not fully_bounded:
                        ceilingless.append(f"{rel}:{node.lineno}")
                self.generic_visit(node)

        V().visit(tree)
    assert not ceilingless, (
        "abort-path shields without a real ceiling on every arm (I8 — "
        "the r49 no-ceiling ruling was checkpointer-shaped; a remote "
        "checkpointer re-opens the hang surface): " + "; ".join(ceilingless)
    )


# I9: the passive watcher exemption — status_stream's poll is a consumer
# loop whose cleanup is sync-only (unsubscribe); it owns no durable state.
_I9_POLL_EXEMPT = {"routes/status_stream.py"}


def test_stream_polls_raise_on_disconnect():
    """I9: every is_disconnected poll in a stream module RAISES on a true
    poll — the silent ``break`` is the G1/G2 race-decided-semantics defect."""
    violations = []
    for path in sorted(SERVER_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(SERVER_DIR))
        if rel in _I9_POLL_EXEMPT:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parent = _parent_map(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Await):
                continue
            call = node.value
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "is_disconnected"
            ):
                continue
            enclosing_if = parent.get(id(node))
            while enclosing_if is not None and not isinstance(enclosing_if, ast.If):
                enclosing_if = parent.get(id(enclosing_if))
            outcome = "other"
            if enclosing_if is not None and id(enclosing_if.test) == id(node):
                body = enclosing_if.body
                if any(isinstance(s, ast.Raise) for s in body):
                    outcome = "raise"
                elif any(isinstance(s, ast.Break) for s in body):
                    outcome = "break"
            if outcome != "raise":
                violations.append(f"{rel}:{node.lineno} poll → {outcome}")
    assert not violations, (
        "is_disconnected polls that do not raise (I9 — the poll-loser "
        "path must not fall into the normal completion path): "
        + "; ".join(violations)
    )


def test_abort_paths_write_terminal_rows_through_shared_helper():
    """I10: the abort paths of all three stream modules write the
    TaskStore row's terminal word through write_aborted_task_row."""
    for rel, fn_name in (
        ("routes/turn_event_stream.py", "event_generator"),
        ("routes/inject_stream.py", "event_generator"),
        ("routes/recover_stream.py", "event_generator"),
    ):
        path = SERVER_DIR / rel
        source = path.read_text(encoding="utf-8")
        assert "write_aborted_task_row" in source, (
            f"{rel} must route its abort-path row writes through the shared "
            "guarded helper (I10 — zombie rows on abort/error exits)"
        )
    # The shared helper itself must call update_task_state with the guard.
    helper_src = (SERVER_DIR / "routes/stream_abort.py").read_text(encoding="utf-8")
    assert "skip_if_terminal=True" in helper_src, (
        "stream_abort.write_aborted_task_row must pass skip_if_terminal=True "
        "— the terminal-regression guard (G6) is the helper's whole point"
    )


def test_terminal_word_classification_has_single_source():
    """I11 (round-55 F1/F2; jurisdiction + scan domain widened round-56):
    every abort-path terminal word classifies through abort_row_word —
    the single source referencing ABORT_INTERRUPT_CAUSES, declared in
    stream_abort.py (the shared module that owns write_aborted_task_row).

    The r54 G4 arm classified with an inline tuple that dropped
    confirm_timeout onto "failed" while the same module called that cause
    "a DESIGNED pause" in three other places; the G5 fallback hard-coded
    "failed" for every cause. Both are the same defect shape: a call-site
    ad-hoc classification drifting from the module's own taxonomy. The
    law: the set is declared once, the helper references it once, and
    EVERY abort-path word write routes through the helper — no inline
    tuples, no bare strings at the write sites.

    Round-56 evolution — the r55 first cut declared the taxonomy
    MODULE-PRIVATE in turn_event_stream.py while its docstring claimed
    "every abort-path terminal word": the sibling streams were never in
    the scan domain, and the census found both carrying private mappings
    (recover_stream: an inline cause→word conditional with the OPPOSITE
    unknown-cause polarity — unknown → "cancelled", fail-open;
    inject_stream: an inline flag→word conditional at the write site).
    The declared domain now equals the implemented domain: the taxonomy
    lives beside the row writer in the shared module, and the scan walks
    ALL THREE stream modules' write sites.

    Round-57 evolution — the r56 census counted ONE API family
    (write_aborted_task_row call sites) and the same abort event's
    SECOND user-visible surface was never in any round's scan: the
    session finalize functions carry the same word through
    status_override (finalize_inject_session) and default_status
    (finalize_recover_session). The census found (F1', live) recover's
    abort path ALWAYS falling into the finally fallback whose hard-coded
    default_status="failed" stamped a user-cancelled recovery "failed"
    on the session record while its TaskStore row said "cancelled" —
    the r55 F2 defect recurring verbatim in the twin — and (F2', drift)
    two inline flag→word conditionals on the session surface. The scan
    domain now spans BOTH write families: the row writes (d) and the
    session-status kwargs (g)/(h). Known out-of-jurisdiction debt,
    deliberately NOT routed: l4/recovery.py's two bare
    default_status="failed" calls sit on crash-only paths (cause is
    always internal_error — the word is right) outside the stream
    modules; app.py's status="aborted" is the shutdown-sweep
    infrastructure word (cause unknowable by construction).

    Round-60 evolution — the r57-r59 lineages fixed the word VALUE but
    never asked WHICH GRAPH feeds it: the finally's finalize receives
    ``ctx.result_graph or ctx.intent_graph`` and result_graph names the
    pipeline only on the SUCCESS path, so every mid-pipeline exit
    finalized from INTENT values (no verdict → "failed"; fault_spec
    cleared at dispatch → the persisted summary lost the durable
    recover context; the dry-run preview's successful session said
    "failed"). And the one interrupt cause that sets no flag
    (confirm_timeout) landed on the inference path — "failed" on the
    session while the G4 row said "cancelled" (F1'''); a user-requested
    recover turn — whose intent state carries NO operation field — took
    the INJECT arm and finalized the recover session fail-OPEN
    ("completed", the bridge-state inference) with no parent_task_id
    link, preempting the G5 fallback (F5'''). New scans: the override's
    condition must cover EVERY cause ((j): BoolOp on the flag AND the
    cause memo — the bare flag Name is the defect shape), the recover
    guard ((k): a recover-shaped intent state never enters the inject
    arm), and the wiring ((l): the finally hands the finalize the
    PIPELINE coordinates when one was dispatched, plus the cause memo).
    """
    abort_src = (SERVER_DIR / "routes/stream_abort.py").read_text(encoding="utf-8")
    abort_tree = ast.parse(abort_src)

    # (a) the taxonomy exists in the SHARED module and carries the
    # interrupt causes
    causes = None
    for node in abort_tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "ABORT_INTERRUPT_CAUSES"
            for t in node.targets
        ):
            val = node.value
            if isinstance(val, ast.Call) and getattr(val.func, "id", "") == "frozenset":
                elts = val.args[0].elts if val.args and isinstance(val.args[0], ast.Set) else val.args
                causes = {getattr(e, "value", None) for e in elts}
    assert causes == {"user_cancel", "disconnected", "confirm_timeout"}, (
        "I11: ABORT_INTERRUPT_CAUSES must carry the interrupt causes "
        f"(user_cancel/disconnected/confirm_timeout) — found {sorted(c or '?' for c in (causes or {None}))}"
    )

    # (b) the helper classifies via the set — in the shared module
    helper = next(
        (n for n in abort_tree.body
         if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
         and n.name == "abort_row_word"),
        None,
    )
    assert helper is not None, (
        "I11: abort_row_word must live in stream_abort.py — beside the row "
        "writer every stream already routes through. A taxonomy declared in "
        "any ONE stream module leaves the other two free to grow private "
        "mappings (the round-56 census found both doing exactly that)"
    )
    assert any(
        isinstance(n, ast.Name) and n.id == "ABORT_INTERRUPT_CAUSES"
        for n in ast.walk(helper)
    ), "I11: abort_row_word must classify via ABORT_INTERRUPT_CAUSES"

    stream_modules = [
        "routes/turn_event_stream.py",
        "routes/inject_stream.py",
        "routes/recover_stream.py",
    ]

    # (c) NO module-private twin of the word taxonomy in any stream
    # module — the jurisdiction move must not leave a drift seed behind
    for rel in stream_modules:
        mod_src = (SERVER_DIR / rel).read_text(encoding="utf-8")
        assert "_ABORT_INTERRUPT_CAUSES" not in mod_src and "def _abort_row_word" not in mod_src, (
            f"I11: {rel} must not declare a module-private twin of the word "
            "taxonomy — the single source is stream_abort.abort_row_word"
        )

    # (d) every abort-path word write in ALL THREE modules routes through
    # the helper — no call-site ad-hoc classification (inline tuple, IfExp
    # over a tuple, inline cause/flag conditionals, or a bare string
    # literal). Legal forms beside the direct call:
    #   * a NAME assigned from ``abort_row_word(...)`` in the same module
    #     (the turn fallback shares one classified local between the
    #     finalize call and the row write);
    #   * the flag-driven "cancelled" write inside _finalize_task_session
    #     (turn only) — that arm keys on cancelled=_turn_cancelled, which
    #     the G3 legislation only ever sets on the interrupt exits, so the
    #     word is implied by the flag, not classified from a cause.
    violations = []
    for rel in stream_modules:
        mod_src = (SERVER_DIR / rel).read_text(encoding="utf-8")
        tree = ast.parse(mod_src)
        classified_locals = {
            node.targets[0].id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Call)
            and getattr(node.value.func, "id", "") == "abort_row_word"
        }
        finalize_lines = set()
        if rel.endswith("turn_event_stream.py"):
            finalize_fn = next(
                (n for n in ast.walk(tree)
                 if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
                 and n.name == "_finalize_task_session"),
                None,
            )
            if finalize_fn is not None:
                finalize_lines = {
                    n.lineno for n in ast.walk(finalize_fn)
                    if hasattr(n, "lineno")
                }
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "write_aborted_task_row"):
                continue
            if len(node.args) < 2:
                continue
            word = node.args[1]
            if isinstance(word, ast.Call) and getattr(word.func, "id", "") == "abort_row_word":
                continue  # direct route (incl. the representative-cause
                          # bridge: the exit flags encode the cause class)
            if isinstance(word, ast.Name) and word.id in classified_locals:
                continue  # classified local shared by two consumers
            if (
                isinstance(word, ast.Constant) and word.value == "cancelled"
                and node.lineno in finalize_lines
            ):
                continue  # flag-driven arm — see the comment above
            violations.append(
                f"{rel} line {node.lineno}: word arg is {ast.dump(word)[:60]}"
            )
    assert not violations, (
        "I11: abort-path word writes (ALL THREE stream modules) must route "
        "through abort_row_word — call-site ad-hoc classification is the "
        "recurring defect shape: " + "; ".join(violations)
    )

    # (e) the polarity law: the shared taxonomy is fail-closed — an
    # unknown cause must not understate a crash as a user cancel (the
    # round-56 F2 finding: recover_stream's former inline mapping
    # defaulted exactly the other way)
    from chaos_agent.server.routes.stream_abort import abort_row_word

    assert abort_row_word("mystery_cause") == "failed", (
        "I11: unknown causes classify as 'failed' (fail-closed) — the "
        "opposite default understates a crash as a user cancel"
    )
    assert abort_row_word("") == "failed"
    assert abort_row_word("internal_error") == "failed"
    for cause in ("user_cancel", "disconnected", "confirm_timeout"):
        assert abort_row_word(cause) == "cancelled"

    # (f) the cause memo: the abort chain records its cause on ctx so the
    # finally's fallback arms classify with the same taxonomy — and the
    # fallback's consumers actually read it (default_status included)
    turn_src = (SERVER_DIR / "routes/turn_event_stream.py").read_text(encoding="utf-8")
    turn_tree = ast.parse(turn_src)
    memo_assigned = any(
        isinstance(node, ast.Assign) and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Attribute)
        and node.targets[0].attr == "abort_cause"
        and isinstance(node.value, ast.Name) and node.value.id == "cause"
        for node in ast.walk(turn_tree)
    )
    assert memo_assigned, (
        "I11: _abort_turn_cleanup must record ctx.abort_cause — the finally "
        "fallback receives no arguments and cannot classify without the memo"
    )
    fallback_reads = "ctx.abort_cause or" in turn_src
    assert fallback_reads, (
        "I11: the recover fallback must classify its word from ctx.abort_cause "
        "(F2 — the hard-coded 'failed' stamped user-cancels as failures)"
    )

    # (g) API family B — the session surface: every
    # finalize_inject_session call in the stream modules passes a
    # status_override that is either absent/None (inference — the
    # deliberate session-surface semantic: a crash whose graph completed
    # keeps the inferred word; the session finalize has no
    # terminal-regression guard) or routed through the shared taxonomy.
    # Legal forms: no kwarg, explicit None, a direct abort_row_word call,
    # or IfExp(<interrupt flag>, abort_row_word(...), None) — the flag
    # encodes the cause CLASS, the taxonomy supplies the word VALUE.
    # Illegal: a bare string literal (the F2' drift shape).
    override_violations = []
    for rel in stream_modules:
        mod_tree = ast.parse(
            (SERVER_DIR / rel).read_text(encoding="utf-8")
        )
        for node in ast.walk(mod_tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "finalize_inject_session"):
                continue
            ov = next(
                (kw.value for kw in node.keywords
                 if kw.arg == "status_override"),
                None,
            )
            if ov is None or (isinstance(ov, ast.Constant) and ov.value is None):
                continue
            if isinstance(ov, ast.Call) and getattr(ov.func, "id", "") == "abort_row_word":
                continue
            if (
                isinstance(ov, ast.IfExp)
                and isinstance(ov.body, ast.Call)
                and getattr(ov.body.func, "id", "") == "abort_row_word"
                and isinstance(ov.orelse, ast.Constant)
                and ov.orelse.value is None
            ):
                continue
            override_violations.append(
                f"{rel} line {node.lineno}: status_override is "
                f"{ast.dump(ov)[:60]}"
            )
    assert not override_violations, (
        "I11(g): abort-path session status words (finalize_inject_session's "
        "status_override) must route through abort_row_word — one abort "
        "event, ONE word on every user-visible surface: "
        + "; ".join(override_violations)
    )

    # (h) API family C — the recover session surface: a
    # finalize_recover_session call that passes default_status (the
    # abort-path signature: normal-path callers don't) must route it
    # through the taxonomy — a direct abort_row_word call or a name
    # assigned from one (the turn fallback's classified local). The r57
    # F1' defect: recover's finally fallback hard-coded "failed" while
    # its row write said "cancelled" for the same user cancel.
    default_violations = []
    for rel in stream_modules:
        mod_tree = ast.parse(
            (SERVER_DIR / rel).read_text(encoding="utf-8")
        )
        classified_locals = {
            node.targets[0].id
            for node in ast.walk(mod_tree)
            if isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Call)
            and getattr(node.value.func, "id", "") == "abort_row_word"
        }
        for node in ast.walk(mod_tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "finalize_recover_session"):
                continue
            dv = next(
                (kw.value for kw in node.keywords
                 if kw.arg == "default_status"),
                None,
            )
            if dv is None:
                continue  # normal path — inference territory
            if isinstance(dv, ast.Call) and getattr(dv.func, "id", "") == "abort_row_word":
                continue
            if isinstance(dv, ast.Name) and dv.id in classified_locals:
                continue
            default_violations.append(
                f"{rel} line {node.lineno}: default_status is "
                f"{ast.dump(dv)[:60]}"
            )
    assert not default_violations, (
        "I11(h): abort-path recover session words (finalize_recover_session's "
        "default_status) must route through abort_row_word — the same abort "
        "event must not record 'cancelled' on the row and 'failed' on the "
        "session record: " + "; ".join(default_violations)
    )

    # (i) the recover cause memo — the twin of (f): recover_stream's
    # finally fallback (the abort path's ONLY session writer) receives
    # no arguments either; without the memo it cannot classify
    recover_src = (SERVER_DIR / "routes/recover_stream.py").read_text(encoding="utf-8")
    recover_tree = ast.parse(recover_src)
    memo_declared = any(
        isinstance(node, ast.Assign) and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "abort_cause"
        for node in ast.walk(recover_tree)
    )
    memo_wired = any(
        isinstance(node, ast.Nonlocal) and "abort_cause" in node.names
        for node in ast.walk(recover_tree)
    )
    assert memo_declared and memo_wired, (
        "I11(i): recover_stream must carry an abort_cause closure memo "
        "(declared + nonlocal-wired in the single-source abort cleanup) — "
        "the finally fallback classifies the session word from it (F1')"
    )
    assert "abort_cause or" in recover_src, (
        "I11(i): the recover fallback must classify its default_status from "
        "abort_cause with the fail-closed internal_error default"
    )

    # (j) cause COVERAGE on the turn override (round-60 F1'''): the
    # status_override IfExp's test must be a BoolOp on BOTH the flag and
    # the cause memo — the bare flag Name leaves the no-flag causes
    # (confirm_timeout, internal_error) on the inference path, whose
    # intent-graph values map them to "failed" while the G4 row says
    # "cancelled" for the same event.
    turn_src_full = (SERVER_DIR / "routes/turn_event_stream.py").read_text(
        encoding="utf-8"
    )
    turn_tree_full = ast.parse(turn_src_full)
    override_ifexp = None
    for node in ast.walk(turn_tree_full):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "finalize_inject_session"):
            continue
        ov = next(
            (kw.value for kw in node.keywords if kw.arg == "status_override"),
            None,
        )
        if isinstance(ov, ast.IfExp):
            override_ifexp = ov
            break
    assert override_ifexp is not None, (
        "I11(j): the turn module's finalize_inject_session call must keep "
        "an IfExp status_override (flag-or-cause → classified word, else "
        "None → inference)"
    )
    test_expr = override_ifexp.test
    test_names = {
        n.id for n in ast.walk(test_expr) if isinstance(n, ast.Name)
    }
    # Round-61 R61-5: the inline BoolOp was hoisted into a named gate
    # that ALSO carries the G6 verdict check — the override must not
    # fire for a run that reached its own verdict (an auto-recover turn
    # aborted during the recover segment reads the COMPLETED inject
    # session here). The gate's ASSIGNMENT is now the legislated
    # structure: AND of (flag OR cause) and (not _run_finished), where
    # _run_finished is the verdict predicate (`!= "injecting"`).
    assert test_names == {"_abort_terminal"}, (
        "I11(j): the override condition must key on the named gate "
        "_abort_terminal (round-61 R61-5) — inline conditions bypass the "
        "G6 verdict gate"
    )
    gate_assign = None
    for node in ast.walk(turn_tree_full):
        if (isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "_abort_terminal"):
            gate_assign = node.value
            break
    assert gate_assign is not None, (
        "I11(j): the _abort_terminal gate must be assigned"
    )
    assert isinstance(gate_assign, ast.BoolOp) and isinstance(gate_assign.op, ast.And), (
        "I11(j): the gate must AND the cause coverage with the verdict "
        "check — dropping either leg re-opens R61-5 (verdict override) or "
        "F1''' (no-flag causes on the inference path)"
    )
    cause_leg = next(
        (v for v in gate_assign.values
         if isinstance(v, ast.BoolOp) and isinstance(v.op, ast.Or)),
        None,
    )
    verdict_leg = next(
        (v for v in gate_assign.values
         if isinstance(v, ast.UnaryOp) and isinstance(v.op, ast.Not)),
        None,
    )
    assert cause_leg is not None and verdict_leg is not None, (
        "I11(j): the gate must carry BOTH legs — the OR of flag+cause and "
        "the NOT of the run-finished predicate"
    )
    cause_names = {
        n.id for n in ast.walk(cause_leg) if isinstance(n, ast.Name)
    }
    assert {"cancelled", "abort_cause"} <= cause_names, (
        "I11(j): the cause leg must key on BOTH the cancel flag AND the "
        "abort_cause memo (round-60 F1''') — a bare `cancelled` Name "
        "leaves confirm_timeout on the inference path ('failed' on the "
        "session vs 'cancelled' on the row)"
    )
    finished_assign = None
    for node in ast.walk(turn_tree_full):
        if (isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "_run_finished"):
            finished_assign = node.value
            break
    assert finished_assign is not None, (
        "I11(j): the _run_finished verdict predicate must be assigned "
        "(G6 on the session surface, round-61 R61-5)"
    )
    is_compare = (
        isinstance(finished_assign, ast.Compare)
        and finished_assign.ops
        and isinstance(finished_assign.ops[0], ast.NotEq)
        and any(
            isinstance(c, ast.Constant) and c.value == "injecting"
            for c in finished_assign.comparators
        )
    )
    assert is_compare, (
        "I11(j): the verdict predicate must be infer_task_state(...) != "
        "'injecting' — the mid-flight marker the abort word is allowed "
        "to classify"
    )

    # (k) the recover guard (round-60 F5'''): a recover-shaped intent
    # state (confirmed_intent == "recover" — the clarification recover
    # branch returns NO operation field) must never enter the inject arm
    # of _finalize_task_session: the inject arm finalizes the RECOVER
    # session from intent values — fail-OPEN word (the "recover" bridge
    # state infers "completed"), inject-shaped summary, no
    # parent_task_id link — and preempts the G5 fallback.
    fin_fn = next(
        node for node in turn_tree_full.body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and node.name == "_finalize_task_session"
    )
    guard_present = False
    for node in ast.walk(fin_fn):
        if (isinstance(node, ast.Compare)
                and isinstance(node.left, ast.Call)
                and getattr(node.left.func, "attr", "") == "get"
                and node.left.args
                and isinstance(node.left.args[0], ast.Constant)
                and node.left.args[0].value == "confirmed_intent"):
            for comparator in node.comparators:
                if (isinstance(comparator, ast.Constant)
                        and comparator.value == "recover"):
                    guard_present = True
    assert guard_present, (
        "I11(k): _finalize_task_session must guard recover-shaped intent "
        "states (confirmed_intent == 'recover') out of the inject arm — "
        "the G5 fallback owns recover-session closure (round-60 F5''')"
    )

    # (l) the wiring (round-60 F2'''/F3'''): the finally must hand the
    # finalize the PIPELINE's own coordinates when this turn dispatched
    # one, and pass the cause memo alongside the flag.
    assert (
        "if ctx.pipeline_task_id and ctx.pipeline_config:" in turn_src_full
        and "_result_graph = ctx.pipeline_graph" in turn_src_full
    ), (
        "I11(l): the finally must select the pipeline coordinates when a "
        "pipeline was dispatched — finalizing from intent values infers "
        "'failed' for every mid-pipeline exit and hollows out the "
        "persisted recover context (round-60 F2'''/F3''')"
    )
    assert "abort_cause=ctx.abort_cause" in turn_src_full, (
        "I11(l): the finally's finalize call must pass the cause memo — "
        "the no-flag causes classify their session word from it"
    )

    # (m) the recover coordinates are recorded BEFORE the resolve's
    # awaits (round-61 R61-2 / P6): the intent recover branch already
    # bootstrapped the recover session, so an abort landing during the
    # resolve used to reach the finally with an ACTIVE recover session,
    # the P2 guard yielded it to G5, and the G5 gate — keyed on these
    # coordinates — was still falsy: nobody closed the session. Between
    # the branch's bootstrap and the coordinate record there is no
    # suspension, so the leak window collapses to pure memory work.
    recover_fn = next(
        node for node in turn_tree_full.body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and node.name == "_run_recover"
    )
    coord_lines = [
        node.lineno for node in ast.walk(recover_fn)
        if (isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Attribute)
            and node.targets[0].attr == "recover_task_id"
            and isinstance(node.targets[0].value, ast.Name)
            and node.targets[0].value.id == "ctx")
    ]
    assert coord_lines, (
        "I11(m): _run_recover must record ctx.recover_task_id — the G5 "
        "abort fallback keys on these coordinates to close the session"
    )
    resolve_line = next(
        (node.lineno for node in ast.walk(recover_fn)
         if isinstance(node, ast.Call)
         and isinstance(node.func, ast.Name)
         and node.func.id == "resolve_recover_initial_state"),
        None,
    )
    assert resolve_line is not None, (
        "I11(m): _run_recover must resolve the recover initial state — "
        "without it the coordinates have no await-window to precede"
    )
    assert min(coord_lines) < resolve_line, (
        "I11(m): the recover coordinates must be recorded BEFORE the "
        "resolve's await — a window open during the resolve leaks the "
        "bootstrapped recover session (round-61 R61-2)"
    )

    # (n) the dry-run terminal word is discriminated by the pipeline's
    # OWN dry_run flag (round-61 R61-4/R61-4b / P7): a shape-only gate on
    # plan_summary + needs_confirmation=False + no verification would
    # swallow a REAL run aborted before its verification — planning
    # writes plan_summary for real runs too (extract_planning_metadata
    # feeds the confirm card). infer_task_state's dry-run branch must
    # carry values.get("dry_run") as one of its AND-legs.
    state_src = (SERVER_DIR.parent / "agent" / "state.py").read_text(
        encoding="utf-8"
    )
    state_tree = ast.parse(state_src)
    infer_fn = next(
        node for node in state_tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "infer_task_state"
    )
    dry_discriminator = any(
        isinstance(operand, ast.Call)
        and isinstance(operand.func, ast.Attribute)
        and operand.func.attr == "get"
        and operand.args
        and isinstance(operand.args[0], ast.Constant)
        and operand.args[0].value == "dry_run"
        for node in ast.walk(infer_fn)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.BoolOp)
        and isinstance(node.test.op, ast.And)
        for operand in node.test.values
    )
    assert dry_discriminator, (
        "I11(n): infer_task_state's dry-run terminal branch must key on "
        "values.get('dry_run') — a shape-only gate (plan_summary + "
        "needs_confirmation=False) collides with real runs, whose "
        "planning stage writes the same plan_summary"
    )

    # (o) the session finalizer enforces G6 for EVERY caller (round-62
    # R62-1 / P8): finalize_inject_session must yield a non-None
    # status_override to a reached verdict (infer_task_state !=
    # "injecting") — inject_stream's flag arm passes the override
    # unconditionally, so the guarantee must live INSIDE the finalizer,
    # not at each call site (the turn stream pre-gates for its row arm;
    # this internal check makes the rule unconditional). Without it an
    # interrupt racing in during result extraction rewrites "injected"
    # to "cancelled" on the session while the row keeps "injected" —
    # the r55 F2 word split, session surface.
    finalizer_src = (
        SERVER_DIR.parent / "memory" / "session_finalizer.py"
    ).read_text(encoding="utf-8")
    finalizer_tree = ast.parse(finalizer_src)
    fin_inject_fn = next(
        node for node in ast.walk(finalizer_tree)
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and node.name == "finalize_inject_session"
    )
    verdict_yield = False
    for node in ast.walk(fin_inject_fn):
        if not (isinstance(node, ast.If) and isinstance(node.test, ast.BoolOp)
                and isinstance(node.test.op, ast.And)):
            continue
        outer_names = {
            n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)
        }
        if not {"status_override", "values_fin"} <= outer_names:
            continue
        # The inner If compares infer_task_state(values_fin) to
        # "injecting"; its body must NULL the override.
        inner_ok = False
        nulled = False
        for sub in node.body:
            if not (isinstance(sub, ast.If)
                    and isinstance(sub.test, ast.Compare)):
                continue
            comp = sub.test
            call = comp.left if isinstance(comp.left, ast.Call) else None
            if call is None or "infer_task_state" not in (
                getattr(call.func, "id", "")
                or getattr(call.func, "attr", "")
            ):
                continue
            if any(
                isinstance(c, ast.Constant) and c.value == "injecting"
                for c in comp.comparators
            ):
                inner_ok = True
            for sub2 in ast.walk(sub):
                if (isinstance(sub2, ast.Assign)
                        and len(sub2.targets) == 1
                        and isinstance(sub2.targets[0], ast.Name)
                        and sub2.targets[0].id == "status_override"
                        and isinstance(sub2.value, ast.Constant)
                        and sub2.value.value is None):
                    nulled = True
        if inner_ok and nulled:
            verdict_yield = True
    assert verdict_yield, (
        "I11(o): finalize_inject_session must yield the status_override "
        "to a reached verdict (infer_task_state != 'injecting' on "
        "values_fin → override = None) — the session-surface G6 that "
        "protects inject_stream's unconditional flag arm (round-62 R62-1)"
    )

    # (p) the recover session finalizer enforces the same G6 for EVERY
    # caller (round-63 P8'): finalize_recover_session must yield the
    # classified default_status to a reached verdict
    # (infer_task_state not in {'recovering', 'injecting'} on
    # values_fin) — the G5 fallback and the recover-stream disconnect
    # arm pass the abort word unconditionally, so the guarantee must
    # live INSIDE the finalizer. Without it an interrupt racing in
    # during result extraction rewrites "recovered" to
    # "cancelled"/"failed" on the session while the row keeps its
    # verdict (skip_if_terminal) — the r55 F2 word split, recover
    # edition. The 'injecting' leg excludes the pre-dispatch recover
    # state (operation not yet written), which is a mid-flight position,
    # not a verdict.
    fin_recover_fn = next(
        node for node in ast.walk(finalizer_tree)
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and node.name == "finalize_recover_session"
    )
    recover_verdict_yield = False
    for node in ast.walk(fin_recover_fn):
        if not (isinstance(node, ast.If) and isinstance(node.test, ast.BoolOp)
                and isinstance(node.test.op, ast.And)):
            continue
        outer_names = {
            n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)
        }
        if not {"default_status", "values_fin"} <= outer_names:
            continue
        inner_ok = False
        reassigned = False
        for sub in node.body:
            if not (isinstance(sub, ast.If)
                    and isinstance(sub.test, ast.Compare)):
                continue
            comp = sub.test
            call = comp.left if isinstance(comp.left, ast.Call) else None
            if call is None or "infer_task_state" not in (
                getattr(call.func, "id", "")
                or getattr(call.func, "attr", "")
            ):
                continue
            midflight_words = {
                c.value
                for comp_val in comp.comparators
                if isinstance(comp_val, (ast.Tuple, ast.List))
                for c in comp_val.elts
                if isinstance(c, ast.Constant)
            }
            if {"recovering", "injecting"} <= midflight_words:
                inner_ok = True
            for sub2 in ast.walk(sub):
                if (isinstance(sub2, ast.Assign)
                        and len(sub2.targets) == 1
                        and isinstance(sub2.targets[0], ast.Name)
                        and sub2.targets[0].id == "default_status"):
                    reassigned = True
        if inner_ok and reassigned:
            recover_verdict_yield = True
    assert recover_verdict_yield, (
        "I11(p): finalize_recover_session must yield the classified "
        "default_status to a reached verdict (infer_task_state not in "
        "{'recovering', 'injecting'} on values_fin → default_status "
        "reassigned) — the recover-side session-surface G6 that protects "
        "the G5 fallback's and the disconnect arm's unconditional abort "
        "words (round-63 P8')"
    )

    # (p-R63-1) the gate spans EVERY result_summary_mode — the status
    # conditional's CLI-envelope leg must CONSUME default_status (the
    # Name), never a hard-coded Constant. The former "completed"
    # literal recorded FAILED recoveries as completed: both CLI failure
    # exits (RECOVERY_FAILED return / except in cli/runner.py) land in
    # the finally's finalize, contradicting the same record's
    # data.result (which honestly says failed/unverified).
    status_consumes_default = False
    for node in ast.walk(fin_recover_fn):
        if not (isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "status"):
            continue
        val = node.value
        if not (isinstance(val, ast.IfExp)
                and isinstance(val.body, ast.Name)
                and val.body.id == "default_status"):
            continue
        test_names = {
            n.id for n in ast.walk(val.test) if isinstance(n, ast.Name)
        }
        if "result_summary_mode" in test_names:
            status_consumes_default = True
    assert status_consumes_default, (
        "I11(p-R63-1): finalize_recover_session's CLI-envelope status "
        "leg must consume default_status (Name), not a hard-coded "
        "Constant — the runner's failure exits classify which exit ran "
        "and the verdict gate above stays mode-independent"
    )


def _has_not_ctx_dry_run(expr: ast.AST) -> bool:
    return any(
        isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not)
        and isinstance(n.operand, ast.Attribute)
        and isinstance(n.operand.value, ast.Name)
        and n.operand.value.id == "ctx"
        and n.operand.attr == "dry_run"
        for n in ast.walk(expr)
    )


def test_recover_preview_safety_gates_are_legislated():
    """I12 (round-64 R64-1/R64-2): a dry-run turn (/plan preview) must
    never reach the REAL recover dispatch, and the G5 coordinate
    self-rescue must not fail silently.

    The r63 census proved the inject preview safe (route_after_confirmation
    "end") but left the recover side unexamined: the intent clarification's
    recover branch bootstrapped and confirmed UNCONDITIONALLY, and the turn
    stream's ``_run_recover`` gated only on the confirmed intent — so a
    /plan over a recovery request executed a REAL recovery (and an abort
    in the dispatch window leaked the bootstrapped session, the G5
    fallback's own not-ctx.dry_run gate having kept it closed for previews
    by design). Three structural laws pin the fix:

    (a) ``_run_recover``'s dispatch gate carries the ``not ctx.dry_run``
        leg — the dispatch is the side-effecting act itself, so the
        preview-safety guarantee lives at the single source that would
        execute it, not only at the branch that feeds it (defense in
        depth: the intent branch is (b), the dispatch is (a)).
    (b) the intent clarification's recover branch early-returns on a
        dry-run state BEFORE its ``bootstrap_task_session`` call — a
        preview bootstraps NO session, confirms NO intent, and answers
        with the announcement; ``recover_task_id`` stays recorded (a
        dialogue fact the next non-preview turn reuses).
    (c) the G5 fallback's coordinate self-rescue warns LOUDLY on a failed
        read — a silent ``except: _snap = None`` re-arms the very leak
        the rescue exists to close with nothing in the logs (the r50
        capture-warning family: this single event changes abort-cleanup
        behavior for the turn, and at debug it was invisible in
        production).
    """
    turn_src = (SERVER_DIR / "routes/turn_event_stream.py").read_text(
        encoding="utf-8",
    )
    turn_tree = ast.parse(turn_src)

    # (a) the dispatch gate's dry_run leg
    run_recover_fn = next(
        (n for n in turn_tree.body
         if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
         and n.name == "_run_recover"),
        None,
    )
    assert run_recover_fn is not None, "I12(a): _run_recover must exist"
    gate_with_dry_run = False
    for node in ast.walk(run_recover_fn):
        if not (isinstance(node, ast.If) and _has_not_ctx_dry_run(node.test)):
            continue
        if any(isinstance(s, ast.Return) for s in ast.walk(node)):
            gate_with_dry_run = True
    assert gate_with_dry_run, (
        "I12(a): _run_recover's dispatch gate must carry the "
        "not-ctx.dry_run leg (an early return) — dropping it re-opens the "
        "preview that REALLY RUNS a recovery (round-64 R64-1)"
    )

    # (b) the intent branch's pre-bootstrap dry-run early return
    intent_src = (
        SERVER_DIR.parent / "agent/nodes/planning/intent_clarification.py"
    ).read_text(encoding="utf-8")
    intent_tree = ast.parse(intent_src)
    node_fn = next(
        (n for n in ast.walk(intent_tree)
         if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
         and n.name == "intent_clarification"),
        None,
    )
    assert node_fn is not None, (
        "I12(b): the intent_clarification node must exist"
    )
    recover_bootstraps = [
        n.lineno
        for n in ast.walk(node_fn)
        if (isinstance(n, ast.Call)
            and getattr(n.func, "id", "") == "bootstrap_task_session"
            and any(
                kw.arg == "operation"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value == "recover"
                for kw in n.keywords))
    ]
    assert recover_bootstraps, (
        "I12(b): the recover branch's bootstrap_task_session call must exist"
    )
    dry_run_early_return = None
    for n in ast.walk(node_fn):
        if not isinstance(n, ast.If):
            continue
        test = n.test
        # ``state.get("dry_run")`` — the preview flag read
        if not (isinstance(test, ast.Call)
                and isinstance(test.func, ast.Attribute)
                and test.func.attr == "get"
                and isinstance(test.func.value, ast.Name)
                and test.func.value.id == "state"
                and len(test.args) == 1
                and isinstance(test.args[0], ast.Constant)
                and test.args[0].value == "dry_run"):
            continue
        if any(isinstance(s, ast.Return) for s in ast.walk(n)):
            dry_run_early_return = n.lineno
    assert dry_run_early_return is not None, (
        "I12(b): the recover branch must carry a dry_run early return — "
        "a preview has no side effects (no bootstrap, no confirmation)"
    )
    assert all(dry_run_early_return < ln for ln in recover_bootstraps), (
        "I12(b): the dry_run early return must precede the recover "
        "branch's bootstrap — a preview must never reach it"
    )

    # (c) the G5 self-rescue warns on a failed read — no silent except
    event_gen_fn = next(
        (n for n in turn_tree.body
         if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
         and n.name == "event_generator"),
        None,
    )
    assert event_gen_fn is not None, "I12(c): event_generator must exist"
    self_rescue_warns = False
    for n in ast.walk(event_gen_fn):
        if not isinstance(n, ast.ExceptHandler):
            continue
        handler_names = {
            t.id for t in ast.walk(n)
            if isinstance(t, ast.Name) and isinstance(t.ctx, ast.Store)
        }
        if "_snap" not in handler_names:
            continue  # only the G5 self-rescue assigns _snap in a handler
        has_warning = any(
            isinstance(c, ast.Call)
            and isinstance(c.func, ast.Attribute)
            and c.func.attr == "warning"
            and isinstance(c.func.value, ast.Name)
            and c.func.value.id == "logger"
            for c in ast.walk(n)
        )
        if has_warning:
            self_rescue_warns = True
    assert self_rescue_warns, (
        "I12(c): the G5 self-rescue's except must warn loudly — a silent "
        "except: _snap = None re-arms the session leak it exists to close "
        "(round-64 R64-2, the r50 capture-warning family)"
    )
