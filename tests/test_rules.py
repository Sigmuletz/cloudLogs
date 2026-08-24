"""Tests for the rules loader (PLAN.md section 2, `cloudlogs/rules.py`).

Every validation error listed in PLAN.md 2.8 gets a test that asserts both the
message and the 1-based line number it points at -- a rules error with the
wrong line is worth no more than no error at all.
"""

from __future__ import annotations

import re
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # run without installing the package
    sys.path.insert(0, str(ROOT))

from cloudlogs.rules import (  # noqa: E402
    DEFAULT_RULES_FILENAME,
    ENGINE_COLUMNS,
    VALID_TYPES,
    Column,
    Rule,
    RulesError,
    Ruleset,
    find_rules_path,
    load_rules,
)

VALID = """\
columns:
  - {name: time, type: time}
  - level
  - {name: message, kind: text, label: Message, default_visible: false}
  - {name: req_status_code, type: int, default: 0}
  - {name: origin, internal: true}
  - {name: pod_ref}
  - k8s_namespace
  - k8s_pod
rules:
  - name: log-line
    required: true
    from: log
    regex: '^(?P<level>\\S+) \\[(?P<origin>[^\\]]*)\\] (?P<message>.*)$'

  - target: req_status_code
    from: [ram_status_code, information_status_code]

  - target: pod_ref
    join: [k8s_namespace, k8s_pod]
    sep: '/'
"""


def write(tmp_path: Path, text: str, name: str = DEFAULT_RULES_FILENAME) -> Path:
    """Write an inline rules file and return its path."""
    path = tmp_path / name
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def fails(tmp_path: Path, text: str) -> RulesError:
    """Load a rules file that must not load, and return the error."""
    with pytest.raises(RulesError) as excinfo:
        load_rules(write(tmp_path, text))
    return excinfo.value


# --------------------------------------------------------------------------
# a valid file round-trips into the dataclasses
# --------------------------------------------------------------------------


def test_valid_file_builds_the_expected_columns(tmp_path):
    ruleset = load_rules(write(tmp_path, VALID))
    assert isinstance(ruleset, Ruleset)
    assert ruleset.columns[0] == Column(name="time", type="time", line=2)
    assert ruleset.columns[2] == Column(
        name="message",
        type="str",
        kind="text",
        label="Message",
        default_visible=False,
        line=4,
    )
    assert ruleset.columns[3] == Column(
        name="req_status_code", type="int", default=0, line=5
    )
    assert ruleset.columns[4] == Column(name="origin", internal=True, line=6)


def test_valid_file_builds_the_expected_rules(tmp_path):
    ruleset = load_rules(write(tmp_path, VALID))
    first, second, third = ruleset.rules

    assert first.name == "log-line"
    assert first.required is True
    assert first.target is None
    assert first.sources == ("log",)
    assert first.groups == ("level", "origin", "message")
    assert isinstance(first.pattern, re.Pattern)
    assert first.line == 11

    assert second == Rule(
        name="req_status_code",
        target="req_status_code",
        sources=("ram_status_code", "information_status_code"),
        line=16,
    )

    assert third == Rule(
        name="pod_ref",
        target="pod_ref",
        join=("k8s_namespace", "k8s_pod"),
        sep="/",
        line=19,
    )


def test_ruleset_helpers(tmp_path):
    ruleset = load_rules(write(tmp_path, VALID))
    assert ruleset.names == (
        "time",
        "level",
        "message",
        "req_status_code",
        "origin",
        "pod_ref",
        "k8s_namespace",
        "k8s_pod",
    )
    assert "origin" not in [c.name for c in ruleset.output_columns]
    assert len(ruleset.output_columns) == len(ruleset.columns) - 1
    assert ruleset.column("level") == Column(name="level", line=3)
    assert ruleset.column("nope") is None
    assert ruleset.path.name == DEFAULT_RULES_FILENAME


def test_bare_string_columns_default_everything_else(tmp_path):
    ruleset = load_rules(
        write(
            tmp_path,
            """\
            columns:
              - level
              - message
            """,
        )
    )
    assert ruleset.columns == (
        Column(name="level", line=2),
        Column(name="message", line=3),
    )
    assert ruleset.rules == ()


