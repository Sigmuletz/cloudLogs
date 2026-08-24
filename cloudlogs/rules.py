"""Loading and validation of ``rules.yaml`` (PLAN.md sections 2.1 - 2.8).

This module owns the *declaration* half of the ingest stage: it turns the
YAML file into an immutable :class:`Ruleset` of :class:`Column` and
:class:`Rule` objects. It never looks at a log line -- applying the rules is
the engine's job (``cloudlogs/parse.py``).

Everything is validated before :func:`load_rules` returns, and the **first**
problem aborts with the file, the 1-based line and, where it helps, a caret
block (PLAN.md 2.8)::

    rules.yaml:14: invalid regex in rule 'status'
        regex: 'status code: (\\d+'
                            ^ missing ), unterminated subpattern

A half-applied ruleset is therefore impossible: callers either get a complete,
consistent :class:`Ruleset` with every regex already compiled, or a
:class:`RulesError`.
"""

from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

# --------------------------------------------------------------------------
# schema constants
# --------------------------------------------------------------------------

#: file name looked up next to the project root when nothing else says otherwise
DEFAULT_RULES_FILENAME = "rules.yaml"

#: appended by the engine after the declared columns; not declarable, not
#: writable by a rule (PLAN.md 2.9)
ENGINE_COLUMNS: tuple[str, ...] = ("source_file", "parse_ok")

#: values accepted by a column's ``type:`` (PLAN.md 2.6)
VALID_TYPES: tuple[str, ...] = ("str", "int", "float", "bool", "time")

#: values accepted by a column's ``kind:``; ``None`` means "classify from the
#: data" (PLAN.md 2.11)
VALID_KINDS: tuple[str, ...] = ("facet", "number", "time", "text")

#: keys a ``columns:`` entry may carry
COLUMN_KEYS: frozenset[str] = frozenset(
    {"name", "type", "kind", "label", "default_visible", "default", "internal", "map"}
)

#: keys a ``rules:`` entry may carry (PLAN.md 2.2)
RULE_KEYS: frozenset[str] = frozenset(
    {"name", "target", "from", "regex", "join", "sep", "required", "all"}
)

#: what ``sep:`` defaults to for an ``all: true`` rule -- one match per line
DEFAULT_ALL_SEP = "\n"

#: the project root -- the parent of the ``cloudlogs`` package directory,
#: exactly as ``cloudlogs/main.py`` computes it
BASE_DIR = Path(__file__).resolve().parent.parent

#: environment variable pointing at an alternative rules file (PLAN.md 2.1)
RULES_ENV = "CLOUDLOGS_RULES"

_CARET_INDENT = "    "
_REGEX_PREFIX = _CARET_INDENT + "regex: '"


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


class RulesError(Exception):
    """A rules file that cannot be used, located at ``path`` line ``line``.

    ``caret`` is an already formatted, already indented detail block (the
    offending line plus a ``^`` marker, or a "did you mean" suggestion) shown
    underneath the headline by :meth:`__str__`.
    """

    def __init__(
        self,
        path: Path,
        line: int | None,
        message: str,
        caret: str | None = None,
    ) -> None:
        self.path = Path(path)
        self.line = line
        self.message = message
        self.caret = caret
        super().__init__(str(self))

    def __str__(self) -> str:
        where = self.path.name or str(self.path)
        head = f"{where}:{self.line}: {self.message}" if self.line else f"{where}: {self.message}"
        return f"{head}\n{self.caret}" if self.caret else head


# --------------------------------------------------------------------------
# dataclasses
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Column:
    """One entry of the ``columns:`` block -- the schema of the output."""

    name: str
    type: str = "str"
    kind: str | None = None
    label: str | None = None
    default_visible: bool | None = None
    default: Any = None
    internal: bool = False
    #: alternative spellings folded onto one value (PLAN.md 2.5) -- lowercased
    #: key -> the value to store instead. Applied to whatever a rule wrote,
    #: before casting, so `warning` and `WARN` become one facet value.
    mapping: Mapping[str, Any] = field(default_factory=dict)
    line: int = 0


