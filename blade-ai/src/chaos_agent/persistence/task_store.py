"""Async persistent task store with pluggable storage backend.

The store holds task lifecycle state and execution metrics across three
normalised tables:

- **tasks** – narrow, hot-path row per ``task_id`` (state, stage, phase …)
- **task_details** – wide, cold-path row (target JSON, params, metrics …)
- **task_spans** – one row per graph-node execution (timing / tokens / tools)

All public methods are ``async``; the actual I/O is delegated to a
``StorageBackend`` protocol implementation (SQLite via *aiosqlite* or
PostgreSQL via *asyncpg*).

Usage::

    from chaos_agent.persistence.task_store import get_task_store

    store = await get_task_store()
    await store.upsert("task-1", skill_name="pod-kill", experiment_uid="abc")
    data = await store.get("task-1")
    metrics = await store.get_metric("task-1")
"""

import asyncio
import atexit
import json
import logging
from typing import Optional

from chaos_agent.agent.state import (
    TASK_STATE_COLUMN_VALUES,
    TASK_STATE_TERMINAL_VALUES,
    TaskStateOverlay,
)
from chaos_agent.config.settings import settings
from chaos_agent.persistence.task_identity import is_real_task_id
from chaos_agent.persistence.task_store_backend import (
    StorageBackend,
    _DETAIL_COLUMNS,
    _JSON_COLUMNS,
    _TASK_COLUMNS,
    _extract_index_fields,
    _set_timestamps,
)
from chaos_agent.utils.time import now_iso

logger = logging.getLogger(__name__)

# Lifecycle states that carry a final verdict. ``infer_task_state`` returns
# "injecting" as a fallback whenever the record shows no lifecycle evidence,
# and that fallback must never overwrite a verdict already on record.
# "cancelled" joins the set: once a task is cancelled (intent rejected /
# turn aborted), later field-less flushes must not resurrect it.
# "unverified" joins the set: it is a TERMINAL knowledge claim (verification
# ran, conclusion unavailable) — a later field-less flush must not regress it
# to the "injecting" fallback, which would show a finished run as in-flight.
# Single-sourced from the TaskState legislation (round-15): this frozenset
# was a hand copy of the terminal subset — one word drifting here would
# desync the flush guard from every other terminal consumer.
_TERMINAL_TASK_STATES = TASK_STATE_TERMINAL_VALUES

# Fields whose presence proves the pipeline has taken ownership of a task
# (an intent converged, a fault spec produced, a plan generated, a command
# issued, a verdict recorded, ...). A row with none of them is a newborn
# anchor — e.g. the bare ``upsert(task_id)`` the tracer does before
# persisting spans — and must surface as "pending", not "injecting"
# (see _infer_fields). ``confirmed_intent`` never lands in a column but
# is part of the in-memory merge during upsert, so it is still evidence.
_LIFECYCLE_EVIDENCE_FIELDS = (
    "fault_spec",
    "target",
    "params",
    "namespace",
    "target_name",
    "experiment_uid",
    "needs_confirm",
    "confirmed_intent",
    "plan_summary",
    "safety_reason",
    "verification",
    "recover_verification",
    "result",
    "execution_artifacts",
    "injection_start_time",
    "finished_at",
    "error",
    "skill_name",
)


def _decode_json_value(value: object) -> object:
    """Decode a JSON column value if it is a string; pass through otherwise."""
    if isinstance(value, str) and value:
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
    return value


def _recovery_fully_cleared(record: dict) -> bool:
    """True when the recover flow's OWN verdict proves the whole task clear.

    Two machine-readable landing shapes of the same verdict (both written
    by ``finalize_recover_verification``): the ``recover_verification``
    dict's ``level`` and the ``result`` dict's ``recovered`` /
    ``recovery_level`` pair. Either signal alone is accepted — legacy
    rows may carry only one. Deliberately EXCLUDES ``partial``: a partial
    recovery means at least one fault may survive, which is a live
    liability, not a cleared one.
    """
    rv = _decode_json_value(record.get("recover_verification"))
    if isinstance(rv, dict) and rv.get("level") == "recovered":
        return True
    result = _decode_json_value(record.get("result"))
    if isinstance(result, dict):
        if (
            result.get("recovered") is True
            and result.get("recovery_level") == "recovered"
        ):
            return True
    return False


def _injection_was_issued(record: dict) -> bool:
    """True when the record shows an injection command WAS ACTUALLY ISSUED.

    The old word-predicate's one irreproachable judgment, promoted intact:
    ``injection_start_time`` is written exactly at the moment the command
    goes out (write-once, never cleared — unlike ``injection_method``,
    which execute_loop's multi-step self-check resets to None), and the
    intent evidence (``target`` legacy shape / ``fault_spec`` canonical
    shape) proves a rollback target exists. Without this signal a row
    whose carrier fields were cleared post-issuance would project no
    fault handle and vanish from the recoverable set — the very
    "injected but unrecoverable" family the old predicate's pitfalls
    archive records.
    """
    issued = record.get("injection_start_time")
    if not issued:
        return False
    intent = _decode_json_value(record.get("target")) or _decode_json_value(
        record.get("fault_spec")
    )
    return bool(intent)


