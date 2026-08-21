"""Pure filtering / faceting / sorting / pagination over ``list[dict]`` records.

No FastAPI, no file I/O — everything here operates on plain Python records as
produced by :mod:`cloudlogs.parse` (see PLAN.md section 2.2).

Filter kinds (PLAN.md section 3)::

    {"kind": "facet",  "values": ["WARN", "ERROR"]}      # OR within the column
    {"kind": "number", "min": 100, "max": null}          # inclusive, both optional
    {"kind": "time",   "from": "...", "to": null}        # RFC3339, parsed datetimes
    {"kind": "text",   "value": "404", "regex": false}   # substring, case-insensitive

Within a column values OR together; across columns filters AND together.
A filter with nothing set (empty ``values``/``value``, both bounds ``None``) is
inactive and ignored.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Callable, Iterable, Mapping, Sequence

__all__ = [
    "InvalidFilterError",
    "apply_filters",
    "facets",
    "sort_records",
    "paginate",
    "parse_time",
    "distinct_counts",
]

# Sentinel used to key ``None`` in facet value maps (str keys keep 404 == "404").
_NULL_KEY = "\x00null"


class InvalidFilterError(ValueError):
    """Raised for a filter the caller got wrong (e.g. an invalid regex).

    Callers (``main.py``) turn this into a 400, never a 500.
    """


# --------------------------------------------------------------------------- #
# value normalisation helpers
# --------------------------------------------------------------------------- #


def _key(value: Any) -> str:
    """Normalise a value to a comparison key.

    JSON round-trips lose types (the UI sends back ``"404"`` for an int column),
    so facet matching happens on the string form. ``None`` gets its own key.
    """
    if value is None:
        return _NULL_KEY
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return value
    return str(value)


def _to_number(value: Any) -> float | None:
    """Best-effort numeric coercion; ``None`` when the value is not a number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


_TS_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})[ Tt]"
    r"(?P<time>\d{2}:\d{2}(?::\d{2})?)"
    r"(?:\.(?P<frac>\d+))?"
    r"\s*(?P<tz>[Zz]|[+-]\d{2}:?\d{2})?$"
)


