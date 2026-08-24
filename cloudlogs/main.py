"""FastAPI app for the cloudlogs viewer (PLAN.md section 3).

Records live in memory (``RECORDS: list[dict]``), loaded from ``data/logs.json``
at startup. All filtering, sorting and faceting happen server-side in
:mod:`cloudlogs.query`.

Routes
------
``POST /api/logs``      filtered / sorted / paginated rows + cross-filtered facets
``GET  /api/columns``   column metadata from ``data/columns.json``
``GET  /api/row/{idx}`` one record **including** ``_raw`` (detail drawer)
``POST /api/reload``    re-run ingest and reload ``RECORDS`` without a restart
``GET  /``              ``static/index.html`` (``/static/*`` is mounted too)

Environment
-----------
``CLOUDLOGS_INPUT``
    Ingest inputs: files, globs or directories, separated by ``,`` (or by
    ``os.pathsep``). Default ``example/logs.log``.
``CLOUDLOGS_DATA``
    Path of the generated records file. Default ``data/logs.json``; the column
    metadata is read from ``columns.json`` next to it.

Startup runs ``ingest()`` when ``data/logs.json`` is missing or ``is_stale()``
against those inputs, prints what it did, then loads the records. A failure
anywhere in there is printed and the app still starts (with zero records), so a
half-built workspace never blocks the UI.

Row identity
------------
Every record gets a stable ``_idx`` (its position in ``RECORDS``) at load time.
``/api/logs`` rows carry ``_idx`` and have ``_raw`` stripped; the drawer fetches
``GET /api/row/{_idx}`` for the full record.

Facet columns in the response
-----------------------------
``facets`` covers the union of:

* every facet-kind column that appears in the request's ``filters``, and
* the request's ``facet_cols`` when given, otherwise **all** facet-kind columns
  whose distinct count is <= 200 (the ``facet`` cap from PLAN.md section 2.5 —
  in practice every facet column, which is affordable because
  :func:`cloudlogs.query.facets` counts all columns in a single pass).

A column the client explicitly asks for is always counted, whatever its kind;
per column at most 1000 values are returned (highest counts first), so a
pathological cardinality cannot blow up the payload.
"""

from __future__ import annotations

import argparse
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Sequence

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from cloudlogs import lucene, query

try:  # ingest.py is written by another stage; never fatal at import time
    from cloudlogs.ingest import format_summary, ingest, is_stale  # type: ignore
    from cloudlogs.rules import RulesError, load_rules  # type: ignore
except Exception:  # pragma: no cover - exercised only before ingest.py exists
    ingest = None  # type: ignore[assignment]
    format_summary = None  # type: ignore[assignment]
    is_stale = None  # type: ignore[assignment]

    load_rules = None  # type: ignore[assignment]

    class RulesError(Exception):  # type: ignore[no-redef]
        """Placeholder so the startup guard below still names something."""


BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"

DEFAULT_INPUT = "example/logs.log"
DEFAULT_DATA = "data/logs.json"

FACET_DISTINCT_CAP = 200  # PLAN.md 2.6: a column is facet-kind at <= 200 distinct
MAX_FACET_VALUES = 1000  # hard cap on values returned per facet column

# --------------------------------------------------------------------------- #
# in-memory state
# --------------------------------------------------------------------------- #

RECORDS: list[dict] = []
COLUMNS: list[dict] = []
TEXT_COLS: list[str] = []
NUMERIC_COLS: list[str] = []
FACET_COLS: list[str] = []
SEARCH_COLS: list[str] = []


def input_paths() -> list[str]:
    """Ingest inputs from ``CLOUDLOGS_INPUT`` (``,`` or os.pathsep separated).

    Relative entries are resolved against the project root, not the working
    directory, so the server behaves the same however it was launched -- from
    a different directory, from a Windows service, or from an IDE.
    """
    raw = os.environ.get("CLOUDLOGS_INPUT", DEFAULT_INPUT)
    parts = [p.strip() for p in raw.replace(os.pathsep, ",").split(",")]
    return [str(p if Path(p).is_absolute() else BASE_DIR / p) for p in parts if p]


def data_path() -> Path:
    path = Path(os.environ.get("CLOUDLOGS_DATA", DEFAULT_DATA))
    return path if path.is_absolute() else BASE_DIR / path


