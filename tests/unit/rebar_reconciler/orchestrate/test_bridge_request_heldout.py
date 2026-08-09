"""Held-out direct-engine oracle for bridge request normalization."""

from __future__ import annotations

import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import rebar_reconciler.__main__ as main_mod


def _capture_run(argv: list[str], tmp_path: Path) -> tuple[int, dict]:
    """Run the real parser/guard spine while replacing only lock and pass I/O."""
    captured: dict = {}
    real_load = main_mod._load_sibling_keyed
    advisory = types.SimpleNamespace(
        acquire_pass_lock=MagicMock(return_value=None),
        release_pass_lock=MagicMock(return_value=None),
    )

    def load_sibling(key: str, filename: str):
        if filename == "_advisory_lock.py":
            return advisory
        return real_load(key, filename)

    def run_pass(**kwargs) -> int:
        captured.update(kwargs)
        return 0

    with (
        patch.object(main_mod, "_load_sibling_keyed", side_effect=load_sibling),
        patch.object(main_mod, "_pause_exit_code", return_value=None),
        patch.object(main_mod, "_purge_committed_reconciler_locks"),
        patch.object(main_mod, "_post_pause_preflight", return_value=(False, None)),
        patch.object(main_mod, "run_pass", side_effect=run_pass),
    ):
        rc = main_mod.main([*argv, "--repo-root", str(tmp_path)])
    captured["advisory"] = advisory
    return rc, captured


@pytest.mark.parametrize(
    ("argv", "expected_mode", "expected_lock_count"),
    [
        (["preview"], "dry-run", 0),
        (["sync"], "live", 1),
    ],
)
def test_primary_and_legacy_routes_preserve_distinct_engine_defaults(
    tmp_path: Path, argv: list[str], expected_mode: str, expected_lock_count: int
) -> None:
    rc, captured = _capture_run(argv, tmp_path)

    assert rc == 0
    assert captured["target_mode"].value == expected_mode
    assert captured["advisory"].acquire_pass_lock.call_count == expected_lock_count
    assert captured["advisory"].release_pass_lock.call_count == expected_lock_count


def test_capped_sync_remains_live_for_gate_and_pass_classification(tmp_path: Path) -> None:
    rc, captured = _capture_run(["sync", "--max-changes", "10"], tmp_path)

    assert rc == 0
    assert captured["target_mode"].value == "live"
    assert captured["max_changes"] == 10


@pytest.mark.parametrize("value", ["0", "-1", "not-a-number"])
def test_invalid_max_changes_returns_two_before_lock(
    tmp_path: Path, value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    acquire = MagicMock(side_effect=AssertionError("lock must not be acquired"))
    advisory = types.SimpleNamespace(acquire_pass_lock=acquire)
    real_load = main_mod._load_sibling_keyed

    def load_sibling(key: str, filename: str):
        if filename == "_advisory_lock.py":
            return advisory
        return real_load(key, filename)

    with patch.object(main_mod, "_load_sibling_keyed", side_effect=load_sibling):
        rc = main_mod.main(["sync", "--max-changes", value, "--repo-root", str(tmp_path)])

    assert rc == 2
    assert "max-changes" in capsys.readouterr().err
    acquire.assert_not_called()


def test_unresolved_selection_names_every_identifier_before_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    acquire = MagicMock(side_effect=AssertionError("lock must not be acquired"))
    advisory = types.SimpleNamespace(acquire_pass_lock=acquire)
    real_load = main_mod._load_sibling_keyed

    def load_sibling(key: str, filename: str):
        if filename == "_advisory_lock.py":
            return advisory
        return real_load(key, filename)

    with patch.object(main_mod, "_load_sibling_keyed", side_effect=load_sibling):
        rc = main_mod.main(
            [
                "preview",
                "--only",
                "missing-local,MISSING-9",
                "--repo-root",
                str(tmp_path),
            ]
        )

    assert rc == 2
    stderr = capsys.readouterr().err
    assert "missing-local" in stderr
    assert "MISSING-9" in stderr
    assert "resolve" in stderr.lower()
    acquire.assert_not_called()