def _experiment_shaped_only(record: dict) -> bool:
    """Whether the row's whole liability is experiment-shaped (A2 gate).

    The balanced wing already proved every OWNED experiment dead. The
    residual question is whether anything ELSE rides this row — the combo
    shape (experiment + native mutation) whose native half survives wing
    balance. Three-leg discrimination on the persisted combo marker
    (``combo_native_issued``, round-32b):

    - ``true``  → combo: the native half may still be owed → NOT
      experiments-only (keep the committed-carrier fallback).
    - ``NULL``  → never asserted (legacy rows, pre-round-32b windows):
      absence of evidence is NOT evidence of absence — a combo whose
      marker never landed would be false-cleared by a guessed "no" →
      keep the fallback.
    - ``false`` → the execute birth seam asserted experiments-only. One
      cross-check before trusting it: a NATIVE-family method attribution
      alongside the experiments is combo evidence the marker missed (the
      recover-side criterion-2 mirror — the upgrade seam can miss the
      native→experiment re-attribution, leaving the row
      method=kubectl_native with live experiments). Only an
      experiment-family (or absent) attribution lets the balanced wing
      settle the row.
    """
    marker = _decode_json_value(record.get("combo_native_issued"))
    # Tri-state: None = never asserted → unknown → keep the fallback.
    # Only an explicit false/0 asserts experiments-only; true/1 (and any
    # unrecognised shape) is the combo leg — conservative by default.
    if marker is None:
        return False
    if not (marker is False or marker == 0):
        return False
    method = record.get("injection_method")
    if method:
        from chaos_agent.agent.providers import FaultProviderRegistry

        provider = FaultProviderRegistry.resolve_by_method(method)
        if provider is not None and not provider.has_experiment_uid:
            # Native-family attribution alongside experiments — combo
            # evidence the marker missed (criterion-2 mirror).
            return False
    return True


def may_carry_live_fault(record: dict) -> bool:
    """Single-source predicate behind the materialised ``tasks.liability_live``
    column (round-32 root-cause fix).

    "May this row still carry a live fault the cluster owes a destroy for?"
    — the RECOVERY question. Previously answered by guessing from the
    ``task_state`` word (TASK_STATE_ACTIVE_VALUES: "which lifecycle words
    sound unfinished"), which required a fresh human re-derivation for
    every new word and lost twice (round-32 K1 ``recovering`` orphan rows,
    K2 ``failed``-with-experiment verdicts) — permanently blinding
    recovery to faults that were deterministically live.

    Evidence order (highest authority first):

    A) **Ledger wing** — ``owned_experiment_uids`` / ``retired_experiment_uids``
       (the row-level persistence of ``live_liability_uids``, agent/state.py):
       ``owned − retired`` non-empty → True, no further appeal. An issued
       destroy that was never PROVEN dead stays a liability (fail-closed:
       a failed destroy leaves the experiment possibly-alive and still
       owed). BALANCED wings settle an experiments-only row dead outright
       (round-32b C2 — pre-fix the committed-carrier fallback overruled
       the row's own death record, so every swept-but-never-recovered
       task haunted ``query_active`` forever); a combo row keeps the
       fallback because its native half may still be owed — see
       :func:`_experiment_shaped_only` for the three-leg discrimination.

    B) **Committed-carrier fallback** (legacy rows that predate the ledger,
       and UID-less native carriers the ledger is structurally blind to):
       a fault handle still projects (``has_active_fault`` — carrier-agnostic
       committed predicate) OR the issued-evidence pair survived on its own
       (``_injection_was_issued``: write-once ``injection_start_time`` plus
       intent evidence) → True UNLESS the recover flow's own final verdict
       proves the whole task cleared (``_recovery_fully_cleared``).

    C) Never injected → False.

    The function is PURE over its record: both write paths (``upsert``
    inference and ``update_task_state`` recompute) feed it the merged
    tasks+task_details logical record, JSON columns as strings or decoded
    values alike (decoded internally). Monotonicity is inherited from the
    inputs' own monotonicity: the birth wing is append-only, the death
    wing only grows, and the recovery verdict only lands at finalize.
    """
    from chaos_agent.agent.state import has_active_fault

    owned = _decode_json_value(record.get("owned_experiment_uids"))
    retired = _decode_json_value(record.get("retired_experiment_uids"))
    owned = owned if isinstance(owned, list) else []
    retired = retired if isinstance(retired, list) else []

    # A) Ledger axis: authoritative for every row that ever landed one.
    if owned or retired:
        if set(owned) - set(retired):
            return True
        # Wings balanced → every owned experiment has a PROVEN death. For
        # an experiments-only row that IS the whole liability settled
        # (C2: the row's own death record outranks the committed-shape
        # fallback). Combo rows — marker true, marker NULL (legacy), or a
        # native-family attribution the marker missed — keep the fallback:
        # their native half survives wing balance.
        if _experiment_shaped_only(record):
            return False
        # else fall through to B.

    # B) Committed-carrier fallback: EITHER a fault handle still projects
    # (legacy experiment_uid / injection_method columns, native carriers)
    # OR the issued-evidence pair survived on its own (command went out,
    # carrier fields later cleared). Both are "committed"; neither clears
    # without the recover flow's own final verdict.
    if has_active_fault(record) or _injection_was_issued(record):
        return not _recovery_fully_cleared(record)

    # C) No committed fault, no ledger → nothing owed.
    return False


# ---------------------------------------------------------------------------
# TaskStore — business logic layer
# ---------------------------------------------------------------------------

