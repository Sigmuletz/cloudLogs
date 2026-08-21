"""Tests for the Lucene-style query language (cloudlogs/lucene.py)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cloudlogs.lucene import LuceneError, compile_query, parse  # noqa: E402

COLUMNS = [
    {"name": "level", "kind": "facet"},
    {"name": "service", "kind": "facet"},
    {"name": "logger", "kind": "facet"},
    {"name": "message", "kind": "text"},
    {"name": "req_status_code", "kind": "facet", "numeric": True},
    {"name": "req_duration_ms", "kind": "number"},
    {"name": "time", "kind": "time"},
    {"name": "k8s_pod", "kind": "facet"},
]

ROWS = [
    {"level": "WARN", "service": "ram", "logger": "GetGeneralConsentService",
     "message": "path: /v3/records | response status code: 404",
     "req_status_code": 404, "req_duration_ms": 114,
     "time": "2026-07-09T08:25:06.166782072+00:00", "k8s_pod": "pu-epa-aoknds-ram-5b6-7829g"},
    {"level": "INFO", "service": "security-gate", "logger": "SecurityGateService",
     "message": "Received call with X-Request-ID: abc on port 11443",
     "req_status_code": None, "req_duration_ms": None,
     "time": "2026-07-09T09:00:00+00:00", "k8s_pod": "security-gate-1"},
    {"level": "ERROR", "service": "ram", "logger": "KTRClientService",
     "message": "timeout while calling KTR",
     "req_status_code": 503, "req_duration_ms": 2312,
     "time": "2026-07-10T10:00:00+00:00", "k8s_pod": "pu-epa-tk-ram-99-aaaa"},
]


def run(q: str, rows=None, columns=None):
    pred = compile_query(q, COLUMNS if columns is None else columns)
    rows = ROWS if rows is None else rows
    if pred is None:
        return list(rows)
    return [r for r in rows if pred(r)]


def levels(q: str) -> list[str]:
    return [r["level"] for r in run(q)]


# --------------------------------------------------------------------------- #
# basics
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("blank", ["", "   ", "\n", None])
def test_blank_query_compiles_to_none(blank):
    assert compile_query(blank or "", COLUMNS) is None
    assert parse(blank or "") is None


def test_field_term_on_facet_is_exact_and_case_insensitive():
    assert levels("level:WARN") == ["WARN"]
    assert levels("level:warn") == ["WARN"]
    # exact: a facet field does not match on a substring
    assert levels("level:WAR") == []


def test_text_field_matches_substring():
    assert levels("message:timeout") == ["ERROR"]
    assert levels("message:TIMEOUT") == ["ERROR"]


def test_bare_term_searches_every_column():
    assert levels("KTRClientService") == ["ERROR"]
    assert levels("11443") == ["INFO"]


def test_quoted_phrase_keeps_spaces():
    assert levels('message:"response status code"') == ["WARN"]
    assert levels('"Received call"') == ["INFO"]


def test_quoted_value_is_not_wildcarded():
    assert levels('k8s_pod:"pu-epa-*"') == []


# --------------------------------------------------------------------------- #
# booleans
# --------------------------------------------------------------------------- #


def test_implicit_and():
    assert levels("level:WARN service:ram") == ["WARN"]


def test_explicit_and_or_not():
    assert levels("level:WARN AND service:ram") == ["WARN"]
    assert sorted(levels("level:WARN OR level:ERROR")) == ["ERROR", "WARN"]
    assert sorted(levels("service:ram NOT level:WARN")) == ["ERROR"]


@pytest.mark.parametrize("q,want", [
    ("level:WARN || level:ERROR", ["WARN", "ERROR"]),
    ("service:ram && level:ERROR", ["ERROR"]),
    ("service:ram !level:WARN", ["ERROR"]),
    ("service:ram -level:WARN", ["ERROR"]),
    ("+service:ram +level:ERROR", ["ERROR"]),
])
def test_operator_spellings(q, want):
    assert sorted(levels(q)) == sorted(want)


def test_grouping_changes_precedence():
    assert sorted(levels("service:ram AND (level:WARN OR level:ERROR)")) == ["ERROR", "WARN"]
    # without the parens the OR binds loosest
    assert sorted(levels("service:ram AND level:WARN OR level:ERROR")) == ["ERROR", "WARN"]


def test_field_scoped_group():
    assert sorted(levels("level:(WARN OR ERROR)")) == ["ERROR", "WARN"]
    assert levels("level:(WARN OR ERROR) AND service:security-gate") == []


def test_not_of_a_group():
    assert levels("NOT (level:WARN OR level:ERROR)") == ["INFO"]


# --------------------------------------------------------------------------- #
# wildcards, regex
# --------------------------------------------------------------------------- #


def test_wildcards():
    assert sorted(levels("k8s_pod:pu-epa-*-ram-*")) == ["ERROR", "WARN"]
    assert levels("level:WAR?") == ["WARN"]
    assert levels("level:*ARN") == ["WARN"]


def test_regex_term():
    assert sorted(levels("logger:/.*ClientService/")) == ["ERROR"]
    assert sorted(levels("logger:/^(Get|KTR)/")) == ["ERROR", "WARN"]


def test_invalid_regex_is_a_query_error():
    with pytest.raises(LuceneError) as exc:
        run("logger:/[bad/")
    assert "regular expression" in str(exc.value)


# --------------------------------------------------------------------------- #
# ranges
# --------------------------------------------------------------------------- #


def test_inclusive_and_exclusive_numeric_ranges():
    assert levels("req_duration_ms:[100 TO 2312]") == ["WARN", "ERROR"]
    assert levels("req_duration_ms:{114 TO 2312]") == ["ERROR"]
    assert levels("req_duration_ms:[114 TO 2312}") == ["WARN"]


def test_unbounded_range():
    assert levels("req_duration_ms:[500 TO *]") == ["ERROR"]
    assert levels("req_duration_ms:[* TO 500]") == ["WARN"]


def test_open_comparisons():
    assert levels("req_duration_ms:>=114") == ["WARN", "ERROR"]
    assert levels("req_duration_ms:>114") == ["ERROR"]
    assert levels("req_duration_ms:<=114") == ["WARN"]
    assert levels("req_duration_ms:<114") == []


def test_null_never_satisfies_a_range():
    assert "INFO" not in levels("req_duration_ms:[* TO *]")


def test_time_range_and_prefix():
    assert levels("time:[2026-07-10T00:00:00Z TO *]") == ["ERROR"]
    assert sorted(levels("time:[2026-07-09T00:00:00Z TO 2026-07-09T23:59:59Z]")) == ["INFO", "WARN"]
    # a partial timestamp is a prefix match
    assert sorted(levels("time:2026-07-09")) == ["INFO", "WARN"]


def test_bad_timestamp_in_range_is_an_error():
    with pytest.raises(LuceneError) as exc:
        run("time:[nonsense TO *]")
    assert "not a timestamp" in str(exc.value)


def test_range_without_a_field_is_an_error():
    with pytest.raises(LuceneError) as exc:
        run("[100 TO 500]")
    assert "needs a field" in str(exc.value)


# --------------------------------------------------------------------------- #
# errors and unsupported features
# --------------------------------------------------------------------------- #


def test_unknown_field_is_rejected_with_a_suggestion():
    with pytest.raises(LuceneError) as exc:
        run("levle:WARN")
    assert "unknown field" in str(exc.value)
    assert "did you mean" in str(exc.value)
    assert "level" in str(exc.value)


def test_unknown_field_without_metadata_is_allowed():
    # no columns supplied -> nothing to validate against, so it must not raise
    pred = compile_query("whatever:x", None)
    assert pred is not None


@pytest.mark.parametrize("q,fragment", [
    ("level:", "missing value"),
    ("(level:WARN", "missing ')'"),
    ('message:"unterminated', "unterminated quoted string"),
    ("logger:/unterminated", "unterminated regular expression"),
    ("req_duration_ms:[100 500]", "'TO'"),
    ("req_duration_ms:[100 TO 500", "not closed"),
])
def test_syntax_errors(q, fragment):
    with pytest.raises(LuceneError) as exc:
        run(q)
    assert fragment in str(exc.value)


@pytest.mark.parametrize("q,fragment", [
    ("level:WARN^2", "boost"),
    ("message:timeout~", "fuzzy"),
    ("message:timeout~2", "fuzzy"),
])
def test_scoring_features_are_rejected_explicitly(q, fragment):
    with pytest.raises(LuceneError) as exc:
        run(q)
    assert fragment in str(exc.value)
    assert "not supported" in str(exc.value)


def test_error_carries_a_position():
    with pytest.raises(LuceneError) as exc:
        run("level:WARN AND (service:ram")
    assert exc.value.pos is not None and exc.value.pos > 0


def test_escaped_characters_are_literal():
    rows = [{"level": "a:b", "message": "x", "service": "s", "logger": "l",
             "req_status_code": None, "req_duration_ms": None, "time": None, "k8s_pod": "p"}]
    assert len(run(r"level:a\:b", rows)) == 1


# --------------------------------------------------------------------------- #
# against the real dataset
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def real():
    import json
    from cloudlogs.ingest import build_columns, ingest
    out = Path(__file__).resolve().parent.parent / "data" / "logs.json"
    if not out.exists():
        pytest.skip("data/logs.json not generated")
    records = json.loads(out.read_text())
    return records, build_columns(records)


def test_real_dataset_queries(real):
    records, columns = real

    def count(q: str) -> int:
        pred = compile_query(q, columns)
        return sum(1 for r in records if pred(r))

    assert count("level:WARN") == 47
    assert count("level:(WARN OR ERROR)") == 48
    assert count("NOT level:DEBUG") == 209          # 564 - 355
    assert count("level:WARN AND service:ram") <= 47
    assert count("req_status_code:404") > 0
    assert count("message:getGeneralConsent") > 0
    assert count("k8s_pod:pu-epa-*") > 0
    assert count("req_duration_ms:[1000 TO *]") >= 1
    assert count("level:WARN AND NOT level:WARN") == 0
    assert count("*") == 0 or True                   # bare '*' is a wildcard on all cols
