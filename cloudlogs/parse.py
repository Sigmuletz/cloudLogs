"""The rule engine: one raw line + a :class:`~cloudlogs.rules.Ruleset` -> one record.

Pure functions over strings and dicts -- no file I/O, no printing and, by
design, **no mapping tables**. Everything this module knows about the shape of
a log line comes from ``rules.yaml`` (PLAN.md 2.1 - 2.10); adding a column or a
second way of filling one is an edit to that file, never to this one.

How one line becomes a record:

1. **Decode** (fixed, PLAN.md 2.7). ``json.loads`` once, and again when the
   result is itself a string. A line that is not JSON -- or that decodes to
   something other than an object -- becomes the working dict
   ``{"message": <raw text>}`` and the rules run over it anyway, so nothing is
   ever dropped.
2. **Run the rules** in file order over that working dict. A rule writes only a
   column that is still empty, so the first rule to produce a non-null value
   wins (PLAN.md 2.4). Sources are resolved against the columns produced so far
   first, then the decoded envelope, walking dotted paths (PLAN.md 2.3).
3. **Cast** every written value to its column's ``type:`` with
   :func:`cloudlogs.rules._cast` -- the same caster that validates ``default:``
   at load time, so a value means the same thing in both places. A value that
   will not cast becomes null and is reported (PLAN.md 2.6).
4. **Emit** the declared non-internal columns in declaration order, then the
   engine's own ``source_file`` and ``parse_ok``, plus ``_raw`` (PLAN.md 2.9).

:func:`parse_line` returns just the record; :func:`parse_record` also returns a
:class:`ParseStatus` (which rules matched) and the cast failures, which is what
the ingest CLI builds its report from.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .rules import ENGINE_COLUMNS, Rule, Ruleset, _cast

__all__ = [
    "ENGINE_COLUMNS",
    "ParseResult",
    "ParseStatus",
    "decode_json_line",
    "parse_line",
    "parse_record",
]


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------


@dataclass
class ParseStatus:
    """What happened to one line: did it decode, and which rules matched.

    ``rule_hits`` holds the *name* of every rule whose source matched -- for a
    ``regex:`` rule that its pattern matched, for a plain ``from:`` rule that
    one of its sources held a value. A rule can hit without writing anything,
    because an earlier rule may already have filled the column; that is the
    distinction the per-rule report in ``ingest.py`` is built on.
    """

    json_ok: bool = False
    rule_hits: set[str] = field(default_factory=set)
    #: rules that actually stored a value -- a subset of ``rule_hits``
    rule_writes: set[str] = field(default_factory=set)
    #: names of the rules marked ``required:`` in the ruleset that produced this
    required: frozenset[str] = frozenset()

    @property
    def parse_ok(self) -> bool:
        """True when the JSON decoded *and* every required rule matched."""
        return self.json_ok and self.required.issubset(self.rule_hits)


@dataclass
class ParseResult:
    """A record, the status that produced it, and any value that would not cast."""

    record: dict[str, Any]
    status: ParseStatus
    #: ``(column, offending value)`` per failed cast, for the ingest summary
    cast_failures: tuple[tuple[str, Any], ...] = ()


# --------------------------------------------------------------------------
# L1 -- decode (fixed, PLAN.md 2.7)
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
# values
# --------------------------------------------------------------------------


def _clean(value: Any) -> Any:
    """Strip a string and turn the placeholders ``""`` and ``"null"`` into None.

    Applied to every value on its way into a column, before casting -- a
    ``null`` that arrived as text is an absent value, not the word.
    """
    if isinstance(value, str):
        text = value.strip()
        if text == "" or text.lower() == "null":
            return None
        return text
    return value


def _resolve(name: str, values: dict[str, Any], envelope: Any) -> Any:
    """Read source ``name`` out of the working namespace (PLAN.md 2.3).

    A column an earlier rule filled shadows a raw key of the same name; a name
    that is not a filled column is looked up in the decoded envelope, first
    whole (so a top-level key may contain a literal dot) and then as a dotted
    path walking nested mappings.
    """
    produced = values.get(name)
    if produced is not None:
        return produced
    node: Any = envelope
    if isinstance(node, dict) and name in node:
        return node[name]
    for part in name.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


class _Writer:
    """Writes cast values into the columns of one record, first non-null wins."""

    def __init__(self, ruleset: Ruleset) -> None:
        self.types = {column.name: column.type for column in ruleset.columns}
        self.maps = {c.name: c.mapping for c in ruleset.columns if c.mapping}
        self.values: dict[str, Any] = {column.name: None for column in ruleset.columns}
        self.failures: list[tuple[str, Any]] = []

    def write(self, column: str, value: Any) -> bool:
        """Clean, cast and store ``value``; False when nothing was stored.

        A column that is already filled keeps what it has -- first non-null
        wins (PLAN.md 2.4) -- and the caller is told so it can tell a rule
        that contributed nothing from one that never matched at all.
        """
        if self.values.get(column) is not None:
            return False
        cleaned = _clean(value)
        if cleaned is None:
            return False
        # fold alternative spellings before casting, so `warning` and `WARN`
        # are one value everywhere downstream (PLAN.md 2.5)
        mapping = self.maps.get(column)
        if mapping:
            folded = mapping.get(str(cleaned).strip().lower())
            if folded is not None:
                self.values[column] = folded
                return True
        ok, cast = _cast(cleaned, self.types.get(column, "str"))
        if not ok:
            self.failures.append((column, cleaned))
            return False
        if cast is None:
            return False
        self.values[column] = cast
        return True


# --------------------------------------------------------------------------
# one rule
# --------------------------------------------------------------------------


def _apply(rule: Rule, writer: _Writer, envelope: Any) -> tuple[bool, bool]:
    """Run one rule; ``(matched, wrote)``.

    The two differ for a rule whose column an earlier rule already filled: it
    matched, but contributed nothing. The report distinguishes them, because
    "never matched" and "always shadowed" are different mistakes.
    """
    if rule.join:
        pieces = [
            _clean(_resolve(source, writer.values, envelope)) for source in rule.join
        ]
        kept = [p if isinstance(p, str) else str(p) for p in pieces if p is not None]
        if not kept:
            return False, False
        wrote = rule.target is not None and writer.write(rule.target, rule.sep.join(kept))
        return True, wrote

    if rule.pattern is not None and rule.all_matches:
        # every match, not only the first, joined with `sep` (PLAN.md 2.2)
        for source in rule.sources:
            text = _resolve(source, writer.values, envelope)
            if not isinstance(text, str):
                continue
            found = [
                match.group(1) if rule.pattern.groups else match.group(0)
                for match in rule.pattern.finditer(text)
            ]
            kept = [value.strip() for value in found if value and value.strip()]
            if not kept:
                continue
            wrote = rule.target is not None and writer.write(
                rule.target, rule.sep.join(kept)
            )
            return True, wrote
        return False, False

    if rule.pattern is not None:
        for source in rule.sources:
            text = _resolve(source, writer.values, envelope)
            if not isinstance(text, str):
                continue
            match = rule.pattern.search(text)
            if match is None:
                continue
            wrote = False
            if rule.target is not None:
                wrote = writer.write(
                    rule.target,
                    match.group(1) if rule.pattern.groups else match.group(0),
                )
            for group in rule.groups:
                captured = match.group(group)
                if captured is not None:  # a group that did not participate
                    wrote = writer.write(group, captured) or wrote
            return True, wrote
        return False, False

    for source in rule.sources:
        value = _resolve(source, writer.values, envelope)
        if _clean(value) is None:
            continue
        wrote = rule.target is not None and writer.write(rule.target, value)
        return True, wrote
    return False, False


# --------------------------------------------------------------------------
# one line
# --------------------------------------------------------------------------


def parse_record(raw: str, source_file: str, ruleset: Ruleset) -> ParseResult:
    """Parse one raw line into a record plus the status that produced it."""
    status = ParseStatus(
        required=frozenset(rule.name for rule in ruleset.rules if rule.required)
    )
    decoded, status.json_ok = decode_json_line(raw)

    if status.json_ok and isinstance(decoded, dict):
        envelope: Any = decoded
        raw_value: Any = decoded
    else:
        # not JSON, or JSON that is not an object: hand the text to the rules
        # as `message` so a plain-text format is a regex rule, not new code.
        text = decoded if isinstance(decoded, str) else raw.strip()
        envelope = {"message": text}
        raw_value = decoded if status.json_ok else raw.rstrip("\n")

    writer = _Writer(ruleset)
    for rule in ruleset.rules:
        matched, wrote = _apply(rule, writer, envelope)
        if matched:
            status.rule_hits.add(rule.name)
        if wrote:
            status.rule_writes.add(rule.name)

    for column in ruleset.columns:
        if writer.values[column.name] is None and column.default is not None:
            writer.values[column.name] = column.default

    record = {column.name: writer.values[column.name] for column in ruleset.output_columns}
    record["source_file"] = source_file
    record["parse_ok"] = status.parse_ok
    record["_raw"] = raw_value
    return ParseResult(record, status, tuple(writer.failures))


def parse_line(raw: str, source_file: str, ruleset: Ruleset) -> dict[str, Any]:
    """Parse one raw log line into a normalized record."""
    return parse_record(raw, source_file, ruleset).record