class TaskStore:
    """Async persistent store for task state and execution metrics.

    Delegates all SQL I/O to a ``StorageBackend`` implementation.
    """

    def __init__(self, backend: StorageBackend) -> None:
        self._backend = backend

    # -- upsert --------------------------------------------------------------

    async def upsert(self, task_id: str, **fields: object) -> None:
        """Insert a new task or update specific columns on an existing row.

        Automatically:
        1. Reads the current ``tasks`` row (if any) and merges with *fields*.
        2. Extracts ``namespace`` / ``target_name`` from the ``target`` JSON.
        3. Infers ``task_state`` / ``stage`` / ``phase``.
        4. Sets ``gmt_create`` (preserved on update) / ``gmt_modified``.
        5. Splits merged fields into tasks + task_details and writes both.

        Non-task callers are rejected up front: only a real ``task-``
        identity may create or mutate a row (see
        ``persistence.task_identity``).  Conversation thread ids
        (``chaos-<session>``), per-turn ids (``turn-<hex>``) and
        placeholders (``"unknown"``) describe a *dialogue*, not a task,
        and previously leaked in as "ghost" experiments.
        """
        if not is_real_task_id(task_id):
            return

        # 1. Read current tasks row
        row = await self._backend.select_task(task_id)

        # 2. Merge: current DB values + incoming fields
        merged: dict = dict(row) if row else {"task_id": task_id}
        for k, v in fields.items():
            if k in _JSON_COLUMNS and v is not None:
                v = json.dumps(v, ensure_ascii=False, default=str)
            merged[k] = v

        # 3. Extract index fields (namespace, target_name) from target JSON
        merged = _extract_index_fields(merged)

        # 4. Infer task_state / stage / phase from the FULL logical record.
        # The lifecycle inputs (verification / recover_verification / result)
        # live in task_details, NOT the tasks row — the read path get()
        # already merges both tables, and inference must see the same record.
        # Deriving from the tasks row alone let a field-less tracer flush
        # re-project a verified task back to the "injecting" fallback
        # (inject-9bf2dddd: tasks row has experiment_uid but no verification
        # column, so infer saw "no verification" and regressed 'injected').
        inference_base = dict(merged)
        detail_row = None
        if row:
            detail_row = await self._backend.select_details(task_id)
            if detail_row:
                for k, v in detail_row.items():
                    inference_base.setdefault(k, v)
        # Round-32 — liability ledger wings merge MONOTONICALLY (union),
        # never overwrite: the sync path writes full AgentState snapshots,
        # so a hydration gap (state.owned is None after a DB-only recovery
        # path never re-read the ledger) would otherwise overwrite the
        # persisted wings with NULL and erase the row's recovery record.
        # Both wings are append-only by legislation (agent/state.py), so
        # union is lossless and idempotent.
        # ❗ previous MUST be read from the raw DB detail row: the fields
        # merge above already wrote the INCOMING wing value into
        # merged/inference_base, so ``inference_base.get(wing)`` would
        # union the incoming list with itself and silently drop the
        # persisted members (the hydration-gap erase this exists to stop).
        for wing in ("owned_experiment_uids", "retired_experiment_uids"):
            if wing not in merged:
                continue  # writer did not touch the wing → DB value survives
            incoming = _decode_json_value(merged.get(wing))
            previous_source = (
                (detail_row or {}).get(wing) if row else inference_base.get(wing)
            )
            previous = _decode_json_value(previous_source)
            incoming = incoming if isinstance(incoming, list) else []
            previous = previous if isinstance(previous, list) else []
            union = sorted({str(u) for u in previous} | {str(u) for u in incoming})
            payload = json.dumps(union, ensure_ascii=False)
            merged[wing] = payload
            inference_base[wing] = payload
        # Round-32b — combo-marker latch. The marker is tri-state and the
        # sync path writes full AgentState snapshots, so a None flush
        # (replan clear, hydration gap) must never erase the persisted
        # assertion — mirroring the wings' discipline above. True
        # additionally STICKS over a later False/None: a native companion
        # issued alongside a live experiment is a historical fact about
        # those experiments, and the wings it describes are append-only,
        # so downgrading the row back to "experiments-only" after the
        # fact would license exactly the false clear the marker exists to
        # prevent (fail-closed: an over-sticky true costs a lingering
        # recoverable row, a lost true can leak a live native mutation).
        if "combo_native_issued" in merged:
            _marker_in = _decode_json_value(merged.get("combo_native_issued"))
            # ❗ read the persisted leg from the RAW DB detail row — the
            # fields merge above already wrote the incoming value into
            # merged (the same shadowing the wing union guards against).
            _marker_db = (
                _decode_json_value((detail_row or {}).get("combo_native_issued"))
                if row
                else _marker_in
            )
            if _marker_in is None:
                _final = _marker_db  # a None flush never erases
            elif _marker_db is True or _marker_db == 1:
                _final = True  # sticky: a committed combo stays a combo
            else:
                _final = bool(_marker_in)
            _payload = None if _final is None else json.dumps(_final)
            merged["combo_native_issued"] = _payload
            inference_base["combo_native_issued"] = _payload
        merged.update(self._infer_fields(inference_base))
        # Round-32 — materialised liability verdict (single-source
        # predicate; see may_carry_live_fault). Computed on the SAME merged
        # logical record the state inference sees, so the word and the
        # verdict can never disagree about different data.
        merged["liability_live"] = 1 if may_carry_live_fault(inference_base) else 0
        merged["task_id"] = task_id

        # 5. Set gmt_create / gmt_modified
        _set_timestamps(merged, row)

        # 6. Split fields into two tables (exclude auto-increment `id`)
        task_cols = [c for c in _TASK_COLUMNS if c in merged and c != "id"]
        task_vals = [merged[c] for c in task_cols]

        detail_cols = [c for c in _DETAIL_COLUMNS if c in merged and c != "id"]
        detail_vals = [merged[c] for c in detail_cols]

        # 7. Write both tables (ON CONFLICT(task_id) DO UPDATE SET)
        await self._backend.upsert_task(task_id, task_cols, task_vals)
        if detail_cols:
            await self._backend.upsert_details(task_id, detail_cols, detail_vals)

    # -- read ----------------------------------------------------------------

    async def update_task_state(
        self,
        task_id: str,
        task_state: str,
        *,
        recover_verification: Optional[dict] = None,
        skip_if_terminal: bool = False,
    ) -> bool:
        """Directly update ``task_state`` on a task **without** inference.

        Unlike ``upsert``, this method writes the ``task_state`` column
        as-is, bypassing ``infer_task_state``.  Used by the recover flow
        to mark the ORIGINAL inject task as ``recovered`` /
        ``partial_recovered`` / ``failed`` without overwriting its
        ``operation``, ``result``, or ``verification`` fields.

        ``recover_verification`` (round-33b single-source): the clearance
        verdict that JUSTIFIES a CLEARED word, propagated onto the SAME
        row in the SAME write. A CLEARED state word and its clearance
        verdict are one atomic fact; before this they could split across
        rows (the recover flow wrote the verdict to the recover- row but
        only the bare word to the inject- row), leaving the inject row a
        CLEARED word with no row-local proof — a permanent
        "completed-but-uncleared" ghost under the fail-closed predicate.
        When supplied, the verdict is persisted to
        ``task_details.recover_verification`` BEFORE the liability
        recompute below, so the word and the verdict clear together.
        Omitting it keeps the fail-closed behaviour: a CLEARED word with
        no verdict on record stays ``liability_live = 1``.

        ``skip_if_terminal`` (round-54 G6, the abort-path guard): when
        True, a row already resting on its OWN terminal word — the run
        reached ``completed`` / ``recovered`` / ``failed`` / … before
        the abort fired — is left untouched and the call returns False.
        The abort exits (stream cancel / disconnect / internal error)
        write ``cancelled`` / ``failed`` for runs whose graph NEVER
        finished; a race that lands the abort write during result
        extraction, after the pipeline completed, must not rewrite the
        run's own verdict ("completed" → "cancelled"). Returns True
        when the write landed.

        Same identity guard as :meth:`upsert` — only a real ``task-``
        id may be written.

        Closed-set guard (round-16 S5, domain widened round-17 D4): an
        out-of-set word here is a PROGRAM BUG (typo, foreign domain
        word), not legacy data — a silent clamp would mask it, so the
        write is rejected loudly. The domain is the COLUMN's value
        domain (lifecycle closed set ∪ persistence overlay words —
        upsert's _infer_fields legitimately emits ``waiting_input`` /
        ``pending`` into the same column), not the bare TaskState set:
        before round-17 the gate would have rejected the very words
        the upsert write path emits (one column, two write paths,
        one gated one not). Contrast the verification-vocabulary read
        gate (round-15 D5): reads face legacy rows and may only
        normalise, never reject; writes face fresh code and must fail
        fast.

        Round-32: the word and the materialised liability verdict travel
        together on this write path too — ``recovering`` (this method's
        dominant caller during recover start) is a MID-FLIGHT word, and
        the row it lands on must keep its ``liability_live = 1`` (the
        pre-fix write lost exactly this: a crash between this write and
        finalize left the row in a state no query could ever find again
        — round-32 K1). The verdict is recomputed from the merged row
        evidence (not derived from the word — that's the whole point of
        the column), so a finalize write of ``recovered`` lands on a row
        whose recover verdict is already on record and clears it, while
        a mid-flight ``recovering`` write cannot clear what no verdict
        proved dead.
        """
        if not is_real_task_id(task_id):
            return
        if task_state not in TASK_STATE_COLUMN_VALUES:
            raise ValueError(
                f"update_task_state: {task_state!r} is outside the task_state "
                f"column value domain (lifecycle closed set + persistence "
                f"overlay); refusing to persist an unlegislated word"
            )
        # Rebuild the logical record (tasks + task_details merged) the
        # single-source predicate sees on the upsert path, so both write
        # paths compute ``liability_live`` from identical evidence.
        record: dict = dict(await self._backend.select_task(task_id) or {})
        detail_row = await self._backend.select_details(task_id)
        if detail_row:
            record.update(
                {k: v for k, v in detail_row.items() if k not in ("id",)}
            )
        # Round-54 G6: an abort word must never regress a row that
        # already reached its own terminal verdict. The abort exits'
        # write (``write_aborted_task_row``) races the run's own tail
        # writers — a cancel landing during result extraction, after
        # the pipeline completed, used to rewrite "completed" into
        # "cancelled". The run that finished keeps its own word; the
        # abort word is only for runs whose graph never got to finish.
        if skip_if_terminal and str(record.get("task_state") or "") in (
            TASK_STATE_TERMINAL_VALUES
        ):
            return False
        # Round-33b: land the clearance verdict on THIS row first so the
        # liability recompute below sees it (word + verdict clear together).
        if recover_verification is not None:
            _verdict_payload = (
                recover_verification
                if isinstance(recover_verification, str)
                else json.dumps(recover_verification, ensure_ascii=False)
            )
            # ❗ task_id MUST be in the column list: upsert_details builds
            # INSERT … ON CONFLICT(task_id) from ``columns`` (its task_id
            # arg is not injected into the SQL), so omitting it yields a
            # NOT NULL violation on task_details.task_id — the same trap
            # upsert_task guards against.
            await self._backend.upsert_details(
                task_id,
                ["task_id", "recover_verification"],
                [task_id, _verdict_payload],
            )
            record["recover_verification"] = _verdict_payload
        liability_live = 1 if may_carry_live_fault(record) else 0
        # ❗ task_id MUST be in the column list: the backend builds
        # INSERT … ON CONFLICT(task_id) DO UPDATE SET from it. Omitting it
        # produced a NULL-task_id ghost row on SQLite and a NOT NULL
        # violation on PostgreSQL (empty SET clause on PG additionally
        # yields a syntax error for conflict-key-only upserts).
        # Terminal words imply an END TIME (W-56-8 review round 4). The abort
        # paths (three stream modules' cancel / disconnect / internal-error
        # exits, the CLI's signal handler, and the intent phase's user
        # cancellation) write their word through THIS method — which used to
        # write exactly ["task_id", "task_state", "liability_live"], no
        # timestamps — so a run interrupted mid-graph kept finished_at='' and
        # the read-side derivation rendered duration_ms=0, while the SAME
        # event's other user-visible surface stamped its own end time
        # (session_store.finalize_session: session["finished_at"] =
        # now_iso()) and the row's own word already said the run was over.
        # Measured on a real row before this: word 'injecting' ->
        # 'cancelled' with finished_at None -> None.
        #
        # The word and its timestamp are one fact, so they travel in ONE
        # write: the stamp is appended to this call's column list rather than
        # issued as a second statement, which also means no crash window can
        # leave a dated row still saying the run was in flight.
        #
        # Only terminal words stamp: this method's mid-flight callers
        # ("injecting" at a resume, "recovering" at recover start) describe a
        # run still in motion. ``record`` is the merged tasks+details view,
        # so a row that already carries a stamp keeps it — the run that
        # finished keeps its own end time, the same way skip_if_terminal
        # keeps its own word.
        _columns = ["task_id", "task_state", "liability_live"]
        _values = [task_id, task_state, liability_live]
        if task_state in _TERMINAL_TASK_STATES and not record.get("finished_at"):
            _columns.append("finished_at")
            _values.append(now_iso())
        await self._backend.upsert_task(task_id, _columns, _values)
        return True

    async def get(self, task_id: str) -> Optional[dict]:
        """Return the full task data (tasks + task_details merged).

        Returns ``None`` if not found.
        """
        task_row = await self._backend.select_task(task_id)
        if task_row is None:
            return None
        detail_row = await self._backend.select_details(task_id)
        # Merge: detail_columns first, then task_columns override (e.g. task_id)
        merged = {**(detail_row or {}), **task_row}
        return self._row_to_dict(merged)

    async def list_tasks(self, task_state: str = None, limit: int = 50, offset: int = 0) -> list[dict]:
        """Return tasks from the narrow table, ordered by ``gmt_create`` DESC.

        No large JSON fields are included (only the hot-path columns).
        """
        if task_state:
            rows = await self._backend.select_tasks_by_state(task_state, limit, offset)
        else:
            rows = await self._backend.select_tasks_ordered(limit, offset)
        return [self._row_to_dict(r) for r in rows]

    async def query_active(self, namespace: str = "", target_name: str = "", tenant_id: str = "", workspace_id: str = "") -> list[dict]:
        """Return liability-carrying rows (``tasks.liability_live = 1``) as
        ExperimentStore-compatible dicts — the recovery-discovery set.

        Round-32 re-keyed this query off the retired word predicate
        (``task_state IN ACTIVE_SET``) onto the materialised verdict column
        (written by ``may_carry_live_fault`` on both write paths); the
        release channel for that column is the recover flow's own verdict
        (round-33) — an issued-but-never-recovered row staying listed is
        the fail-closed design, not a leak.

        Filtering is done at the SQL level using the ``namespace`` /
        ``target_name`` / ``tenant_id`` / ``workspace_id`` indexed columns
        (no Python-side JSON filtering). Empty filter values mean
        unfiltered — local CLI / bare SDK entries pass empty strings and
        see everything, exactly as before the workspace axis existed.
        """
        rows = await self._backend.select_active_tasks(namespace, target_name, tenant_id, workspace_id)
        results = []
        for d in (self._row_to_dict(r) for r in rows):
            # Need target JSON from task_details for compatibility
            detail = self._row_to_dict(await self._backend.select_details(d["task_id"]) or {})
            target = detail.get("target") or {}
            fault_type = self._compute_fault_type({**detail, **d})
            results.append({
                "task_id": d["task_id"],
                "operation": d.get("operation", "inject"),
                "skill": d.get("skill_name", ""),
                "fault_type": fault_type,
                "target": target,
                "target_name": d.get("target_name", ""),
                "params": detail.get("params") or {},
                "experiment_uid": d.get("experiment_uid", ""),
                "plan_summary": detail.get("plan_summary") or "",
                "gmt_create": d.get("gmt_create", ""),
                "status": "success" if not d.get("error") else "failed",
                "error": d.get("error"),
            })
        return results

    async def delete(self, task_id: str) -> bool:
        """Delete a task and its associated details + spans.

        Deletion order: spans → details → tasks (code-maintained consistency).
        """
        await self._backend.delete_spans_by_task(task_id)
        await self._backend.delete_details(task_id)
        return await self._backend.delete_task(task_id)

    async def count(self, task_state: str = None) -> int:
        """Count tasks, optionally filtered by state."""
        return await self._backend.count_tasks(task_state)

    # -- span methods --------------------------------------------------------

    async def append_span(
        self,
        task_id: str,
        node_name: str,
        start_time: float,
        end_time: float,
        duration_ms: float,
        token_input: int = 0,
        token_output: int = 0,
        tool_calls: list[str] | None = None,
        error: str | None = None,
    ) -> None:
        """Append a span row and update task_details summary fields."""
        now = now_iso()
        await self._backend.insert_span(
            task_id, node_name, start_time, end_time, duration_ms,
            token_input, token_output,
            json.dumps(tool_calls or [], ensure_ascii=False),
            error, now, now,  # gmt_create, gmt_modified
        )
        await self._backend.update_task_summary(
            task_id, token_input, token_output, int(duration_ms),
            len(tool_calls) if tool_calls else 0,
            1 if token_input > 0 else 0,  # heuristic: tokens consumed → LLM call
            now,  # gmt_modified
        )

    async def get_spans(self, task_id: str) -> list[dict]:
        """Return all spans for a task, ordered by ``id``."""
        rows = await self._backend.select_spans(task_id)
        result = []
        for d in rows:
            d["tool_calls"] = json.loads(d.get("tool_calls", "[]"))
            result.append(d)
        return result

    async def get_summary(self, task_id: str) -> Optional[dict]:
        """Return summary metrics from the ``task_details`` row."""
        detail = await self._backend.select_details(task_id)
        if detail is None:
            return None
        return {k: detail[k] for k in (
            "total_token_input", "total_token_output", "total_token_cached",
            "total_llm_calls", "total_tool_calls", "total_duration_ms",
        ) if k in detail}

    # -- metric methods ------------------------------------------------------

    async def get_metric(self, task_id: str) -> Optional[dict]:
        """Return combined metric data (status + spans + summary) for a task.

        This is the primary method for the ``metric --task-id`` command.
        Beyond lifecycle/metrics it also carries the trace narrative —
        ``fault_spec`` (original intent), ``feasibility_report``, and the
        raw ``postmortem`` dict (path / summary / markdown) — so a detail
        view renders the whole evidence chain from one envelope. Clients
        parse the markdown's ``## Timeline`` section themselves.
        """
        task = await self.get(task_id)
        if task is None:
            return None

        spans = await self.get_spans(task_id)
        summary = await self.get_summary(task_id) or {
            "total_token_input": 0,
            "total_token_output": 0,
            "total_token_cached": 0,
            "total_llm_calls": 0,
            "total_tool_calls": 0,
            "total_duration_ms": 0,
        }

        fault_type = self._compute_fault_type(task)
        from chaos_agent.agent.state import infer_status
        # Read-side missing-field sentinel (round-16 S4): a row without the
        # task_state column (legacy schema / partial migration) is UNKNOWN,
        # not in-flight — defaulting to "injecting" dressed a terminal or
        # unknown record up as running (the round-14 default+closed-set
        # trap, phase-domain edition). "unknown" is deliberately OUTSIDE
        # the TaskState closed set: it asserts "no evidence in the row",
        # never a lifecycle claim.
        task_state = task.get("task_state") or "unknown"
        if "task_state" not in task:
            logger.warning(
                "task row %s has no task_state column (legacy schema?); "
                "reporting 'unknown' instead of guessing a lifecycle word",
                task.get("task_id", "?"),
            )
        operation = task.get("operation", "")
        stage = task.get("stage", "injection")

        # Compute duration_ms from timestamps if not already set
        duration_ms = task.get("duration_ms", 0)
        if not duration_ms:
            gmt_create = task.get("gmt_create", "")
            finished_at = task.get("finished_at", "")
            if gmt_create and finished_at:
                try:
                    from chaos_agent.utils.time import parse_iso_timestamp
                    ct = parse_iso_timestamp(gmt_create)
                    ft = parse_iso_timestamp(finished_at)
                    duration_ms = int((ft - ct).total_seconds() * 1000)
                except (ValueError, TypeError):
                    pass

        # Merge failure_reason into error
        merged_error = task.get("failure_reason") or task.get("error") or ""

        return {
            "task_id": task_id,
            # See ``get_all_metrics`` for why both ``task_state`` and
            # ``status`` are exposed (raw lifecycle vs derived rollup).
            "task_state": task_state,
            "operation": operation,
            "stage": stage,
            "status": infer_status(stage, task_state, operation),
            "phase": task.get("phase", "planning"),
            "fault_type": fault_type,
            "skill_name": task.get("skill_name", ""),
            # LLM model frozen at task finalize time (snapshot of the
            # at-run config, NOT live config) — empty for tasks archived
            # before the model_name column existed.
            "model_name": task.get("model_name") or "",
            "target": task.get("target"),
            "params": task.get("params"),
            "experiment_uid": task.get("experiment_uid", ""),
            "safety_status": task.get("safety_status", "pending"),
            "safety_reason": task.get("safety_reason"),
            "needs_confirm": bool(task.get("needs_confirm", 0)),
            "verification": task.get("verification"),
            "recover_verification": task.get("recover_verification"),
            "fault_spec": task.get("fault_spec") or {},
            "feasibility_report": task.get("feasibility_report"),
            "postmortem": task.get("postmortem"),
            "plan_summary": task.get("plan_summary", ""),
            "error": merged_error,
            "gmt_create": task.get("gmt_create", ""),
            "gmt_modified": task.get("gmt_modified", ""),
            "finished_at": task.get("finished_at", ""),
            "duration_ms": duration_ms,
            "spans": spans,
            "summary": summary,
        }

    async def get_all_metrics(self, task_state: str = None, limit: int = 200) -> dict:
        """Return metric data for all tasks.

        Uses a single batch read of ``task_details`` to avoid N+1 queries.
        This is the primary method for the ``metric`` (no task-id) command.
        """
        tasks = await self.list_tasks(task_state=task_state, limit=limit)
        if not tasks:
            return {"total": 0, "tasks": []}

        # Batch-read details (eliminates N+1)
        task_ids = [t["task_id"] for t in tasks]
        details_rows = await self._backend.select_details_batch(task_ids)
        details_map = {r["task_id"]: r for r in details_rows}

        from chaos_agent.agent.state import (
            infer_status,
            liability_group_for,
        )
        task_list = []
        for task in tasks:
            detail = self._row_to_dict(details_map.get(task["task_id"], {}))
            summary = {k: detail.get(k, 0) for k in (
                "total_token_input", "total_token_output", "total_token_cached",
                "total_llm_calls", "total_tool_calls", "total_duration_ms",
            )}
            # Same missing-field sentinel as get_metric (round-16 S4):
            # no lifecycle column → "unknown", never "injecting".
            _ts = task.get("task_state") or "unknown"
            _op = task.get("operation", "")
            _stage = task.get("stage", "injection")

            target = detail.get("target")
            fault_type = self._compute_fault_type({**detail, **task})

            # Merge failure_reason into error
            merged_error = detail.get("failure_reason") or task.get("error") or ""

            task_list.append({
                "task_id": task["task_id"],
                # ``task_state`` is the raw lifecycle field
                # (injecting / injected / recovering / recovered /
                # partial_recovered / failed / rejected / completed)
                # — clients that need to gate "is this still in
                # flight" reach for it directly. ``status`` below is
                # the derived success/failed/in_progress/pending
                # rollup; both are exposed because they answer
                # different questions and the prior rollup-only shape
                # silently broke the TS TUI's PendingTasksCard, which
                # filters on ``task_state in {"injecting","injected"}``
                # but was reading ``undefined`` on every row.
                "task_state": _ts,
                # Round-32 — the materialised liability verdict, same column
                # ``select_active_tasks`` keys on. Exposed here so
                # list-consuming clients (TUI boot card's pending list)
                # filter on the EVIDENCE-backed verdict instead of
                # re-guessing from task_state words — the boot card's old
                # PENDING_STATES word copy is exactly the round-16 S1 /
                # round-32 drift family this field retires.
                "liability_live": bool(task.get("liability_live")),
                # Round-32b P3 — the boot card's three-group split
                # (in_flight / needs_recovery / uncleared), legislated
                # server-side off the state.py word tables so the TS
                # display layer carries no word copy (the PENDING_STATES
                # drift family stays retired). Only meaningful for
                # liability-live rows — dead rows ship null.
                "liability_group": (
                    liability_group_for(_ts)
                    if task.get("liability_live")
                    else None
                ),
                # Commitment evidence (injection command was issued).
                # NOTE (round-32): this is NO LONGER the same source of
                # truth as ``select_active_tasks`` — the recoverable set
                # now keys on ``liability_live`` (ledger-first verdict);
                # ``committed`` remains the issued-evidence signal for
                # display-layer "in flight" rollups.
                "committed": bool(detail.get("injection_start_time")),
                "operation": _op,
                "stage": _stage,
                "status": infer_status(_stage, _ts, _op),
                "phase": task.get("phase", "planning"),
                "fault_type": fault_type,
                "skill_name": task.get("skill_name", ""),
                "experiment_uid": task.get("experiment_uid", ""),
                "target": target,
                "gmt_create": task.get("gmt_create", ""),
                "gmt_modified": task.get("gmt_modified", ""),
                "finished_at": task.get("finished_at", ""),
                "error": merged_error,
                "summary": summary,
            })
        return {
            "total": len(task_list),
            "tasks": task_list,
        }

    # -- internal helpers (pure Python, sync) --------------------------------

    @staticmethod
    def _row_to_dict(row: dict) -> dict:
        """Deserialize JSON columns in a row dict."""
        d = dict(row)
        for col in _JSON_COLUMNS:
            val = d.get(col)
            if isinstance(val, str) and val:
                try:
                    d[col] = json.loads(val)
                except json.JSONDecodeError:
                    pass
        return d

    @staticmethod
    def _infer_fields(merged: dict) -> dict:
        """Run infer_task_state / infer_stage / infer_phase on merged values.

        Returns a dict with ``task_state``, ``stage``, ``phase``.
        Inference is based on the full merged data (DB row + new fields),
        so the result always reflects the current state of all fields.

        Additionally detects ``waiting_input`` state: when a task is paused
        at an interrupt point (confirmation_gate or ask_human), waiting for
        user input. This is used by TUI crash recovery to discover tasks
        that need to be resumed.
        """
        try:
            from chaos_agent.agent.state import (
                has_active_fault,
                infer_phase,
                infer_stage,
                infer_task_state,
            )

            values = dict(merged)
            # Deserialize JSON fields for inference
            for col in _JSON_COLUMNS:
                val = values.get(col)
                if isinstance(val, str) and val:
                    try:
                        values[col] = json.loads(val)
                    except json.JSONDecodeError:
                        pass
            # Convert DB column names to AgentState names where they differ
            if "needs_confirm" in values:
                values["needs_confirmation"] = values["needs_confirm"]

            task_state = infer_task_state(values)
            # Monotonicity guard: a terminal verdict never regresses to the
            # "injecting" fallback ("no lifecycle evidence in the record") —
            # a record that already proved injected/recovered/failed cannot
            # un-prove it. Defense in depth on top of the full-record merge
            # in upsert(); also protects state.py consumers that call this
            # with a partial dict.
            prev_state = (values.get("task_state") or "").strip()
            if task_state == "injecting" and prev_state in _TERMINAL_TASK_STATES:
                task_state = prev_state
            stage = infer_stage(values)
            phase = infer_phase(values)

            # Non-injection intents (chat, recover) are completed immediately.
            # Older sessions may still carry "query"/"explore" as confirmed_intent;
            # treat them the same way to stay backward-compatible with persisted state.
            if values.get("confirmed_intent") in ("chat", "recover", "query", "explore"):
                task_state = "completed"
                # DB columns stage/phase are NOT NULL — use descriptive defaults
                # instead of None which would violate the constraint
                if stage is None:
                    stage = "injection"  # DB default; non-injection has no meaningful stage
                if phase is None:
                    phase = "completed"  # More descriptive than "planning" for a completed task

            # Detect waiting_input: task is paused at an interrupt point
            # (needs confirmation but no committed fault yet, or
            # interaction_mode=tui with confirmed_intent still None).
            # ``cancelled`` joins the first branch: ids are reused across
            # rejections, and the *next* clarification round writes
            # needs_confirm again — that row is waiting on a fresh
            # confirmation card, and the TUI crash-recovery detector must
            # still be able to find it (a cancelled verdict from the
            # previous round must not blind it).
            #
            # Round-64 R4: the confirmation branch delegates to the shared
            # ``paused_task_state`` so the row and the result/session
            # surfaces derive the SAME word from the SAME predicate — the
            # derivation used to live only here, which is why the row said
            # ``waiting_input`` while every envelope said ``failed``.
            from chaos_agent.agent.state import paused_task_state

            _paused_word = paused_task_state(values)
            if _paused_word:
                task_state = _paused_word
            elif values.get("interaction_mode") == "tui" and not values.get("confirmed_intent") and not has_active_fault(values):
                task_state = TaskStateOverlay.WAITING_INPUT.value

            # Newborn anchor: a row with zero lifecycle evidence has not
            # entered its pipeline yet. Reporting it as "injecting" made
            # rejected / abandoned intents masquerade as unfinished work
            # on the boot card. Runs AFTER waiting_input detection so rows
            # the crash-recovery detector claims keep their semantics
            # (interaction_mode is deliberately not evidence — it marks
            # the session, not pipeline ownership).
            if task_state == "injecting" and not any(
                values.get(_field) for _field in _LIFECYCLE_EVIDENCE_FIELDS
            ):
                task_state = TaskStateOverlay.PENDING.value

            return {
                "task_state": task_state,
                "stage": stage,
                "phase": phase,
            }
        except Exception as e:
            logger.warning(f"TaskStore infer failed: {e}")
            return {}

    @staticmethod
    def _compute_fault_type(task: dict) -> str:
        """Infer fault_type through the shared FaultSpec projection."""
        from chaos_agent.agent.spec.fault_spec import FaultSpec, fault_type_from_state

        state = dict(task or {})
        params = task.get("params") or {}
        if isinstance(params, dict):
            scope = params.get("scope", "")
            action = params.get("action", "")
            target_action = params.get("target", "")
            if scope and target_action and action:
                target = task.get("target") if isinstance(task.get("target"), dict) else {}
                state["fault_spec"] = FaultSpec(
                    namespace=str(target.get("namespace") or ""),
                    scope=str(scope),
                    names=tuple(str(n) for n in (target.get("names") or [])),
                    labels=dict(target.get("labels") or {}),
                    fault_target=str(target_action),
                    fault_action=str(action),
                    params={
                        k: v
                        for k, v in params.items()
                        if k not in {"scope", "target", "action"}
                    },
                    source="task_store_legacy_params",
                ).to_dict()
        return fault_type_from_state(state)

    # -- sessions ------------------------------------------------------------

    _SESSION_COLUMNS: list[str] = [
        "session_id", "status", "cluster_name", "namespace",
        "started_at", "finished_at", "gmt_create", "gmt_modified",
    ]

    async def record_session(self, session_id: str, **fields: object) -> None:
        """Insert or update a session record."""
        if not session_id:
            return
        existing = await self._backend.select_session(session_id)
        merged: dict = dict(existing) if existing else {"session_id": session_id}
        merged.update(fields)
        merged["session_id"] = session_id
        _set_timestamps(merged, existing)

        cols = [c for c in self._SESSION_COLUMNS if c in merged]
        vals = [merged[c] for c in cols]
        await self._backend.upsert_session(session_id, cols, vals)

    async def list_sessions(self, status: str = "", limit: int = 50, offset: int = 0) -> list[dict]:
        """Return sessions ordered by gmt_create DESC."""
        return await self._backend.select_sessions_ordered(limit, offset, status=status)