def _infer_columns(records: list[dict]) -> list[dict]:
    """Fallback column metadata when ``columns.json`` is absent (PLAN.md 2.6)."""
    names: list[str] = []
    seen: set[str] = set()
    for rec in records:
        for key in rec:
            if key.startswith("_") or key in seen:
                continue
            seen.add(key)
            names.append(key)
    distinct = query.distinct_counts(records, names)
    columns: list[dict] = []
    for name in names:
        numeric = any(
            isinstance(rec.get(name), (int, float)) and not isinstance(rec.get(name), bool)
            for rec in records
            if rec.get(name) is not None
        )
        if name in ("time", "app_time"):
            kind = "time"
        elif numeric:
            kind = "number"
        elif distinct.get(name, 0) <= FACET_DISTINCT_CAP:
            kind = "facet"
        else:
            kind = "text"
        columns.append(
            {
                "name": name,
                "kind": kind,
                "label": name.replace("_", " "),
                "distinct": distinct.get(name, 0),
                "numeric": numeric,
                "default_visible": name
                in ("time", "level", "service", "k8s_namespace", "logger", "req_status_code", "message"),
                "default_filter": name
                in ("level", "logger", "service", "k8s_namespace", "req_status_code"),
            }
        )
    return columns


def _index_columns() -> None:
    global TEXT_COLS, NUMERIC_COLS, FACET_COLS, SEARCH_COLS
    TEXT_COLS = [c["name"] for c in COLUMNS if c.get("kind") == "text"]
    # The panel's search box is advertised as "search all logs", so it spans
    # every column, not only the text-kind ones -- most columns here are
    # low-cardinality facets, and searching a pod name or a status code is
    # exactly what someone types into it. Values are stringified to match.
    SEARCH_COLS = [c["name"] for c in COLUMNS if not c["name"].startswith("_")]
    NUMERIC_COLS = [
        c["name"] for c in COLUMNS if c.get("kind") == "number" or c.get("numeric")
    ]
    FACET_COLS = [
        c["name"]
        for c in COLUMNS
        if c.get("kind") == "facet"
        and (c.get("distinct") is None or c["distinct"] <= FACET_DISTINCT_CAP)
    ]


