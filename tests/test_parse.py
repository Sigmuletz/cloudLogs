"""Tests for the ingest stage (PLAN.md section 6, `tests/test_parse.py`)."""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # run without installing the package
    sys.path.insert(0, str(ROOT))

from cloudlogs.ingest import build_columns, expand_inputs, ingest, is_stale  # noqa: E402
from cloudlogs.parse import (  # noqa: E402
    collapse_prefixes,
    decode_json_line,
    derive_service,
    flatten_kubernetes,
    parse_line,
    parse_log_string,
    parse_pipe_fields,
    parse_record,
    to_bool,
    to_int,
)

SOURCE = "test.log"

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

KUBERNETES = {
    "cluster_name": "epa-clstr-prod-be-z1",
    "container_name": "pu-epa-aoknds-ram",
    "namespace_name": "pu-epa-system-aoknds-ram",
    "pod_name": "pu-epa-aoknds-ram-5b69569657-7829g",
    "labels": {
        "app_kubernetes_io/component": "BE",
        "app_kubernetes_io/instance": "muc-pu-epa-aoknds-ram",
        "app_kubernetes_io/name": "pu-epa-aoknds-ram",
        "app_kubernetes_io/part-of": "ePA-AS",
        "app_kubernetes_io/revision": "v-4.31.159",
        "app_kubernetes_io/version": "ram-epa-3.1.3-32",
        "de_ibm/deployment-id": "E1241",
        "de_ibm/product-id": "P12",
        "helm_sh/chart": "record-account-management-3.1.3-32-epa-ram",
        "pod-template-hash": "5b69569657",
    },
}


def envelope(**overrides) -> dict:
    base = {
        "_p": "F",
        "stream": "stdout",
        "file": "/var/log/containers/pu-epa-aoknds-ram.log",
        "tag": "kube.pu-epa-system-aoknds-ram.pod.container",
        "time": "2026-07-09T08:25:06.166782072+00:00",
        "node_name": "epa-be-prod-z1-cmpt1-c02",
        "kubernetes": KUBERNETES,
        "log": PLAIN_LOG,
    }
    base.update(overrides)
    return base


def double_encoded(env: dict) -> str:
    return json.dumps(json.dumps(env))


# --------------------------------------------------------------------------
# L1 -- double decode
# --------------------------------------------------------------------------


def test_decode_json_line_double_encoded():
    decoded, ok = decode_json_line(double_encoded(envelope()))
    assert ok is True
    assert decoded["log"] == PLAIN_LOG


def test_decode_json_line_plain_json():
    decoded, ok = decode_json_line(json.dumps(envelope()))
    assert ok is True
    assert decoded["time"] == "2026-07-09T08:25:06.166782072+00:00"


def test_decode_json_line_rejects_garbage():
    assert decode_json_line("not json at all") == (None, False)
    assert decode_json_line("   ") == (None, False)


def test_both_encodings_produce_the_same_record():
    single = parse_line(json.dumps(envelope()), SOURCE)
    double = parse_line(double_encoded(envelope()), SOURCE)
    assert single == double
    assert single["parse_ok"] is True


# --------------------------------------------------------------------------
# L2 -- envelope flatten + label pruning
# --------------------------------------------------------------------------


def test_flatten_kubernetes_keeps_informative_labels():
    flat = flatten_kubernetes(KUBERNETES)
    assert flat == {
        "k8s_cluster": "epa-clstr-prod-be-z1",
        "k8s_namespace": "pu-epa-system-aoknds-ram",
        "k8s_pod": "pu-epa-aoknds-ram-5b69569657-7829g",
        "k8s_container": "pu-epa-aoknds-ram",
        "k8s_pod_hash": "5b69569657",
        "k8s_version": "ram-epa-3.1.3-32",
        "k8s_revision": "v-4.31.159",
        "k8s_deployment_id": "E1241",
        "k8s_instance": "muc-pu-epa-aoknds-ram",
    }


def test_envelope_noise_is_dropped_but_kept_in_raw():
    record = parse_line(double_encoded(envelope(x_trace_id="c074e9a7-8336")), SOURCE)
    for dropped in ("_p", "stream", "file", "tag", "component", "part-of", "helm_sh/chart"):
        assert dropped not in record
    assert record["time"] == "2026-07-09T08:25:06.166782072+00:00"
    assert record["node_name"] == "epa-be-prod-z1-cmpt1-c02"
    assert record["x_trace_id"] == "c074e9a7-8336"
    assert record["source_file"] == SOURCE
    # _raw keeps the untouched decoded original
    assert record["_raw"]["_p"] == "F"
    assert record["_raw"]["kubernetes"]["labels"]["helm_sh/chart"].startswith("record-")


