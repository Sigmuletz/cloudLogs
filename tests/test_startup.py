"""The server's startup contract around `rules.yaml` (PLAN.md 2.7).

A broken ruleset is the one failure `cloudlogs.main` must not survive: serving
the previous `logs.json` would show data that no longer matches the rules on
disk, which is exactly what "validate up front" exists to prevent.
"""

from __future__ import annotations

import json

import pytest

from cloudlogs.rules import RulesError

main = pytest.importorskip("cloudlogs.main", reason="fastapi is not installed")


BROKEN = "columns:\n  - {name: level}\nrules:\n  - target: level\n    from: log\n    regex: 'oops (\\d+'\n"
VALID = "columns:\n  - {name: level}\nrules:\n  - {target: level, from: log}\n"


def _workspace(tmp_path, monkeypatch, rules_text: str, *, fresh_data: bool) -> None:
    """Point main.py at a scratch workspace with the given rules file."""
    rules = tmp_path / "rules.yaml"
    rules.write_text(rules_text, encoding="utf-8")
    source = tmp_path / "logs.log"
    source.write_text(json.dumps({"log": "2026-07-09 08:25:06 INFO  [A.b:1] (t) hi"}) + "\n", encoding="utf-8")
    data = tmp_path / "logs.json"
    if fresh_data:
        # already ingested and newer than everything: nothing needs re-running
        data.write_text("[]", encoding="utf-8")
        (tmp_path / "columns.json").write_text("[]", encoding="utf-8")
    monkeypatch.setenv("CLOUDLOGS_RULES", str(rules))
    monkeypatch.setenv("CLOUDLOGS_INPUT", str(source))
    monkeypatch.setenv("CLOUDLOGS_DATA", str(data))


def test_startup_refuses_a_broken_ruleset(tmp_path, monkeypatch) -> None:
    """`load_state` raises rather than serving past an invalid rules.yaml."""
    _workspace(tmp_path, monkeypatch, BROKEN, fresh_data=False)
    with pytest.raises(RulesError) as excinfo:
        main.load_state()
    assert "invalid regex" in str(excinfo.value)


def test_startup_refuses_even_when_nothing_needs_ingesting(tmp_path, monkeypatch) -> None:
    """The check runs before the staleness check, not inside the ingest branch.

    A fresh `logs.json` used to mean the rules were never loaded at all, so a
    broken file was served straight past.
    """
    _workspace(tmp_path, monkeypatch, BROKEN, fresh_data=True)
    with pytest.raises(RulesError):
        main.load_state()


def test_startup_succeeds_with_a_valid_ruleset(tmp_path, monkeypatch) -> None:
    """The good path is untouched: ingest runs and records load."""
    _workspace(tmp_path, monkeypatch, VALID, fresh_data=False)
    summary = main.load_state()
    assert summary["ingested"] is True
    assert summary["records"] == 1
