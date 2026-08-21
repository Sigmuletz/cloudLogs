"""Tests for cloudlogs.query (PLAN.md section 6, query bullets).

Records are built inline — nothing here depends on data/logs.json, parse.py or
ingest.py. The FastAPI tests at the bottom skip cleanly when fastapi/httpx are
not importable.
"""

from __future__ import annotations

import pytest

from cloudlogs import query
from cloudlogs.query import (
    InvalidFilterError,
    apply_filters,
    facets,
    paginate,
    parse_time,
    sort_records,
)


def rec(**kw):
    base = {
        "time": None,
        "level": None,
        "service": None,
        "logger": None,
        "message": None,
        "req_status_code": None,
        "req_duration_ms": None,
    }
    base.update(kw)
    return base


@pytest.fixture
def records():
    return [
        rec(time="2026-07-09T08:00:00.000000000+00:00", level="INFO", service="ram",
            logger="A", message="Received request getGeneralConsent",
            req_status_code=200, req_duration_ms=10),
        rec(time="2026-07-09T08:30:00.500000000+00:00", level="WARN", service="ram",
            logger="B", message="path: /v3/x | response status code: 404",
            req_status_code=404, req_duration_ms=114),
        rec(time="2026-07-09T09:00:00+00:00", level="ERROR", service="information",
            logger="A", message="boom 500", req_status_code=500, req_duration_ms=None),
        rec(time="2026-07-09T10:00:00+00:00", level="DEBUG", service="information",
            logger="C", message="debug getGeneralConsent noise",
            req_status_code=None, req_duration_ms=3),
        rec(time="2026-07-10T08:00:00+00:00", level="INFO", service="notification",
            logger=None, message=None, req_status_code=200, req_duration_ms=2000),
    ]


def levels(rows):
    return [r["level"] for r in rows]


# --------------------------------------------------------------------------- #
# OR within a column, AND across columns
# --------------------------------------------------------------------------- #


def test_facet_or_within_column(records):
    out = apply_filters(records, {"level": {"kind": "facet", "values": ["WARN", "ERROR"]}})
    assert levels(out) == ["WARN", "ERROR"]


def test_and_across_columns(records):
    out = apply_filters(
        records,
        {
            "level": {"kind": "facet", "values": ["WARN", "ERROR", "INFO"]},
            "service": {"kind": "facet", "values": ["ram"]},
        },
    )
    assert levels(out) == ["INFO", "WARN"]


def test_empty_filter_is_inactive(records):
    assert len(apply_filters(records, {"level": {"kind": "facet", "values": []}})) == len(records)
    assert len(apply_filters(records, {"message": {"kind": "text", "value": ""}})) == len(records)
    assert len(apply_filters(records, {"req_duration_ms": {"kind": "number"}})) == len(records)


def test_facet_matches_across_json_types(records):
    """The UI sends facet values back as strings; ints must still match."""
    out = apply_filters(records, {"req_status_code": {"kind": "facet", "values": ["404"]}})
    assert levels(out) == ["WARN"]
    out = apply_filters(records, {"req_status_code": {"kind": "facet", "values": [404]}})
    assert levels(out) == ["WARN"]


def test_facet_can_select_nulls(records):
    out = apply_filters(records, {"req_status_code": {"kind": "facet", "values": [None]}})
    assert levels(out) == ["DEBUG"]


# --------------------------------------------------------------------------- #
# number / time / text
# --------------------------------------------------------------------------- #


def test_number_min_max_inclusive(records):
    out = apply_filters(records, {"req_duration_ms": {"kind": "number", "min": 10, "max": 114}})
    assert levels(out) == ["INFO", "WARN"]
    out = apply_filters(records, {"req_duration_ms": {"kind": "number", "min": 114, "max": None}})
    assert levels(out) == ["WARN", "INFO"]
    out = apply_filters(records, {"req_duration_ms": {"kind": "number", "min": None, "max": 10}})
    assert levels(out) == ["INFO", "DEBUG"]


def test_number_excludes_nulls(records):
    out = apply_filters(records, {"req_duration_ms": {"kind": "number", "min": 0}})
    assert "ERROR" not in levels(out)  # req_duration_ms is None there


def test_number_compares_numerically_not_as_strings():
    rows = [rec(level="a", req_status_code="9"), rec(level="b", req_status_code="100")]
    out = apply_filters(rows, {"req_status_code": {"kind": "number", "min": 10}})
    assert levels(out) == ["b"]


