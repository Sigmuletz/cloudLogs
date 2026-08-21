"""Pure line -> record parsing for cloudlogs (PLAN.md sections 2.1 - 2.4).

No file I/O, no printing. Everything here is a plain function over strings and
dicts so it can be unit tested in isolation.

Layers (each degrades on its own, a line is never dropped):

    L1  decode_json_line   json.loads once, then again if the result is a str
    L2  envelope fields + flatten_kubernetes
    L3  parse_log_string   regex over the `log` string
    L4  parse_pipe_fields  `key: value | key: value | ...` inside the message

`parse_line` returns just the record; `parse_record` returns the record plus a
`ParseStatus` so the ingest CLI can build its per-layer summary.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

# --------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------

#: every emitted record carries exactly these keys, in this order (missing
#: values are ``None``) so the table/column metadata is stable across files.
FIELDS: tuple[str, ...] = (
    "time",
    "app_time",
    "level",
    "logger",
    "method",
    "src_line",
    "thread",
    "message",
    "service",
    "req_path",
    "req_status_code",
    "req_duration_ms",
    "req_host",
    "req_user_agent",
    "req_x_header",
    "op_x_request_id",
    "k8s_cluster",
    "k8s_namespace",
    "k8s_pod",
    "k8s_container",
    "k8s_pod_hash",
    "k8s_version",
    "k8s_revision",
    "k8s_deployment_id",
    "k8s_instance",
    "node_name",
    "x_trace_id",
    "has_response_payload",
    "source_file",
    "parse_ok",
)

#: envelope keys that carry no information of their own (PLAN.md 2.2)
DROPPED_ENVELOPE_KEYS = frozenset({"_p", "stream", "file", "tag"})

#: kubernetes labels that are single-valued in practice (PLAN.md 2.2)
DROPPED_LABELS = frozenset(
    {
        "app_kubernetes_io/component",
        "app_kubernetes_io/part-of",
        "de_ibm/product-id",
        "helm_sh/chart",
        # duplicates kubernetes.container_name
        "app_kubernetes_io/name",
    }
)

#: `ram_<suffix>` / `information_<suffix>` -> `req_<suffix>` (PLAN.md 2.4).
#: `log_level` is intentionally not collapsed: it always repeats `level`.
PREFIXES = ("ram", "information")
PREFIX_SUFFIXES = (
    "path",
    "status_code",
    "duration_ms",
    "host",
    "user_agent",
    "x_header",
)

#: pipe-field key (lowercased) -> record field (PLAN.md 2.3)
PIPE_FIELD_MAP = {
    "path": "req_path",
    "response status code": "req_status_code",
    "time(ms)": "req_duration_ms",
    "host header": "req_host",
    "user-agent": "req_user_agent",
    "x-forwarded-for header": "req_x_header",
    "x-request-id": "op_x_request_id",
    "hasresponsepayload": "has_response_payload",
}

#: fields that are always integers
INT_FIELDS = frozenset({"src_line", "req_status_code", "req_duration_ms"})
#: fields that are always booleans
BOOL_FIELDS = frozenset({"has_response_payload"})

LOG_PATTERN = re.compile(
    r"^(?P<app_time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+"
    r"(?P<level>\S+)\s+"
    r"\[(?P<origin>[^\]]*)\]\s+"
    r"\((?P<thread>[^)]*)\)\s*"
    r"(?P<message>.*)$",
    re.DOTALL,
)

#: `Logger.method:44`, `pkg.Logger.method:44`, `Logger:44` or just `Logger`
ORIGIN_PATTERN = re.compile(
    r"^(?P<logger>[^:]+?)(?:\.(?P<method>[^.:]+))?(?::(?P<line>\d+))?$"
)

PIPE_FIELD_PATTERN = re.compile(r"^(?P<key>[A-Za-z][A-Za-z0-9 _.()\-]{0,40}):\s?(?P<value>.*)$", re.DOTALL)


@dataclass
class ParseStatus:
    """Which layers succeeded for one line."""

    json_ok: bool = False
    log_ok: bool = False
    pipe_ok: bool = False

    @property
    def parse_ok(self) -> bool:
        """A record is 'ok' when the envelope decoded and the log line matched."""
        return self.json_ok and self.log_ok


@dataclass
class ParseResult:
    """A normalized record plus the per-layer status that produced it."""

    record: dict[str, Any]
    status: ParseStatus


# --------------------------------------------------------------------------
# coercion helpers -- these must never raise
# --------------------------------------------------------------------------


def to_int(value: Any) -> Any:
    """Best-effort int. Returns the original value when it is not int-like."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            try:
                number = float(text)
            except ValueError:
                return value
            return int(number) if number.is_integer() else number
    return value


