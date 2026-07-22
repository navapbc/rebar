"""Unit gate tests must not persist synthetic tickets in an ambient store."""

from pathlib import Path

from _store_isolation import assert_nodes_do_not_mutate_external_store


def test_plan_review_outage_unit_test_does_not_write_ambient_store(tmp_path: Path) -> None:
    assert_nodes_do_not_mutate_external_store(
        tmp_path,
        "tests/unit/test_gate_engine_cutover.py::"
        "test_plan_review_workflow_outage_degrades_to_unsigned_indeterminate",
    )