def test_time_from_to(records):
    out = apply_filters(records, {"time": {"kind": "time", "from": "2026-07-09T08:30:00Z"}})
    assert levels(out) == ["WARN", "ERROR", "DEBUG", "INFO"]
    out = apply_filters(
        records,
        {"time": {"kind": "time", "from": "2026-07-09T08:30:00Z", "to": "2026-07-09T09:00:00+00:00"}},
    )
    assert levels(out) == ["WARN", "ERROR"]


def test_time_bounds_are_inclusive_and_tolerate_forms(records):
    for bound in ("2026-07-09T09:00:00Z", "2026-07-09T09:00:00+00:00",
                  "2026-07-09T09:00:00.000000000+00:00", "2026-07-09 09:00:00"):
        out = apply_filters(records, {"time": {"kind": "time", "from": bound, "to": bound}})
        assert levels(out) == ["ERROR"], bound


def test_time_compared_as_datetimes_not_strings():
    """String comparison would order these wrongly (offsets differ)."""
    rows = [
        rec(level="early", time="2026-07-09T09:30:00+02:00"),  # 07:30Z
        rec(level="late", time="2026-07-09T08:00:00Z"),
    ]
    out = apply_filters(rows, {"time": {"kind": "time", "to": "2026-07-09T07:45:00Z"}})
    assert levels(out) == ["early"]


def test_parse_time_forms():
    base = parse_time("2026-07-09T08:00:00Z")
    assert parse_time("2026-07-09T08:00:00+00:00") == base
    assert parse_time("2026-07-09T08:00:00.000000000+00:00") == base
    assert parse_time("2026-07-09 08:00:00") == base
    assert parse_time("2026-07-09T08:00:00+0000") == base
    assert parse_time("not a time") is None
    assert parse_time(None) is None
    assert parse_time(42) is None


def test_time_filter_with_unparsable_bound_is_reported(records):
    with pytest.raises(InvalidFilterError):
        apply_filters(records, {"time": {"kind": "time", "from": "yesterday"}})


def test_text_substring_case_insensitive(records):
    out = apply_filters(records, {"message": {"kind": "text", "value": "GENERALCONSENT"}})
    assert levels(out) == ["INFO", "DEBUG"]


def test_text_regex(records):
    out = apply_filters(records, {"message": {"kind": "text", "value": r"^path: /v3", "regex": True}})
    assert levels(out) == ["WARN"]
    out = apply_filters(records, {"message": {"kind": "text", "value": r"\b(404|500)\b", "regex": True}})
    assert levels(out) == ["WARN", "ERROR"]


def test_invalid_regex_is_reported_not_crashed(records):
    with pytest.raises(InvalidFilterError):
        apply_filters(records, {"message": {"kind": "text", "value": "[unclosed", "regex": True}})


def test_unknown_filter_kind_is_reported(records):
    with pytest.raises(InvalidFilterError):
        apply_filters(records, {"level": {"kind": "nope", "values": ["INFO"]}})


# --------------------------------------------------------------------------- #
# global q
# --------------------------------------------------------------------------- #


def test_global_q_across_text_columns(records):
    out = apply_filters(records, {}, "getgeneralconsent")
    assert levels(out) == ["INFO", "DEBUG"]


def test_global_q_restricted_to_given_text_cols(records):
    assert levels(apply_filters(records, {}, "ram", q_cols=["message"])) == []
    assert len(apply_filters(records, {}, "ram", q_cols=["service"])) == 2


def test_global_q_ands_with_filters(records):
    out = apply_filters(records, {"level": {"kind": "facet", "values": ["DEBUG"]}}, "getGeneralConsent")
    assert levels(out) == ["DEBUG"]


# --------------------------------------------------------------------------- #
# facets: cross-filtered counts
# --------------------------------------------------------------------------- #


def as_map(entries):
    return {str(e["value"]): e["count"] for e in entries}


def test_facets_unfiltered_counts_everything(records):
    f = facets(records, {}, None, ["level", "service"])
    assert as_map(f["level"]) == {"INFO": 2, "WARN": 1, "ERROR": 1, "DEBUG": 1}
    assert as_map(f["service"]) == {"ram": 2, "information": 2, "notification": 1}