def test_missing_kubernetes_gives_null_columns_and_unknown_service():
    env = envelope()
    del env["kubernetes"]
    record = parse_line(double_encoded(env), SOURCE)
    assert record["service"] == "unknown"
    assert record["k8s_namespace"] is None
    assert record["k8s_pod"] is None
    assert record["k8s_container"] is None
    assert record["parse_ok"] is True


# --------------------------------------------------------------------------
# L3 -- the `log` string
# --------------------------------------------------------------------------


def test_parse_log_string_fields():
    parsed = parse_log_string(OP_LOG)
    assert parsed["app_time"] == "2026-07-09 08:25:06"
    assert parsed["level"] == "WARN"
    assert parsed["logger"] == "OperationLogInterceptor"
    assert parsed["method"] == "filter"
    assert parsed["src_line"] == 44
    assert parsed["thread"] == "executor-thread-44978"
    # the bare separator after the thread is dropped, the rest is untouched
    assert parsed["message"].startswith("path: /v3/internal/records/getGeneralConsent")
    assert parsed["message"].endswith("Finished processing request")


def test_parse_log_string_plain_message():
    parsed = parse_log_string(PLAIN_LOG)
    assert (parsed["logger"], parsed["method"], parsed["src_line"]) == (
        "KTRClientService",
        "getGeneralConsent",
        100,
    )
    assert parsed["message"] == "Fetching general consent from KTR"


def test_parse_log_string_dotted_logger_and_constructor():
    parsed = parse_log_string("2026-07-09 08:25:06 INFO  [de.epa.RequestContext.<init>:85] (main) hi")
    assert parsed["logger"] == "de.epa.RequestContext"
    assert parsed["method"] == "<init>"
    assert parsed["src_line"] == 85


def test_parse_log_string_rejects_other_shapes():
    assert parse_log_string("just some text") is None
    assert parse_log_string(None) is None


# --------------------------------------------------------------------------
# 2.4 -- prefix collapse and service
# --------------------------------------------------------------------------


def test_collapse_prefixes_ram():
    req, prefix = collapse_prefixes(
        {
            "ram_log_level": "WARN",
            "ram_path": "/v3/x",
            "ram_status_code": "404",
            "ram_duration_ms": "114",
            "ram_host": "host:11443",
            "ram_user_agent": "Apache-HttpClient/4.5.14",
            "ram_x_header": "unknown",
        }
    )
    assert prefix == "ram"
    assert req["req_path"] == "/v3/x"
    assert req["req_status_code"] == 404
    assert req["req_duration_ms"] == 114
    assert req["req_host"] == "host:11443"
    assert req["req_x_header"] == "unknown"
    # log_level always repeats `level` and is not collapsed
    assert "req_log_level" not in req


def test_collapse_prefixes_information():
    req, prefix = collapse_prefixes({"information_path": "/v3/y", "information_status_code": "202"})
    assert prefix == "information"
    assert (req["req_path"], req["req_status_code"]) == ("/v3/y", 202)


def test_collapse_prefixes_absent():
    req, prefix = collapse_prefixes({"log": "x"})
    assert prefix is None
    assert set(req.values()) == {None}


@pytest.mark.parametrize(
    "prefix,container,has_k8s,expected",
    [
        ("ram", "security-gate", True, "ram"),
        ("information", "pu-epa-aokby-ram", True, "information"),
        (None, "security-gate", True, "security-gate"),
        (None, "notification-service", True, "notification"),
        (None, "pu-epa-information-service", True, "information"),
        (None, "pu-epa-aoknds-ram", True, "ram"),
        (None, None, False, "unknown"),
        (None, "something-else", True, "unknown"),
    ],
)
def test_derive_service(prefix, container, has_k8s, expected):
    assert derive_service(prefix, container, has_k8s) == expected


def test_prefixed_keys_are_gone_from_the_record():
    env = envelope(log=OP_LOG, ram_path="/v3/from-envelope", ram_status_code="404")
    record = parse_line(double_encoded(env), SOURCE)
    assert not [k for k in record if k.startswith(("ram_", "information_"))]
    assert record["service"] == "ram"
    assert record["req_path"] == "/v3/from-envelope"


# --------------------------------------------------------------------------
# L4 -- pipe fields and backfill precedence
# --------------------------------------------------------------------------