@dataclass(frozen=True)
class Rule:
    """One entry of the ``rules:`` block -- a single extraction step."""

    name: str
    target: str | None = None
    sources: tuple[str, ...] = ()
    pattern: re.Pattern[str] | None = None
    groups: tuple[str, ...] = ()
    join: tuple[str, ...] = ()
    sep: str = ""
    required: bool = False
    #: collect EVERY match of ``pattern`` instead of only the first, joining
    #: them with ``sep`` (PLAN.md 2.2)
    all_matches: bool = False
    line: int = 0


@dataclass(frozen=True)
class Ruleset:
    """A validated ``rules.yaml``: the schema plus the ordered pipeline."""

    columns: tuple[Column, ...] = ()
    rules: tuple[Rule, ...] = ()
    path: Path = field(default_factory=lambda: Path(DEFAULT_RULES_FILENAME))

    def column(self, name: str) -> Column | None:
        """The declared column called ``name``, or ``None``."""
        for col in self.columns:
            if col.name == name:
                return col
        return None

    @property
    def names(self) -> tuple[str, ...]:
        """Every declared column name, in declaration order."""
        return tuple(col.name for col in self.columns)

    @property
    def output_columns(self) -> tuple[Column, ...]:
        """Declared columns that survive into the record, in order."""
        return tuple(col for col in self.columns if not col.internal)


# --------------------------------------------------------------------------
# YAML loading with line numbers
# --------------------------------------------------------------------------


class _LineDict(dict):
    """A mapping that remembers where it -- and each of its keys -- was written."""

    line: int = 0
    key_lines: dict[str, int]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.key_lines = {}


class _LineList(list):
    """A sequence that remembers the line each of its items started on."""

    lines: tuple[int, ...] = ()


class _RulesLoader(yaml.SafeLoader):
    """``SafeLoader`` that records 1-based line numbers on mappings and lists.

    Without this every validation message would have to say "somewhere in
    rules.yaml", which is worth about as much as no message at all.
    """

    def construct_mapping(self, node: yaml.Node, deep: bool = False) -> _LineDict:
        mapping = _LineDict(super().construct_mapping(node, deep=deep))
        mapping.line = node.start_mark.line + 1
        for key_node, _value_node in node.value:
            key = getattr(key_node, "value", None)
            if isinstance(key, str):
                mapping.key_lines[key] = key_node.start_mark.line + 1
        return mapping

    def construct_yaml_map(self, node: yaml.Node) -> Any:
        data = _LineDict()
        data.line = node.start_mark.line + 1
        yield data
        built = self.construct_mapping(node)
        data.update(built)
        data.key_lines = built.key_lines

    def construct_yaml_seq(self, node: yaml.Node) -> Any:
        data = _LineList()
        data.lines = tuple(child.start_mark.line + 1 for child in node.value)
        yield data
        data.extend(self.construct_sequence(node))


_RulesLoader.add_constructor("tag:yaml.org,2002:map", _RulesLoader.construct_yaml_map)
_RulesLoader.add_constructor("tag:yaml.org,2002:seq", _RulesLoader.construct_yaml_seq)


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def _line_of(obj: Any, key: str | None = None, default: int | None = None) -> int | None:
    """The line of ``key`` inside mapping ``obj``, else the mapping's own line."""
    line = getattr(obj, "line", None)
    if key is not None:
        line = getattr(obj, "key_lines", {}).get(key, line)
    return line if isinstance(line, int) and line > 0 else default


def _item_line(seq: Any, index: int, default: int | None = None) -> int | None:
    """The line item ``index`` of sequence ``seq`` started on."""
    lines = getattr(seq, "lines", ())
    if index < len(lines):
        return lines[index]
    return default


def _suggest(name: str, known: Iterable[str]) -> str | None:
    """A ``did you mean 'x'?`` caret block, phrased as in ``cloudlogs/lucene.py``."""
    near = difflib.get_close_matches(str(name), [str(k) for k in known], n=3, cutoff=0.6)
    if not near:
        return None
    return _CARET_INDENT + "did you mean " + ", ".join(f"{n!r}" for n in near) + "?"