def test_from_accepts_a_string_and_a_list(tmp_path):
    ruleset = load_rules(
        write(
            tmp_path,
            """\
            columns:
              - status
            rules:
              - {target: status, from: ram_status}
              - {target: status, from: [a, b, c]}
            """,
        )
    )
    assert ruleset.rules[0].sources == ("ram_status",)
    assert ruleset.rules[1].sources == ("a", "b", "c")


def test_regexes_are_compiled_once_at_load(tmp_path):
    ruleset = load_rules(write(tmp_path, VALID))
    pattern = ruleset.rules[0].pattern
    assert pattern is not None
    match = pattern.match("WARN [Interceptor.filter:44] hello there")
    assert match is not None
    assert match.group("level") == "WARN"
    assert match.group("message") == "hello there"


def test_rule_without_a_name_is_labelled_by_target_then_groups(tmp_path):
    ruleset = load_rules(
        write(
            tmp_path,
            """\
            columns:
              - level
              - message
            rules:
              - {target: level, from: raw_level}
              - {from: log, regex: '(?P<level>\\w+) (?P<message>.*)'}
            """,
        )
    )
    assert ruleset.rules[0].name == "level"
    assert ruleset.rules[1].name == "level+message"


def test_defaults_are_cast_to_the_column_type(tmp_path):
    ruleset = load_rules(
        write(
            tmp_path,
            """\
            columns:
              - {name: a, type: int, default: '42'}
              - {name: b, type: float, default: 1}
              - {name: c, type: bool, default: 'yes'}
              - {name: d, default: unknown}
              - {name: e, type: time, default: '2026-07-09T08:25:06.166782072+00:00'}
            """,
        )
    )
    assert [c.default for c in ruleset.columns] == [
        42,
        1.0,
        True,
        "unknown",
        "2026-07-09T08:25:06.166782072+00:00",
    ]


def test_sep_defaults_to_empty_and_join_without_sep_is_fine(tmp_path):
    ruleset = load_rules(
        write(
            tmp_path,
            """\
            columns:
              - a
              - b
              - ab
            rules:
              - {target: ab, join: [a, b]}
            """,
        )
    )
    assert ruleset.rules[0].sep == ""
    assert ruleset.rules[0].join == ("a", "b")


# --------------------------------------------------------------------------
# error formatting
# --------------------------------------------------------------------------


def test_rules_error_str_has_file_line_and_caret():
    err = RulesError(Path("/somewhere/rules.yaml"), 14, "boom", "    ^ here")
    assert str(err) == "rules.yaml:14: boom\n    ^ here"
    assert err.line == 14
    assert err.path.name == "rules.yaml"
    assert err.message == "boom"


def test_rules_error_str_without_a_line_or_caret():
    assert str(RulesError(Path("rules.yaml"), None, "boom")) == "rules.yaml: boom"


# --------------------------------------------------------------------------
# file-level errors
# --------------------------------------------------------------------------


def test_yaml_syntax_error_reports_the_line_and_a_caret(tmp_path):
    err = fails(
        tmp_path,
        """\
        columns:
          - {name: a}
           - {name: b}
        """,
    )
    assert "invalid YAML" in err.message
    assert err.line == 3
    assert err.caret is not None and "^" in err.caret


def test_top_level_must_be_a_mapping(tmp_path):
    err = fails(tmp_path, "- just\n- a\n- list\n")
    assert "top level of a rules file must be a mapping" in err.message
    assert err.line == 1


def test_empty_file_is_rejected(tmp_path):
    err = fails(tmp_path, "\n")
    assert "empty" in err.message
    assert err.line is None


def test_unknown_top_level_key(tmp_path):
    err = fails(
        tmp_path,
        """\
        columns:
          - a
        colums:
          - b
        """,
    )
    assert "unknown top-level key 'colums'" in err.message
    assert err.line == 3
    assert err.caret == "    did you mean 'columns'?"


def test_columns_block_is_required(tmp_path):
    err = fails(tmp_path, "rules: []\n")
    assert "no 'columns:' block" in err.message
    assert err.line == 1