def test_parse_pipe_fields_extraction():
    fields = parse_pipe_fields(parse_log_string(OP_LOG)["message"])
    assert fields["path"] == "/v3/internal/records/getGeneralConsent"
    assert fields["response status code"] == "404"
    assert fields["time(ms)"] == "114"
    assert fields["x-request-id"] == "005056a2-18c4-1fd1-9ed9-5b6297c1a000"
    assert fields["x-forwarded-for header"] == "unknown"
    assert fields["hasresponsepayload"] == "true"


def test_parse_pipe_fields_ignores_prose():
    prose = "Starting to process for request on path: /v3/x, with x-useragent: null"
    assert parse_pipe_fields(prose) == {}
    assert parse_pipe_fields("no pipes here") == {}


def test_pipe_fields_backfill_only_missing_twins():
    env = envelope(
        log=OP_LOG,
        ram_path="/v3/from-envelope",
        ram_status_code="503",
        ram_user_agent="Apache-HttpClient/4.5.14",
    )
    record = parse_line(double_encoded(env), SOURCE)
    # prefixed twins win ...
    assert record["req_path"] == "/v3/from-envelope"
    assert record["req_status_code"] == 503
    assert record["req_user_agent"] == "Apache-HttpClient/4.5.14"
    # ... the rest is backfilled from the pipe fields
    assert record["req_duration_ms"] == 114
    assert record["req_host"] == "pu-epa-aoknds-ram-private.svc.cluster.local:11443"
    assert record["req_x_header"] == "unknown"
    # x-request-id always lands in op_x_request_id
    assert record["op_x_request_id"] == "005056a2-18c4-1fd1-9ed9-5b6297c1a000"
    # message keeps its full original text
    assert record["message"].count("|") == 9
    assert record["message"].endswith("Finished processing request")


def test_pipe_fields_fill_everything_when_no_prefix_present():
    record = parse_line(double_encoded(envelope(log=OP_LOG)), SOURCE)
    assert record["req_path"] == "/v3/internal/records/getGeneralConsent"
    assert record["req_status_code"] == 404
    assert record["req_duration_ms"] == 114
    assert record["req_user_agent"] == "Apache-HttpClient/4.5.14 (Java/21.0.11)"
    assert record["has_response_payload"] is True
    # no prefix -> service comes from the container name
    assert record["service"] == "ram"


def test_pipe_layer_reported_only_for_operation_logs():
    assert parse_record(double_encoded(envelope(log=OP_LOG)), SOURCE).status.pipe_ok is True
    assert parse_record(double_encoded(envelope()), SOURCE).status.pipe_ok is False


# --------------------------------------------------------------------------
# type coercion
# --------------------------------------------------------------------------


def test_type_coercion_values():
    assert to_int("404") == 404
    assert to_int(404) == 404
    assert to_int("12.0") == 12
    assert to_bool("true") is True
    assert to_bool("false") is False
    assert to_bool(True) is True


def test_type_coercion_never_raises():
    for value in ("abc", "", None, [], {}, object(), "12abc"):
        to_int(value)
        to_bool(value)
    assert to_int("abc") == "abc"
    assert to_int("") is None
    assert to_int(None) is None
    assert to_bool("maybe") == "maybe"
    assert to_bool("null") is None


def test_coercion_in_record():
    env = envelope(log=OP_LOG, has_response_payload="true", ram_status_code="404", ram_duration_ms="114")
    record = parse_line(double_encoded(env), SOURCE)
    assert record["req_status_code"] == 404
    assert record["req_duration_ms"] == 114
    assert record["src_line"] == 44
    assert record["has_response_payload"] is True


def test_bad_numbers_do_not_break_the_row():
    env = envelope(log=OP_LOG, ram_status_code="n/a", ram_duration_ms="")
    record = parse_line(double_encoded(env), SOURCE)
    assert record["req_status_code"] == "n/a"
    # empty prefixed value leaves room for the pipe backfill
    assert record["req_duration_ms"] == 114
    assert record["parse_ok"] is True


# --------------------------------------------------------------------------
# malformed input -- never drop a row
# --------------------------------------------------------------------------


def test_not_json_still_yields_a_row():
    result = parse_record("this is not json", SOURCE)
    record = result.record
    assert record["parse_ok"] is False
    assert result.status.json_ok is False
    assert record["message"] == "this is not json"
    assert record["_raw"] == "this is not json"
    assert record["service"] == "unknown"
    assert record["source_file"] == SOURCE


