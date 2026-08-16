"""Unit gate tests must not persist synthetic tickets in an ambient store."""

from __future__ import annotations

import getpass
from pathlib import Path

import pytest
from _store_isolation import assert_nodes_do_not_mutate_external_store

_OUTAGE_NODE = (
    "tests/unit/test_gate_engine_cutover.py::"
    "test_plan_review_workflow_outage_degrades_to_unsigned_indeterminate"
)


def test_plan_review_outage_unit_test_does_not_write_ambient_store(tmp_path: Path) -> None:
    assert_nodes_do_not_mutate_external_store(tmp_path, _OUTAGE_NODE)


def test_nested_pytest_does_not_clean_shared_numbered_temp_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temproot = tmp_path / "shared-temproot"
    shared_root = temproot / f"pytest-of-{getpass.getuser()}"
    for number in range(4):
        (shared_root / f"pytest-{number}").mkdir(parents=True)
    sentinel = shared_root / "pytest-0" / "belongs-to-another-run"
    sentinel.write_text("must survive\n")
    entries_before = {path.name for path in shared_root.iterdir()}
    assert sentinel.is_file()

    monkeypatch.setenv("PYTEST_DEBUG_TEMPROOT", str(temproot))
    assert_nodes_do_not_mutate_external_store(tmp_path, _OUTAGE_NODE)

    assert sentinel.is_file(), "nested pytest deleted another run's temp tree"
    assert sentinel.read_text() == "must survive\n"
    assert {path.name for path in shared_root.iterdir()} == entries_before
