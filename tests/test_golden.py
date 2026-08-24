"""The golden snapshot: `rules.yaml` must reproduce the pre-rules output exactly.

`tests/golden/logs.json` (564 records) and `tests/golden/columns.json` (30
columns) were produced by the hand-written ingest that the rule engine replaced.
The comparison runs against `tests/migration_rules.yaml`, a frozen copy of the
ruleset as shipped -- NOT the live `rules.yaml`, which is meant to be edited:
adding a column or a rule there must never look like a regression here. If the
frozen ruleset reproduces both byte-for-byte, the
migration lost nothing -- that is the whole claim this file checks.

The snapshot and `example/logs.log` are gitignored, so a fresh clone has
neither; the tests skip with an explanation rather than fail.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # run without installing the package
    sys.path.insert(0, str(ROOT))

from cloudlogs.ingest import ingest  # noqa: E402

GOLDEN = ROOT / "tests" / "golden"
GOLDEN_LOGS = GOLDEN / "logs.json"
GOLDEN_COLUMNS = GOLDEN / "columns.json"
EXAMPLE = ROOT / "example" / "logs.log"
RULES = ROOT / "tests" / "migration_rules.yaml"


@pytest.fixture(scope="module")
def produced(tmp_path_factory) -> tuple[list, list]:
    """Re-ingest `example/logs.log` with the frozen migration ruleset."""
    missing = [p for p in (GOLDEN_LOGS, GOLDEN_COLUMNS, EXAMPLE) if not p.exists()]
    if missing:
        pytest.skip(
            "golden snapshot unavailable: "
            + ", ".join(str(p.relative_to(ROOT)) for p in missing)
            + " (gitignored; regenerate it before comparing)"
        )

    out = tmp_path_factory.mktemp("golden") / "logs.json"
    # `source_file` is recorded relative to the cwd, so compare from the root
    previous = Path.cwd()
    os.chdir(ROOT)
    try:
        ingest(["example/logs.log"], out, rules=RULES)
    finally:
        os.chdir(previous)
    return (
        json.loads(out.read_text(encoding="utf-8")),
        json.loads(out.with_name("columns.json").read_text(encoding="utf-8")),
    )


def test_records_match_the_golden_snapshot(produced):
    """Every one of the 564 records, field for field, in order."""
    records, _ = produced
    golden = json.loads(GOLDEN_LOGS.read_text(encoding="utf-8"))

    assert len(records) == len(golden) == 564
    for index, (got, want) in enumerate(zip(records, golden)):
        assert list(got) == list(want), f"record {index}: column order differs"
        for name in want:
            assert got[name] == want[name], f"record {index}, column {name!r}"
    assert records == golden


def test_columns_match_the_golden_snapshot(produced):
    """`columns.json` too -- kinds, labels, distinct counts and UI defaults."""
    _, columns = produced
    golden = json.loads(GOLDEN_COLUMNS.read_text(encoding="utf-8"))

    assert [c["name"] for c in columns] == [c["name"] for c in golden]
    for got, want in zip(columns, golden):
        assert got == want, f"column {want['name']!r}"
    assert columns == golden
