"""Defect-seeded tests for the terraform tree-currency gate [rebar:eebc-6aa1-e45b-4325].

The 2026-09-05 instance is reproduced literally: a clone that is behind its origin, whose
``.tf`` file lacks alarms that origin declares. From that tree ``terraform plan`` reported
``0 to add, 0 to change, 0 to destroy`` — indistinguishable from converged infrastructure —
while the same plan from a tree at origin returned ``5 to add, 1 to change``.

The reverse direction is seeded too, because it is the dangerous one: a tree that predates a
DELETION would propose recreating the deleted resource. Both directions are the same git fact,
and both must fire.

The load-bearing contrast is that the gate is GREEN on the current tree and RED on the stale
one from the SAME fixture. A gate that is red everywhere proves nothing; a gate that is green
everywhere is the silence it was written to replace.

Hermetic: temp git repositories over local paths — no network, no terraform, no AWS.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from _subprocess_env import subprocess_env

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "scripts" / "check_terraform_tree_currency.py"

# The shape of the observed defect: origin declares alarms the stale clone has never seen.
_BASE_TF = 'resource "aws_cloudwatch_metric_alarm" "a" {\n  alarm_name = "a"\n}\n'
_ADDED_TF = _BASE_TF + 'resource "aws_cloudwatch_metric_alarm" "b" {\n  alarm_name = "b"\n}\n'


def _load_gate() -> Any:
    """Import the gate by path (``scripts/`` is not an importable package)."""
    spec = importlib.util.spec_from_file_location("check_terraform_tree_currency", GATE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_terraform_tree_currency"] = module
    spec.loader.exec_module(module)
    return module


gate = _load_gate()


def _clean_git_env() -> dict[str, str]:
    """Ambient GIT_* pointing at the REAL checkout would redirect the fixture's git."""
    env = subprocess_env()
    for leaked in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"):
        env.pop(leaked, None)
    return env


def _git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=_clean_git_env(),
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.stdout.strip()


def _commit(repo: Path, body: str, message: str) -> None:
    (repo / "monitoring.tf").write_text(body, encoding="utf-8")
    _git("add", "monitoring.tf", cwd=repo)
    _git("commit", "-q", "-m", message, cwd=repo)


