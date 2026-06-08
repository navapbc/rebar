"""RED tests for Fix #5: truthful mutations_applied tally.

Historical bug context (bug 85a1-f581-2252-4a21): __main__.py:223 printed
``result['mutation_count']`` which is set to ``len(mutations)`` BEFORE apply
runs (reconcile.py:789). The message therefore reads
``OK: steady-state pass converged — N mutations`` even when N mutations
silently failed to land in Jira. Phase 6 of the e2e probe disproved
convergence (3 consecutive "no-op" passes each computed 21 filter-matching
mutations) while the same passes self-reported OK with positive N.

The fix surfaces ``mutations_applied`` (count of outcomes that reached a
handler without raising) and ``mutation_failures`` (count of outcomes that
recorded an ``error`` field) separately from ``mutation_count`` (computed)
so operators can distinguish "dispatch ran, X succeeded" from
"X mutations computed, dispatch unknown".

The "converged" verb is reserved for confirmation passes where the
applied count is zero on a post-sync diff — i.e., the genuine no-op
case. Any pass with applied > 0 must print ``applied N (F failed)``.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
MAIN_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "dso_reconciler" / "__main__.py"
)


def _load_main_module():
    spec = importlib.util.spec_from_file_location(
        "dso_reconciler_main_truthful", MAIN_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dso_reconciler_main_truthful"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def main_mod():
    if not MAIN_PATH.exists():
        pytest.fail(f"__main__.py not found at {MAIN_PATH}")
    return _load_main_module()


def _make_stub_reconcile(return_value):
    stub = types.ModuleType("stub_reconcile_truthful")
    stub.reconcile_once = MagicMock(return_value=return_value)  # type: ignore[attr-defined]
    return stub


def test_run_pass_prints_applied_count_when_writes_succeed(main_mod, tmp_path, capsys):
    """When 3 of 10 mutations land in Jira, the OK line must report applied=3."""
    stub = _make_stub_reconcile({
        "pass_id": "p-001",
        "mutation_count": 10,
        "mutations_applied": 3,
        "mutation_failures": 7,
        "manifest_path": str(tmp_path / "manifest.json"),
    })
    with patch.object(main_mod, "_try_load_step", return_value=stub):
        rc = main_mod.run_pass(repo_root=tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "applied 3" in out or "applied=3" in out, (
        f"OK line must report applied count truthfully; got: {out!r}"
    )
    assert "7 failed" in out or "failed=7" in out or "failures=7" in out, (
        f"OK line must report failure count when non-zero; got: {out!r}"
    )


def test_run_pass_does_not_say_converged_when_applied_nonzero(
    main_mod, tmp_path, capsys
):
    """'converged' verb must be reserved for applied=0 confirmation passes.

    The historical lying message was 'OK: steady-state pass converged — N
    mutations' where N was the computed count. After the fix, 'converged'
    must NOT appear on the line when mutations_applied > 0.
    """
    stub = _make_stub_reconcile({
        "pass_id": "p-002",
        "mutation_count": 10,
        "mutations_applied": 10,
        "mutation_failures": 0,
        "manifest_path": str(tmp_path / "manifest.json"),
    })
    with patch.object(main_mod, "_try_load_step", return_value=stub):
        main_mod.run_pass(repo_root=tmp_path)
    out = capsys.readouterr().out
    assert "converged" not in out.lower(), (
        f"'converged' verb leaks onto applied>0 pass — historical lying-message "
        f"regression; got: {out!r}"
    )


def test_run_pass_says_converged_only_when_applied_zero(main_mod, tmp_path, capsys):
    """When applied=0, the converged verb is permitted (genuine no-op pass)."""
    stub = _make_stub_reconcile({
        "pass_id": "p-003",
        "mutation_count": 0,
        "mutations_applied": 0,
        "mutation_failures": 0,
        "manifest_path": str(tmp_path / "manifest.json"),
    })
    with patch.object(main_mod, "_try_load_step", return_value=stub):
        main_mod.run_pass(repo_root=tmp_path)
    out = capsys.readouterr().out
    assert "converged" in out.lower() or "applied 0" in out, (
        f"applied=0 no-op pass should print converged or applied 0; got: {out!r}"
    )