def load_state(force_ingest: bool = False) -> dict[str, Any]:
    """Ingest if needed, then load records + column metadata into memory."""
    global RECORDS, COLUMNS

    # Validate the ruleset first, even when nothing needs re-ingesting: a
    # broken rules.yaml must never be served past (PLAN.md 2.8), and skipping
    # this when logs.json happens to be fresh would do exactly that.
    if load_rules is not None:
        load_rules()

    out = data_path()
    paths = input_paths()
    summary: dict[str, Any] = {"data": str(out), "inputs": paths, "ingested": False}

    need = force_ingest or not out.exists()
    if not need and is_stale is not None:
        try:
            need = bool(is_stale(out, paths))
        except Exception as exc:  # pragma: no cover - defensive
            print(f"cloudlogs: is_stale() failed ({exc}); using existing {out}")
            need = False

    if need:
        if ingest is None:
            print("cloudlogs: cloudlogs.ingest is not importable yet — skipping ingest")
            summary["ingest_error"] = "cloudlogs.ingest unavailable"
        else:
            print(f"cloudlogs: ingesting {paths} -> {out}")
            result = ingest(paths, out=out)
            summary["ingested"] = True
            summary["ingest"] = result
            for line in format_summary(result).splitlines():
                print(f"cloudlogs: {line}")

    records: list[dict] = []
    if out.exists():
        with out.open(encoding="utf-8") as fh:
            payload = json.load(fh)
        if isinstance(payload, dict):
            payload = payload.get("records", [])
        records = [r for r in payload if isinstance(r, dict)]
    else:
        print(f"cloudlogs: {out} missing — serving 0 records")

    for i, rec in enumerate(records):
        rec["_idx"] = i
    RECORDS = records

    columns: list[dict] = []
    cols_file = out.with_name("columns.json")
    if cols_file.exists():
        try:
            with cols_file.open(encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                loaded = loaded.get("columns", [])
            columns = [c for c in loaded if isinstance(c, dict) and c.get("name")]
        except Exception as exc:
            print(f"cloudlogs: could not read {cols_file} ({exc}); inferring columns")
    if not columns and records:
        columns = _infer_columns(records)
    COLUMNS = columns
    _index_columns()

    summary["records"] = len(RECORDS)
    summary["columns"] = len(COLUMNS)
    print(f"cloudlogs: loaded {len(RECORDS)} records, {len(COLUMNS)} columns from {out}")
    return summary


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        load_state()
    except RulesError as exc:
        # A broken rules.yaml is the one failure the server must not survive
        # (PLAN.md 2.8): serving the previous logs.json would quietly show
        # stale data that does not match the rules on disk.
        print(f"cloudlogs: {exc}")
        raise
    except Exception as exc:  # never let a bad workspace stop the server
        print(f"cloudlogs: startup load failed: {exc!r}")
    yield


app = FastAPI(title="cloudlogs", version="0.1.0", lifespan=lifespan)
# check_dir=False: index.html/app.js may not exist yet while the UI is built.
app.mount("/static", StaticFiles(directory=str(STATIC_DIR), check_dir=False), name="static")


# --------------------------------------------------------------------------- #
# models
# --------------------------------------------------------------------------- #


class FilterSpec(BaseModel):
    """One column filter. Only the fields of its ``kind`` are used."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    kind: Literal["facet", "number", "time", "text"]
    values: list[Any] | None = None  # facet
    min: float | None = None  # number
    max: float | None = None  # number
    from_: str | None = Field(default=None, alias="from")  # time
    to: str | None = None  # time
    value: str | None = None  # text
    regex: bool = False  # text


class SortKey(BaseModel):
    col: str
    dir: Literal["asc", "desc"] = "asc"


class LogsRequest(BaseModel):
    """``query`` is a Lucene-style expression (see ``cloudlogs.lucene``); it is
    ANDed with ``filters`` and ``q``."""

    model_config = ConfigDict(extra="ignore")

    filters: dict[str, FilterSpec] = Field(default_factory=dict)
    q: str | None = None
    sort: list[SortKey] = Field(default_factory=list)
    limit: int = Field(default=200, ge=0, le=10000)
    offset: int = Field(default=0, ge=0)
    facet_cols: list[str] | None = None
    query: str | None = None      # Lucene-style expression, ANDed with the rest


class FacetValue(BaseModel):
    value: Any = None
    count: int


class LogsResponse(BaseModel):
    rows: list[dict]
    total: int
    offset: int
    limit: int
    facets: dict[str, list[FacetValue]]


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #


def _strip_raw(rec: dict) -> dict:
    if "_raw" not in rec:
        return dict(rec)
    return {k: v for k, v in rec.items() if k != "_raw"}


def _facet_columns(req: LogsRequest, filters: dict[str, dict]) -> list[str]:
    facet_set = set(FACET_COLS)
    cols: list[str] = []
    for name in req.facet_cols if req.facet_cols is not None else FACET_COLS:
        if name not in cols:
            cols.append(name)
    for name, spec in filters.items():
        if name not in cols and (spec.get("kind") == "facet" or name in facet_set):
            cols.append(name)
    return cols


@app.post("/api/logs", response_model=LogsResponse)
def api_logs(req: LogsRequest) -> LogsResponse:
    filters = {name: spec.model_dump(by_alias=True) for name, spec in req.filters.items()}
    q_cols = SEARCH_COLS or None

    # A bad query is the user mistyping, not a server fault: 400 with the
    # message and the offset so the UI can point at the character.
    try:
        expr = lucene.compile_query(req.query or "", COLUMNS, search_cols=q_cols)
    except lucene.LuceneError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": exc.message, "pos": exc.pos, "kind": "query"},
        ) from exc

    try:
        matched = query.apply_filters(RECORDS, filters, req.q, q_cols=q_cols, expr=expr)
        facet_counts = query.facets(
            RECORDS,
            filters,
            req.q,
            _facet_columns(req, filters),
            q_cols=q_cols,
            max_values=MAX_FACET_VALUES,
            expr=expr,
        )
    except query.InvalidFilterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    ordered = query.sort_records(
        matched,
        [s.model_dump() for s in req.sort],
        numeric_cols=NUMERIC_COLS,
    )
    page = query.paginate(ordered, req.limit, req.offset)
    return LogsResponse(
        rows=[_strip_raw(r) for r in page],
        total=len(matched),
        offset=req.offset,
        limit=req.limit,
        facets={c: [FacetValue(**v) for v in vals] for c, vals in facet_counts.items()},
    )


@app.get("/api/columns")
def api_columns() -> list[dict]:
    return COLUMNS


@app.get("/api/row/{idx}")
def api_row(idx: int) -> dict:
    if idx < 0 or idx >= len(RECORDS):
        raise HTTPException(status_code=404, detail=f"no record at index {idx}")
    return RECORDS[idx]


@app.post("/api/reload")
def api_reload() -> dict:
    """Re-run ingest (forced) and reload ``RECORDS`` without a restart."""
    if ingest is None:
        raise HTTPException(status_code=503, detail="cloudlogs.ingest is not available")
    try:
        return load_state(force_ingest=True)
    except RulesError as exc:
        # the user's typo in rules.yaml, not a server fault -- same shape as a
        # bad query (PLAN.md 3.1): 400 with the message the CLI would print
        raise HTTPException(
            status_code=400, detail={"error": str(exc), "kind": "rules"}
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"reload failed: {exc}") from exc


@app.get("/", response_class=HTMLResponse)
def index() -> Any:
    page = STATIC_DIR / "index.html"
    if page.exists():
        return FileResponse(page)
    return HTMLResponse(
        "<h1>cloudlogs</h1><p>static/index.html is not there yet. "
        'The API is up: <code>POST /api/logs</code>, <code>GET /api/columns</code>.</p>',
        status_code=200,
    )


# --------------------------------------------------------------------------
# CLI -- `python -m cloudlogs.main app.log`, the same job as run.sh
# --------------------------------------------------------------------------


def _resolve_inputs(values: Sequence[str]) -> str:
    """Absolutise each input against the CALLER's directory.

    `input_paths()` resolves relative entries against the project root so the
    server behaves the same however it was launched -- but a path typed on the
    command line means "relative to where I am standing", so it is resolved
    here instead. A glob is kept as a pattern; ingest expands it.
    """
    here = Path.cwd()
    parts = [str(v if Path(v).is_absolute() else here / v) for v in values]
    return os.pathsep.join(parts)


def main(argv: Sequence[str] | None = None) -> int:
    """Start the viewer, optionally against the log files named on the CLI."""
    import uvicorn

    parser = argparse.ArgumentParser(
        prog="cloudlogs",
        description="Serve the cloudlogs viewer, ingesting first when needed.",
        epilog=(
            "examples:\n"
            "  python -m cloudlogs.main                     serve the default input\n"
            "  python -m cloudlogs.main app.log             ingest this file instead\n"
            "  python -m cloudlogs.main a.log b.log logs/   several files, or a directory\n"
            "  python -m cloudlogs.main 'logs/**/*.log'     a glob -- quote it\n"
            "  python -m cloudlogs.main app.log -p 9000 --host 127.0.0.1\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        metavar="LOG",
        help="log files, globs or directories to ingest (default: $CLOUDLOGS_INPUT, "
        "then example/logs.log); relative paths are taken from the current directory",
    )
    parser.add_argument(
        "-p", "--port", type=int, default=int(os.environ.get("PORT", 8000)),
        help="port to listen on (default: 8000, or $PORT)",
    )
    parser.add_argument(
        "--host", default=os.environ.get("HOST", "0.0.0.0"),
        help="address to bind (default: 0.0.0.0, or $HOST). There is no "
        "authentication: use 127.0.0.1 on an untrusted network",
    )
    parser.add_argument(
        "--rules", metavar="PATH",
        help="columns + extraction rules to ingest with (default: rules.yaml)",
    )
    parser.add_argument(
        "--data", metavar="PATH",
        help=f"where the normalized records live (default: {DEFAULT_DATA})",
    )
    parser.add_argument(
        "--reload", action="store_true",
        help="restart the server when the source changes (development)",
    )
    args = parser.parse_args(argv)

    # the app reads its inputs from the environment, so the CLI just sets it
    if args.inputs:
        os.environ["CLOUDLOGS_INPUT"] = _resolve_inputs(args.inputs)
    if args.rules:
        os.environ["CLOUDLOGS_RULES"] = str(Path(args.rules).resolve())
    if args.data:
        os.environ["CLOUDLOGS_DATA"] = str(Path(args.data).resolve())

    print(f"cloudlogs: input {', '.join(input_paths())}")
    print(f"cloudlogs: http://localhost:{args.port}")
    uvicorn.run(
        "cloudlogs.main:app", host=args.host, port=args.port, reload=args.reload
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by hand
    raise SystemExit(main())