def test_facet_counts_exclude_own_filter_but_apply_others(records):
    filters = {
        "level": {"kind": "facet", "values": ["WARN"]},
        "service": {"kind": "facet", "values": ["ram"]},
    }
    f = facets(records, filters, None, ["level", "service"])
    # level counts: own filter dropped, service=ram still applied -> the 2 ram rows
    assert as_map(f["level"]) == {"INFO": 1, "WARN": 1, "ERROR": 0, "DEBUG": 0}
    # service counts: own filter dropped, level=WARN still applied -> the 1 WARN row
    assert as_map(f["service"]) == {"ram": 1, "information": 0, "notification": 0}
    # and the actual result set is the intersection
    assert levels(apply_filters(records, filters)) == ["WARN"]


def test_facet_counts_predict_ticking_a_box(records):
    filters = {"service": {"kind": "facet", "values": ["ram"]}}
    f = facets(records, filters, None, ["level"])
    for entry in f["level"]:
        with_level = dict(filters, level={"kind": "facet", "values": [entry["value"]]})
        assert len(apply_filters(records, with_level)) == entry["count"], entry


def test_facets_include_zero_counts(records):
    f = facets(records, {"service": {"kind": "facet", "values": ["notification"]}}, None, ["logger"])
    counts = as_map(f["logger"])
    assert counts["A"] == 0 and counts["B"] == 0 and counts["C"] == 0
    assert counts["None"] == 1  # the notification row has logger=None
    assert len(f["logger"]) == 4  # nothing dropped


def test_facets_respect_non_facet_filters_and_q(records):
    f = facets(records, {"req_duration_ms": {"kind": "number", "min": 100}}, None, ["level"])
    assert as_map(f["level"]) == {"WARN": 1, "INFO": 1, "ERROR": 0, "DEBUG": 0}
    f = facets(records, {}, "getGeneralConsent", ["level"])
    assert as_map(f["level"]) == {"INFO": 1, "DEBUG": 1, "WARN": 0, "ERROR": 0}


def test_facets_sorted_by_count_desc(records):
    counts = [e["count"] for e in facets(records, {}, None, ["level"])["level"]]
    assert counts == sorted(counts, reverse=True)


def test_facets_max_values_truncates_lowest_counts(records):
    f = facets(records, {}, None, ["level"], max_values=2)
    # count desc, ties broken by value ascending -> INFO(2), then DEBUG(1)
    assert as_map(f["level"]) == {"INFO": 2, "DEBUG": 1}


def test_facets_no_columns_requested(records):
    assert facets(records, {}, None, []) == {}


def test_distinct_counts(records):
    assert query.distinct_counts(records, ["level", "service"]) == {"level": 4, "service": 3}


# --------------------------------------------------------------------------- #
# sorting
# --------------------------------------------------------------------------- #


def test_sort_single_key_both_directions(records):
    assert levels(sort_records(records, [{"col": "time", "dir": "asc"}])) == [
        "INFO", "WARN", "ERROR", "DEBUG", "INFO"
    ]
    assert levels(sort_records(records, [{"col": "time", "dir": "desc"}])) == [
        "INFO", "DEBUG", "ERROR", "WARN", "INFO"
    ]


def test_sort_multi_key_is_stable_and_ordered():
    rows = [
        rec(level="INFO", logger="b", time="2026-07-09T08:00:00Z"),
        rec(level="WARN", logger="a", time="2026-07-09T09:00:00Z"),
        rec(level="INFO", logger="a", time="2026-07-09T07:00:00Z"),
        rec(level="INFO", logger="a", time="2026-07-09T10:00:00Z"),
    ]
    out = sort_records(rows, [{"col": "level", "dir": "asc"}, {"col": "time", "dir": "desc"}])
    assert [(r["level"], r["time"][11:13]) for r in out] == [
        ("INFO", "10"), ("INFO", "08"), ("INFO", "07"), ("WARN", "09")
    ]


def test_sort_nulls_always_last_in_both_directions():
    rows = [rec(level="a", req_status_code=None), rec(level="b", req_status_code=200),
            rec(level="c", req_status_code=500)]
    assert levels(sort_records(rows, [{"col": "req_status_code", "dir": "asc"}])) == ["b", "c", "a"]
    assert levels(sort_records(rows, [{"col": "req_status_code", "dir": "desc"}])) == ["c", "b", "a"]


def test_sort_numeric_column_compares_numerically():
    rows = [rec(level="a", req_status_code=9), rec(level="b", req_status_code=100),
            rec(level="c", req_status_code=20)]
    assert levels(sort_records(rows, [{"col": "req_status_code", "dir": "asc"}])) == ["a", "c", "b"]
    # ...even when the values arrived as strings, if the column is declared numeric
    rows_s = [rec(level="a", req_status_code="9"), rec(level="b", req_status_code="100"),
              rec(level="c", req_status_code="20")]
    got = sort_records(rows_s, [{"col": "req_status_code", "dir": "asc"}],
                       numeric_cols=["req_status_code"])
    assert levels(got) == ["a", "c", "b"]
    # while plain string sorting is lexicographic
    assert levels(sort_records(rows_s, [{"col": "req_status_code", "dir": "asc"}])) == ["b", "c", "a"]


