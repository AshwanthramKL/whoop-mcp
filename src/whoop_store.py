"""
SQLite cache for the WHOOP MCP server — M3.

A thin, dependency-free wrapper around stdlib ``sqlite3`` that holds the
raw WHOOP response bodies alongside their flattened/LLM-friendly
equivalents. No business logic lives here — it only knows how to:

- init / migrate the schema idempotently
- upsert a batch of records with a staleness guard (newer ``updated_at``
  wins; equal or older is skipped)
- range-query a table by ``start`` timestamp
- filter ``sleeps`` / ``recoveries`` by ``cycle_id``
- upsert single-row snapshots (profile, body_measurement)
- record sync_runs audit rows and read them back

.. warning::

   The DB holds your raw fitness data. Treat it as sensitive as
   ``tokens.json``. The file is created with ``chmod 600`` on first
   open.

Schema (v1):
    cycles / recoveries / sleeps / workouts
        id TEXT PK | start TEXT | end TEXT | updated_at TEXT NOT NULL
        score_state TEXT | cycle_id TEXT NULL | raw_json TEXT | flat_json TEXT

    profile_snapshots / body_measurements
        id TEXT PK ('current') | updated_at TEXT | raw_json TEXT | flat_json TEXT

    sync_runs
        id INTEGER PK AUTO | started_at TEXT | completed_at TEXT NULL
        resource TEXT | records_fetched INTEGER | records_upserted INTEGER
        status TEXT | error_message TEXT NULL
        since_cursor TEXT NULL | until_cursor TEXT NULL
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["RECORD_TABLES", "SCHEMA_VERSION", "SNAPSHOT_TABLES", "WhoopStore"]


SCHEMA_VERSION = 1

# Tables holding time-indexed records (one row per WHOOP record).
RECORD_TABLES = ("cycles", "recoveries", "sleeps", "workouts")

# Tables holding a single "current" row (overwritten on each sync).
SNAPSHOT_TABLES = ("profile_snapshots", "body_measurements")

# Tables where each row has a cycle_id we want to filter by.
CYCLE_CHILD_TABLES = ("sleeps", "recoveries")

logger = logging.getLogger("whoop_store")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class WhoopStore:
    """Thin SQLite wrapper. Not an ORM — call sites pass pre-flattened dicts."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    # ----- lifecycle -----

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            Path(os.path.dirname(self.db_path) or ".").mkdir(parents=True, exist_ok=True)
            first_time = not os.path.exists(self.db_path)
            conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                isolation_level=None,  # autocommit; we manage transactions explicitly
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._conn = conn
            if first_time:
                try:
                    os.chmod(self.db_path, 0o600)
                except OSError:  # pragma: no cover - best effort
                    pass
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    def __enter__(self) -> WhoopStore:
        self.init_schema()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ----- schema -----

    def init_schema(self) -> None:
        """Create tables if they don't exist. Safe to call repeatedly."""
        with self._lock:
            conn = self._connect()
            # Ensure chmod 600 even on an existing file we didn't just create.
            try:
                os.chmod(self.db_path, 0o600)
            except OSError:  # pragma: no cover
                pass

            cur = conn.execute("PRAGMA user_version")
            current_version = cur.fetchone()[0]

            if current_version >= SCHEMA_VERSION:
                return

            with conn:  # implicit transaction
                for table in RECORD_TABLES:
                    conn.execute(
                        f"""
                        CREATE TABLE IF NOT EXISTS {table} (
                            id TEXT PRIMARY KEY,
                            start TEXT,
                            "end" TEXT,
                            updated_at TEXT NOT NULL,
                            score_state TEXT,
                            cycle_id TEXT,
                            raw_json TEXT NOT NULL,
                            flat_json TEXT NOT NULL
                        )
                        """
                    )
                    conn.execute(
                        f"CREATE INDEX IF NOT EXISTS idx_{table}_updated_at ON {table}(updated_at)"
                    )
                    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_start ON {table}(start)")
                for table in CYCLE_CHILD_TABLES:
                    conn.execute(
                        f"CREATE INDEX IF NOT EXISTS idx_{table}_cycle_id ON {table}(cycle_id)"
                    )

                for table in SNAPSHOT_TABLES:
                    conn.execute(
                        f"""
                        CREATE TABLE IF NOT EXISTS {table} (
                            id TEXT PRIMARY KEY,
                            updated_at TEXT NOT NULL,
                            raw_json TEXT NOT NULL,
                            flat_json TEXT NOT NULL
                        )
                        """
                    )

                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sync_runs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        started_at TEXT NOT NULL,
                        completed_at TEXT,
                        resource TEXT NOT NULL,
                        records_fetched INTEGER NOT NULL DEFAULT 0,
                        records_upserted INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL,
                        error_message TEXT,
                        since_cursor TEXT,
                        until_cursor TEXT
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_sync_runs_started_at ON sync_runs(started_at)"
                )
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # ----- record upsert (staleness-aware) -----

    def upsert_records(
        self,
        table: str,
        rows: Iterable[tuple[dict[str, Any], dict[str, Any]]],
    ) -> int:
        """Insert or replace records, honoring the ``updated_at`` staleness guard.

        Each item is a ``(raw_dict, flat_dict)`` tuple. ``raw_dict`` must
        include ``id`` (or ``cycle_id`` for recoveries, which are keyed on
        the parent cycle) and ``updated_at``.

        Returns the number of rows that actually changed (new insertions
        + replacements). Rows skipped because their ``updated_at`` was
        older than the stored value do not count.
        """
        if table not in RECORD_TABLES:
            raise ValueError(f"upsert_records: unknown table {table!r}")

        count = 0
        with self._lock:
            conn = self._connect()
            with conn:
                for raw, flat in rows:
                    pk = raw.get("id")
                    if pk is None and table == "recoveries":
                        pk = raw.get("cycle_id")
                    if pk is None:
                        logger.warning("upsert_records skip: no id in row for %s", table)
                        continue

                    pk_s = str(pk)
                    updated_at = raw.get("updated_at")
                    if not updated_at:
                        logger.warning(
                            "upsert_records skip: no updated_at for %s id=%s", table, pk_s
                        )
                        continue

                    # Staleness guard: skip if existing row has >= updated_at.
                    existing = conn.execute(
                        f"SELECT updated_at FROM {table} WHERE id = ?", (pk_s,)
                    ).fetchone()
                    if existing is not None and existing["updated_at"] >= updated_at:
                        continue

                    cycle_id = raw.get("cycle_id")
                    cycle_id_s = str(cycle_id) if cycle_id is not None else None

                    # Recoveries have no own start/end in v2 — inherit from parent cycle
                    # so date-window queries work. If the cycle isn't in the DB yet, the
                    # backfill helper below will fix it on a subsequent sync.
                    start_val = raw.get("start")
                    end_val = raw.get("end")
                    if table == "recoveries" and start_val is None and cycle_id_s is not None:
                        cyc = conn.execute(
                            'SELECT start, "end" FROM cycles WHERE id = ?', (cycle_id_s,)
                        ).fetchone()
                        if cyc is not None:
                            start_val = cyc["start"]
                            end_val = cyc["end"]

                    conn.execute(
                        f"""
                        INSERT INTO {table}
                            (id, start, "end", updated_at, score_state, cycle_id, raw_json, flat_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET
                            start = excluded.start,
                            "end" = excluded."end",
                            updated_at = excluded.updated_at,
                            score_state = excluded.score_state,
                            cycle_id = excluded.cycle_id,
                            raw_json = excluded.raw_json,
                            flat_json = excluded.flat_json
                        """,
                        (
                            pk_s,
                            start_val,
                            end_val,
                            updated_at,
                            raw.get("score_state"),
                            cycle_id_s,
                            json.dumps(raw, default=str),
                            json.dumps(flat, default=str),
                        ),
                    )
                    count += 1
        return count

    def backfill_recovery_windows(self) -> int:
        """Populate NULL start/end on recoveries by copying from the parent cycle.

        Idempotent. Call after cycles are synced. Returns rows updated.
        """
        with self._lock:
            conn = self._connect()
            with conn:
                cur = conn.execute(
                    """
                    UPDATE recoveries
                       SET start = (SELECT c.start FROM cycles c WHERE c.id = recoveries.cycle_id),
                           "end" = (SELECT c."end" FROM cycles c WHERE c.id = recoveries.cycle_id)
                     WHERE (start IS NULL OR "end" IS NULL)
                       AND cycle_id IS NOT NULL
                       AND EXISTS (SELECT 1 FROM cycles c WHERE c.id = recoveries.cycle_id)
                    """
                )
                return cur.rowcount

    # ----- snapshot upsert (single "current" row) -----

    def upsert_snapshot(self, table: str, raw: dict[str, Any], flat: dict[str, Any]) -> int:
        """Upsert the single ``current`` row for a snapshot table.

        Uses a canonical SHA-256 hash of the raw payload to decide whether
        anything actually changed:

        - If the new payload hashes to the same value as the stored one,
          this is a no-op: we do NOT update ``updated_at`` and we return 0.
          This is what keeps ``sync_whoop()`` idempotent even across
          repeated runs on unchanged upstream data, and it's what keeps
          the event feed quiet when nothing meaningful moved.

        - If the hashes differ, we overwrite and advance ``updated_at``
          (to the payload's own ``updated_at`` when present, else
          ``now``). Returns 1.
        """
        if table not in SNAPSHOT_TABLES:
            raise ValueError(f"upsert_snapshot: unknown table {table!r}")

        raw_blob = json.dumps(raw, default=str, sort_keys=True)
        flat_blob = json.dumps(flat, default=str, sort_keys=True)
        new_hash = hashlib.sha256(raw_blob.encode("utf-8")).hexdigest()

        with self._lock:
            conn = self._connect()
            with conn:
                existing = conn.execute(
                    f"SELECT raw_json FROM {table} WHERE id = 'current'"
                ).fetchone()
                if existing is not None:
                    existing_hash = hashlib.sha256(
                        (existing["raw_json"] or "").encode("utf-8")
                    ).hexdigest()
                    if existing_hash == new_hash:
                        return 0

                updated_at = raw.get("updated_at") or _utcnow_iso()
                conn.execute(
                    f"""
                    INSERT INTO {table} (id, updated_at, raw_json, flat_json)
                    VALUES ('current', ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        updated_at = excluded.updated_at,
                        raw_json = excluded.raw_json,
                        flat_json = excluded.flat_json
                    """,
                    (updated_at, raw_blob, flat_blob),
                )
                return 1

    def get_latest_snapshot(self, table: str) -> dict[str, Any] | None:
        if table not in SNAPSHOT_TABLES:
            raise ValueError(f"get_latest_snapshot: unknown table {table!r}")
        with self._lock:
            conn = self._connect()
            row = conn.execute(f"SELECT flat_json FROM {table} WHERE id = 'current'").fetchone()
            if row is None:
                return None
            return json.loads(row["flat_json"])

    # ----- queries -----

    def query_range(
        self,
        table: str,
        *,
        start: str | None,
        end: str | None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return flat_json rows overlapping the window ``[start, end)``.

        Overlap semantics match the WHOOP API: a record is included if its
        own time range intersects the window, not just if it starts inside.
        Cycles, sleeps, and workouts frequently span across date boundaries.
        ``None`` on either side means unbounded. Results are ordered by
        ``start`` DESC (most recent first).
        """
        if table not in RECORD_TABLES:
            raise ValueError(f"query_range: unknown table {table!r}")

        clauses: list[str] = []
        params: list[Any] = []
        if start is not None:
            # Record ended at/after window_start, or is still in progress.
            clauses.append("(end > ? OR end IS NULL)")
            params.append(start)
        if end is not None:
            # Record started before window_end.
            clauses.append("start < ?")
            params.append(end)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT flat_json FROM {table}{where} ORDER BY start DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)

        with self._lock:
            conn = self._connect()
            rows = conn.execute(sql, params).fetchall()
            return [json.loads(r["flat_json"]) for r in rows]

    def iter_records(
        self,
        resource: str,
        start: str | None,
        end: str | None,
    ) -> Iterator[dict[str, Any]]:
        """Yield decoded flat_json dicts for a resource overlapping ``[start, end]``.

        Overlap semantics match ``query_range`` / the WHOOP API: a record is
        yielded if its own time range intersects the window, so cycles and
        sleeps that span across date boundaries are included regardless of
        which boundary their ``start`` falls on. ``None`` on either side
        means unbounded. Results are ordered by ``start`` ASC for
        deterministic exports.

        For snapshot tables (``profile_snapshots``, ``body_measurements``)
        the window is ignored and the single ``current`` row is yielded if
        present.

        Uses parameterized SQL exclusively.
        """
        if resource in SNAPSHOT_TABLES:
            snap = self.get_latest_snapshot(resource)
            if snap is not None:
                yield snap
            return

        if resource not in RECORD_TABLES:
            raise ValueError(f"iter_records: unknown resource {resource!r}")

        clauses: list[str] = []
        params: list[Any] = []
        if start is not None:
            clauses.append("(end >= ? OR end IS NULL)")
            params.append(start)
        if end is not None:
            clauses.append("start <= ?")
            params.append(end)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT flat_json FROM {resource}{where} ORDER BY start ASC"

        with self._lock:
            conn = self._connect()
            rows = conn.execute(sql, params).fetchall()
        for r in rows:
            yield json.loads(r["flat_json"])

    def iter_events(
        self,
        resources: Iterable[str],
        since: str,
        until: str,
        limit: int,
        *,
        since_cursor: tuple[str, str, str] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield ``{resource, id, updated_at, record}`` dicts across resources.

        Half-open window: ``updated_at > since AND updated_at < until`` —
        the "since" bound is strict so callers can pass the previous
        ``next_cursor`` and not re-see events at that timestamp.

        Results are sorted by ``updated_at`` ASC, then by ``resource`` ASC
        as tiebreaker. Iteration stops after yielding ``limit + 1`` rows so
        the caller can detect truncation cheaply (expects ``limit + 1``
        when more events exist beyond the cap).

        ``resources`` may mix record tables (``cycles``/``recoveries``/
        ``sleeps``/``workouts``) with snapshot aliases (``body_measurement``
        -> ``body_measurements``, ``profile`` -> ``profile_snapshots``).
        Unknown names raise ``ValueError``.

        All SQL is parameterized and table names are drawn from a fixed
        allowlist.
        """
        # Map public resource names -> physical table names.
        alias = {
            "cycles": "cycles",
            "recoveries": "recoveries",
            "sleeps": "sleeps",
            "workouts": "workouts",
            "body_measurement": "body_measurements",
            "body_measurements": "body_measurements",
            "profile": "profile_snapshots",
            "profile_snapshots": "profile_snapshots",
        }
        # Record-table resource names we emit on events.
        public_of = {
            "cycles": "cycles",
            "recoveries": "recoveries",
            "sleeps": "sleeps",
            "workouts": "workouts",
            "body_measurements": "body_measurement",
            "profile_snapshots": "profile",
        }

        tables: list[tuple[str, str]] = []  # (table, public_resource)
        for r in resources:
            if r not in alias:
                raise ValueError(f"iter_events: unknown resource {r!r}")
            t = alias[r]
            pub = public_of[t]
            if (t, pub) not in tables:
                tables.append((t, pub))

        if not tables:
            return

        # Build a UNION ALL query so SQLite does the sort + limit for us.
        # When a composite cursor is present, the lower bound on each
        # branch becomes:
        #     (updated_at > cursor.ts)
        #   OR (updated_at = cursor.ts AND resource > cursor.resource)
        #   OR (updated_at = cursor.ts AND resource = cursor.resource
        #       AND id > cursor.id)
        # When absent, fall back to the plain strict lower bound
        # ``updated_at > since`` so ISO-string callers stay back-compat.
        parts: list[str] = []
        params: list[Any] = []
        for table, pub in tables:
            if since_cursor is not None:
                cts, cres, cid = since_cursor
                parts.append(
                    f"SELECT ? AS resource, id AS id, updated_at AS updated_at, "
                    f"flat_json AS flat_json FROM {table} "
                    f"WHERE updated_at < ? AND ("
                    f"updated_at > ? "
                    f"OR (updated_at = ? AND ? > ?) "
                    f"OR (updated_at = ? AND ? = ? AND id > ?))"
                )
                # Param order matches the placeholders above:
                # resource, until, cursor.ts, cursor.ts, pub, cursor.res,
                # cursor.ts, pub, cursor.res, cursor.id
                params.extend([pub, until, cts, cts, pub, cres, cts, pub, cres, cid])
            else:
                parts.append(
                    f"SELECT ? AS resource, id AS id, updated_at AS updated_at, "
                    f"flat_json AS flat_json FROM {table} "
                    f"WHERE updated_at > ? AND updated_at < ?"
                )
                params.extend([pub, since, until])
        sql = " UNION ALL ".join(parts) + " ORDER BY updated_at ASC, resource ASC, id ASC LIMIT ?"
        params.append(int(limit) + 1)

        with self._lock:
            conn = self._connect()
            rows = conn.execute(sql, params).fetchall()

        for row in rows:
            try:
                record = json.loads(row["flat_json"])
            except (TypeError, ValueError):
                record = {}
            yield {
                "resource": row["resource"],
                "id": row["id"],
                "updated_at": row["updated_at"],
                "record": record,
            }

    def query_by_cycle_id(self, table: str, *, cycle_id: int) -> list[dict[str, Any]]:
        if table not in CYCLE_CHILD_TABLES:
            raise ValueError(f"query_by_cycle_id: unknown table {table!r}")
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                f"SELECT flat_json FROM {table} WHERE cycle_id = ? ORDER BY start DESC",
                (str(cycle_id),),
            ).fetchall()
            return [json.loads(r["flat_json"]) for r in rows]

    def get_by_id(self, table: str, *, id: str) -> dict[str, Any] | None:
        if table not in RECORD_TABLES:
            raise ValueError(f"get_by_id: unknown table {table!r}")
        with self._lock:
            conn = self._connect()
            row = conn.execute(f"SELECT flat_json FROM {table} WHERE id = ?", (str(id),)).fetchone()
            return json.loads(row["flat_json"]) if row else None

    def count(self, table: str) -> int:
        with self._lock:
            conn = self._connect()
            return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]

    def get_max_updated_at(self, table: str) -> str | None:
        if table not in RECORD_TABLES:
            raise ValueError(f"get_max_updated_at: unknown table {table!r}")
        with self._lock:
            conn = self._connect()
            row = conn.execute(f"SELECT MAX(updated_at) AS m FROM {table}").fetchone()
            return row["m"] if row and row["m"] else None

    # ----- sync_runs audit -----

    def start_sync_run(self, resource: str, *, since_cursor: str | None = None) -> int:
        with self._lock:
            conn = self._connect()
            with conn:
                cur = conn.execute(
                    """
                    INSERT INTO sync_runs
                        (started_at, resource, status, since_cursor,
                         records_fetched, records_upserted)
                    VALUES (?, ?, 'running', ?, 0, 0)
                    """,
                    (_utcnow_iso(), resource, since_cursor),
                )
                assert cur.lastrowid is not None  # INSERT always yields a rowid
                return cur.lastrowid

    def finish_sync_run(
        self,
        run_id: int,
        *,
        status: str,
        records_fetched: int = 0,
        records_upserted: int = 0,
        until_cursor: str | None = None,
        error_message: str | None = None,
    ) -> None:
        with self._lock:
            conn = self._connect()
            with conn:
                conn.execute(
                    """
                    UPDATE sync_runs SET
                        completed_at = ?,
                        status = ?,
                        records_fetched = ?,
                        records_upserted = ?,
                        until_cursor = ?,
                        error_message = ?
                    WHERE id = ?
                    """,
                    (
                        _utcnow_iso(),
                        status,
                        records_fetched,
                        records_upserted,
                        until_cursor,
                        error_message,
                        run_id,
                    ),
                )

    def list_sync_runs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT * FROM sync_runs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