def to_bool(value: Any) -> Any:
    """Best-effort bool for "true"/"false". Unknown input is returned as-is."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "yes", "1"):
            return True
        if text in ("false", "no", "0"):
            return False
        if text in ("", "null", "none"):
            return None
    return value


def coerce_field(name: str, value: Any) -> Any:
    """Apply the coercion configured for `name` (int / bool / passthrough)."""
    if name in INT_FIELDS:
        return to_int(value)
    if name in BOOL_FIELDS:
        return to_bool(value)
    return value


def _clean(value: Any) -> Any:
    """Normalize placeholder strings ("", "null") to None."""
    if isinstance(value, str):
        text = value.strip()
        if text == "" or text.lower() == "null":
            return None
        return text
    return value


# --------------------------------------------------------------------------
# L1 -- decode
# --------------------------------------------------------------------------


def decode_json_line(raw: str) -> tuple[Any, bool]:
    """Decode a (possibly double-encoded) JSON line.

    Returns ``(decoded, ok)``. ``json.loads`` runs once, and again when the
    result is itself a string. ``ok`` is False when nothing decodes.
    """
    text = raw.strip()
    if not text:
        return None, False
    try:
        decoded = json.loads(text)
    except (ValueError, TypeError):
        return None, False
    if isinstance(decoded, str):
        try:
            decoded = json.loads(decoded)
        except (ValueError, TypeError):
            # a plain JSON string that is not itself JSON: keep the string
            return decoded, True
    return decoded, True


# --------------------------------------------------------------------------
# L2 -- envelope
# --------------------------------------------------------------------------


def flatten_kubernetes(kubernetes: Any) -> dict[str, Any]:
    """Flatten the nested `kubernetes` object into `k8s_*` fields.

    Unknown/absent input yields all-None values; single-valued labels are
    pruned (PLAN.md 2.2).
    """
    flat: dict[str, Any] = {
        "k8s_cluster": None,
        "k8s_namespace": None,
        "k8s_pod": None,
        "k8s_container": None,
        "k8s_pod_hash": None,
        "k8s_version": None,
        "k8s_revision": None,
        "k8s_deployment_id": None,
        "k8s_instance": None,
    }
    if not isinstance(kubernetes, dict):
        return flat
    flat["k8s_cluster"] = _clean(kubernetes.get("cluster_name"))
    flat["k8s_namespace"] = _clean(kubernetes.get("namespace_name"))
    flat["k8s_pod"] = _clean(kubernetes.get("pod_name"))
    flat["k8s_container"] = _clean(kubernetes.get("container_name"))

    labels = kubernetes.get("labels")
    if isinstance(labels, dict):
        keep = {k: v for k, v in labels.items() if k not in DROPPED_LABELS}
        flat["k8s_pod_hash"] = _clean(keep.get("pod-template-hash"))
        flat["k8s_version"] = _clean(keep.get("app_kubernetes_io/version"))
        flat["k8s_revision"] = _clean(keep.get("app_kubernetes_io/revision"))
        flat["k8s_deployment_id"] = _clean(keep.get("de_ibm/deployment-id"))
        flat["k8s_instance"] = _clean(keep.get("app_kubernetes_io/instance"))
    return flat


def collapse_prefixes(envelope: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Collapse `ram_*` / `information_*` envelope keys into `req_*`.

    Returns ``(req_fields, prefix)`` where `prefix` is the prefix that supplied
    the values (``"ram"``, ``"information"`` or ``None``).
    """
    req: dict[str, Any] = {f"req_{suffix}": None for suffix in PREFIX_SUFFIXES}
    found: str | None = None
    for prefix in PREFIXES:
        present = [s for s in PREFIX_SUFFIXES if f"{prefix}_{s}" in envelope]
        if not present:
            continue
        found = found or prefix
        for suffix in present:
            name = f"req_{suffix}"
            if req[name] is None:
                req[name] = coerce_field(name, _clean(envelope[f"{prefix}_{suffix}"]))
    return req, found


def derive_service(prefix: str | None, container: str | None, has_kubernetes: bool) -> str:
    """Service name per PLAN.md 2.4."""
    if prefix:
        return prefix
    if not has_kubernetes:
        return "unknown"
    name = (container or "").lower()
    if "security-gate" in name:
        return "security-gate"
    if "notification" in name:
        return "notification"
    if "information" in name:
        return "information"
    if name.endswith("-ram") or "-ram-" in name:
        return "ram"
    return "unknown"


# --------------------------------------------------------------------------
# L3 -- the `log` string
# --------------------------------------------------------------------------