# ---------------------------------------------------------------------------
# Singleton / global instance
# ---------------------------------------------------------------------------

_store: Optional[TaskStore] = None
_store_key: str = ""  # "sqlite:<path>" or "postgresql:<dsn>"


def _current_store_key() -> str:
    """Return the cache key derived from current settings."""
    backend_type = getattr(settings, "tasks_db_backend", "sqlite")
    if backend_type == "postgresql":
        dsn = getattr(settings, "tasks_pg_dsn", "")
        return f"postgresql:{dsn}"
    return f"sqlite:{settings.resolved_tasks_db_path}"


async def get_task_store() -> TaskStore:
    """Get or create the TaskStore for the current backend configuration.

    Backend selection is driven by ``settings.tasks_db_backend``
    (``"sqlite"`` or ``"postgresql"``).  If the configuration changes
    (e.g. ``blade_ai_context(tasks_db_backend=\"postgresql\")``), the
    cached store is closed and recreated so that reads and writes always
    go to the same data source.
    """
    global _store, _store_key

    key = _current_store_key()
    if _store is not None and _store_key == key:
        return _store

    # Backend changed (or first call) — close old store if any
    if _store is not None:
        try:
            await _store._backend.close()
        except Exception:
            pass
        _store = None

    backend_type = getattr(settings, "tasks_db_backend", "sqlite")
    backend: StorageBackend
    try:
        if backend_type == "postgresql":
            from chaos_agent.persistence.task_store_postgresql import PostgreSQLBackend
            dsn = getattr(settings, "tasks_pg_dsn", "")
            if not dsn:
                raise ValueError("tasks_pg_dsn must be set when tasks_db_backend=postgresql")
            backend = await PostgreSQLBackend.create(dsn)
        else:
            from chaos_agent.persistence.task_store_sqlite import SQLiteBackend
            backend = await SQLiteBackend.create(db_path=settings.resolved_tasks_db_path)
    except Exception:
        try:
            await backend.close()  # type: ignore[possibly-undefined]
        except Exception:
            pass
        raise

    _store = TaskStore(backend=backend)
    _store_key = key
    return _store