def test_columns_block_must_not_be_empty(tmp_path):
    err = fails(tmp_path, "columns: []\n")
    assert "'columns:' is empty" in err.message
    assert err.line == 1


def test_columns_block_must_be_a_list(tmp_path):
    err = fails(
        tmp_path,
        """\
        rules: []
        columns:
          name: level
        """,
    )
    assert "'columns:' must be a list" in err.message
    assert err.line == 2


def test_rules_block_must_be_a_list(tmp_path):
    err = fails(
        tmp_path,
        """\
        columns:
          - level
        rules:
          target: level
        """,
    )
    assert "'rules:' must be a list" in err.message
    assert err.line == 3


# --------------------------------------------------------------------------
# column errors
# --------------------------------------------------------------------------


def test_column_entry_must_be_a_mapping_or_a_name(tmp_path):
    err = fails(
        tmp_path,
        """\
        columns:
          - level
          - [nested, list]
        """,
    )
    assert "column entry 2 must be a mapping or a plain column name" in err.message
    assert err.line == 3


def test_column_without_a_name(tmp_path):
    err = fails(
        tmp_path,
        """\
        columns:
          - level
          - {type: int}
        """,
    )
    assert "column entry 2 has no 'name:'" in err.message
    assert err.line == 3


def test_column_with_a_blank_name(tmp_path):
    err = fails(
        tmp_path,
        """\
        columns:
          - level
          - {name: '   '}
        """,
    )
    assert "blank or non-string 'name:'" in err.message
    assert err.line == 3


def test_duplicate_column_names_point_at_the_first_declaration(tmp_path):
    err = fails(
        tmp_path,
        """\
        columns:
          - level
          - message
          - {name: level, type: int}
        """,
    )
    assert err.message == (
        "column 'level' is declared twice; it was already declared on line 2"
    )
    assert err.line == 4


def test_unknown_column_key_is_rejected_with_a_suggestion(tmp_path):
    err = fails(
        tmp_path,
        """\
        columns:
          - level
          - {name: message, kinds: text}
        """,
    )
    assert "column entry 2 has unknown key 'kinds'" in err.message
    assert err.line == 3
    assert err.caret == "    did you mean 'kind'?"


@pytest.mark.parametrize("name", ENGINE_COLUMNS)
def test_engine_columns_cannot_be_declared(tmp_path, name):
    err = fails(
        tmp_path,
        f"""\
        columns:
          - level
          - {{name: {name}}}
        """,
    )
    assert err.message == f"'{name}' is engine-provided and cannot be declared in 'columns:'"
    assert err.line == 3


def test_column_name_may_not_start_with_underscore(tmp_path):
    err = fails(
        tmp_path,
        """\
        columns:
          - level
          - _raw
        """,
    )
    assert "may not start with '_'" in err.message
    assert err.line == 3


def test_unknown_column_type(tmp_path):
    err = fails(
        tmp_path,
        """\
        columns:
          - {name: level}
          - {name: src_line, type: integer}
        """,
    )
    assert "column 'src_line' has unknown type 'integer'" in err.message
    assert all(f"'{t}'" in err.message for t in VALID_TYPES)
    assert err.line == 3
    assert err.caret == "    did you mean 'int'?"


def test_unknown_column_kind(tmp_path):
    err = fails(
        tmp_path,
        """\
        columns:
          - {name: level, kind: facets}
        """,
    )
    assert "column 'level' has unknown kind 'facets'" in err.message
    assert err.line == 2
    assert err.caret == "    did you mean 'facet'?"


def test_default_that_does_not_cast_is_rejected_at_load(tmp_path):
    err = fails(
        tmp_path,
        """\
        columns:
          - {name: level}
          - {name: req_status_code, type: int, default: 'n/a'}
        """,
    )
    assert err.message == (
        "column 'req_status_code' has a default of 'n/a', which does not cast to int"
    )
    assert err.line == 3


def test_default_of_the_wrong_type_for_bool_and_time(tmp_path):
    assert "does not cast to bool" in fails(
        tmp_path,
        """\
        columns:
          - {name: flag, type: bool, default: maybe}
        """,
    ).message
    assert "does not cast to time" in fails(
        tmp_path,
        """\
        columns:
          - {name: when, type: time, default: 'yesterday'}
        """,
    ).message