def _typename(value: Any) -> str:
    """A human word for the type of ``value``, for "got X" messages."""
    if value is None:
        return "nothing"
    # isinstance, not type(): the line-tracking loader below subclasses dict
    # and list, and "got _LineList" means nothing to whoever reads the error
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, Mapping):
        return "a mapping"
    if isinstance(value, (list, tuple)):
        return "a list"
    return type(value).__name__


_FRACTION_RE = re.compile(r"(\.\d{6})\d+")


def _cast(value: Any, type_: str) -> tuple[bool, Any]:
    """Cast ``value`` to a column ``type:``; ``(False, None)`` when it will not.

    The single caster in the project (PLAN.md 2.6): it validates a ``default:``
    here at load time and ``cloudlogs.parse`` imports it to cast every value it
    writes, so a column's type means exactly the same thing in both places.

    ``time`` is deliberately a *validator*, not a normalizer -- it accepts a
    ``Z`` suffix, a space date/time separator and a sub-microsecond fraction,
    then returns the original string untouched. Reformatting a timestamp here
    would rewrite what the log actually said.
    """
    if value is None:
        return True, None
    if type_ == "str":
        if isinstance(value, bool):
            return True, "true" if value else "false"
        if isinstance(value, (str, int, float)):
            return True, str(value)
        return False, None
    if type_ in ("int", "float"):
        if isinstance(value, bool):
            return False, None
        if isinstance(value, str):
            text = value.strip()
            try:
                number: float | int = float(text)
            except ValueError:
                return False, None
        elif isinstance(value, (int, float)):
            number = value
        else:
            return False, None
        if type_ == "float":
            return True, float(number)
        if isinstance(number, int) or float(number).is_integer():
            return True, int(number)
        return False, None
    if type_ == "bool":
        if isinstance(value, bool):
            return True, value
        if isinstance(value, int):
            return (True, bool(value)) if value in (0, 1) else (False, None)
        if isinstance(value, str):
            text = value.strip().lower()
            if text in ("true", "yes", "on", "1"):
                return True, True
            if text in ("false", "no", "off", "0"):
                return True, False
        return False, None
    if type_ == "time":
        if isinstance(value, datetime):
            return True, value.isoformat()
        if isinstance(value, date):
            return True, value.isoformat()
        if isinstance(value, str):
            text = value.strip()
            probe = _FRACTION_RE.sub(r"\1", text.replace("Z", "+00:00").replace(" ", "T", 1))
            try:
                datetime.fromisoformat(probe)
            except ValueError:
                return False, None
            return True, text
        return False, None
    return False, None  # pragma: no cover - unreachable, type_ is validated first


# --------------------------------------------------------------------------
# locating the file
# --------------------------------------------------------------------------


def find_rules_path(path: str | os.PathLike[str] | None = None) -> Path:
    """Resolve which rules file to use (PLAN.md 2.1).

    In order: the explicit ``path``, then ``$CLOUDLOGS_RULES``, then
    ``rules.yaml`` next to the project root. A relative ``$CLOUDLOGS_RULES``
    resolves against the project root -- not the working directory -- exactly
    as ``cloudlogs/main.py`` does for ``CLOUDLOGS_INPUT``.
    """
    if path is not None and str(path) != "":
        candidate = Path(path)
        source = "the rules file given on the command line"
    else:
        env = os.environ.get(RULES_ENV, "").strip()
        if env:
            candidate = Path(env)
            if not candidate.is_absolute():
                candidate = BASE_DIR / candidate
            source = f"the rules file named by ${RULES_ENV}"
        else:
            candidate = BASE_DIR / DEFAULT_RULES_FILENAME
            source = "the default rules file"
    if candidate.is_dir():
        raise RulesError(
            candidate, None, f"{source} is a directory, not a file: {candidate}"
        )
    if not candidate.is_file():
        raise RulesError(candidate, None, f"{source} does not exist: {candidate}")
    return candidate


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------


def load_rules(path: str | os.PathLike[str] | None = None) -> Ruleset:
    """Read, validate and compile a rules file into a :class:`Ruleset`.

    Every regex is compiled here, once; nothing downstream ever compiles a
    pattern. Raises :class:`RulesError` on the first problem found.
    """
    rules_path = find_rules_path(path)
    try:
        text = rules_path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - permissions/IO, hard to fake
        raise RulesError(rules_path, None, f"cannot read the rules file: {exc}") from exc

    document = _parse_yaml(text, rules_path)
    columns = _build_columns(document, rules_path)
    rules = _build_rules(document, rules_path, columns)
    return Ruleset(columns=columns, rules=rules, path=rules_path)