def _origin(tmp_path: Path) -> Path:
    """A non-bare 'origin' repository holding the declaration of record."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", "-q", "-b", "main", cwd=origin)
    _git("config", "user.email", "fixture@example.com", cwd=origin)
    _git("config", "user.name", "Fixture", cwd=origin)
    _git("config", "commit.gpgsign", "false", cwd=origin)
    # A non-bare origin refuses a push to its checked-out branch; nothing here pushes, but
    # the clones fetch from it, which is all the gate needs.
    _commit(origin, _BASE_TF, "Seed the declaration")
    return origin


def _clone(origin: Path, name: str, tmp_path: Path) -> Path:
    clone = tmp_path / name
    _git("clone", "-q", str(origin), str(clone), cwd=tmp_path)
    _git("config", "user.email", "fixture@example.com", cwd=clone)
    _git("config", "user.name", "Fixture", cwd=clone)
    _git("config", "commit.gpgsign", "false", cwd=clone)
    return clone


def _check(repo: Path, **kwargs: Any) -> Any:
    params: dict[str, Any] = {
        "remote": "origin",
        "branch": "main",
        "mode": gate.MODE_TIP,
        "fetch": True,
    }
    params.update(kwargs)
    return gate.check(repo, **params)


# --- the observed instance: a tree BEHIND origin ----------------------------------------- #


def test_current_clone_is_green(tmp_path: Path) -> None:
    """The control. Without this, a red-everywhere gate would pass the defect-seeded test."""
    origin = _origin(tmp_path)
    clone = _clone(origin, "current", tmp_path)
    verdict = _check(clone)
    assert verdict.code == gate.EXIT_CURRENT, verdict.lines


def test_clone_behind_origin_is_reported_stale(tmp_path: Path) -> None:
    """The 2026-09-05 case: origin declares an alarm this tree has never seen."""
    origin = _origin(tmp_path)
    clone = _clone(origin, "stale", tmp_path)
    _commit(origin, _ADDED_TF, "Declare a second alarm on main")

    verdict = _check(clone)

    assert verdict.code == gate.EXIT_STALE, verdict.lines
    joined = " ".join(verdict.lines)
    assert "STALE" in joined
    assert "behind" in joined


def test_stale_verdict_distinguishes_itself_from_no_changes(tmp_path: Path) -> None:
    """AC1: a reader must tell "your tree is stale" from "infrastructure matches"."""
    origin = _origin(tmp_path)
    clone = _clone(origin, "stale", tmp_path)
    _commit(origin, _ADDED_TF, "Declare a second alarm on main")

    joined = " ".join(_check(clone).lines)

    assert '"No changes"' in joined, (
        "the stale verdict never says that a plan's 'No changes' from this tree is not "
        "evidence of convergence, which is the entire signal the reader is missing"
    )
    assert "RECREATION" in joined, "the un-deletion direction is not named in the verdict"


def test_tree_predating_a_deletion_is_reported_stale(tmp_path: Path) -> None:
    """The dangerous direction: an apply from here would RECREATE a deleted resource."""
    origin = _origin(tmp_path)
    _commit(origin, _ADDED_TF, "Declare a second alarm on main")
    clone = _clone(origin, "predates-deletion", tmp_path)
    _commit(origin, _BASE_TF, "Delete the second alarm on main")

    assert _check(clone).code == gate.EXIT_STALE


def test_diverged_tree_is_reported_stale(tmp_path: Path) -> None:
    """A local commit does not make a tree current — it makes it diverged."""
    origin = _origin(tmp_path)
    clone = _clone(origin, "diverged", tmp_path)
    _commit(origin, _ADDED_TF, "Declare a second alarm on main")
    _commit(clone, _BASE_TF + "# local edit\n", "Local-only change")

    verdict = _check(clone)

    assert verdict.code == gate.EXIT_STALE, verdict.lines
    assert "diverged from" in " ".join(verdict.lines)


# --- the fetch is what makes the check undefeatable (AC2) --------------------------------- #


def test_check_is_not_defeated_by_a_stale_local_remote_tracking_ref(tmp_path: Path) -> None:
    """AC2: currency is established from a FRESH fetch, not from anything in the tree.

    ``--no-fetch`` compares against the clone's own ``origin/main``, which a stale clone still
    believes is its own HEAD — so it wrongly reads as current. The default fetching path,
    on the same fixture, is red. That contrast IS the anti-defeat property.
    """
    origin = _origin(tmp_path)
    clone = _clone(origin, "stale", tmp_path)
    _commit(origin, _ADDED_TF, "Declare a second alarm on main")

    assert _check(clone, fetch=False).code == gate.EXIT_CURRENT
    assert _check(clone).code == gate.EXIT_STALE


def test_unreachable_remote_is_unknown_not_current(tmp_path: Path) -> None:
    """An unestablished currency must never be reported as an established one."""
    origin = _origin(tmp_path)
    clone = _clone(origin, "orphaned", tmp_path)
    _git("remote", "set-url", "origin", str(tmp_path / "does-not-exist"), cwd=clone)

    verdict = _check(clone)

    assert verdict.code == gate.EXIT_UNKNOWN, verdict.lines
    assert "UNKNOWN" in " ".join(verdict.lines)


# --- ancestor mode: a PR merge commit contains the tip without being it ------------------- #


def test_ancestor_mode_accepts_a_tree_that_contains_the_tip(tmp_path: Path) -> None:
    origin = _origin(tmp_path)
    clone = _clone(origin, "ahead", tmp_path)
    _commit(clone, _ADDED_TF, "A change on top of the tip")

    assert _check(clone, mode=gate.MODE_ANCESTOR).code == gate.EXIT_CURRENT
    assert _check(clone, mode=gate.MODE_TIP).code == gate.EXIT_STALE


def test_an_ahead_tree_is_called_ahead_not_diverged(tmp_path: Path) -> None:
    """A tree that strictly CONTAINS the tip has diverged from nothing.

    Calling it "diverged from" sends the operator hunting for a divergence that does not
    exist — the same collapse of distinct states the three-valued exit code refuses one
    level up. The remedy line must move too: you cannot fast-forward a tree that is ahead.
    """
    origin = _origin(tmp_path)
    clone = _clone(origin, "ahead", tmp_path)
    _commit(clone, _ADDED_TF, "A change on top of the tip")

    joined = " ".join(_check(clone, mode=gate.MODE_TIP).lines)

    assert "ahead of" in joined, joined
    assert "diverged" not in joined, joined
    assert "behind" not in joined, joined
    assert "merge --ff-only" not in joined, (
        "an ahead tree is told to fast-forward, which cannot work — the remedy is to plan "
        "from a worktree at the tip"
    )


def test_a_diverged_tree_is_still_called_diverged(tmp_path: Path) -> None:
    """The control for the test above: narrowing 'diverged' must not empty it."""
    origin = _origin(tmp_path)
    clone = _clone(origin, "diverged", tmp_path)
    _commit(origin, _ADDED_TF, "Declare a second alarm on main")
    _commit(clone, _BASE_TF + "# local edit\n", "Local-only change")

    joined = " ".join(_check(clone, mode=gate.MODE_TIP).lines)

    assert "diverged from" in joined, joined
    assert "ahead of" not in joined, joined


def test_ancestor_mode_still_rejects_a_tree_behind_the_tip(tmp_path: Path) -> None:
    origin = _origin(tmp_path)
    clone = _clone(origin, "behind", tmp_path)
    _commit(origin, _ADDED_TF, "Declare a second alarm on main")

    assert _check(clone, mode=gate.MODE_ANCESTOR).code == gate.EXIT_STALE


# --- the pure verdict function ------------------------------------------------------------ #


def test_evaluate_treats_an_uncomputable_merge_base_as_unknown() -> None:
    verdict = gate.evaluate(
        head="a" * 40,
        tip="b" * 40,
        merge_base=None,
        mode=gate.MODE_ANCESTOR,
        label="origin/main",
    )
    assert verdict.code == gate.EXIT_UNKNOWN


def test_cli_exit_code_matches_the_verdict(tmp_path: Path) -> None:
    """The workflow consumes the EXIT CODE, so it is part of the contract."""
    origin = _origin(tmp_path)
    clone = _clone(origin, "stale", tmp_path)
    _commit(origin, _ADDED_TF, "Declare a second alarm on main")

    proc = subprocess.run(
        [sys.executable, str(GATE), "--repo", str(clone)],
        env=_clean_git_env(),
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert proc.returncode == gate.EXIT_STALE, proc.stderr
    assert "STALE" in proc.stderr, proc.stdout