def test_non_boolean_internal_and_default_visible(tmp_path):
    err = fails(
        tmp_path,
        """\
        columns:
          - {name: level, internal: yep}
        """,
    )
    assert "non-boolean 'internal:'" in err.message
    assert err.line == 2
    err = fails(
        tmp_path,
        """\
        columns:
          - {name: level, default_visible: 'sometimes'}
        """,
    )
    assert "non-boolean 'default_visible:'" in err.message


def test_non_string_label(tmp_path):
    err = fails(
        tmp_path,
        """\
        columns:
          - {name: level, label: [a, b]}
        """,
    )
    assert "non-string 'label:'" in err.message
    assert err.line == 2


# --------------------------------------------------------------------------
# rule errors
# --------------------------------------------------------------------------


def test_rule_entry_must_be_a_mapping(tmp_path):
    err = fails(
        tmp_path,
        """\
        columns:
          - level
        rules:
          - level
        """,
    )
    assert "rule 1 must be a mapping of rule keys" in err.message
    assert err.line == 4


def test_unknown_rule_key_is_rejected_with_a_suggestion(tmp_path):
    err = fails(
        tmp_path,
        """\
        columns:
          - level
        rules:
          - name: lvl
            form: log
            regex: '(?P<level>\\w+)'
        """,
    )
    assert "rule 'lvl' has unknown key 'form'" in err.message
    assert err.line == 5
    assert err.caret == "    did you mean 'from'?"


def test_invalid_regex_names_the_rule_and_carets_the_offset(tmp_path):
    err = fails(
        tmp_path,
        r"""
        columns:
          - {name: req_status_code, type: int}
        rules:
          - name: status
            target: req_status_code
            from: message
            regex: 'status code: (\d+'
        """,
    )
    assert err.message == "invalid regex in rule 'status'"
    assert err.line == 8
    assert err.caret is not None
    source_line, caret_line = err.caret.splitlines()
    assert source_line == r"    regex: 'status code: (\d+'"
    assert caret_line.strip().startswith("^")
    assert "unterminated subpattern" in caret_line
    # the caret sits under the '(' the regex engine complained about
    assert source_line[len(caret_line) - len(caret_line.lstrip())] == "("


def test_rule_that_writes_nothing(tmp_path):
    err = fails(
        tmp_path,
        """\
        columns:
          - level
        rules:
          - from: log
            regex: 'nothing named here'
        """,
    )
    assert "rule 1 writes nothing" in err.message
    assert "'target:'" in err.message
    assert err.line == 4


def test_target_naming_an_undeclared_column_suggests_the_closest(tmp_path):
    err = fails(
        tmp_path,
        """\
        columns:
          - {name: req_status_code, type: int}
        rules:
          - target: req_stauts_code
            from: ram_status_code
        """,
    )
    assert err.message == "rule 1 targets unknown column 'req_stauts_code'"
    assert err.line == 4
    assert err.caret == "    did you mean 'req_status_code'?"
    assert str(err).splitlines()[0].endswith(
        "rule 1 targets unknown column 'req_stauts_code'"
    )


def test_named_group_naming_an_undeclared_column(tmp_path):
    err = fails(
        tmp_path,
        r"""
        columns:
          - message
          - level
        rules:
          - name: log-line
            from: log
            regex: '(?P<level>\S+) (?P<mesage>.*)'
        """,
    )
    assert err.message == "rule 'log-line' has a named group for unknown column 'mesage'"
    assert err.line == 8
    assert err.caret == "    did you mean 'message'?"


def test_target_may_not_be_an_engine_column(tmp_path):
    err = fails(
        tmp_path,
        """\
        columns:
          - level
        rules:
          - target: source_file
            from: file
        """,
    )
    assert err.message == (
        "rule 1 writes 'source_file', but 'source_file' is engine-provided, not writable"
    )
    assert err.line == 4


def test_named_group_may_not_be_an_engine_column(tmp_path):
    err = fails(
        tmp_path,
        r"""
        columns:
          - level
        rules:
          - name: line
            from: log
            regex: '(?P<parse_ok>\S+)'
        """,
    )
    assert "'parse_ok' is engine-provided, not writable" in err.message
    assert err.line == 7