def test_sort_strings_are_lexicographic():
    rows = [rec(level="b", logger="beta"), rec(level="a", logger="Alpha"), rec(level="c", logger="gamma")]
    assert levels(sort_records(rows, [{"col": "logger", "dir": "asc"}])) == ["a", "b", "c"]


def test_sort_mixed_types_does_not_raise():
    rows = [rec(level="a", logger=5), rec(level="b", logger="text"), rec(level="c", logger=None),
            rec(level="d", logger=True), rec(level="e", logger=["x"]), rec(level="f", logger=1.5)]
    for direction in ("asc", "desc"):
        out = sort_records(rows, [{"col": "logger", "dir": direction}])
        assert len(out) == len(rows)
        assert out[-1]["level"] == "c"  # null last in both directions


def test_sort_empty_or_unknown_column_is_harmless(records):
    assert sort_records(records, []) == list(records)
    assert len(sort_records(records, [{"col": "nope", "dir": "asc"}])) == len(records)


def test_sort_does_not_mutate_input(records):
    before = list(records)
    sort_records(records, [{"col": "time", "dir": "desc"}])
    assert records == before


# --------------------------------------------------------------------------- #
# pagination
# --------------------------------------------------------------------------- #


def test_paginate_slices(records):
    assert levels(paginate(records, 2, 0)) == ["INFO", "WARN"]
    assert levels(paginate(records, 2, 2)) == ["ERROR", "DEBUG"]
    assert levels(paginate(records, 2, 4)) == ["INFO"]
    assert paginate(records, 2, 99) == []
    assert len(paginate(records, None, 1)) == 4
    assert paginate(records, 0, 0) == []


def test_total_is_the_filtered_count_not_the_page(records):
    matched = apply_filters(records, {"service": {"kind": "facet", "values": ["ram", "information"]}})
    page = paginate(matched, 2, 0)
    assert len(matched) == 4 and len(page) == 2


# --------------------------------------------------------------------------- #
# FastAPI surface (skipped when fastapi/httpx are unavailable)
# --------------------------------------------------------------------------- #


@pytest.fixture
def client(records, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from cloudlogs import main

    monkeypatch.setattr(main, "load_state", lambda *a, **k: {"records": len(records)})
    rows = [dict(r, _raw={"orig": i}) for i, r in enumerate(records)]
    for i, row in enumerate(rows):
        row["_idx"] = i
    monkeypatch.setattr(main, "RECORDS", rows)
    monkeypatch.setattr(main, "COLUMNS", main._infer_columns(rows))
    monkeypatch.setattr(main, "TEXT_COLS", ["message"])
    monkeypatch.setattr(main, "NUMERIC_COLS", ["req_status_code", "req_duration_ms"])
    monkeypatch.setattr(main, "FACET_COLS", ["level", "service", "logger"])
    with TestClient(main.app) as c:
        yield c


def test_api_logs_shape(client):
    r = client.post("/api/logs", json={
        "filters": {"level": {"kind": "facet", "values": ["WARN", "ERROR"]}},
        "sort": [{"col": "time", "dir": "desc"}],
        "limit": 1,
        "offset": 0,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2 and body["limit"] == 1 and body["offset"] == 0
    assert len(body["rows"]) == 1
    assert body["rows"][0]["level"] == "ERROR"
    assert "_raw" not in body["rows"][0]
    assert "_idx" in body["rows"][0]
    assert {e["value"]: e["count"] for e in body["facets"]["level"]}["DEBUG"] == 1


def test_api_logs_invalid_regex_is_400_not_500(client):
    r = client.post("/api/logs", json={
        "filters": {"message": {"kind": "text", "value": "[bad", "regex": True}}
    })
    assert r.status_code == 400


def test_api_row_includes_raw_and_404s(client):
    r = client.get("/api/row/0")
    assert r.status_code == 200 and r.json()["_raw"] == {"orig": 0}
    assert client.get("/api/row/999").status_code == 404


def test_api_columns(client):
    r = client.get("/api/columns")
    assert r.status_code == 200
    assert {c["name"] for c in r.json()} >= {"time", "level", "message"}
