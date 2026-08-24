"""Ingest CLI: `.log` files/globs/directories -> `data/logs.json` + `data/columns.json`.

    python -m cloudlogs.ingest example/logs.log
    python -m cloudlogs.ingest 'logs/**/*.log' -o data/all.json
    python -m cloudlogs.ingest logdir/                  # recurses *.log
    python -m cloudlogs.ingest logs.log --rules experiments.yaml

What each line becomes is declared in `rules.yaml`, not here: this module owns
the file walking, the column metadata and the report (PLAN.md 2.10 - 2.12).

Reusable API (used by `cloudlogs.main` to ingest on startup):

    ingest(paths, out=Path("data/logs.json"), rules=None) -> dict
        Parse every input into `out`, write column metadata next to it as
        `columns.json`, and return a summary dict:
        {"files": [str], "lines": int, "records": int, "json_ok": int,
         "rules": [{"name", "required", "hits", "writes"}], "cast_failures": [...],
         "failed": int, "columns": int, "out": str, "columns_path": str,
         "rules_path": str}
        `rules` takes a path or an already loaded `Ruleset`; None means the
        default `rules.yaml` (or `$CLOUDLOGS_RULES`).

    is_stale(out, paths, rules=None) -> bool
        True when `out` is missing or older than any of the resolved inputs
        **or than the rules file**, i.e. when ingest should run again. `paths`
        takes the same files/globs/directories the CLI accepts.

    format_summary(summary) -> str
        The per-rule report printed by the CLI (PLAN.md 2.11).
"""

from __future__ import annotations

import argparse
import glob as globlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

from .parse import parse_record
from .rules import ENGINE_COLUMNS, Column, Ruleset, RulesError, find_rules_path, load_rules

DEFAULT_OUT = Path("data/logs.json")
COLUMNS_NAME = "columns.json"

#: `facet` when a column has at most this many distinct values (PLAN.md 2.10)
FACET_MAX_DISTINCT = 200

#: a numeric column with at most this many distinct values is still a checkbox
#: facet (status codes, ports); above it, a min/max range makes more sense
FACET_MAX_NUMERIC_DISTINCT = 25

#: filter cards the panel opens with (PLAN.md 4.2). Unlike `default_visible`,
#: which every column declares for itself in `rules.yaml`, this is not part of
#: the column schema `rules.py` accepts, so it stays here.
DEFAULT_FILTERS = ("level", "logger", "service", "k8s_namespace", "req_status_code")