def test_join_together_with_from_is_an_error(tmp_path):
    err = fails(
        tmp_path,
        """\
        columns:
          - a
          - b
          - ab
        rules:
          - target: ab
            join: [a, b]
            from: a
        """,
    )
    assert "combines 'join:' with 'from'" in err.message
    assert err.line == 7


def test_join_together_with_regex_is_an_error(tmp_path):
    err = fails(
        tmp_path,
        """\
        columns:
          - a
          - b
          - ab
        rules:
          - target: ab
            join: [a, b]
            regex: '.*'
        """,
    )
    assert "combines 'join:' with 'regex'" in err.message
    assert err.line == 7


def test_join_needs_at_least_two_sources(tmp_path):
    err = fails(
        tmp_path,
        """\
        columns:
          - a
          - ab
        rules:
          - target: ab
            join: [a]
        """,
    )
    assert "a join needs at least two" in err.message
    assert err.line == 6


def test_regex_without_a_from_is_an_error(tmp_path):
    err = fails(
        tmp_path,
        r"""
        columns:
          - level
        rules:
          - target: level
            regex: '(\w+)'
        """,
    )
    assert "has a 'regex:' but no 'from:'" in err.message
    assert err.line == 6


def test_target_without_a_source_is_an_error(tmp_path):
    err = fails(
        tmp_path,
        """\
        columns:
          - level
        rules:
          - target: level
        """,
    )
    assert "has a 'target:' but no 'from:' or 'join:'" in err.message
    assert err.line == 4


def test_from_entries_must_be_non_empty_strings(tmp_path):
    err = fails(
        tmp_path,
        """\
        columns:
          - level
        rules:
          - target: level
            from: [ram_level, '']
        """,
    )
    assert "'from' entry that is not a non-empty source name" in err.message
    assert err.line == 5

    err = fails(
        tmp_path,
        """\
        columns:
          - level
        rules:
          - target: level
            from: {a: b}
        """,
    )
    assert "neither a source name nor a list of source names" in err.message
    assert err.line == 5


def test_join_entries_must_be_non_empty_strings(tmp_path):
    err = fails(
        tmp_path,
        """\
        columns:
          - a
          - ab
        rules:
          - target: ab
            join: [a, 7]
        """,
    )
    assert "'join' entry that is not a non-empty source name" in err.message
    assert err.line == 6


def test_non_boolean_required_and_non_string_sep(tmp_path):
    err = fails(
        tmp_path,
        """\
        columns:
          - level
        rules:
          - target: level
            from: raw_level
            required: sure
        """,
    )
    assert "non-boolean 'required:'" in err.message
    assert err.line == 6

    err = fails(
        tmp_path,
        """\
        columns:
          - a
          - b
          - ab
        rules:
          - target: ab
            join: [a, b]
            sep: 7
        """,
    )
    assert "non-string 'sep:'" in err.message
    assert err.line == 8


def test_blank_rule_name_is_rejected(tmp_path):
    err = fails(
        tmp_path,
        """\
        columns:
          - level
        rules:
          - name: '  '
            target: level
            from: raw
        """,
    )
    assert "rule 1 has a blank or non-string 'name:'" in err.message
    assert err.line == 4


# --------------------------------------------------------------------------
# line numbers stay right deep inside a file
# --------------------------------------------------------------------------


def test_line_numbers_survive_blank_lines_comments_and_nesting(tmp_path):
    text = (
        "# a rules file\n"  # 1
        "\n"  # 2
        "columns:\n"  # 3
        "  # the schema\n"  # 4
        "  - {name: time, type: time}\n"  # 5
        "\n"  # 6
        "  - name: level\n"  # 7
        "    kind: facet\n"  # 8
        "\n"  # 9
        "  - name: broken\n"  # 10
        "    type: nope\n"  # 11
    )
    path = tmp_path / DEFAULT_RULES_FILENAME
    path.write_text(text, encoding="utf-8")
    with pytest.raises(RulesError) as excinfo:
        load_rules(path)
    assert excinfo.value.line == 11
    assert str(excinfo.value).startswith("rules.yaml:11: column 'broken' has unknown type")