@lru_cache(maxsize=1 << 17)
def _parse_time_str(text: str) -> datetime | None:
    s = text.strip()
    if not s:
        return None
    m = _TS_RE.match(s)
    if m:
        frac = m.group("frac") or ""
        frac = frac[:6].ljust(6, "0") if frac else ""  # tolerate nanoseconds
        tz = m.group("tz") or ""
        if tz in ("Z", "z"):
            tz = "+00:00"
        elif len(tz) == 5:  # +0000 -> +00:00
            tz = tz[:3] + ":" + tz[3:]
        iso = f"{m.group('date')}T{m.group('time')}"
        if frac:
            iso += f".{frac}"
        iso += tz
    else:
        iso = s.replace("Z", "+00:00").replace("z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def parse_time(value: Any) -> datetime | None:
    """Parse an RFC3339-ish timestamp; ``None`` when it is not a timestamp.

    Tolerates ``Z`` / ``+00:00`` / ``+0000`` / no offset (assumed UTC) and
    nanosecond fractions (truncated to microseconds).
    """
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    return _parse_time_str(value)


# --------------------------------------------------------------------------- #
# filter compilation
# --------------------------------------------------------------------------- #

Predicate = Callable[[Any], bool]


def _compile_facet(col: str, spec: Mapping[str, Any]) -> Predicate | None:
    values = spec.get("values")
    if not values:
        return None
    wanted = {_key(v) for v in values}
    return lambda value: _key(value) in wanted


def _compile_number(col: str, spec: Mapping[str, Any]) -> Predicate | None:
    lo = _to_number(spec.get("min"))
    hi = _to_number(spec.get("max"))
    if lo is None and hi is None:
        return None

    def pred(value: Any) -> bool:
        n = _to_number(value)
        if n is None:
            return False  # nulls / non-numeric never satisfy a range
        if lo is not None and n < lo:
            return False
        if hi is not None and n > hi:
            return False
        return True

    return pred


def _compile_time(col: str, spec: Mapping[str, Any]) -> Predicate | None:
    raw_from = spec.get("from", spec.get("from_"))
    raw_to = spec.get("to")
    start = parse_time(raw_from) if raw_from else None
    end = parse_time(raw_to) if raw_to else None
    if raw_from and start is None:
        raise InvalidFilterError(f"{col}: unparsable 'from' timestamp {raw_from!r}")
    if raw_to and end is None:
        raise InvalidFilterError(f"{col}: unparsable 'to' timestamp {raw_to!r}")
    if start is None and end is None:
        return None

    def pred(value: Any) -> bool:
        t = parse_time(value)
        if t is None:
            return False
        if start is not None and t < start:
            return False
        if end is not None and t > end:
            return False
        return True

    return pred


def _compile_text(col: str, spec: Mapping[str, Any]) -> Predicate | None:
    value = spec.get("value")
    if value is None or value == "":
        return None
    text = str(value)
    if spec.get("regex"):
        try:
            rx = re.compile(text, re.IGNORECASE)
        except re.error as exc:
            raise InvalidFilterError(f"{col}: invalid regex {text!r}: {exc}") from exc
        return lambda v: v is not None and rx.search(str(v)) is not None
    needle = text.lower()
    return lambda v: v is not None and needle in str(v).lower()


_COMPILERS: dict[str, Callable[[str, Mapping[str, Any]], Predicate | None]] = {
    "facet": _compile_facet,
    "number": _compile_number,
    "time": _compile_time,
    "text": _compile_text,
}


def _compile_filters(
    filters: Mapping[str, Mapping[str, Any]] | None,
) -> list[tuple[str, Predicate]]:
    """Compile the *active* filters into ``(column, predicate)`` pairs."""
    compiled: list[tuple[str, Predicate]] = []
    for col, spec in (filters or {}).items():
        if not spec:
            continue
        kind = spec.get("kind", "text")
        compiler = _COMPILERS.get(kind)
        if compiler is None:
            raise InvalidFilterError(f"{col}: unknown filter kind {kind!r}")
        pred = compiler(col, spec)
        if pred is not None:
            compiled.append((col, pred))
    return compiled


def _compile_q(
    q: str | None, q_cols: Sequence[str] | None
) -> Callable[[Mapping[str, Any]], bool] | None:
    """Global search: case-insensitive substring across text columns.

    ``q_cols`` should be the text-kind columns; when ``None`` every non-private
    string field is searched.
    """
    if not q or not q.strip():
        return None
    needle = q.strip().lower()

    if q_cols is None:
        def pred(rec: Mapping[str, Any]) -> bool:
            for key, value in rec.items():
                if key.startswith("_"):
                    continue
                if isinstance(value, str) and needle in value.lower():
                    return True
            return False
        return pred

    cols = tuple(q_cols)

    def pred_cols(rec: Mapping[str, Any]) -> bool:
        for col in cols:
            value = rec.get(col)
            if value is not None and needle in str(value).lower():
                return True
        return False

    return pred_cols


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #


def apply_filters(
    records: Sequence[Mapping[str, Any]],
    filters: Mapping[str, Mapping[str, Any]] | None = None,
    q: str | None = None,
    *,
    q_cols: Sequence[str] | None = None,
    expr: Callable[[Mapping[str, Any]], bool] | None = None,
) -> list[dict]:
    """Return the records matching every active filter and the global ``q``.

    ``expr`` is an extra whole-record predicate -- the compiled Lucene query
    (see ``cloudlogs.lucene``). It gates records exactly like ``q`` does: it is
    ANDed with everything else and, in ``facets``, applies to every column
    rather than being excluded from its own the way a column filter is.
    """
    compiled = _compile_filters(filters)
    q_pred = _compile_q(q, q_cols)
    if not compiled and q_pred is None and expr is None:
        return list(records)

    out: list[dict] = []
    for rec in records:
        if expr is not None and not expr(rec):
            continue
        if q_pred is not None and not q_pred(rec):
            continue
        for col, pred in compiled:
            if not pred(rec.get(col)):
                break
        else:
            out.append(rec)  # type: ignore[arg-type]
    return out


def facets(
    records: Sequence[Mapping[str, Any]],
    filters: Mapping[str, Mapping[str, Any]] | None = None,
    q: str | None = None,
    cols: Iterable[str] | None = None,
    *,
    q_cols: Sequence[str] | None = None,
    max_values: int | None = None,
    expr: Callable[[Mapping[str, Any]], bool] | None = None,
) -> dict[str, list[dict]]:
    """Cross-filtered facet counts.

    ``records`` is the **unfiltered** base set. For each requested column the
    count is computed over the records that pass ``q`` and every *other* active
    filter, with that column's own filter excluded — so a count predicts what
    ticking that box would yield.

    Every distinct value seen anywhere in ``records`` is reported, including the
    ones whose cross-filtered count is 0 (never omitted). ``None`` is reported
    as a value in its own right.

    Single pass: a record is counted for every requested column when it fails no
    filter, for exactly one column when it fails only that column's filter, and
    for none when it fails ``q``, the Lucene ``expr``, or two or more filters.
    """
    col_list = list(cols or [])
    if not col_list:
        return {}
    compiled = _compile_filters(filters)
    q_pred = _compile_q(q, q_cols)

    # column -> normalised key -> [raw value, count]
    counters: dict[str, dict[str, list[Any]]] = {c: {} for c in col_list}

    for rec in records:
        eligible = True
        only_col: str | None = None
        if expr is not None and not expr(rec):
            eligible = False
        elif q_pred is not None and not q_pred(rec):
            eligible = False
        else:
            for col, pred in compiled:
                if not pred(rec.get(col)):
                    if only_col is None:
                        only_col = col
                    else:
                        eligible = False  # fails two filters: counts nowhere
                        break
        for col in col_list:
            raw = rec.get(col)
            key = _key(raw)
            bucket = counters[col]
            entry = bucket.get(key)
            if entry is None:
                entry = [raw, 0]
                bucket[key] = entry
            if eligible and (only_col is None or only_col == col):
                entry[1] += 1

    result: dict[str, list[dict]] = {}
    for col in col_list:
        values = sorted(
            counters[col].values(),
            key=lambda e: (-e[1],) + _sort_key(e[0], desc=False, numeric=False),
        )
        if max_values is not None and len(values) > max_values:
            values = values[:max_values]
        result[col] = [{"value": raw, "count": count} for raw, count in values]
    return result


def distinct_counts(
    records: Sequence[Mapping[str, Any]], cols: Iterable[str]
) -> dict[str, int]:
    """Number of distinct values per column (used to size facet widgets)."""
    seen: dict[str, set[str]] = {c: set() for c in cols}
    for rec in records:
        for col, bucket in seen.items():
            bucket.add(_key(rec.get(col)))
    return {col: len(bucket) for col, bucket in seen.items()}


def _sort_key(value: Any, desc: bool, numeric: bool) -> tuple:
    """Sort key: ``(non_null_flag, type_rank, comparable)``.

    ``non_null_flag`` is inverted for descending sorts so nulls end up last in
    both directions. ``type_rank`` keeps mixed types comparable without raising.
    """
    if numeric:
        value = _to_number(value)
    if value is None:
        # placeholder rank/value: only ever compared against other nulls
        return (0 if desc else 1, 0, 0.0)
    non_null = 1 if desc else 0
    if isinstance(value, bool):
        return (non_null, 0, float(value))
    if isinstance(value, (int, float)):
        return (non_null, 0, float(value))
    if isinstance(value, str):
        return (non_null, 1, value)
    return (non_null, 2, repr(value))


def sort_records(
    records: Sequence[Mapping[str, Any]],
    sort: Sequence[Mapping[str, Any]] | None = None,
    *,
    numeric_cols: Iterable[str] | None = None,
) -> list[dict]:
    """Stable multi-key sort. Nulls last in both directions.

    Keys are applied least-significant first, relying on Python's stable sort.
    Columns listed in ``numeric_cols`` compare numerically (``"404"`` -> 404);
    values that cannot be coerced there sort as nulls. Mixed types never raise.
    """
    out: list[dict] = list(records)  # type: ignore[arg-type]
    if not sort:
        return out
    numeric = set(numeric_cols or ())
    for spec in reversed(list(sort)):
        col = spec.get("col")
        if not col:
            continue
        desc = str(spec.get("dir") or "asc").lower().startswith("desc")
        is_numeric = col in numeric
        out.sort(
            key=lambda rec, c=col, d=desc, n=is_numeric: _sort_key(rec.get(c), d, n),
            reverse=desc,
        )
    return out


def paginate(
    records: Sequence[Mapping[str, Any]],
    limit: int | None = None,
    offset: int = 0,
) -> list[dict]:
    """Slice a page out of ``records``. ``limit=None`` returns the whole tail."""
    start = max(0, int(offset or 0))
    if limit is None:
        return list(records[start:])
    count = max(0, int(limit))
    return list(records[start : start + count])
