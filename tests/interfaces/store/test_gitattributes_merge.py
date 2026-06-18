"""``merge=ours`` for shared mutable root files (epic 97e7 / P1.4, WU-3).

uuid-named ticket dirs never collide, but ``.bridge_state/bindings.json`` and the
``.reconciler-*`` lock/gate files are rewritten every reconcile pass and would
conflict on the union reconverge (WU-2). They are derived caches the reconciler
rebuilds next pass — never ticket events — so the policy is "keep OURS". These
tests pin that ``init`` installs both halves of that policy (the committed
``.gitattributes`` AND the ``ours`` merge driver it names) and that a divergent
``bindings.json`` therefore merges without a wedge.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

import rebar


@pytest.fixture
def fresh_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "i"], cwd=repo, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    yield repo


def _tracker(repo: Path) -> Path:
    return repo / ".tickets-tracker"


def _git(tracker: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(tracker), *args], capture_output=True, text=True)


# ── init installs both halves of the merge=ours policy ────────────────────────
def test_init_commits_gitattributes_and_configures_driver(fresh_repo: Path) -> None:
    rebar.init_repo(repo_root=str(fresh_repo))
    tracker = _tracker(fresh_repo)

    show = _git(tracker, "show", "tickets:.gitattributes")
    assert show.returncode == 0, "init must commit .gitattributes on the tickets branch"
    assert ".bridge_state/* merge=ours" in show.stdout
    assert ".reconciler-* merge=ours" in show.stdout
    attr_lines = [ln for ln in show.stdout.splitlines() if ln.strip() and not ln.startswith("#")]
    assert all("merge=union" not in ln for ln in attr_lines), "union would corrupt JSON — must NOT be used"

    drv = _git(tracker, "config", "--get", "merge.ours.driver")
    assert drv.stdout.strip() == "true", "the 'ours' driver must be configured or the attr is ignored"


def test_gitattributes_migration_is_idempotent(fresh_repo: Path) -> None:
    rebar.init_repo(repo_root=str(fresh_repo))
    tracker = _tracker(fresh_repo)
    first = _git(tracker, "rev-list", "--count", "tickets").stdout.strip()
    # Re-init: create-if-absent means no NEW .gitattributes commit, no error.
    rebar.init_repo(repo_root=str(fresh_repo))
    second = _git(tracker, "rev-list", "--count", "tickets").stdout.strip()
    assert first == second, "re-init must not re-commit .gitattributes"
    assert _git(tracker, "config", "--get", "merge.ours.driver").stdout.strip() == "true"


# ── the payoff: a divergent bindings.json merges without a wedge ──────────────
def test_bindings_json_race_converges_keeping_ours(fresh_repo: Path) -> None:
    rebar.init_repo(repo_root=str(fresh_repo))
    tracker = _tracker(fresh_repo)

    bridge = tracker / ".bridge_state"
    bridge.mkdir(parents=True, exist_ok=True)
    (bridge / "bindings.json").write_text('{"v":1}\n')
    _git(tracker, "add", ".bridge_state/bindings.json")
    assert _git(tracker, "commit", "-q", "--no-verify", "-m", "base bindings").returncode == 0

    # A divergent rewrite on a sibling branch (models the other clone's pass).
    assert _git(tracker, "checkout", "-q", "-b", "other").returncode == 0
    (bridge / "bindings.json").write_text('{"v":2}\n')
    _git(tracker, "commit", "-aq", "--no-verify", "-m", "other rewrites bindings")

    # Our pass rewrites it differently.
    assert _git(tracker, "checkout", "-q", "tickets").returncode == 0
    (bridge / "bindings.json").write_text('{"v":3}\n')
    _git(tracker, "commit", "-aq", "--no-verify", "-m", "our pass rewrites bindings")

    merge = _git(tracker, "merge", "other", "--no-edit")
    assert merge.returncode == 0, f"bindings.json race wedged the merge:\n{merge.stdout}{merge.stderr}"
    assert (bridge / "bindings.json").read_text() == '{"v":3}\n', "merge=ours must keep OUR copy"
