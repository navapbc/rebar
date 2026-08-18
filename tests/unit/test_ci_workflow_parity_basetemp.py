"""Nested parity collection must not clean another pytest run's temp tree."""

from __future__ import annotations

import getpass
from pathlib import Path
from typing import Any

from test_ci_workflow_parity import _DEFAULT_SELECTION, _collect_node_ids


def test_nested_collection_preserves_shared_pytest_temp_roots(
    tmp_path: Path, monkeypatch: Any
) -> None:
    temproot = tmp_path / "shared-temproot"
    shared_root = temproot / f"pytest-of-{getpass.getuser()}"
    for number in range(4):
        (shared_root / f"pytest-{number}").mkdir(parents=True)
    sentinel = shared_root / "pytest-0" / "belongs-to-another-run"
    sentinel.write_text("must survive\n")
    entries_before = {path.name for path in shared_root.iterdir()}
    assert sentinel.is_file()

    probe_dir = tmp_path / "collection-probe"
    probe_dir.mkdir()
    (probe_dir / "conftest.py").write_text(
        "def pytest_sessionstart(session):\n    session.config._tmp_path_factory.getbasetemp()\n"
    )
    probe = probe_dir / "test_probe.py"
    probe.write_text("def test_collected():\n    pass\n")

    monkeypatch.setenv("PYTEST_DEBUG_TEMPROOT", str(temproot))
    node_ids = _collect_node_ids((str(probe),), _DEFAULT_SELECTION)

    assert len(node_ids) == 1
    assert node_ids[0].endswith("test_probe.py::test_collected")
    assert sentinel.is_file(), "nested pytest deleted another run's temp tree"
    assert sentinel.read_text() == "must survive\n"
    assert {path.name for path in shared_root.iterdir()} == entries_before
