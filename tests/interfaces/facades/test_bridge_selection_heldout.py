"""Held-out real-store oracle for canonical bridge selection preflight."""

from __future__ import annotations

import contextlib
import importlib
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from _subprocess_env import subprocess_env

import rebar


@contextlib.contextmanager
def _production_reconciler():
    """Load the engine without shadowing pytest's same-named test package."""
    saved = {
        key: module
        for key, module in sys.modules.items()
        if key == "rebar_reconciler" or key.startswith("rebar_reconciler.")
    }
    for key in saved:
        sys.modules.pop(key, None)
    engine_root = str(Path(__file__).resolve().parents[3] / "src" / "rebar" / "_engine")
    sys.path.insert(0, engine_root)
    try:
        main_mod = importlib.import_module("rebar_reconciler.__main__")
        binding_mod = importlib.import_module("rebar_reconciler.binding_store")
        yield main_mod, binding_mod.BindingStore
    finally:
        sys.path.remove(engine_root)
        for key in tuple(sys.modules):
            if key == "rebar_reconciler" or key.startswith("rebar_reconciler."):
                sys.modules.pop(key, None)
        sys.modules.update(saved)


def _run_with_real_preflight(main_mod, argv: list[str]) -> tuple[int, dict, MagicMock, MagicMock]:
    """Use real ticket/binding reads and replace only the pass lock and execution."""
    captured: dict = {}
    acquire = MagicMock(return_value=None)
    release = MagicMock(return_value=None)
    advisory = types.SimpleNamespace(
        acquire_pass_lock=acquire,
        release_pass_lock=release,
    )
    real_load = main_mod._load_sibling_keyed

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
        rc = main_mod.main(argv)
    return rc, captured, acquire, release


@pytest.mark.parametrize("flag", ["--only", "--except"])
def test_preview_selection_resolves_local_ids_and_bound_jira_keys_without_lock(
    rebar_repo: Path, flag: str
) -> None:
    first = rebar.create_ticket("task", "first", return_alias=True)["id"]
    second = rebar.create_ticket("task", "second", return_alias=True)["id"]
    with _production_reconciler() as (main_mod, binding_store_cls):
        bindings = binding_store_cls(rebar_repo / ".tickets-tracker")
        bindings.bind_confirm(first, "DIG-7")
        bindings.save()
        rc, captured, acquire, release = _run_with_real_preflight(
            main_mod,
            [
                "preview",
                flag,
                f"{second},DIG-7",
                "--repo-root",
                str(rebar_repo),
            ],
        )

    assert rc == 0
    assert captured["selection_kind"] == flag.removeprefix("--")
    assert captured["selection_ids"] == {first, second}
    assert captured["target_mode"].value == "dry-run"
    acquire.assert_not_called()
    release.assert_not_called()


def test_partial_unresolved_selection_is_atomic_and_never_acquires_lock(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    existing = rebar.create_ticket("task", "existing", return_alias=True)["id"]

    with _production_reconciler() as (main_mod, _binding_store_cls):
        rc, captured, acquire, release = _run_with_real_preflight(
            main_mod,
            [
                "sync",
                "--only",
                f"{existing},missing-local,MISSING-8",
                "--repo-root",
                str(rebar_repo),
            ],
        )

    assert rc == 2
    stderr = capsys.readouterr().err
    assert "missing-local" in stderr
    assert "MISSING-8" in stderr
    assert captured == {}
    acquire.assert_not_called()
    release.assert_not_called()


def test_only_and_except_are_mutually_exclusive_before_lock(rebar_repo: Path) -> None:
    ticket_id = rebar.create_ticket("task", "selected", return_alias=True)["id"]

    with _production_reconciler() as (main_mod, _binding_store_cls):
        rc, captured, acquire, release = _run_with_real_preflight(
            main_mod,
            [
                "preview",
                "--only",
                ticket_id,
                "--except",
                ticket_id,
                "--repo-root",
                str(rebar_repo),
            ],
        )

    assert rc == 2
    assert captured == {}
    acquire.assert_not_called()
    release.assert_not_called()


def test_real_cli_and_engine_report_semantic_selection_failure(rebar_repo: Path) -> None:
    """The installed CLI crosses the child-process boundary before rejecting IDs."""
    env = subprocess_env()
    env["REBAR_ROOT"] = str(rebar_repo)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "rebar",
            "bridge",
            "preview",
            "--only",
            "missing-local,MISSING-9",
        ],
        cwd=rebar_repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "missing-local" in completed.stderr
    assert "MISSING-9" in completed.stderr
    assert "unrecognized arguments" not in completed.stderr
