"""Tests for the rule engine (PLAN.md section 6, `tests/test_parse.py`).

Every test builds its own tiny ruleset with `tmp_path`, so what is under test
is the *engine* -- source addressing, ordering, casting, defaults -- and not
the shipped `rules.yaml`. Reproducing today's output with the shipped file is
`tests/test_golden.py`'s job; the handful of tests at the bottom that do load
`rules.yaml` are the ones that are explicitly about it.
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # run without installing the package
    sys.path.insert(0, str(ROOT))

from cloudlogs.ingest import (  # noqa: E402
    FACET_MAX_NUMERIC_DISTINCT,
    build_columns,
    classify,
    expand_inputs,
    format_summary,
    ingest,
    is_stale,
)
from cloudlogs.parse import parse_line, parse_record  # noqa: E402
from cloudlogs.rules import load_rules  # noqa: E402

SOURCE = "test.log"
SHIPPED = ROOT / "rules.yaml"

OP_LOG = (
    "2026-07-09 08:25:06 WARN  [OperationLogInterceptor.filter:44] "
    "(executor-thread-44978) | path: /v3/internal/records/getGeneralConsent "
    "| response status code: 404 "
    "| x-request-id: 005056a2-18c4-1fd1-9ed9-5b6297c1a000 "
    "| x-useragent: null "
    "| user-agent: Apache-HttpClient/4.5.14 (Java/21.0.11) "
    "| time(ms): 114 "
    "| Host header: pu-epa-aoknds-ram-private.svc.cluster.local:11443 "
    "| X-Forwarded-For header: unknown "
    "| HasResponsePayload: true "
    "| Finished processing request"
)

PLAIN_LOG = (
    "2026-07-09 08:25:06 DEBUG [KTRClientService.getGeneralConsent:100] "
    "(quarkus-virtual-thread-18302) Fetching general consent from KTR"
)


def rules(tmp_path: Path, text: str, name: str = "rules.yaml"):
    """Write an inline rules file next to `tmp_path` and load it."""
    path = tmp_path / name
    path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return load_rules(path)


def double_encoded(payload: dict) -> str:
    """The double-encoded shape the real log files use."""
    return json.dumps(json.dumps(payload))


# --------------------------------------------------------------------------
# L1 -- decoding (fixed, PLAN.md 2.6)
# --------------------------------------------------------------------------


def test_double_encoded_and_plain_json_produce_the_same_record(tmp_path):
    """`json.loads` runs a second time when the first result is a string."""
    ruleset = rules(tmp_path, """
        columns:
          - {name: level}
        rules:
          - {name: level, target: level, from: level}
    """)
    envelope = {"level": "WARN"}
    single = parse_line(json.dumps(envelope), SOURCE, ruleset)
    double = parse_line(double_encoded(envelope), SOURCE, ruleset)
    assert single == double
    assert single["level"] == "WARN"
    assert single["_raw"] == envelope


def test_a_non_json_line_reaches_the_rules_as_message(tmp_path):
    """Not JSON -> the working dict is `{message: <raw text>}`, nothing is lost."""
    ruleset = rules(tmp_path, """
        columns:
          - {name: level}
          - {name: message}
        rules:
          - {name: syslog, from: message, regex: '^<\\d+>(?P<level>[A-Z]+) (?P<message>.*)$'}
    """)
    result = parse_record("<14>WARN disk almost full", SOURCE, ruleset)
    assert result.status.json_ok is False
    assert result.record["level"] == "WARN"
    assert result.record["message"] == "disk almost full"
    assert result.record["_raw"] == "<14>WARN disk almost full"


def test_json_that_is_not_an_object_still_yields_a_row(tmp_path):
    """A decoded list is not an envelope, but the line is still a record."""
    ruleset = rules(tmp_path, """
        columns:
          - {name: message}
        rules:
          - {name: message, target: message, from: message}
    """)
    result = parse_record("[1, 2, 3]", SOURCE, ruleset)
    assert result.status.json_ok is True
    assert result.record["message"] == "[1, 2, 3]"
    assert result.record["_raw"] == [1, 2, 3]


# --------------------------------------------------------------------------
# 2.3 -- source addressing
# --------------------------------------------------------------------------


def test_dotted_path_walks_nested_mappings(tmp_path):
    ruleset = rules(tmp_path, """
        columns:
          - {name: k8s_pod_hash}
          - {name: k8s_version}
          - {name: missing}
        rules:
          - {name: hash, target: k8s_pod_hash, from: kubernetes.labels.pod-template-hash}
          - {name: version, target: k8s_version, from: 'kubernetes.labels.app_kubernetes_io/version'}
          - {name: missing, target: missing, from: kubernetes.labels.nope.deeper}
    """)
    record = parse_line(
        json.dumps(
            {
                "kubernetes": {
                    "labels": {
                        "pod-template-hash": "5b69569657",
                        "app_kubernetes_io/version": "ram-epa-3.1.3-32",
                    }
                }
            }
        ),
        SOURCE,
        ruleset,
    )
    assert record["k8s_pod_hash"] == "5b69569657"
    assert record["k8s_version"] == "ram-epa-3.1.3-32"
    assert record["missing"] is None


def test_a_produced_column_shadows_a_raw_key_of_the_same_name(tmp_path):
    """Once a rule fills `message`, `from: message` reads the column, not the raw key."""
    ruleset = rules(tmp_path, """
        columns:
          - {name: message}
          - {name: echo}
        rules:
          - {name: message, target: message, from: log}
          - {name: echo, target: echo, from: message}
    """)
    record = parse_line(
        json.dumps({"log": "from the log", "message": "from the raw key"}), SOURCE, ruleset
    )
    assert record["message"] == "from the log"
    assert record["echo"] == "from the log"


def test_a_raw_key_is_read_while_its_column_is_still_empty(tmp_path):
    """`target: x, from: x` is how an envelope key becomes the column of that name."""
    ruleset = rules(tmp_path, """
        columns:
          - {name: has_response_payload, type: bool}
        rules:
          - {name: hrp, target: has_response_payload, from: has_response_payload}
    """)
    record = parse_line(json.dumps({"has_response_payload": "true"}), SOURCE, ruleset)
    assert record["has_response_payload"] is True


# --------------------------------------------------------------------------
# 2.2 -- what one rule can do
# --------------------------------------------------------------------------


def test_regex_group_one_fills_the_target(tmp_path):
    ruleset = rules(tmp_path, """
        columns:
          - {name: req_status_code, type: int}
        rules:
          - name: status
            target: req_status_code
            from: message
            regex: '(?:^|\\|)\\s*response status code:\\s*([^|]+)'
    """)
    record = parse_line(
        json.dumps({"message": "path: /x | response status code: 404 | done"}), SOURCE, ruleset
    )
    assert record["req_status_code"] == 404


def test_regex_named_groups_fill_several_columns_at_once(tmp_path):
    """A rule with named groups needs no `target:`."""
    ruleset = rules(tmp_path, """
        columns:
          - {name: app_time, type: time}
          - {name: level}
          - {name: thread}
          - {name: message}
        rules:
          - name: log-line
            from: log
            regex: '^(?P<app_time>\\S+ \\S+)\\s+(?P<level>\\S+)\\s+\\((?P<thread>[^)]*)\\)\\s*(?P<message>.*)$'
    """)
    record = parse_line(
        json.dumps({"log": "2026-07-09 08:25:06 WARN  (executor-thread-1) hello"}),
        SOURCE,
        ruleset,
    )
    assert record["app_time"] == "2026-07-09 08:25:06"
    assert record["level"] == "WARN"
    assert record["thread"] == "executor-thread-1"
    assert record["message"] == "hello"


def test_a_named_group_that_did_not_participate_writes_nothing(tmp_path):
    """`Logger` alone leaves `method` and `src_line` empty, it does not blank them."""
    ruleset = rules(tmp_path, """
        columns:
          - {name: logger}
          - {name: method}
          - {name: src_line, type: int}
        rules:
          - name: origin
            from: origin
            regex: '^(?P<logger>[^:]+?)(?:\\.(?P<method>[^.:]+))?(?::(?P<src_line>\\d+))?$'
    """)
    full = parse_line(json.dumps({"origin": "Logger.filter:44"}), SOURCE, ruleset)
    assert (full["logger"], full["method"], full["src_line"]) == ("Logger", "filter", 44)

    bare = parse_line(json.dumps({"origin": "Logger"}), SOURCE, ruleset)
    assert (bare["logger"], bare["method"], bare["src_line"]) == ("Logger", None, None)


def test_from_list_takes_the_first_non_null_source(tmp_path):
    ruleset = rules(tmp_path, """
        columns:
          - {name: req_status_code, type: int}
        rules:
          - {name: status, target: req_status_code, from: [ram_status_code, information_status_code]}
    """)
    both = parse_line(
        json.dumps({"ram_status_code": "404", "information_status_code": "202"}), SOURCE, ruleset
    )
    assert both["req_status_code"] == 404

    # `""` is a placeholder, not a value -- the next source gets its turn
    blank = parse_line(
        json.dumps({"ram_status_code": "", "information_status_code": "202"}), SOURCE, ruleset
    )
    assert blank["req_status_code"] == 202

    neither = parse_line(json.dumps({"other": "x"}), SOURCE, ruleset)
    assert neither["req_status_code"] is None


def test_the_first_rule_to_produce_a_value_wins(tmp_path):
    """Two rules on one column: the second only fills what the first left empty."""
    ruleset = rules(tmp_path, """
        columns:
          - {name: req_path}
        rules:
          - {name: path/envelope, target: req_path, from: ram_path}
          - name: path/message
            target: req_path
            from: message
            regex: '(?:^|\\|)\\s*path:\\s*([^|]+)'
    """)
    envelope_wins = parse_line(
        json.dumps({"ram_path": "/from-envelope", "message": "path: /from-message | x"}),
        SOURCE,
        ruleset,
    )
    assert envelope_wins["req_path"] == "/from-envelope"

    backfilled = parse_line(json.dumps({"message": "path: /from-message | x"}), SOURCE, ruleset)
    assert backfilled["req_path"] == "/from-message"


def test_a_rule_can_match_without_writing(tmp_path):
    """`rule_hits` records what matched, not what was stored -- that is the report."""
    ruleset = rules(tmp_path, """
        columns:
          - {name: req_path}
        rules:
          - {name: path/envelope, target: req_path, from: ram_path}
          - name: path/message
            target: req_path
            from: message
            regex: '(?:^|\\|)\\s*path:\\s*([^|]+)'
    """)
    result = parse_record(
        json.dumps({"ram_path": "/from-envelope", "message": "path: /from-message | x"}),
        SOURCE,
        ruleset,
    )
    assert result.status.rule_hits == {"path/envelope", "path/message"}
    assert result.record["req_path"] == "/from-envelope"


def test_join_concatenates_and_skips_null_pieces(tmp_path):
    ruleset = rules(tmp_path, """
        columns:
          - {name: k8s_namespace}
          - {name: k8s_pod}
          - {name: pod_ref}
        rules:
          - {name: namespace, target: k8s_namespace, from: ns}
          - {name: pod, target: k8s_pod, from: pod}
          - {name: pod_ref, target: pod_ref, join: [k8s_namespace, k8s_pod], sep: '/'}
    """)
    both = parse_line(json.dumps({"ns": "pu-epa", "pod": "ram-7829g"}), SOURCE, ruleset)
    assert both["pod_ref"] == "pu-epa/ram-7829g"

    # a missing piece is dropped, never rendered as a dangling separator
    one = parse_line(json.dumps({"pod": "ram-7829g"}), SOURCE, ruleset)
    assert one["pod_ref"] == "ram-7829g"

    none = parse_line(json.dumps({"other": "x"}), SOURCE, ruleset)
    assert none["pod_ref"] is None


# --------------------------------------------------------------------------
# 2.5 -- types and casting
# --------------------------------------------------------------------------


def test_values_are_cast_to_their_column_type(tmp_path):
    ruleset = rules(tmp_path, """
        columns:
          - {name: req_status_code, type: int}
          - {name: ratio, type: float}
          - {name: has_response_payload, type: bool}
          - {name: level}
        rules:
          - {name: status, target: req_status_code, from: status}
          - {name: ratio, target: ratio, from: ratio}
          - {name: hrp, target: has_response_payload, from: hrp}
          - {name: level, target: level, from: level}
    """)
    record = parse_line(
        json.dumps({"status": "404", "ratio": "0.5", "hrp": "true", "level": "  WARN  "}),
        SOURCE,
        ruleset,
    )
    assert record["req_status_code"] == 404
    assert record["ratio"] == 0.5
    assert record["has_response_payload"] is True
    assert record["level"] == "WARN"  # every value is stripped on its way in


def test_placeholder_strings_become_null(tmp_path):
    """`""` and `"null"` are absent values, not the text `null`."""
    ruleset = rules(tmp_path, """
        columns:
          - {name: a}
          - {name: b}
        rules:
          - {name: a, target: a, from: a}
          - {name: b, target: b, from: b}
    """)
    record = parse_line(json.dumps({"a": "  ", "b": "null"}), SOURCE, ruleset)
    assert record["a"] is None
    assert record["b"] is None


def test_a_value_that_will_not_cast_becomes_null_and_is_counted(tmp_path):
    ruleset = rules(tmp_path, """
        columns:
          - {name: req_status_code, type: int}
        rules:
          - {name: status, target: req_status_code, from: status}
    """)
    result = parse_record(json.dumps({"status": "n/a"}), SOURCE, ruleset)
    assert result.record["req_status_code"] is None
    assert result.cast_failures == (("req_status_code", "n/a"),)
    # a typed column holds that type or nothing, so a range filter is safe
    assert result.status.parse_ok is True


def test_a_failed_cast_leaves_room_for_a_later_rule(tmp_path):
    ruleset = rules(tmp_path, """
        columns:
          - {name: req_status_code, type: int}
        rules:
          - {name: status/envelope, target: req_status_code, from: bad}
          - {name: status/message, target: req_status_code, from: good}
    """)
    result = parse_record(json.dumps({"bad": "n/a", "good": "503"}), SOURCE, ruleset)
    assert result.record["req_status_code"] == 503
    assert result.cast_failures == (("req_status_code", "n/a"),)


def test_time_columns_are_validated_but_never_reformatted(tmp_path):
    """The frontend and the golden snapshot both want the original string back."""
    ruleset = rules(tmp_path, """
        columns:
          - {name: time, type: time}
          - {name: app_time, type: time}
          - {name: broken, type: time}
        rules:
          - {name: time, target: time, from: time}
          - {name: app_time, target: app_time, from: app_time}
          - {name: broken, target: broken, from: broken}
    """)
    result = parse_record(
        json.dumps(
            {
                "time": "2026-07-09T08:25:06.166782072+00:00",
                "app_time": "2026-07-09 08:25:06",
                "broken": "yesterday",
            }
        ),
        SOURCE,
        ruleset,
    )
    assert result.record["time"] == "2026-07-09T08:25:06.166782072+00:00"
    assert result.record["app_time"] == "2026-07-09 08:25:06"
    assert result.record["broken"] is None
    assert result.cast_failures == (("broken", "yesterday"),)


def test_default_fills_a_column_every_rule_left_empty(tmp_path):
    ruleset = rules(tmp_path, """
        columns:
          - {name: service, default: unknown}
        rules:
          - {name: service, target: service, from: k8s_container, regex: '(security-gate)'}
    """)
    matched = parse_line(json.dumps({"k8s_container": "security-gate"}), SOURCE, ruleset)
    assert matched["service"] == "security-gate"
    assert parse_line(json.dumps({}), SOURCE, ruleset)["service"] == "unknown"
    assert parse_line("not json", SOURCE, ruleset)["service"] == "unknown"


# --------------------------------------------------------------------------
# 2.8 / 2.9 -- the output record
# --------------------------------------------------------------------------


def test_internal_columns_never_reach_the_record(tmp_path):
    """`internal: true` is scratch space for later rules, not a column."""
    ruleset = rules(tmp_path, """
        columns:
          - {name: logger}
          - {name: origin, internal: true}
        rules:
          - {name: origin, target: origin, from: log, regex: '\\[([^\\]]*)\\]'}
          - {name: logger, target: logger, from: origin, regex: '^([^.:]+)'}
    """)
    record = parse_line(json.dumps({"log": "x [Logger.filter:44] y"}), SOURCE, ruleset)
    assert record["logger"] == "Logger"
    assert "origin" not in record


def test_engine_columns_are_present_and_last(tmp_path):
    ruleset = rules(tmp_path, """
        columns:
          - {name: time, type: time}
          - {name: level}
        rules:
          - {name: level, target: level, from: level}
    """)
    record = parse_line(json.dumps({"level": "WARN"}), SOURCE, ruleset)
    assert list(record) == ["time", "level", "source_file", "parse_ok", "_raw"]
    assert record["source_file"] == SOURCE
    assert record["parse_ok"] is True


def test_every_record_carries_every_column(tmp_path):
    """However badly a line goes, the row has the same shape as all the others."""
    ruleset = rules(tmp_path, """
        columns:
          - {name: time, type: time}
          - {name: level}
          - {name: message}
        rules:
          - {name: time, target: time, from: time}
          - {name: log-line, required: true, from: log, regex: '^(?P<level>\\S+) (?P<message>.*)$'}
          - {name: message/raw, target: message, from: [log, message]}
    """)
    records = [
        parse_line(double_encoded({"time": "2026-07-09 08:25:06", "log": "WARN hello"}), SOURCE, ruleset),
        parse_line("garbage, not json", SOURCE, ruleset),
        parse_line(json.dumps({"time": "2026-07-09 08:25:06"}), SOURCE, ruleset),
    ]
    assert {frozenset(r) for r in records} == {frozenset(records[0])}
    assert [r["parse_ok"] for r in records] == [True, False, False]
    # nothing is lost: the unparseable line keeps its text
    assert records[1]["message"] == "garbage, not json"
    assert records[1]["_raw"] == "garbage, not json"


def test_parse_ok_is_false_when_a_required_rule_misses(tmp_path):
    ruleset = rules(tmp_path, """
        columns:
          - {name: level}
          - {name: note}
        rules:
          - {name: log-line, required: true, target: level, from: log, regex: '^(\\S+)'}
          - {name: note, target: note, from: note}
    """)
    ok = parse_record(json.dumps({"log": "WARN hello"}), SOURCE, ruleset)
    assert ok.status.parse_ok is True

    # the JSON decoded and an optional rule fired, but the required one did not
    missing = parse_record(json.dumps({"note": "hi"}), SOURCE, ruleset)
    assert missing.status.json_ok is True
    assert missing.status.rule_hits == {"note"}
    assert missing.status.parse_ok is False
    assert missing.record["parse_ok"] is False
    assert missing.record["note"] == "hi"  # the rest is still salvaged

    # a line that never decoded can never be ok, whatever else matched
    assert parse_record("nope", SOURCE, ruleset).status.parse_ok is False


# --------------------------------------------------------------------------
# 2.10 -- column metadata
# --------------------------------------------------------------------------


def test_build_columns_takes_order_type_and_overrides_from_the_ruleset(tmp_path):
    ruleset = rules(tmp_path, """
        columns:
          - {name: time, type: time, default_visible: true}
          - {name: level, default_visible: true}
          - {name: req_duration_ms, type: int, label: Req Duration (ms)}
          - {name: pipe_body, internal: true}
        rules:
          - {name: time, target: time, from: time}
          - {name: level, target: level, from: level}
          - {name: duration, target: req_duration_ms, from: duration}
    """)
    records = [
        parse_line(json.dumps({"time": "2026-07-09 08:25:0%d" % i, "level": "WARN", "duration": i}), SOURCE, ruleset)
        for i in range(9)
    ]
    columns = build_columns(records, ruleset)

    assert [c["name"] for c in columns] == [
        "time", "level", "req_duration_ms", "source_file", "parse_ok"
    ]
    by_name = {c["name"]: c for c in columns}
    assert "_raw" not in by_name and "pipe_body" not in by_name
    assert by_name["time"]["kind"] == "time"                 # from `type: time`
    assert by_name["level"]["kind"] == "facet"
    assert by_name["req_duration_ms"]["label"] == "Req Duration (ms)"
    assert by_name["req_duration_ms"]["numeric"] is True
    assert by_name["level"]["label"] == "Level"              # generic prettifier
    assert {c["name"] for c in columns if c["default_visible"]} == {"time", "level"}
    assert {c["name"] for c in columns if c["default_filter"]} == {"level"}


def test_a_declared_kind_overrides_the_automatic_one(tmp_path):
    ruleset = rules(tmp_path, """
        columns:
          - {name: message, kind: text}
        rules:
          - {name: message, target: message, from: message}
    """)
    records = [parse_line(json.dumps({"message": "hi"}), SOURCE, ruleset)]
    # one distinct value would classify as a facet; the declaration wins
    assert build_columns(records, ruleset)[0]["kind"] == "text"


def test_numeric_column_becomes_number_only_above_the_facet_cutoff():
    """Low-cardinality numerics stay checkbox facets; wide ones get min/max."""
    few = list(range(FACET_MAX_NUMERIC_DISTINCT))
    many = list(range(FACET_MAX_NUMERIC_DISTINCT + 1))
    assert classify("req_status_code", few)[0] == "facet"
    assert classify("req_duration_ms", many)[0] == "number"
    assert classify("time", ["2026-07-09"], "time")[0] == "time"


# --------------------------------------------------------------------------
# CLI helpers
# --------------------------------------------------------------------------


def test_expand_inputs_handles_files_globs_and_dirs(tmp_path):
    (tmp_path / "sub").mkdir()
    first = tmp_path / "a.log"
    second = tmp_path / "sub" / "b.log"
    first.write_text("x\n")
    second.write_text("y\n")
    (tmp_path / "sub" / "ignore.txt").write_text("z\n")

    assert expand_inputs([str(first)]) == [first]
    assert expand_inputs([str(tmp_path)]) == [first, second]
    # `**` matches zero or more directories, so both files are found
    assert [p.name for p in expand_inputs([str(tmp_path / "**" / "*.log")])] == ["a.log", "b.log"]
    assert [p.name for p in expand_inputs([str(tmp_path / "sub" / "*.log")])] == ["b.log"]
    # duplicates collapse
    assert expand_inputs([str(first), str(first)]) == [first]


def test_is_stale_reacts_to_inputs_and_to_the_rules_file(tmp_path):
    """Editing an extraction rule changes the output as much as a new file does."""
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text("columns:\n  - {name: level}\n", encoding="utf-8")
    source = tmp_path / "a.log"
    source.write_text(json.dumps({"level": "WARN"}) + "\n", encoding="utf-8")
    out = tmp_path / "data" / "logs.json"

    assert is_stale(out, [source], rules_path) is True
    ingest([source], out, rules=rules_path)
    assert is_stale(out, [source], rules_path) is False

    future = out.stat().st_mtime + 10
    os.utime(source, (future, future))
    assert is_stale(out, [source], rules_path) is True

    ingest([source], out, rules=rules_path)
    past = out.stat().st_mtime - 10
    os.utime(source, (past, past))
    assert is_stale(out, [source], rules_path) is False

    # ... and to the rules file, so editing a rule alone triggers a re-ingest
    os.utime(rules_path, (future + 10, future + 10))
    assert is_stale(out, [source], rules_path) is True


def test_ingest_writes_both_files_and_reports_per_rule(tmp_path):
    ruleset = rules(tmp_path, """
        columns:
          - {name: level}
          - {name: req_status_code, type: int}
          - {name: trace_span}
        rules:
          - {name: log-line, required: true, target: level, from: log, regex: '^(\\S+)'}
          - {name: status, target: req_status_code, from: status}
          - {name: trace_span, target: trace_span, from: span}
    """)
    source = tmp_path / "a.log"
    source.write_text(
        "\n".join(
            [
                json.dumps({"log": "WARN hi", "status": "404"}),
                json.dumps({"log": "INFO hi", "status": "n/a"}),
                json.dumps({"status": "503"}),
                "",
            ]
        ),
        encoding="utf-8",
    )
    out = tmp_path / "logs.json"
    summary = ingest([source], out, rules=ruleset)

    assert summary["lines"] == 3 and summary["records"] == 3
    assert summary["json_ok"] == 3
    assert summary["failed"] == 1
    assert summary["rules"] == [
        {"name": "log-line", "required": True, "hits": 2, "writes": 2},
        {"name": "status", "required": False, "hits": 3, "writes": 2},
        {"name": "trace_span", "required": False, "hits": 0, "writes": 0},
    ]
    assert summary["cast_failures"] == [
        {
            "column": "req_status_code",
            "type": "int",
            "count": 1,
            "example": "n/a",
            "file": summary["files"][0],
            "line": 2,
        }
    ]
    assert summary["rules_path"] == str(tmp_path / "rules.yaml")

    records = json.loads(out.read_text(encoding="utf-8"))
    assert [r["level"] for r in records] == ["WARN", "INFO", None]
    assert [r["req_status_code"] for r in records] == [404, None, 503]
    assert json.loads((tmp_path / "columns.json").read_text(encoding="utf-8"))[0]["name"] == "level"

    report = format_summary(summary)
    assert report.splitlines()[0] == "3 lines → 3 records"
    assert "log-line       required      2" in report
    assert "⚠ never matched" in report
    assert "⚠ req_status_code: 1 value could not cast to int, e.g. 'n/a' (line 2)" in report


def test_format_summary_separates_a_dead_rule_from_an_unused_fallback() -> None:
    """`⚠` means probably wrong; `·` means a fallback this input did not need."""
    report = format_summary(
        {
            "lines": 5,
            "records": 5,
            "json_ok": 5,
            "failed": 0,
            "columns": 2,
            "out": "data/logs.json",
            "columns_path": "data/columns.json",
            "files": ["logs.log"],
            "rules": [
                {"name": "log-line", "required": True, "hits": 5, "writes": 5},
                {"name": "message/raw", "required": False, "hits": 5, "writes": 0},
                {"name": "trace_span", "required": False, "hits": 0, "writes": 0},
            ],
            "cast_failures": [],
        }
    )
    assert "· matched 5, never needed" in report
    assert "⚠ never matched" in report
    # the count is the contribution, not the match
    assert "message/raw" in report and "  0" in report


# --------------------------------------------------------------------------
# the shipped rules.yaml -- the file, not the engine
# --------------------------------------------------------------------------


def test_shipped_rules_load():
    ruleset = load_rules(SHIPPED)
    assert [c.name for c in ruleset.output_columns][:3] == ["time", "app_time", "level"]
    assert [c.name for c in ruleset.columns if c.internal] == ["origin", "pipe_body"]


def test_shipped_rules_split_an_operation_log():
    ruleset = load_rules(SHIPPED)
    record = parse_line(double_encoded({"log": OP_LOG}), SOURCE, ruleset)
    assert record["level"] == "WARN"
    assert (record["logger"], record["method"], record["src_line"]) == (
        "OperationLogInterceptor", "filter", 44
    )
    # the bare separator after the thread goes, the rest of the message stays
    assert record["message"].startswith("path: /v3/internal/records/getGeneralConsent")
    assert record["message"].endswith("Finished processing request")
    assert record["req_path"] == "/v3/internal/records/getGeneralConsent"
    assert record["req_status_code"] == 404
    assert record["req_duration_ms"] == 114
    assert record["req_user_agent"] == "Apache-HttpClient/4.5.14 (Java/21.0.11)"
    assert record["req_x_header"] == "unknown"
    assert record["op_x_request_id"] == "005056a2-18c4-1fd1-9ed9-5b6297c1a000"
    assert record["has_response_payload"] is True


def test_shipped_rules_ignore_pipe_labels_in_ordinary_prose():
    """497 plain messages mention `x-request-id:`; none of them is a request log."""
    ruleset = load_rules(SHIPPED)
    prose = (
        "2026-07-09 08:25:06 INFO  [Ctx.log:1] (main) "
        "received response code: 200 for x-request-id: abc on path: /v3/x"
    )
    record = parse_line(double_encoded({"log": prose}), SOURCE, ruleset)
    assert record["req_status_code"] is None
    assert record["op_x_request_id"] is None
    assert record["req_path"] is None
    assert record["message"].startswith("received response code: 200")


def test_shipped_rules_derive_service_from_the_container():
    ruleset = load_rules(SHIPPED)

    def service(container):
        envelope = {"log": PLAIN_LOG, "kubernetes": {"container_name": container}}
        return parse_line(double_encoded(envelope), SOURCE, ruleset)["service"]

    assert service("security-gate") == "security-gate"
    assert service("notification-service") == "notification"
    assert service("pu-epa-information-service") == "information"
    assert service("pu-epa-aoknds-ram") == "ram"
    assert service(None) == "unknown"
    assert service("something-else") == "unknown"
    # `-ram` must be a whole trailing segment, not any substring
    assert service("pu-epa-ramp-service") == "unknown"


@pytest.mark.parametrize("prefix", ["ram", "information"])
def test_shipped_rules_prefer_the_envelope_over_the_pipe_fields(prefix):
    ruleset = load_rules(SHIPPED)
    envelope = {
        "log": OP_LOG,
        f"{prefix}_path": "/v3/from-envelope",
        f"{prefix}_status_code": "503",
        f"{prefix}_log_level": "WARN",
    }
    record = parse_line(double_encoded(envelope), SOURCE, ruleset)
    assert record["req_path"] == "/v3/from-envelope"
    assert record["req_status_code"] == 503
    # what the envelope did not carry is still backfilled from the message
    assert record["req_duration_ms"] == 114
    assert record["req_host"] == "pu-epa-aoknds-ram-private.svc.cluster.local:11443"
    # `<prefix>_log_level` repeats `level` and is deliberately not collapsed
    assert not [k for k in record if k.startswith(("ram_", "information_"))]


# --------------------------------------------------------------------------
# `all: true` -- every match, not only the first
# --------------------------------------------------------------------------


def _all_ruleset(tmp_path, rule: str):
    """A two-column ruleset whose second rule is the one under test."""
    path = tmp_path / "rules.yaml"
    path.write_text(
        "columns: [{name: message}, {name: ids}]\n"
        "rules:\n"
        "  - {target: message, from: log}\n"
        f"  {rule}\n",
        encoding="utf-8",
    )
    return load_rules(path)


def test_all_collects_every_match_one_per_line(tmp_path) -> None:
    """`all: true` joins every match with `sep:`, which defaults to a newline."""
    ruleset = _all_ruleset(
        tmp_path, "- {target: ids, from: message, all: true, regex: '(\\d+)'}"
    )
    record = parse_line(json.dumps({"log": "a 1 b 22 c 333"}), "f.log", ruleset)
    assert record["ids"] == "1\n22\n333"


def test_all_with_one_match_is_just_that_match(tmp_path) -> None:
    """A single hit must not gain a separator."""
    ruleset = _all_ruleset(
        tmp_path, "- {target: ids, from: message, all: true, regex: '(\\d+)'}"
    )
    assert parse_line(json.dumps({"log": "only 7"}), "f.log", ruleset)["ids"] == "7"


def test_all_with_no_match_leaves_the_column_null(tmp_path) -> None:
    """No match is not an empty string -- the column stays null."""
    ruleset = _all_ruleset(
        tmp_path, "- {target: ids, from: message, all: true, regex: '(\\d+)'}"
    )
    assert parse_line(json.dumps({"log": "nothing"}), "f.log", ruleset)["ids"] is None


def test_all_honours_an_explicit_sep(tmp_path) -> None:
    """`sep:` overrides the newline default."""
    ruleset = _all_ruleset(
        tmp_path,
        "- {target: ids, from: message, all: true, sep: ', ', regex: '(\\d+)'}",
    )
    record = parse_line(json.dumps({"log": "a 1 b 22"}), "f.log", ruleset)
    assert record["ids"] == "1, 22"


def test_all_without_a_capture_group_collects_whole_matches(tmp_path) -> None:
    """With no group, the whole match is what repeats."""
    ruleset = _all_ruleset(
        tmp_path, "- {target: ids, from: message, all: true, regex: 'ab\\d'}"
    )
    record = parse_line(json.dumps({"log": "ab1 xx ab2"}), "f.log", ruleset)
    assert record["ids"] == "ab1\nab2"


def test_all_still_loses_to_an_earlier_rule(tmp_path) -> None:
    """First non-null wins applies to `all:` rules like any other."""
    path = tmp_path / "rules.yaml"
    path.write_text(
        "columns: [{name: message}, {name: ids}]\n"
        "rules:\n"
        "  - {target: message, from: log}\n"
        "  - {target: ids, from: first}\n"
        "  - {target: ids, from: message, all: true, regex: '(\\d+)'}\n",
        encoding="utf-8",
    )
    ruleset = load_rules(path)
    record = parse_line(json.dumps({"log": "a 1 b 2", "first": "kept"}), "f.log", ruleset)
    assert record["ids"] == "kept"
