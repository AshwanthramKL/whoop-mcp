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

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

__all__ = ["WhoopStore", "SCHEMA_VERSION", "RECORD_TABLES", "SNAPSHOT_TABLES"]


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
        self._conn: Optional[sqlite3.Connection] = None

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

    def __enter__(self) -> "WhoopStore":
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
                    conn.execute(
                        f"CREATE INDEX IF NOT EXISTS idx_{table}_start ON {table}(start)"
                    )
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
        rows: Iterable[Tuple[Dict[str, Any], Dict[str, Any]]],
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
                        logger.warning("upsert_records skip: no updated_at for %s id=%s", table, pk_s)
                        continue

                    # Staleness guard: skip if existing row has >= updated_at.
                    existing = conn.execute(
                        f"SELECT updated_at FROM {table} WHERE id = ?", (pk_s,)
                    ).fetchone()
                    if existing is not None and existing["updated_at"] >= updated_at:
                        continue

                    cycle_id = raw.get("cycle_id")
                    cycle_id_s = str(cycle_id) if cycle_id is not None else None

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
                            raw.get("start"),
                            raw.get("end"),
                            updated_at,
                            raw.get("score_state"),
                            cycle_id_s,
                            json.dumps(raw, default=str),
                            json.dumps(flat, default=str),
                        ),
                    )
                    count += 1
        return count

    # ----- snapshot upsert (single "current" row) -----

    def upsert_snapshot(
        self, table: str, raw: Dict[str, Any], flat: Dict[str, Any]
    ) -> None:
        if table not in SNAPSHOT_TABLES:
            raise ValueError(f"upsert_snapshot: unknown table {table!r}")
        updated_at = raw.get("updated_at") or _utcnow_iso()
        with self._lock:
            conn = self._connect()
            with conn:
                conn.execute(
                    f"""
                    INSERT INTO {table} (id, updated_at, raw_json, flat_json)
                    VALUES ('current', ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        updated_at = excluded.updated_at,
                        raw_json = excluded.raw_json,
                        flat_json = excluded.flat_json
                    """,
                    (
                        updated_at,
                        json.dumps(raw, default=str),
                        json.dumps(flat, default=str),
                    ),
                )

    def get_latest_snapshot(self, table: str) -> Optional[Dict[str, Any]]:
        if table not in SNAPSHOT_TABLES:
            raise ValueError(f"get_latest_snapshot: unknown table {table!r}")
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                f"SELECT flat_json FROM {table} WHERE id = 'current'"
            ).fetchone()
            if row is None:
                return None
            return json.loads(row["flat_json"])

    # ----- queries -----

    def query_range(
        self,
        table: str,
        *,
        start: Optional[str],
        end: Optional[str],
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return flat_json rows whose ``start`` column falls in ``[start, end)``.

        ``start`` / ``end`` None means unbounded on that side. Results are
        ordered by ``start`` DESC (most recent first).
        """
        if table not in RECORD_TABLES:
            raise ValueError(f"query_range: unknown table {table!r}")

        clauses: List[str] = []
        params: List[Any] = []
        if start is not None:
            clauses.append("start >= ?")
            params.append(start)
        if end is not None:
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

    def query_by_cycle_id(self, table: str, *, cycle_id: int) -> List[Dict[str, Any]]:
        if table not in CYCLE_CHILD_TABLES:
            raise ValueError(f"query_by_cycle_id: unknown table {table!r}")
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                f"SELECT flat_json FROM {table} WHERE cycle_id = ? ORDER BY start DESC",
                (str(cycle_id),),
            ).fetchall()
            return [json.loads(r["flat_json"]) for r in rows]

    def get_by_id(self, table: str, *, id: str) -> Optional[Dict[str, Any]]:
        if table not in RECORD_TABLES:
            raise ValueError(f"get_by_id: unknown table {table!r}")
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                f"SELECT flat_json FROM {table} WHERE id = ?", (str(id),)
            ).fetchone()
            return json.loads(row["flat_json"]) if row else None

    def count(self, table: str) -> int:
        with self._lock:
            conn = self._connect()
            return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]

    def get_max_updated_at(self, table: str) -> Optional[str]:
        if table not in RECORD_TABLES:
            raise ValueError(f"get_max_updated_at: unknown table {table!r}")
        with self._lock:
            conn = self._connect()
            row = conn.execute(f"SELECT MAX(updated_at) AS m FROM {table}").fetchone()
            return row["m"] if row and row["m"] else None

    # ----- sync_runs audit -----

    def start_sync_run(
        self, resource: str, *, since_cursor: Optional[str] = None
    ) -> int:
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
                return cur.lastrowid

    def finish_sync_run(
        self,
        run_id: int,
        *,
        status: str,
        records_fetched: int = 0,
        records_upserted: int = 0,
        until_cursor: Optional[str] = None,
        error_message: Optional[str] = None,
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

    def list_sync_runs(self, *, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT * FROM sync_runs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