def _parse_yaml(text: str, path: Path) -> _LineDict:
    """Parse ``text``, turning a YAML syntax error into a :class:`RulesError`."""
    try:
        document = yaml.load(text, Loader=_RulesLoader)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
        problem = (getattr(exc, "problem", None) or str(exc)).strip()
        context = getattr(exc, "context", None)
        message = f"invalid YAML: {context}, {problem}" if context else f"invalid YAML: {problem}"
        line = mark.line + 1 if mark is not None else None
        caret = None
        if mark is not None:
            source = text.splitlines()
            if 0 <= mark.line < len(source):
                caret = (
                    f"{_CARET_INDENT}{source[mark.line].rstrip()}\n"
                    f"{' ' * (len(_CARET_INDENT) + max(mark.column, 0))}^"
                )
        raise RulesError(path, line, message, caret) from exc

    if document is None:
        raise RulesError(
            path, None, "the rules file is empty; it must declare a 'columns:' block"
        )
    if not isinstance(document, dict):
        raise RulesError(
            path,
            1,
            "the top level of a rules file must be a mapping with a 'columns:' and a "
            f"'rules:' block, got {_typename(document)}",
        )

    unknown = [key for key in document if key not in ("columns", "rules")]
    if unknown:
        key = str(unknown[0])
        raise RulesError(
            path,
            _line_of(document, key, 1),
            f"unknown top-level key {key!r}; a rules file has only 'columns:' and 'rules:'",
            _suggest(key, ("columns", "rules")),
        )
    return document


# --------------------------------------------------------------------------
# columns
# --------------------------------------------------------------------------


def _build_columns(document: Mapping[str, Any], path: Path) -> tuple[Column, ...]:
    """Validate the ``columns:`` block into :class:`Column` objects."""
    if "columns" not in document:
        raise RulesError(
            path, 1, "the rules file has no 'columns:' block; every column must be declared"
        )
    raw = document["columns"]
    if not isinstance(raw, list):
        raise RulesError(
            path,
            _line_of(document, "columns", 1),
            f"'columns:' must be a list of column entries, got {_typename(raw)}",
        )
    if not raw:
        raise RulesError(
            path,
            _line_of(document, "columns", 1),
            "'columns:' is empty; declare at least one column",
        )

    columns: list[Column] = []
    seen: dict[str, int] = {}
    for index, entry in enumerate(raw, start=1):
        line = _item_line(raw, index - 1, _line_of(document, "columns"))
        column = _build_column(entry, index, line, path, seen)
        seen[column.name] = column.line
        columns.append(column)
    return tuple(columns)


