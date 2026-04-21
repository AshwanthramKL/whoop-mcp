"""
Export engine for the WHOOP MCP server — M4.

Writes flat, decoded records from the SQLite cache to disk in CSV,
JSONL, or Parquet format. Pure data layer: no API calls, no analytics.

Public entry point is :func:`export_whoop`. It never raises — errors are
returned as a structured envelope so the MCP tool layer can surface them
verbatim to the LLM.

Envelope codes emitted by this module:

- ``VALIDATION_ERROR`` — bad kind, format, or date string
- ``CACHE_EMPTY`` — the requested window has no rows in the cache (and
  the caller is expected to run ``sync_whoop()`` first)
- ``FILE_EXISTS`` — ``overwrite=False`` and the destination already has
  content
- ``EXPORT_ERROR`` — anything else that went wrong while writing

Security note: exports are flat copies of your fitness data. Write them
to a secure location; the writer does not apply special file modes.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

logger = logging.getLogger("whoop_export")

VALID_KINDS = ("cycles", "recoveries", "sleeps", "workouts", "all")
VALID_FORMATS = ("csv", "jsonl", "parquet")

# The resources written when kind="all". The record resources come first;
# snapshots are included so a single "all" export is a complete archive.
ALL_RESOURCES = (
    "cycles",
    "recoveries",
    "sleeps",
    "workouts",
    "body_measurements",
    "profile_snapshots",
)

# Extension per format
_EXT = {"csv": ".csv", "jsonl": ".jsonl", "parquet": ".parquet"}


def _error(code: str, message: str) -> Dict[str, Any]:
    return {"error": {"code": code, "message": message, "endpoint": "export_whoop"}}


def _parse_iso_date(s: str) -> datetime:
    """Parse YYYY-MM-DD or raise ValueError."""
    try:
        d = datetime.strptime(s, "%Y-%m-%d")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"date must be YYYY-MM-DD: {s!r}") from exc
    return d.replace(tzinfo=timezone.utc)


def _window_bounds(
    start: Optional[str], end: Optional[str]
) -> Tuple[Optional[str], Optional[str]]:
    """Translate YYYY-MM-DD start/end to ISO8601 Z bounds.

    ``start`` becomes ``YYYY-MM-DDT00:00:00.000Z`` (inclusive lower).
    ``end`` becomes ``YYYY-MM-DDT23:59:59.999Z`` (inclusive upper).
    """
    s: Optional[str] = None
    e: Optional[str] = None
    if start is not None:
        _parse_iso_date(start)
        s = f"{start}T00:00:00.000Z"
    if end is not None:
        _parse_iso_date(end)
        e = f"{end}T23:59:59.999Z"
    return s, e


# ---------- atomic existence check ----------


def _path_has_content(path: Path) -> bool:
    """True if the path exists and points to something that holds data."""
    if not path.exists():
        return False
    if path.is_dir():
        try:
            return any(path.iterdir())
        except OSError:
            return False
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


# ---------- CSV writer ----------


def _encode_cell(value: Any) -> Any:
    """Encode a flat_json value for a CSV cell.

    Nested dicts/lists are JSON-stringified; scalars pass through. None
    becomes an empty string so the column is empty, not literal "None".
    """
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, default=str)
    return value


def _write_csv(path: Path, records: List[Dict[str, Any]]) -> int:
    """Write a CSV file. Returns the number of records written.

    Header is the union of keys across all records, sorted alphabetically
    for determinism. Empty record list still writes a header row so the
    file is a valid RFC 4180 CSV — except when we also have no keys at
    all, in which case we write an empty file.
    """
    keys: List[str] = sorted({k for r in records for k in r.keys()})
    # Use newline="" per csv docs on Windows/RFC 4180 quoting
    with open(path, "w", encoding="utf-8", newline="") as f:
        if keys:
            writer = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
            writer.writeheader()
            for r in records:
                writer.writerow({k: _encode_cell(r.get(k)) for k in keys})
    return len(records)


# ---------- JSONL writer ----------


def _write_jsonl(path: Path, records: List[Dict[str, Any]]) -> int:
    """Write a JSONL file. Each line is a JSON object with sorted keys.

    Empty record list results in a zero-byte file (no lines, no header).
    """
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, sort_keys=True, default=str))
            f.write("\n")
    return len(records)


# ---------- Parquet writer ----------


def _write_parquet(path: Path, records: List[Dict[str, Any]]) -> int:
    """Write a Parquet file via pyarrow.

    Schema is inferred from the record list. Sparse keys become nullable
    columns automatically (pyarrow unions the key space across dicts).
    Empty record list writes an empty table.
    """
    import pyarrow as pa  # local import: keep module importable without pyarrow
    import pyarrow.parquet as pq

    if not records:
        # Empty table — write an empty parquet with zero columns to keep
        # the contract "a file is always created".
        table = pa.table({})
        pq.write_table(table, str(path), compression="snappy")
        return 0

    # Normalize: for each record, JSON-encode nested structures *only if*
    # pyarrow cannot handle them uniformly. pyarrow handles nested dicts
    # via struct types when schemas match, but flat_json is a union of
    # sparse keys — safer to let pyarrow infer and accept struct types.
    # Convert non-primitive values to JSON strings if inference fails.
    try:
        table = pa.Table.from_pylist(records)
    except (pa.ArrowInvalid, pa.ArrowTypeError, TypeError):
        normalized = [
            {k: (json.dumps(v, default=str) if isinstance(v, (dict, list)) else v)
             for k, v in r.items()}
            for r in records
        ]
        table = pa.Table.from_pylist(normalized)

    pq.write_table(table, str(path), compression="snappy")
    return len(records)


_WRITERS = {"csv": _write_csv, "jsonl": _write_jsonl, "parquet": _write_parquet}


# ---------- main entry point ----------


def _read_resource(
    store: Any, resource: str, start_iso: Optional[str], end_iso: Optional[str]
) -> List[Dict[str, Any]]:
    return list(store.iter_records(resource, start=start_iso, end=end_iso))


def _write_one(
    fmt: str,
    resource: str,
    path: Path,
    records: List[Dict[str, Any]],
    overwrite: bool,
) -> Dict[str, Any]:
    if not overwrite and _path_has_content(path):
        return _error(
            "FILE_EXISTS",
            f"{path} exists and overwrite=False",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = _WRITERS[fmt]
    n = writer(path, records)
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return {
        "resource": resource,
        "path": str(path),
        "records": n,
        "bytes": size,
    }


def export_whoop(
    *,
    store: Any,
    kind: str,
    format: str,
    path: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    overwrite: bool = False,
) -> Dict[str, Any]:
    """Export cached WHOOP records to disk.

    This is the impl called by the MCP tool wrapper. It never raises;
    any unexpected exception is mapped to an ``EXPORT_ERROR`` envelope.
    """
    # ----- validate kind/format -----
    if kind not in VALID_KINDS:
        return _error(
            "VALIDATION_ERROR",
            f"kind must be one of {VALID_KINDS}, got {kind!r}",
        )
    if format not in VALID_FORMATS:
        return _error(
            "VALIDATION_ERROR",
            f"format must be one of {VALID_FORMATS}, got {format!r}",
        )

    # ----- validate dates -----
    try:
        start_iso, end_iso = _window_bounds(start, end)
    except ValueError as exc:
        return _error("VALIDATION_ERROR", str(exc))

    range_echo = {"start": start, "end": end}

    try:
        # ----- resolve resources to export -----
        if kind == "all":
            resources = list(ALL_RESOURCES)
            out_root = Path(path)
            # out_root is a directory for kind=all
            if not overwrite and _path_has_content(out_root):
                return _error(
                    "FILE_EXISTS",
                    f"{out_root} exists and overwrite=False",
                )
            out_root.mkdir(parents=True, exist_ok=True)
        else:
            resources = [kind]
            out_root = None  # single file mode

        # ----- gather all records first so we can enforce CACHE_EMPTY
        # before writing anything -----
        per_resource: List[Tuple[str, List[Dict[str, Any]]]] = []
        total = 0
        for res in resources:
            recs = _read_resource(store, res, start_iso, end_iso)
            per_resource.append((res, recs))
            total += len(recs)

        if total == 0:
            return _error(
                "CACHE_EMPTY",
                "no cached records for the requested window; run sync_whoop() first",
            )

        # ----- write files -----
        files: List[Dict[str, Any]] = []
        if kind == "all":
            for res, recs in per_resource:
                fname = f"{res}{_EXT[format]}"
                fpath = out_root / fname  # type: ignore[operator]
                # For kind=all we already validated the directory; file
                # existence check is per-file with overwrite semantics.
                if not overwrite and _path_has_content(fpath):
                    return _error(
                        "FILE_EXISTS",
                        f"{fpath} exists and overwrite=False",
                    )
                info = _write_one(format, res, fpath, recs, overwrite=True)
                if "error" in info:
                    return info
                files.append(info)
        else:
            res, recs = per_resource[0]
            fpath = Path(path)
            info = _write_one(format, res, fpath, recs, overwrite=overwrite)
            if "error" in info:
                return info
            files.append(info)

        return {
            "status": "success",
            "kind": kind,
            "format": format,
            "files": files,
            "range": range_echo,
            "warnings": [],
        }

    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("export_whoop failed")
        return _error("EXPORT_ERROR", f"{type(exc).__name__}: {exc}")