#: labels for the engine's own columns; a declared column says `label:` in
#: `rules.yaml` when the generic prettifier is not good enough
LABEL_OVERRIDES = {
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


def resolve_ruleset(rules: str | os.PathLike[str] | Ruleset | None = None) -> Ruleset:
    """Accept a `Ruleset`, a path, or None (the default `rules.yaml`)."""
    if isinstance(rules, Ruleset):
        return rules
    return load_rules(rules)


def _rules_path(rules: str | os.PathLike[str] | Ruleset | None) -> Path | None:
    """Where the rules live, or None when they cannot be located at all."""
    if isinstance(rules, Ruleset):
        return rules.path
    try:
        return find_rules_path(rules)
    except RulesError:
        return None


def is_stale(
    out: str | os.PathLike[str],
    paths: Iterable[str | os.PathLike[str]],
    rules: str | os.PathLike[str] | Ruleset | None = None,
) -> bool:
    """True when `out` is missing or older than any input, or than `rules.yaml`.

    Editing an extraction rule changes the output just as much as a new log
    file does, so the rules file counts as an input (PLAN.md 2.1).
    """
    out_path = Path(out)
    if not out_path.exists():
        return True
    columns_path = out_path.with_name(COLUMNS_NAME)
    if not columns_path.exists():
        return True
    out_mtime = min(out_path.stat().st_mtime, columns_path.stat().st_mtime)

    rules_path = _rules_path(rules)
    if rules_path is not None and rules_path.is_file():
        if rules_path.stat().st_mtime > out_mtime:
            return True

    inputs = expand_inputs(paths)
    if not inputs:
        return False
    return any(source.stat().st_mtime > out_mtime for source in inputs)


# --------------------------------------------------------------------------
# column metadata (PLAN.md 2.10)
# --------------------------------------------------------------------------


def prettify(name: str) -> str:
    """`k8s_namespace` -> `K8s Namespace`."""
    if name in LABEL_OVERRIDES:
        return LABEL_OVERRIDES[name]
    words = []
    for word in name.split("_"):
        words.append(_ACRONYMS.get(word.lower(), word.capitalize()))
    return " ".join(words)


def classify(name: str, values: list[Any], type_: str = "str") -> tuple[str, bool, int]:
    """Return `(kind, numeric, distinct)` for one column.

    `type: time` columns first, then numeric columns with many distinct values
    (min/max inputs), then anything with few enough distinct values (checkbox
    facet), else free text. A low-cardinality numeric column such as
    `req_status_code` stays a facet -- ticking 404 and 503 is what you want
    there, not a range.
    """
    distinct = len({v for v in values if v is not None})
    numeric = bool(values) and all(
        isinstance(v, (int, float)) and not isinstance(v, bool) for v in values
    )
    if type_ == "time":
        kind = "time"
    elif numeric and distinct > FACET_MAX_NUMERIC_DISTINCT:
        kind = "number"
    elif distinct <= FACET_MAX_DISTINCT:
        kind = "facet"
    else:
        kind = "text"
    return kind, numeric, distinct


def build_columns(
    records: Sequence[dict[str, Any]], ruleset: Ruleset | None = None
) -> list[dict[str, Any]]:
    """Build `columns.json` metadata for every column except `_raw`.

    Order, type and any UI override come from `rules.yaml`; `kind`, `numeric`
    and `distinct` are measured off the records unless the column declared a
    `kind:` of its own.
    """
    declared: tuple[Column, ...] = ruleset.output_columns if ruleset is not None else ()
    by_name = {column.name: column for column in declared}

    present: list[str] = [column.name for column in declared]
    present.extend(name for name in ENGINE_COLUMNS if name not in by_name)
    extra = sorted({key for record in records for key in record} - set(present) - {"_raw"})
    present.extend(extra)

    columns: list[dict[str, Any]] = []
    for name in present:
        column = by_name.get(name)
        values = [record[name] for record in records if record.get(name) is not None]
        kind, numeric, distinct = classify(name, values, column.type if column else "str")
        columns.append(
            {
                "name": name,
                "kind": column.kind if column is not None and column.kind else kind,
                "label": column.label if column is not None and column.label else prettify(name),
                "distinct": distinct,
                "numeric": numeric,
                "default_visible": bool(column.default_visible) if column is not None else False,
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
    rules: str | os.PathLike[str] | Ruleset | None = None,
) -> dict[str, Any]:
    """Parse every input file into `out` and write `columns.json` beside it.

    Returns the summary dict documented at the top of this module. The ruleset
    is loaded once and handed to every line.
    """
    ruleset = resolve_ruleset(rules)
    out_path = Path(out)
    columns_path = out_path.with_name(COLUMNS_NAME)
    files = expand_inputs(paths)

    records: list[dict[str, Any]] = []
    lines = json_ok = failed = 0
    hits: dict[str, int] = {rule.name: 0 for rule in ruleset.rules}
    writes: dict[str, int] = {rule.name: 0 for rule in ruleset.rules}
    failures: dict[str, dict[str, Any]] = {}

    for path in files:
        label = source_label(path)
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for number, raw in enumerate(handle, start=1):
                if not raw.strip():
                    continue
                lines += 1
                result = parse_record(raw, label, ruleset)
                records.append(result.record)
                json_ok += result.status.json_ok
                failed += not result.status.parse_ok
                for name in result.status.rule_hits:
                    if name in hits:
                        hits[name] += 1
                for name in result.status.rule_writes:
                    if name in writes:
                        writes[name] += 1
                for column, value in result.cast_failures:
                    entry = failures.setdefault(
                        column,
                        {
                            "column": column,
                            "type": next(
                                (c.type for c in ruleset.columns if c.name == column), "str"
                            ),
                            "count": 0,
                            "example": value,
                            "file": label,
                            "line": number,
                        },
                    )
                    entry["count"] += 1

    columns = build_columns(records, ruleset)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as handle:
        json.dump(records, handle, ensure_ascii=False)
    with columns_path.open("w", encoding="utf-8") as handle:
        json.dump(columns, handle, ensure_ascii=False, indent=2)

    seen: set[str] = set()
    rule_rows: list[dict[str, Any]] = []
    for rule in ruleset.rules:
        if rule.name in seen:
            continue
        seen.add(rule.name)
        rule_rows.append(
            {
                "name": rule.name,
                "required": rule.required,
                "hits": hits[rule.name],
                "writes": writes[rule.name],
            }
        )

    return {
        "files": [source_label(p) for p in files],
        "lines": lines,
        "records": len(records),
        "json_ok": json_ok,
        "rules": rule_rows,
        "cast_failures": list(failures.values()),
        "failed": failed,
        "columns": len(columns),
        "out": str(out_path),
        "columns_path": str(columns_path),
        "rules_path": str(ruleset.path),
    }


def format_summary(summary: dict[str, Any]) -> str:
    """The per-rule report of PLAN.md 2.11.

    The count is what a rule *contributed*, not merely what it matched, and two
    kinds of idle rule are flagged apart (PLAN.md 2.11). A rule that never
    matched carries ``⚠``: it is probably a mistake, and nothing else reveals
    it. A rule that matched but always lost to an earlier one carries a plain
    ``·``: a fallback that this input never needed is doing its job.
    """
    rows: list[tuple[str, str, int, str]] = [("json ok", "", summary["json_ok"], "")]
    for rule in summary.get("rules", ()):
        wrote = rule.get("writes", rule["hits"])
        if not rule["hits"]:
            note = "  ⚠ never matched"
        elif not wrote:
            note = f"  · matched {rule['hits']}, never needed"
        else:
            note = ""
        rows.append(
            (
                rule["name"],
                "required" if rule["required"] else "",
                wrote,
                note,
            )
        )
    rows.append(("parse_ok=false", "", summary["failed"], ""))

    name_width = max(15, *(len(name) for name, _, _, _ in rows))
    count_width = max(7, *(len(str(count)) for _, _, count, _ in rows))

    lines = [f"{summary['lines']} lines → {summary['records']} records"]
    lines += [
        f"  {name:<{name_width}}{mark:<8}{count:>{count_width}}{note}"
        for name, mark, count, note in rows
    ]
    for failure in summary.get("cast_failures", ()):
        where = f" (line {failure['line']}" + (
            f" of {failure['file']})" if len(summary.get("files", ())) > 1 else ")"
        )
        lines.append(
            f"  ⚠ {failure['column']}: {failure['count']} value"
            f"{'' if failure['count'] == 1 else 's'} could not cast to "
            f"{failure['type']}, e.g. {failure['example']!r}{where}"
        )
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
    parser.add_argument(
        "--rules",
        default=None,
        metavar="PATH",
        help=(
            "the columns + extraction rules to use (default: rules.yaml next to the "
            "project root, or $CLOUDLOGS_RULES)"
        ),
    )
    args = parser.parse_args(argv)

    try:
        ruleset = load_rules(args.rules)
    except RulesError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    files = expand_inputs(args.inputs)
    if not files:
        parser.error(f"no log files matched: {' '.join(args.inputs)}")

    summary = ingest(files, args.out, rules=ruleset)
    print(format_summary(summary))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