def _build_column(
    entry: Any,
    index: int,
    line: int | None,
    path: Path,
    seen: Mapping[str, int],
) -> Column:
    """Validate a single ``columns:`` entry (a mapping, or a bare name)."""
    if isinstance(entry, str):
        entry = {"name": entry}
        keys: Mapping[str, Any] = {}
    elif isinstance(entry, dict):
        keys = entry
    else:
        raise RulesError(
            path,
            line,
            f"column entry {index} must be a mapping or a plain column name, "
            f"got {_typename(entry)}",
        )

    line = _line_of(entry, None, line)

    unknown = [key for key in keys if key not in COLUMN_KEYS]
    if unknown:
        key = str(unknown[0])
        raise RulesError(
            path,
            _line_of(entry, key, line),
            f"column entry {index} has unknown key {key!r}",
            _suggest(key, sorted(COLUMN_KEYS)),
        )

    name = entry.get("name")
    if name is None:
        raise RulesError(path, line, f"column entry {index} has no 'name:'")
    if not isinstance(name, str) or not name.strip():
        raise RulesError(
            path,
            _line_of(entry, "name", line),
            f"column entry {index} has a blank or non-string 'name:', got {_typename(name)}",
        )
    name = name.strip()

    if name in ENGINE_COLUMNS:
        raise RulesError(
            path,
            _line_of(entry, "name", line),
            f"{name!r} is engine-provided and cannot be declared in 'columns:'",
        )
    if name.startswith("_"):
        raise RulesError(
            path,
            _line_of(entry, "name", line),
            f"column {name!r} may not start with '_'; those names are reserved for the engine",
        )
    if name in seen:
        raise RulesError(
            path,
            _line_of(entry, "name", line),
            f"column {name!r} is declared twice; it was already declared on line {seen[name]}",
        )

    type_ = entry.get("type", "str")
    if type_ is None:
        type_ = "str"
    if not isinstance(type_, str) or type_ not in VALID_TYPES:
        raise RulesError(
            path,
            _line_of(entry, "type", line),
            f"column {name!r} has unknown type {type_!r}; use one of "
            + ", ".join(repr(t) for t in VALID_TYPES),
            _suggest(type_, VALID_TYPES) if isinstance(type_, str) else None,
        )

    kind = entry.get("kind")
    if kind is not None and (not isinstance(kind, str) or kind not in VALID_KINDS):
        raise RulesError(
            path,
            _line_of(entry, "kind", line),
            f"column {name!r} has unknown kind {kind!r}; use one of "
            + ", ".join(repr(k) for k in VALID_KINDS),
            _suggest(kind, VALID_KINDS) if isinstance(kind, str) else None,
        )

    label = entry.get("label")
    if label is not None and not isinstance(label, str):
        raise RulesError(
            path,
            _line_of(entry, "label", line),
            f"column {name!r} has a non-string 'label:', got {_typename(label)}",
        )

    default_visible = entry.get("default_visible")
    if default_visible is not None and not isinstance(default_visible, bool):
        raise RulesError(
            path,
            _line_of(entry, "default_visible", line),
            f"column {name!r} has a non-boolean 'default_visible:', "
            f"got {_typename(default_visible)}",
        )

    internal = entry.get("internal", False)
    if internal is None:
        internal = False
    if not isinstance(internal, bool):
        raise RulesError(
            path,
            _line_of(entry, "internal", line),
            f"column {name!r} has a non-boolean 'internal:', got {_typename(internal)}",
        )

    mapping: dict[str, Any] = {}
    if "map" in entry:
        raw_map = entry["map"]
        if not isinstance(raw_map, dict) or not raw_map:
            raise RulesError(
                path,
                _line_of(entry, "map", line),
                f"column {name!r} has a 'map:' that is not a non-empty mapping, "
                f"got {_typename(raw_map)}",
            )
        for key, value in raw_map.items():
            if not isinstance(key, (str, int, float, bool)) or str(key).strip() == "":
                raise RulesError(
                    path,
                    _line_of(entry, "map", line),
                    f"column {name!r} has a blank or non-scalar key in 'map:', "
                    f"got {_typename(key)}",
                )
            folded = str(key).strip().lower()
            if folded in mapping:
                raise RulesError(
                    path,
                    _line_of(entry, "map", line),
                    f"column {name!r} maps {key!r} twice; keys are matched "
                    "case-insensitively, so they must differ by more than case",
                )
            ok, cast = _cast(value, type_)
            if not ok:
                raise RulesError(
                    path,
                    _line_of(entry, "map", line),
                    f"column {name!r} maps {key!r} to {value!r}, which does not "
                    f"cast to {type_}",
                )
            mapping[folded] = cast

    default = entry.get("default")
    ok, cast = _cast(default, type_)
    if not ok:
        raise RulesError(
            path,
            _line_of(entry, "default", line),
            f"column {name!r} has a default of {default!r}, which does not cast to {type_}",
        )

    return Column(
        name=name,
        type=type_,
        kind=kind,
        label=label,
        default_visible=default_visible,
        default=cast,
        internal=internal,
        mapping=mapping,
        line=line or 0,
    )


# --------------------------------------------------------------------------
# rules
# --------------------------------------------------------------------------


