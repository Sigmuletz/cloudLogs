"""Ingest CLI: `.log` files/globs/directories -> `data/logs.json` + `data/columns.json`.

    python -m cloudlogs.ingest example/logs.log
    python -m cloudlogs.ingest 'logs/**/*.log' -o data/all.json
    python -m cloudlogs.ingest logdir/            # recurses *.log

Reusable API (used by `cloudlogs.main` to ingest on startup):

    ingest(paths, out=Path("data/logs.json")) -> dict
        Parse every input into `out`, write column metadata next to it as
        `columns.json`, and return a summary dict:
        {"files": [str], "lines": int, "records": int, "json_ok": int,
         "log_ok": int, "pipe_ok": int, "failed": int, "columns": int,
         "out": str, "columns_path": str}

    is_stale(out, paths) -> bool
        True when `out` is missing or older than any of the resolved inputs,
        i.e. when ingest should run again. `paths` takes the same
        files/globs/directories the CLI accepts.

    format_summary(summary) -> str
        The per-layer report printed by the CLI (PLAN.md 2.1).
"""

from __future__ import annotations

import argparse
import glob as globlib
import json
import os
from pathlib import Path
from typing import Any, Iterable, Sequence

from .parse import FIELDS, parse_record

DEFAULT_OUT = Path("data/logs.json")
COLUMNS_NAME = "columns.json"

#: `facet` when a column has at most this many distinct values (PLAN.md 2.5)
FACET_MAX_DISTINCT = 200

#: a numeric column with at most this many distinct values is still a checkbox
#: facet (status codes, ports); above it, a min/max range makes more sense
FACET_MAX_NUMERIC_DISTINCT = 25

#: rendered as a from/to range
TIME_COLUMNS = frozenset({"time", "app_time"})

#: table columns shown by default (PLAN.md 4.3)
DEFAULT_VISIBLE = (
    "time",
    "level",
    "service",
    "k8s_namespace",
    "logger",
    "req_status_code",
    "message",
)

#: filter cards the panel opens with (PLAN.md 4.2)
DEFAULT_FILTERS = ("level", "logger", "service", "k8s_namespace", "req_status_code")

#: deterministic column order so the UI never shuffles between runs
COLUMN_ORDER = FIELDS

#: nicer labels than the generic prettifier produces
LABEL_OVERRIDES = {
    "time": "Time",
    "app_time": "App Time",
    "src_line": "Source Line",
    "op_x_request_id": "X-Request-ID",
    "x_trace_id": "X-Trace-ID",
    "req_x_header": "Req X-Forwarded-For",
    "req_duration_ms": "Req Duration (ms)",
    "node_name": "Node",
    "source_file": "Source File",
    "parse_ok": "Parse OK",
}

_ACRONYMS = {"k8s": "K8s", "id": "ID", "ms": "ms", "ok": "OK", "req": "Req", "op": "Op"}


# --------------------------------------------------------------------------
# inputs
# --------------------------------------------------------------------------


def expand_inputs(paths: Iterable[str | os.PathLike[str]]) -> list[Path]:
    """Resolve files, glob patterns and directories into a list of files.

    Globs are expanded here because the shell may pass the pattern through
    unexpanded; directories are recursed for `*.log`. Order is preserved and
    duplicates are removed.
    """
    resolved: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        key = path.resolve()
        if key not in seen:
            seen.add(key)
            resolved.append(path)

    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            for found in sorted(path.rglob("*.log")):
                if found.is_file():
                    add(found)
        elif path.is_file():
            add(path)
        else:
            matches = sorted(globlib.glob(str(raw), recursive=True))
            for match in matches:
                candidate = Path(match)
                if candidate.is_dir():
                    for found in sorted(candidate.rglob("*.log")):
                        if found.is_file():
                            add(found)
                elif candidate.is_file():
                    add(candidate)
    return resolved


def source_label(path: Path) -> str:
    """`source_file` value: relative to the cwd when possible."""
    try:
        return os.path.relpath(path, Path.cwd())
    except ValueError:  # different drive on Windows
        return str(path)


def is_stale(out: str | os.PathLike[str], paths: Iterable[str | os.PathLike[str]]) -> bool:
    """True when `out` is missing or older than any input (so ingest should run)."""
    out_path = Path(out)
    if not out_path.exists():
        return True
    columns_path = out_path.with_name(COLUMNS_NAME)
    if not columns_path.exists():
        return True
    inputs = expand_inputs(paths)
    if not inputs:
        return False
    out_mtime = min(out_path.stat().st_mtime, columns_path.stat().st_mtime)
    return any(source.stat().st_mtime > out_mtime for source in inputs)


# --------------------------------------------------------------------------
# column metadata (PLAN.md 2.5)
# --------------------------------------------------------------------------


