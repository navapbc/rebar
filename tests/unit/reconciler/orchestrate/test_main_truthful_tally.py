"""The pass reports applied writes rather than planned mutations. It says converged
only after an applied=0 confirmation pass and fails loudly on mutation failures.
"""

from __future__ import annotations

import importlib
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
MAIN_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "__main__.py"


def _load_main_module():
    return importlib.import_module("rebar.reconciler.__main__")


@pytest.fixture(scope="module")
def main_mod():
    if not MAIN_PATH.exists():
        pytest.fail(f"__main__.py not found at {MAIN_PATH}")
    return _load_main_module()


@pytest.fixture(scope="module")
def guarded_mod():
    """The guarded-pass module that owns the pass body and its pipeline-load seam."""
    return importlib.import_module("rebar.reconciler.guarded_pass")


def _run_pass(main_mod, guarded_mod, repo_root: Path) -> int:
    """Run the pass body and render it for the compatibility route (the old ``run_pass``)."""
    return main_mod._render_pass(guarded_mod.run_pass_result(repo_root=repo_root), None)


def _make_stub_reconcile(return_value):
    stub = types.ModuleType("stub_reconcile_truthful")
    stub.reconcile_once = MagicMock(return_value=return_value)  # type: ignore[attr-defined]
    return stub


def test_run_pass_prints_applied_count_when_writes_succeed(main_mod, guarded_mod, tmp_path, capsys):
    """If 3 of 10 planned mutations land, report applied=3; failures still produce a
    nonzero exit.
    """
    stub = _make_stub_reconcile(
        {
            "pass_id": "p-001",
            "mutation_count": 10,
            "mutations_applied": 3,
            "mutation_failures": 7,
            "manifest_path": str(tmp_path / "manifest.json"),
        }
    )
    with patch.object(guarded_mod, "_try_load_step", return_value=stub):
        rc = _run_pass(main_mod, guarded_mod, tmp_path)
    out = capsys.readouterr().out
    assert rc != 0, "a pass with mutation_failures > 0 must fail loud (non-zero exit)"
    assert "applied 3" in out or "applied=3" in out, (
        f"OK line must report applied count truthfully; got: {out!r}"
    )
    assert "7 failed" in out or "failed=7" in out or "failures=7" in out, (
        f"OK line must report failure count when non-zero; got: {out!r}"
    )


def test_run_pass_does_not_say_converged_when_applied_nonzero(
    main_mod, guarded_mod, tmp_path, capsys
):
    """Reserve 'converged' for applied=0 confirmation; a pass that writes reports its
    applied count instead.
    """
    stub = _make_stub_reconcile(
        {
            "pass_id": "p-002",
            "mutation_count": 10,
            "mutations_applied": 10,
            "mutation_failures": 0,
            "manifest_path": str(tmp_path / "manifest.json"),
        }
    )
    with patch.object(guarded_mod, "_try_load_step", return_value=stub):
        _run_pass(main_mod, guarded_mod, tmp_path)
    out = capsys.readouterr().out
    assert "converged" not in out.lower(), (
        f"'converged' verb leaks onto applied>0 pass — historical lying-message "
        f"regression; got: {out!r}"
    )
    assert out.strip() != "", "a successful applied>0 pass must not print empty output"
    assert "applied 10" in out or "applied=10" in out, (
        f"OK line must report the exact nonempty applied/total tally truthfully; got: {out!r}"
    )


def test_run_pass_empty_stdout_fails_truthful_tally() -> None:
    """Negative control: empty stdout must NOT satisfy the truthful-tally oracle.

    Directly proves the oracle strengthened above rejects the vacuous case
    that the prior (converged-absence-only) assertion let through.
    """
    out = ""
    assert "converged" not in out.lower()  # the old, insufficient check still holds...
    with pytest.raises(AssertionError):
        # ...but the strengthened tally assertion correctly rejects it.
        assert out.strip() != "", "a successful applied>0 pass must not print empty output"


def test_run_pass_says_converged_only_when_applied_zero(main_mod, guarded_mod, tmp_path, capsys):
    """When applied=0, the converged verb is permitted (genuine no-op pass)."""
    stub = _make_stub_reconcile(
        {
            "pass_id": "p-003",
            "mutation_count": 0,
            "mutations_applied": 0,
            "mutation_failures": 0,
            "manifest_path": str(tmp_path / "manifest.json"),
        }
    )
    with patch.object(guarded_mod, "_try_load_step", return_value=stub):
        _run_pass(main_mod, guarded_mod, tmp_path)
    out = capsys.readouterr().out
    assert "converged" in out.lower() or "applied 0" in out, (
        f"applied=0 no-op pass should print converged or applied 0; got: {out!r}"
    )