def _build_rules(
    document: Mapping[str, Any], path: Path, columns: Sequence[Column]
) -> tuple[Rule, ...]:
    """Validate the ``rules:`` block into compiled :class:`Rule` objects."""
    raw = document.get("rules")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise RulesError(
            path,
            _line_of(document, "rules", 1),
            f"'rules:' must be a list of rule entries, got {_typename(raw)}",
        )

    declared = [col.name for col in columns]
    rules: list[Rule] = []
    for index, entry in enumerate(raw, start=1):
        line = _item_line(raw, index - 1, _line_of(document, "rules"))
        rules.append(_build_rule(entry, index, line, path, declared))
    return tuple(rules)


def _label(entry: Any, index: int, *, use_target: bool = True) -> str:
    """How a message refers to a rule: its name, its target, else ``rule 12``.

    ``use_target=False`` for a message that already quotes the target, so it
    does not read "rule 'x' targets unknown column 'x'".
    """
    keys = ("name", "target") if use_target else ("name",)
    if isinstance(entry, dict):
        for key in keys:
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                return f"rule {value.strip()!r}"
    return f"rule {index}"


def _string_list(
    value: Any,
    key: str,
    entry: Any,
    line: int | None,
    path: Path,
    label: str,
) -> tuple[str, ...]:
    """Normalize ``from:``/``join:`` -- a string or a list of strings -- to a tuple."""
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list):
        raise RulesError(
            path,
            _line_of(entry, key, line),
            f"{label} has a {key!r} that is neither a source name nor a list of source "
            f"names, got {_typename(value)}",
        )
    if not items:
        raise RulesError(
            path, _line_of(entry, key, line), f"{label} has an empty {key!r} list"
        )
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise RulesError(
                path,
                _line_of(entry, key, line),
                f"{label} has a {key!r} entry that is not a non-empty source name, "
                f"got {item!r}",
            )
    return tuple(item.strip() for item in items)


def _check_writable(
    name: str,
    what: str,
    declared: Sequence[str],
    entry: Any,
    key: str,
    line: int | None,
    path: Path,
    label: str,
) -> None:
    """A rule may only write a declared, non-engine column (PLAN.md 2.9)."""
    if name in ENGINE_COLUMNS:
        raise RulesError(
            path,
            _line_of(entry, key, line),
            f"{label} writes {name!r}, but {name!r} is engine-provided, not writable",
        )
    if name not in declared:
        raise RulesError(
            path,
            _line_of(entry, key, line),
            f"{label} {what} unknown column {name!r}",
            _suggest(name, declared),
        )