def test_json_without_log_still_yields_a_row():
    env = envelope()
    del env["log"]
    result = parse_record(double_encoded(env), SOURCE)
    assert result.status.json_ok is True
    assert result.status.log_ok is False
    assert result.record["parse_ok"] is False
    # envelope data is still salvaged
    assert result.record["k8s_namespace"] == "pu-epa-system-aoknds-ram"
    assert result.record["time"] == "2026-07-09T08:25:06.166782072+00:00"
    assert result.record["message"]


def test_log_not_matching_the_pattern_keeps_the_text():
    result = parse_record(double_encoded(envelope(log="segfault at 0xdeadbeef")), SOURCE)
    assert result.status.json_ok is True
    assert result.status.log_ok is False
    assert result.record["parse_ok"] is False
    assert result.record["message"] == "segfault at 0xdeadbeef"
    assert result.record["level"] is None
    # the envelope layer still worked
    assert result.record["k8s_pod"] == "pu-epa-aoknds-ram-5b69569657-7829g"


def test_every_record_has_the_same_keys():
    records = [
        parse_line(double_encoded(envelope()), SOURCE),
        parse_line("garbage", SOURCE),
        parse_line(double_encoded(envelope(log=OP_LOG, ram_path="/x")), SOURCE),
    ]
    assert {frozenset(r) for r in records} == {frozenset(records[0])}


# --------------------------------------------------------------------------
# column metadata
# --------------------------------------------------------------------------


def test_build_columns_kinds_and_defaults():
    records = [parse_line(double_encoded(envelope(log=OP_LOG, ram_status_code="404")), SOURCE)]
    columns = {c["name"]: c for c in build_columns(records)}
    assert "_raw" not in columns
    assert columns["time"]["kind"] == "time"
    assert columns["app_time"]["kind"] == "time"
    assert columns["level"]["kind"] == "facet"
    # numeric but low-cardinality -> still a checkbox facet
    assert columns["req_status_code"]["kind"] == "facet"
    assert columns["req_status_code"]["numeric"] is True
    assert columns["level"]["distinct"] == 1
    visible = {c["name"] for c in build_columns(records) if c["default_visible"]}
    assert visible == {
        "time",
        "level",
        "service",
        "k8s_namespace",
        "logger",
        "req_status_code",
        "message",
    }
    filters = {c["name"] for c in build_columns(records) if c["default_filter"]}
    assert filters == {"level", "logger", "service", "k8s_namespace", "req_status_code"}


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


def test_is_stale(tmp_path):
    source = tmp_path / "a.log"
    source.write_text(double_encoded(envelope()) + "\n")
    out = tmp_path / "data" / "logs.json"
    assert is_stale(out, [source]) is True
    ingest([source], out)
    assert is_stale(out, [source]) is False
    import os

    future = out.stat().st_mtime + 10
    os.utime(source, (future, future))
    assert is_stale(out, [source]) is True


# --------------------------------------------------------------------------
# integration -- the real example file
# --------------------------------------------------------------------------


def test_ingest_example_logs(tmp_path):
    out = tmp_path / "logs.json"
    summary = ingest([ROOT / "example" / "logs.log"], out)

    assert summary["lines"] == 564
    assert summary["records"] == 564
    assert summary["json_ok"] == 564
    assert summary["log_ok"] == 564
    assert summary["pipe_ok"] == 51
    assert summary["failed"] == 0

    records = json.loads(out.read_text())
    assert len(records) == 564
    assert all(r["parse_ok"] for r in records)

    levels = collections.Counter(r["level"] for r in records)
    assert levels == {"DEBUG": 355, "INFO": 161, "WARN": 47, "ERROR": 1}

    keys = {k for r in records for k in r}
    assert not [k for k in keys if k.startswith(("ram_", "information_"))]
    assert {"req_path", "req_status_code", "req_duration_ms", "req_host"} <= keys
    assert sum(1 for r in records if r["req_status_code"] is not None) == 51
    assert collections.Counter(r["service"] for r in records)["ram"] > 0

    columns = json.loads((tmp_path / "columns.json").read_text())
    assert {c["name"] for c in columns} == keys - {"_raw"}
    assert all(set(c) == {
        "name", "kind", "label", "distinct", "numeric", "default_visible", "default_filter"
    } for c in columns)


def test_numeric_column_becomes_number_only_above_the_facet_cutoff():
    """Low-cardinality numerics stay checkbox facets; wide ones get min/max."""
    from cloudlogs.ingest import FACET_MAX_NUMERIC_DISTINCT, classify

    few = list(range(FACET_MAX_NUMERIC_DISTINCT))
    many = list(range(FACET_MAX_NUMERIC_DISTINCT + 1))
    assert classify("req_status_code", few)[0] == "facet"
    assert classify("req_duration_ms", many)[0] == "number"