def prettify(name: str) -> str:
    """`k8s_namespace` -> `K8s Namespace`."""
    if name in LABEL_OVERRIDES:
        return LABEL_OVERRIDES[name]
    words = []
    for word in name.split("_"):
        words.append(_ACRONYMS.get(word.lower(), word.capitalize()))
    return " ".join(words)


def classify(name: str, values: list[Any]) -> tuple[str, bool, int]:
    """Return `(kind, numeric, distinct)` for one column.

    `time` columns first, then numeric columns with many distinct values
    (min/max inputs), then anything with few enough distinct values (checkbox
    facet), else free text. A low-cardinality numeric column such as
    `req_status_code` stays a facet -- ticking 404 and 503 is what you want
    there, not a range.
    """
    distinct = len({v for v in values if v is not None})
    numeric = bool(values) and all(
        isinstance(v, (int, float)) and not isinstance(v, bool) for v in values
    )
    if name in TIME_COLUMNS:
        kind = "time"
    elif numeric and distinct > FACET_MAX_NUMERIC_DISTINCT:
        kind = "number"
    elif distinct <= FACET_MAX_DISTINCT:
        kind = "facet"
    else:
        kind = "text"
    return kind, numeric, distinct


def build_columns(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build `columns.json` metadata for every column except `_raw`."""
    present: list[str] = [name for name in COLUMN_ORDER]
    extra = sorted(
        {key for record in records for key in record}
        - set(present)
        - {"_raw"}
    )
    present.extend(extra)

    columns: list[dict[str, Any]] = []
    for name in present:
        values = [record[name] for record in records if record.get(name) is not None]
        kind, numeric, distinct = classify(name, values)
        columns.append(
            {
                "name": name,
                "kind": kind,
                "label": prettify(name),
                "distinct": distinct,
                "numeric": numeric,
                "default_visible": name in DEFAULT_VISIBLE,
                "default_filter": name in DEFAULT_FILTERS,
            }
        )
    return columns


# --------------------------------------------------------------------------
# ingest
# --------------------------------------------------------------------------


def ingest(
    paths: Iterable[str | os.PathLike[str]],
    out: str | os.PathLike[str] = DEFAULT_OUT,
) -> dict[str, Any]:
    """Parse every input file into `out` and write `columns.json` beside it.

    Returns the per-layer summary dict documented at the top of this module.
    """
    out_path = Path(out)
    columns_path = out_path.with_name(COLUMNS_NAME)
    files = expand_inputs(paths)

    records: list[dict[str, Any]] = []
    lines = json_ok = log_ok = pipe_ok = failed = 0

    for path in files:
        label = source_label(path)
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                if not raw.strip():
                    continue
                lines += 1
                result = parse_record(raw, label)
                records.append(result.record)
                json_ok += result.status.json_ok
                log_ok += result.status.log_ok
                pipe_ok += result.status.pipe_ok
                failed += not result.status.parse_ok

    columns = build_columns(records)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as handle:
        json.dump(records, handle, ensure_ascii=False)
    with columns_path.open("w", encoding="utf-8") as handle:
        json.dump(columns, handle, ensure_ascii=False, indent=2)

    return {
        "files": [source_label(p) for p in files],
        "lines": lines,
        "records": len(records),
        "json_ok": json_ok,
        "log_ok": log_ok,
        "pipe_ok": pipe_ok,
        "failed": failed,
        "columns": len(columns),
        "out": str(out_path),
        "columns_path": str(columns_path),
    }


def format_summary(summary: dict[str, Any]) -> str:
    """The per-layer report of PLAN.md 2.1."""
    width = max(len(str(summary[k])) for k in ("lines", "json_ok", "log_ok", "pipe_ok", "failed"))
    rows = [
        ("json ok", summary["json_ok"]),
        ("log-pattern ok", summary["log_ok"]),
        ("pipe-fields ok", summary["pipe_ok"]),
        ("parse_ok=false", summary["failed"]),
    ]
    lines = [f"{summary['lines']} lines → {summary['records']} records"]
    lines += [f"  {label:<16}{value:>{width}}" for label, value in rows]
    lines.append(
        f"  → {summary['out']} ({summary['columns']} columns → {summary['columns_path']})"
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point (`python -m cloudlogs.ingest` / `cloudlogs-ingest`)."""
    parser = argparse.ArgumentParser(
        prog="cloudlogs-ingest",
        description="Normalize Kubernetes/Quarkus JSON logs into logs.json + columns.json.",
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        metavar="INPUT",
        help="log files, glob patterns or directories (recursed for *.log)",
    )
    parser.add_argument(
        "-o",
        "--out",
        default=str(DEFAULT_OUT),
        help=f"output JSON file (default: {DEFAULT_OUT}); columns.json is written next to it",
    )
    args = parser.parse_args(argv)

    files = expand_inputs(args.inputs)
    if not files:
        parser.error(f"no log files matched: {' '.join(args.inputs)}")

    summary = ingest(files, args.out)
    print(format_summary(summary))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