def _build_rule(
    entry: Any,
    index: int,
    line: int | None,
    path: Path,
    declared: Sequence[str],
) -> Rule:
    """Validate and compile a single ``rules:`` entry."""
    if not isinstance(entry, dict):
        raise RulesError(
            path,
            line,
            f"rule {index} must be a mapping of rule keys, got {_typename(entry)}",
        )
    line = _line_of(entry, None, line)
    label = _label(entry, index)

    unknown = [key for key in entry if key not in RULE_KEYS]
    if unknown:
        key = str(unknown[0])
        raise RulesError(
            path,
            _line_of(entry, key, line),
            f"{label} has unknown key {key!r}",
            _suggest(key, sorted(RULE_KEYS)),
        )

    name = entry.get("name")
    if name is not None and (not isinstance(name, str) or not name.strip()):
        raise RulesError(
            path,
            _line_of(entry, "name", line),
            f"rule {index} has a blank or non-string 'name:', got {_typename(name)}",
        )

    target = entry.get("target")
    if target is not None and (not isinstance(target, str) or not target.strip()):
        raise RulesError(
            path,
            _line_of(entry, "target", line),
            f"{label} has a blank or non-string 'target:', got {_typename(target)}",
        )
    if isinstance(target, str):
        target = target.strip()

    required = entry.get("required", False)
    if required is None:
        required = False
    if not isinstance(required, bool):
        raise RulesError(
            path,
            _line_of(entry, "required", line),
            f"{label} has a non-boolean 'required:', got {_typename(required)}",
        )

    all_matches = entry.get("all", False)
    if all_matches is None:
        all_matches = False
    if not isinstance(all_matches, bool):
        raise RulesError(
            path,
            _line_of(entry, "all", line),
            f"{label} has a non-boolean 'all:', got {_typename(all_matches)}",
        )

    sep = entry.get("sep", DEFAULT_ALL_SEP if all_matches else "")
    if sep is None:
        sep = ""
    if not isinstance(sep, str):
        raise RulesError(
            path,
            _line_of(entry, "sep", line),
            f"{label} has a non-string 'sep:', got {_typename(sep)}",
        )

    join: tuple[str, ...] = ()
    if "join" in entry:
        join = _string_list(entry["join"], "join", entry, line, path, label)
        if "from" in entry or "regex" in entry:
            other = "from" if "from" in entry else "regex"
            raise RulesError(
                path,
                _line_of(entry, "join", line),
                f"{label} combines 'join:' with {other!r}; a join rule takes neither "
                "'from:' nor 'regex:'",
            )
        if len(join) < 2:
            raise RulesError(
                path,
                _line_of(entry, "join", line),
                f"{label} has a 'join:' of {len(join)} source; a join needs at least two",
            )

    sources: tuple[str, ...] = ()
    if "from" in entry:
        sources = _string_list(entry["from"], "from", entry, line, path, label)

    pattern: re.Pattern[str] | None = None
    groups: tuple[str, ...] = ()
    if "regex" in entry:
        raw_regex = entry["regex"]
        if not isinstance(raw_regex, str) or not raw_regex:
            raise RulesError(
                path,
                _line_of(entry, "regex", line),
                f"{label} has a blank or non-string 'regex:', got {_typename(raw_regex)}",
            )
        if not sources:
            raise RulesError(
                path,
                _line_of(entry, "regex", line),
                f"{label} has a 'regex:' but no 'from:'; a regex needs a source to read",
            )
        try:
            pattern = re.compile(raw_regex)
        except re.error as exc:
            offset = exc.pos if isinstance(getattr(exc, "pos", None), int) else 0
            caret = (
                f"{_REGEX_PREFIX}{raw_regex}'\n"
                f"{' ' * (len(_REGEX_PREFIX) + offset)}^ {exc.msg}"
            )
            raise RulesError(
                path,
                _line_of(entry, "regex", line),
                f"invalid regex in {label}",
                caret,
            ) from exc
        groups = tuple(sorted(pattern.groupindex, key=lambda g: pattern.groupindex[g]))

    if all_matches:
        if join:
            raise RulesError(
                path,
                _line_of(entry, "all", line),
                f"{label} combines 'all:' with 'join:'; 'join' concatenates "
                "sources, 'all' concatenates matches -- use one or the other",
            )
        if pattern is None:
            raise RulesError(
                path,
                _line_of(entry, "all", line),
                f"{label} has 'all: true' but no 'regex:'; there is nothing to "
                "match repeatedly",
            )
        if groups:
            raise RulesError(
                path,
                _line_of(entry, "all", line),
                f"{label} combines 'all: true' with named groups; it is undefined "
                "which group repeats -- use a single unnamed group",
            )
        if pattern.groups > 1:
            raise RulesError(
                path,
                _line_of(entry, "all", line),
                f"{label} has 'all: true' and {pattern.groups} capture groups; "
                "collecting every match needs exactly one, or none",
            )

    if target is None and not groups and not join:
        raise RulesError(
            path,
            line,
            f"{label} writes nothing: it needs a 'target:', named groups in its "
            "'regex:', or a 'join:'",
        )
    if target is not None and not join and not sources:
        raise RulesError(
            path,
            _line_of(entry, "target", line),
            f"{label} has a 'target:' but no 'from:' or 'join:' to read from",
        )

    plain = _label(entry, index, use_target=False)
    if target is not None:
        _check_writable(target, "targets", declared, entry, "target", line, path, plain)
    for group in groups:
        _check_writable(
            group, "has a named group for", declared, entry, "regex", line, path, plain
        )

    if isinstance(name, str):
        rule_name = name.strip()
    elif target is not None:
        rule_name = target
    else:
        rule_name = "+".join(groups)

    return Rule(
        name=rule_name,
        target=target,
        sources=sources,
        pattern=pattern,
        groups=groups,
        join=join,
        sep=sep,
        required=required,
        all_matches=all_matches,
        line=line or 0,
    )