def parse_log_string(log: Any) -> dict[str, Any] | None:
    """Split a Quarkus log line into its parts, or None when it does not match.

    ``2026-07-09 08:25:06 WARN  [Logger.method:44] (thread) message``
    """
    if not isinstance(log, str):
        return None
    match = LOG_PATTERN.match(log.strip())
    if not match:
        return None
    origin = ORIGIN_PATTERN.match(match.group("origin") or "")
    logger = method = src_line = None
    if origin:
        logger = _clean(origin.group("logger"))
        method = _clean(origin.group("method"))
        src_line = to_int(origin.group("line"))
    message = match.group("message").strip()
    # operation-log lines start with a bare separator: `(thread) | path: ...`
    if message.startswith("|"):
        message = message[1:].strip()
    return {
        "app_time": _clean(match.group("app_time")),
        "level": _clean(match.group("level")),
        "logger": logger,
        "method": method,
        "src_line": src_line,
        "thread": _clean(match.group("thread")),
        "message": message,
    }


# --------------------------------------------------------------------------
# L4 -- pipe fields
# --------------------------------------------------------------------------


def parse_pipe_fields(message: Any) -> dict[str, str]:
    """Extract `key: value | key: value | ...` pairs from a message.

    Only returns something when at least two pipe-separated segments look like
    key/value pairs, so ordinary prose containing a colon is left alone.
    """
    if not isinstance(message, str) or "|" not in message:
        return {}
    fields: dict[str, str] = {}
    for segment in message.split("|"):
        segment = segment.strip()
        if not segment:
            continue
        match = PIPE_FIELD_PATTERN.match(segment)
        if not match:
            continue
        key = match.group("key").strip().lower()
        if key in PIPE_FIELD_MAP:
            fields[key] = match.group("value").strip()
    return fields if len(fields) >= 2 else {}


def apply_pipe_fields(record: dict[str, Any], fields: dict[str, str]) -> None:
    """Backfill `req_*` from pipe fields, in place.

    `x-request-id` always lands in `op_x_request_id`; everything else only
    fills a field that is still empty (PLAN.md 2.3).
    """
    for key, value in fields.items():
        name = PIPE_FIELD_MAP[key]
        cleaned = coerce_field(name, _clean(value))
        if name == "op_x_request_id":
            record[name] = cleaned
        elif record.get(name) is None:
            record[name] = cleaned


# --------------------------------------------------------------------------
# top level
# --------------------------------------------------------------------------


def empty_record(source_file: str) -> dict[str, Any]:
    """A record with every field present and empty."""
    record: dict[str, Any] = {name: None for name in FIELDS}
    record["source_file"] = source_file
    record["parse_ok"] = False
    return record


def parse_record(raw: str, source_file: str) -> ParseResult:
    """Parse one raw line into a record plus its per-layer `ParseStatus`."""
    status = ParseStatus()
    record = empty_record(source_file)
    record["service"] = "unknown"

    decoded, status.json_ok = decode_json_line(raw)
    if not status.json_ok or not isinstance(decoded, dict):
        # L1 failed (or the payload is not an object): keep everything raw.
        record["message"] = decoded if isinstance(decoded, str) else raw.strip()
        record["_raw"] = decoded if status.json_ok else raw.rstrip("\n")
        record["parse_ok"] = status.parse_ok
        return ParseResult(record, status)

    envelope = {k: v for k, v in decoded.items() if k not in DROPPED_ENVELOPE_KEYS}

    # L2 -- envelope
    record["time"] = _clean(envelope.get("time"))
    record["node_name"] = _clean(envelope.get("node_name"))
    record["x_trace_id"] = _clean(envelope.get("x_trace_id"))
    if "has_response_payload" in envelope:
        record["has_response_payload"] = to_bool(_clean(envelope["has_response_payload"]))
    record.update(flatten_kubernetes(envelope.get("kubernetes")))

    req_fields, prefix = collapse_prefixes(envelope)
    record.update(req_fields)
    record["service"] = derive_service(
        prefix, record["k8s_container"], isinstance(envelope.get("kubernetes"), dict)
    )

    # L3 -- the application log line
    log = envelope.get("log")
    parsed = parse_log_string(log)
    if parsed is not None:
        status.log_ok = True
        record.update(parsed)
    elif isinstance(log, str):
        record["message"] = log.strip()
    else:
        record["message"] = raw.strip()

    # L4 -- pipe fields inside the message
    fields = parse_pipe_fields(record["message"])
    if fields:
        status.pipe_ok = True
        apply_pipe_fields(record, fields)

    record["parse_ok"] = status.parse_ok
    record["_raw"] = decoded
    return ParseResult(record, status)


def parse_line(raw: str, source_file: str) -> dict[str, Any]:
    """Parse one raw log line into a normalized record (PLAN.md 2.2)."""
    return parse_record(raw, source_file).record
