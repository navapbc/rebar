"""Held-out ambient-store isolation cases for synthetic completion verdicts."""

from __future__ import annotations

from pathlib import Path

from _store_isolation import assert_nodes_do_not_mutate_external_store


def test_completion_outage_unit_test_does_not_write_ambient_store(tmp_path: Path) -> None:
    assert_nodes_do_not_mutate_external_store(
        tmp_path,
        "tests/unit/test_gate_engine_cutover.py::"
        "test_completion_workflow_outage_raises_so_close_fails_closed",
    )


def test_force_close_unit_test_does_not_write_ambient_store(tmp_path: Path) -> None:
    assert_nodes_do_not_mutate_external_store(
        tmp_path,
        "tests/unit/test_llm_failure_matrix.py::test_force_close_skips_completion_gate",
    )