def test_rule_line_is_the_first_line_of_the_entry(tmp_path):
    ruleset = load_rules(write(tmp_path, VALID))
    assert [rule.line for rule in ruleset.rules] == [11, 16, 19]
    assert [column.line for column in ruleset.columns] == [2, 3, 4, 5, 6, 7, 8, 9]


# --------------------------------------------------------------------------
# find_rules_path
# --------------------------------------------------------------------------


def test_find_rules_path_prefers_the_argument(tmp_path, monkeypatch):
    explicit = write(tmp_path, "columns: [level]\n", "explicit.yaml")
    other = write(tmp_path, "columns: [level]\n", "env.yaml")
    monkeypatch.setenv("CLOUDLOGS_RULES", str(other))
    assert find_rules_path(explicit) == explicit
    assert find_rules_path(str(explicit)) == explicit
    assert load_rules(explicit).path == explicit


def test_find_rules_path_falls_back_to_the_environment(tmp_path, monkeypatch):
    env_file = write(tmp_path, "columns: [level]\n", "env.yaml")
    monkeypatch.setenv("CLOUDLOGS_RULES", str(env_file))
    assert find_rules_path() == env_file
    assert load_rules().names == ("level",)


def test_relative_environment_path_resolves_against_the_project_root(monkeypatch):
    monkeypatch.setenv("CLOUDLOGS_RULES", "pyproject.toml")
    assert find_rules_path() == ROOT / "pyproject.toml"


def test_find_rules_path_defaults_to_the_project_root(monkeypatch, tmp_path):
    monkeypatch.delenv("CLOUDLOGS_RULES", raising=False)
    monkeypatch.chdir(tmp_path)  # cwd must not matter
    expected = ROOT / DEFAULT_RULES_FILENAME
    if expected.is_file():
        assert find_rules_path() == expected
    else:  # the shipped rules.yaml is written by the ingest stage
        with pytest.raises(RulesError) as excinfo:
            find_rules_path()
        assert excinfo.value.path == expected


def test_missing_rules_file_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.delenv("CLOUDLOGS_RULES", raising=False)
    missing = tmp_path / "nope.yaml"
    with pytest.raises(RulesError) as excinfo:
        load_rules(missing)
    assert "does not exist" in str(excinfo.value)
    assert str(missing) in str(excinfo.value)
    assert excinfo.value.line is None

    monkeypatch.setenv("CLOUDLOGS_RULES", str(missing))
    with pytest.raises(RulesError) as excinfo:
        load_rules()
    assert "$CLOUDLOGS_RULES" in str(excinfo.value)


def test_a_directory_is_not_a_rules_file(tmp_path, monkeypatch):
    monkeypatch.delenv("CLOUDLOGS_RULES", raising=False)
    with pytest.raises(RulesError) as excinfo:
        load_rules(tmp_path)
    assert "is a directory" in str(excinfo.value)


# --------------------------------------------------------------------------
# `all:` -- collect every match (PLAN.md 2.2)
# --------------------------------------------------------------------------


def test_all_defaults_sep_to_a_newline(tmp_path) -> None:
    """`all: true` without `sep:` puts one match per line."""
    rules = write(
        tmp_path,
        "columns: [{name: message}, {name: ids}]\n"
        "rules:\n"
        "  - {target: ids, from: message, all: true, regex: '(\\d+)'}\n",
    )
    rule = load_rules(rules).rules[0]
    assert rule.all_matches is True
    assert rule.sep == "\n"


def test_all_keeps_an_explicit_sep(tmp_path) -> None:
    """An explicit `sep:` is not overridden by the `all:` default."""
    rules = write(
        tmp_path,
        "columns: [{name: message}, {name: ids}]\n"
        "rules:\n"
        "  - {target: ids, from: message, all: true, sep: ', ', regex: '(\\d+)'}\n",
    )
    assert load_rules(rules).rules[0].sep == ", "


def test_all_without_a_regex_is_an_error(tmp_path) -> None:
    """There is nothing to match repeatedly without a pattern."""
    rules = write(
        tmp_path,
        "columns: [{name: message}, {name: ids}]\n"
        "rules:\n"
        "  - {target: ids, from: message, all: true}\n",
    )
    with pytest.raises(RulesError) as excinfo:
        load_rules(rules)
    assert "no 'regex:'" in str(excinfo.value)
    assert excinfo.value.line == 3


