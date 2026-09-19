"""Fake asyncpg Pool/Connection for testing PostgreSQLBackend offline.

No real PostgreSQL (or even the ``asyncpg`` package) is available in CI,
so the backend is exercised against an in-memory fake that implements the
*actual SQL semantics* the backend relies on rather than just recording
calls:

- ``INSERT … ON CONFLICT(<col>) DO UPDATE SET`` is parsed and applied as
  a true column-level merge (partial upserts must not clobber other cols).
- The incremental ``UPDATE task_details SET total_* = total_* + $N`` is
  applied as accumulation, not assignment.
- ``DELETE`` returns the ``"DELETE n"`` command tag asyncpg produces.
- TIMESTAMPTZ columns reject string timestamps — mirroring asyncpg's real
  type checking, so a missing ``_coerce_timestamps`` call fails loudly.
- ``ALTER TABLE … ADD COLUMN`` without ``IF NOT EXISTS`` raises on the
  second run, reproducing the migration path ``ensure_schema`` depends on.

Statements outside the recognized patterns raise, so any new backend SQL
fails tests instead of silently passing a no-op fake.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone


class FakePgError(Exception):
    """Stand-in for asyncpg.DuplicateColumnError and friends."""


# ---------------------------------------------------------------------------
# Record
# ---------------------------------------------------------------------------

class FakeRecord:
    """Minimal asyncpg.Record stand-in supporting dict(record) and row[0]."""

    def __init__(self, mapping: dict):
        self._mapping = dict(mapping)

    def keys(self):
        return self._mapping.keys()

    def __iter__(self):
        return iter(self._mapping)

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self._mapping.values())[key]
        return self._mapping[key]

    def __len__(self):
        return len(self._mapping)


# ---------------------------------------------------------------------------
# In-memory database
# ---------------------------------------------------------------------------

# Columns declared TIMESTAMPTZ in the backend DDL. asyncpg encodes these
# from datetime only — passing a str raises DataError. The fake enforces
# the same contract so timestamp coercion bugs surface in tests.
_TIMESTAMP_COLUMNS = frozenset(
    {"gmt_create", "gmt_modified", "started_at", "finished_at"}
)


class _FakeDatabase:
    def __init__(self):
        self.tables: dict[str, dict[str, dict]] = {}   # table -> key -> row
        self.conflict_cols = {"tasks": "task_id", "task_details": "task_id",
                             "task_spans": None, "sessions": "session_id"}
        self.task_details_extra_cols: set[str] = set()  # ALTER TABLE history
        self.sequences: dict[str, int] = {}
        self.executed: list[str] = []                    # full statement log


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

class FakeConn:
    def __init__(self, db: _FakeDatabase):
        self._db = db

    # -- asyncpg API ---------------------------------------------------------

    async def execute(self, sql: str, *args) -> str:
        self._db.executed.append(sql)
        s = sql.strip()
        if s.startswith("ALTER TABLE"):
            return self._alter(s)
        if s.startswith("CREATE TABLE") or s.startswith("CREATE UNIQUE INDEX") \
                or s.startswith("CREATE INDEX"):
            return "OK"
        if s.startswith("INSERT INTO"):
            return self._insert(s, args)
        if s.startswith("UPDATE"):
            return self._update(s, args)
        if s.startswith("DELETE FROM"):
            return self._delete(s, args)
        raise FakePgError(f"fake conn: unsupported execute: {s[:80]}")

    async def fetchrow(self, sql: str, *args):
        rows = await self.fetch(sql, *args)
        return rows[0] if rows else None

    async def fetch(self, sql: str, *args) -> list[FakeRecord]:
        s = sql.strip()
        if s.startswith("SELECT COUNT(*)"):
            return [FakeRecord({"count": self._count(s, args)})]
        # Round-32: select_active_tasks dropped the LEFT JOIN (the predicate
        # moved onto tasks.liability_live), so dispatch on the t-alias shape —
        # it is the only fetch in the backend using "FROM tasks t".
        if "FROM tasks t" in s:
            return self._select_active(s, args)
        if s.startswith("SELECT * FROM task_details WHERE task_id IN"):
            return self._select_details_batch(s, args)
        if s.startswith("SELECT * FROM"):
            return self._select_star(s, args)
        raise FakePgError(f"fake conn: unsupported fetch: {s[:80]}")

    def transaction(self):
        return _FakeTransaction()

    # -- helpers -------------------------------------------------------------

    def _check_timestamps(self, columns: list[str], values: list) -> None:
        for col, val in zip(columns, values):
            if col in _TIMESTAMP_COLUMNS and isinstance(val, str):
                raise FakePgError(
                    f'invalid input for query parameter "{col}": '
                    f"expected datetime for TIMESTAMPTZ, got str"
                )

    def _alter(self, s: str) -> str:
        m = re.match(r"ALTER TABLE (\w+) ADD COLUMN( IF NOT EXISTS)? (\w+)", s)
        if not m:
            raise FakePgError(f"fake conn: unsupported ALTER: {s[:80]}")
        table, if_not_exists, col = m.groups()
        if col in self._db.task_details_extra_cols:
            if if_not_exists:
                return "ALTER TABLE"
            # Mirrors PG raising DuplicateColumnError — ensure_schema relies
            # on this to run one-shot migrations exactly once.
            raise FakePgError(f'column "{col}" of relation "{table}" already exists')
        self._db.task_details_extra_cols.add(col)
        # The injection_start_time migration backfills inside the same
        # transaction; reproduce its semantics so the one-shot behavior is
        # observable in tests.
        if table == "task_details" and col == "injection_start_time":
            for row in self._db.tables.get("task_details", {}).values():
                if row.get("injection_start_time") is None and (
                    row.get("target") is not None or row.get("fault_spec") is not None
                ):
                    task_row = self._db.tables.get("tasks", {}).get(row["task_id"])
                    row["injection_start_time"] = (
                        (task_row or {}).get("gmt_create") or row.get("gmt_create")
                    )
        # Round-32: the liability_live migration backfills inside the same
        # transaction (cleared-word fallback + issued-intent evidence);
        # reproduce its semantics, mirroring injection_start_time above, so
        # legacy already-blinded rows are observably re-admitted in tests.
        if table == "tasks" and col == "liability_live":
            from chaos_agent.agent.state import TASK_STATE_CLEARED_VALUES

            details = self._db.tables.get("task_details", {})
            for row in self._db.tables.get("tasks", {}).values():
                # ALTER ADD COLUMN … DEFAULT 0 materialises 0 on every
                # pre-existing row in real PG; then the one-shot backfill
                # re-admits issued-but-uncleared rows.
                row.setdefault("liability_live", 0)
                if row["liability_live"] == 1:
                    continue
                if row.get("task_state") in TASK_STATE_CLEARED_VALUES:
                    continue
                d = details.get(row.get("task_id"))
                if d is None:
                    continue
                if d.get("target") is None and d.get("fault_spec") is None:
                    continue
                if d.get("injection_start_time") is None:
                    continue
                row["liability_live"] = 1
        return "ALTER TABLE"

    def _insert(self, s: str, args: tuple) -> str:
        # INSERT INTO <t> (<cols>) VALUES ($1, …) [ON CONFLICT(<k>) DO UPDATE SET …]
        m = re.match(
            r"INSERT INTO (\w+) \(([^)]+)\) VALUES \(([^)]+)\)"
            r"(?: ON CONFLICT\((\w+)\) (DO UPDATE SET.+|DO NOTHING))?$",
            s,
        )
        if not m:
            raise FakePgError(f"fake conn: unsupported INSERT: {s[:80]}")
        table, cols_s, phs_s, conflict_col = m.group(1, 2, 3, 4)
        do_nothing = m.group(5) == "DO NOTHING"
        columns = [c.strip() for c in cols_s.split(",")]
        placeholders = [p.strip() for p in phs_s.split(",")]
        if len(placeholders) != len(args):
            raise FakePgError(
                f"placeholder/param mismatch: {len(placeholders)} vs {len(args)} in {table}"
            )
        self._check_timestamps(columns, list(args))
        rows = self._db.tables.setdefault(table, {})
        key_col = conflict_col or self._db.conflict_cols.get(table)
        row = dict(zip(columns, args))
        if key_col is None:
            # append-only table (task_spans)
            seq = self._db.sequences.get(table, 0) + 1
            self._db.sequences[table] = seq
            row["id"] = seq
            rows[f"__seq__{seq}"] = row
        else:
            key = row.get(key_col)
            existing = rows.get(key)
            if existing is not None:
                if conflict_col is None:
                    raise FakePgError(f"duplicate key on {table}.{key_col}")
                if do_nothing:
                    row = existing  # ON CONFLICT DO NOTHING — keep as-is
                else:
                    # ON CONFLICT DO UPDATE — column-level merge
                    existing.update({c: v for c, v in row.items() if c != key_col})
                    row = existing
            rows[key] = row
        return "INSERT 0 1"

    def _update(self, s: str, args: tuple) -> str:
        # Incremental summary rollup: SET col = col + $N …, gmt_modified = $N
        if s.startswith("UPDATE task_details SET") and "total_token_input = total_token_input + $1" in s:
            token_in, token_out, duration, tool_calls, llm_calls, gmt_modified, task_id = args
            rows = self._db.tables.get("task_details", {})
            row = rows.get(task_id)
            n = 0
            if row is not None:
                row["total_token_input"] = row.get("total_token_input", 0) + token_in
                row["total_token_output"] = row.get("total_token_output", 0) + token_out
                row["total_duration_ms"] = row.get("total_duration_ms", 0) + duration
                row["total_tool_calls"] = row.get("total_tool_calls", 0) + tool_calls
                row["total_llm_calls"] = row.get("total_llm_calls", 0) + llm_calls
                row["gmt_modified"] = gmt_modified
                n = 1
            return f"UPDATE {n}"
        raise FakePgError(f"fake conn: unsupported UPDATE: {s[:80]}")

    def _delete(self, s: str, args: tuple) -> str:
        m = re.match(r"DELETE FROM (\w+) WHERE task_id = \$1$", s)
        if not m:
            raise FakePgError(f"fake conn: unsupported DELETE: {s[:80]}")
        table = m.group(1)
        rows = self._db.tables.get(table, {})
        if table == "task_spans":
            before = len(rows)
            kept = {k: r for k, r in rows.items() if r.get("task_id") != args[0]}
            self._db.tables[table] = kept
            return f"DELETE {before - len(kept)}"
        existed = args[0] in rows
        rows.pop(args[0], None)
        return f"DELETE {1 if existed else 0}"

    def _count(self, s: str, args: tuple) -> int:
        rows = list(self._db.tables.get("tasks", {}).values())
        if "WHERE task_state = $1" in s:
            rows = [r for r in rows if r.get("task_state") == args[0]]
        return len(rows)

    def _select_active(self, s: str, args: tuple) -> list[FakeRecord]:
        # Round-32: replicates the select_active_tasks criteria — the
        # materialised liability verdict column (tasks.liability_live,
        # written by TaskStore.upsert / update_task_state via
        # may_carry_live_fault), including the positional filter params
        # appended in tenant/namespace/target order. The word-vs-liability
        # semantics (never-issued ghosts, unverified fail-closed, K1/K2
        # blinded words) live one layer up in task_store.py — the backend
        # is a pure column-filter here, and so is this fake.
        out = []
        for row in self._db.tables.get("tasks", {}).values():
            if row.get("liability_live") != 1:
                continue
            out.append(row)
        # Positional filters in the exact order the backend appends them
        # (tenant_id → workspace_id → namespace → target_name; the workspace
        # axis slots in right after tenant per the 方案 A contract)
        idx = 0
        for field in ("tenant_id", "workspace_id", "namespace", "target_name"):
            if f"t.{field} = $" in s:
                out = [r for r in out if r.get(field) == args[idx]]
                idx += 1
        if idx != len(args):
            raise FakePgError("select_active_tasks: filter/param count mismatch")
        out.sort(key=lambda r: r.get("gmt_create") or datetime.min.replace(tzinfo=timezone.utc),
                 reverse=True)
        return [FakeRecord(r) for r in out]

    def _select_details_batch(self, s: str, args: tuple) -> list[FakeRecord]:
        placeholders = s[s.index("IN (") + 4 : s.index(")")]
        n = len([p for p in placeholders.split(",") if p.strip()])
        if n != len(args):
            raise FakePgError("details batch: placeholder/param mismatch")
        rows = self._db.tables.get("task_details", {})
        return [FakeRecord(rows[tid]) for tid in args if tid in rows]

    def _select_star(self, s: str, args: tuple) -> list[FakeRecord]:
        m = re.match(
            r"SELECT \* FROM (\w+)"
            r"( WHERE (\w+) = \$1)?"
            r"( ORDER BY (\w+)( DESC)?)?"
            r"( LIMIT \$\d+ OFFSET \$\d+)?$",
            s,
        )
        if not m:
            raise FakePgError(f"fake conn: unsupported SELECT: {s[:80]}")
        table = m.group(1)
        where_col = m.group(3)
        order_col = m.group(5)
        desc = m.group(6) is not None
        has_paging = m.group(7) is not None
        rows = list(self._db.tables.get(table, {}).values())
        if where_col:
            rows = [r for r in rows if r.get(where_col) == args[0]]
        if order_col:
            # int ids sort directly; datetime timestamps need a comparable
            # fallback for rows missing the column
            sample = next((r.get(order_col) for r in rows
                           if r.get(order_col) is not None), None)
            if isinstance(sample, datetime):
                key = lambda r: r.get(order_col) or datetime.min.replace(  # noqa: E731
                    tzinfo=timezone.utc)
            else:
                key = lambda r: r.get(order_col) or 0  # noqa: E731
            rows.sort(key=key, reverse=desc)
        if has_paging:
            limit, offset = args[-2], args[-1]
            rows = rows[offset : offset + limit]
        return [FakeRecord(r) for r in rows]


class _FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# Pool
# ---------------------------------------------------------------------------

class _AcquireCtx:
    def __init__(self, conn: FakeConn):
        self._conn = conn

    async def __aenter__(self) -> FakeConn:
        return self._conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self):
        self.db = _FakeDatabase()
        self.closed = False

    def acquire(self):
        if self.closed:
            raise FakePgError("pool is closed")
        return _AcquireCtx(FakeConn(self.db))

    async def close(self):
        self.closed = True