async def reset_task_store() -> None:
    """Close and reset the global TaskStore instance."""
    global _store, _store_key
    if _store is not None:
        try:
            await _store._backend.close()
        except Exception:
            pass
    _store = None
    _store_key = ""


def _sync_close_store() -> None:
    """atexit / per-test callback: close the TaskStore, stopping its worker thread.

    A previous version closed only the underlying ``sqlite3.Connection``
    (``_conn._conn``) and left aiosqlite's background worker thread running.
    That thread keeps a reference to the event loop the connection was created
    on; once that loop closes (e.g. pytest-asyncio's per-test loop), the
    thread's next ``call_soon_threadsafe`` raises ``RuntimeError: Event loop is
    closed``. Across a large suite the leaked threads accumulate and surface as
    flaky, non-deterministic failures and hangs on CI.

    The reliable stop is a real async ``close()`` — but it must run on a *live*
    loop. We drive it on a fresh, throwaway loop: aiosqlite resolves the close
    future via ``future.get_loop()`` (the fresh loop), so the worker completes
    and exits cleanly regardless of whether the original loop is already gone.

    For asyncpg: ``pool.close()`` is likewise awaited on the fresh loop.
    """
    global _store
    store = _store
    _store = None
    if store is None or getattr(store, "_backend", None) is None:
        return
    backend = store._backend

    # Preferred path: a clean async close that also stops the worker thread.
    try:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(backend.close())
        finally:
            loop.close()
        return
    except Exception:
        pass

    # Best-effort fallback: close the raw sqlite3 connection directly. Leaves
    # the worker thread if the async close was unavailable, but never raises.
    try:
        if hasattr(backend, "_conn") and backend._conn is not None:
            raw_conn = getattr(backend._conn, "_conn", None)
            if raw_conn is not None:
                raw_conn.close()  # sqlite3.Connection.close() is sync
    except Exception:
        pass


atexit.register(_sync_close_store)