def test_all_with_join_is_an_error(tmp_path) -> None:
    """`join` concatenates sources, `all` concatenates matches -- not both."""
    rules = write(
        tmp_path,
        "columns: [{name: a}, {name: b}, {name: ids}]\n"
        "rules:\n"
        "  - {target: ids, join: [a, b], all: true}\n",
    )
    with pytest.raises(RulesError) as excinfo:
        load_rules(rules)
    assert "use one or the other" in str(excinfo.value)


def test_all_with_named_groups_is_an_error(tmp_path) -> None:
    """Which group repeats would be undefined."""
    rules = write(
        tmp_path,
        "columns: [{name: message}, {name: ids}]\n"
        "rules:\n"
        "  - {from: message, all: true, regex: '(?P<ids>\\d+)'}\n",
    )
    with pytest.raises(RulesError) as excinfo:
        load_rules(rules)
    assert "which group repeats" in str(excinfo.value)


def test_all_with_two_capture_groups_is_an_error(tmp_path) -> None:
    """Collecting every match needs exactly one group, or none."""
    rules = write(
        tmp_path,
        "columns: [{name: message}, {name: ids}]\n"
        "rules:\n"
        "  - {target: ids, from: message, all: true, regex: '(\\d)(\\d)'}\n",
    )
    with pytest.raises(RulesError) as excinfo:
        load_rules(rules)
    assert "2 capture groups" in str(excinfo.value)


def test_all_must_be_a_boolean(tmp_path) -> None:
    """A stray string in `all:` is a typo, not a truthy value."""
    rules = write(
        tmp_path,
        "columns: [{name: message}, {name: ids}]\n"
        "rules:\n"
        "  - {target: ids, from: message, all: yes-please, regex: '(\\d+)'}\n",
    )
    with pytest.raises(RulesError) as excinfo:
        load_rules(rules)
    assert "non-boolean 'all:'" in str(excinfo.value)


# --------------------------------------------------------------------------
# `map:` -- alternative spellings folded onto one value (PLAN.md 2.5)
# --------------------------------------------------------------------------


def test_map_is_folded_to_lowercase_keys(tmp_path) -> None:
    """Keys are matched case-insensitively, so they are stored folded."""
    rules = write(
        tmp_path,
        "columns:\n"
        "  - {name: level, map: {Warning: WARN, INFO0: INFO}}\n"
        "rules: [{target: level, from: lv}]\n",
    )
    column = load_rules(rules).columns[0]
    assert column.mapping == {"warning": "WARN", "info0": "INFO"}


def test_map_values_must_cast_to_the_column_type(tmp_path) -> None:
    """A map that would put a string in an int column is caught at load."""
    rules = write(
        tmp_path,
        "columns:\n"
        "  - {name: code, type: int, map: {missing: not-a-number}}\n"
        "rules: [{target: code, from: c}]\n",
    )
    with pytest.raises(RulesError) as excinfo:
        load_rules(rules)
    assert "does not cast to int" in str(excinfo.value)


def test_map_keys_may_not_differ_only_by_case(tmp_path) -> None:
    """`WARN` and `warn` as separate keys is a mistake, not two rules."""
    rules = write(
        tmp_path,
        "columns:\n"
        "  - {name: level, map: {WARN: WARN, warn: WARNING}}\n"
        "rules: [{target: level, from: lv}]\n",
    )
    with pytest.raises(RulesError) as excinfo:
        load_rules(rules)
    assert "twice" in str(excinfo.value)


def test_map_must_be_a_non_empty_mapping(tmp_path) -> None:
    """A list, or an empty block, is a typo."""
    for bad in ("[]", "{}"):
        rules = write(
            tmp_path,
            f"columns:\n  - {{name: level, map: {bad}}}\nrules: [{{target: level, from: lv}}]\n",
        )
        with pytest.raises(RulesError) as excinfo:
            load_rules(rules)
        assert "non-empty mapping" in str(excinfo.value)
