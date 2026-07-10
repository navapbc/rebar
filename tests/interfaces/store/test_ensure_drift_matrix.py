"""Synthetic drift-matrix: a store initialized by an OLDER rebar, behind on various
combinations of the five ensure units, converged by `run_ensures` / `fsck --repair`.

Where the per-surface tests (test_ensures / test_pending_hint / test_fsck_ensures /
test_ensure_invariants) prove the wiring and single-condition behaviour, this file
proves the *differential* contract end-to-end: fabricate legacy drift on an arbitrary
subset of {env-id, gc-config, merge-ours, gitattributes, gitignore}, then assert that
a single sweep corrects EXACTLY the drifted units (`changed`), leaves the converged
ones untouched (`ok`), never `failed`, actually reconverges the store, and is a no-op
on the second pass (zero new commits). Then the operator path (`rebar fsck --repair`)
is shown to converge a fully-legacy store, and a pre-feature store (no marker) is
reported pending by the read-only line.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands import fsck as fsck_mod
from rebar._commands.init import _GITATTRIBUTES, _GITIGNORE, _RETIRED_GITATTRIBUTES_LINES
from rebar._store import ensures

ALL_UNITS = {"env-id", "gc-config", "merge-ours", "gitattributes", "gitignore"}


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "t@e"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=r, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "base"], cwd=r, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(r))
    monkeypatch.delenv("REBAR_TRACKER_DIR", raising=False)
    monkeypatch.setenv("REBAR_SYNC_PULL", "off")
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    rebar.init_repo(repo_root=str(r))  # fresh, fully-converged store
    ensures._reset_pending_cache()
    return r


def _tracker(repo: Path) -> Path:
    return repo / ".tickets-tracker"


def _git(tracker: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(tracker), *args], capture_output=True, text=True)


def _commit_count(tracker: Path) -> int:
    return int(_git(tracker, "rev-list", "--count", "tickets").stdout.strip())


def _commit_tracker_file(tracker: Path, name: str, content: str, msg: str) -> None:
    (tracker / name).write_text(content, encoding="utf-8")
    _git(tracker, "add", name)
    _git(tracker, "commit", "-q", "--no-verify", "-m", msg)


# ── fabricate legacy drift on a chosen subset ────────────────────────────────
def _make_behind(tracker: Path, units: set[str]) -> None:
    if "gc-config" in units:
        # An older rebar force-set gc.auto=0 and never set autoDetach.
        _git(tracker, "config", "gc.auto", "0")
        _git(tracker, "config", "--unset", "gc.autoDetach")
    if "merge-ours" in units:
        _git(tracker, "config", "--unset", "merge.ours.driver")
    if "gitattributes" in units:
        # A committed .gitattributes predating the ref-lock still carries the retired line.
        _commit_tracker_file(
            tracker,
            ".gitattributes",
            _GITATTRIBUTES + _RETIRED_GITATTRIBUTES_LINES[0] + "\n",
            "legacy .gitattributes with retired reconciler line",
        )
    if "gitignore" in units:
        # A stale committed .gitignore missing the newer lock/cache/marker entries.
        _commit_tracker_file(tracker, ".gitignore", ".env-id\n", "stale legacy .gitignore")
    if "env-id" in units:
        (tracker / ".env-id").unlink(missing_ok=True)
    ensures._reset_pending_cache()


# ── per-unit convergence assertions ──────────────────────────────────────────
def _assert_converged(tracker: Path) -> None:
    assert _git(tracker, "config", "--get", "gc.auto").returncode != 0, "gc.auto still set"
    assert _git(tracker, "config", "--get", "gc.autoDetach").stdout.strip() == "true"
    assert _git(tracker, "config", "--get", "merge.ours.driver").stdout.strip() == "true"
    ga = _git(tracker, "show", "tickets:.gitattributes").stdout
    assert all(r.strip() not in ga.splitlines() for r in _RETIRED_GITATTRIBUTES_LINES)
    gi = set(_git(tracker, "show", "tickets:.gitignore").stdout.splitlines())
    assert {ln for ln in _GITIGNORE.splitlines() if ln} <= gi, "gitignore still missing entries"
    assert (tracker / ".env-id").is_file(), ".env-id absent"


# ── the matrix ───────────────────────────────────────────────────────────────
DRIFT_SCENARIOS = [
    pytest.param(set(), id="S0-converged"),
    pytest.param({"gc-config"}, id="S1-legacy-gc"),
    pytest.param({"merge-ours"}, id="S2-no-merge-driver"),
    pytest.param({"gitattributes"}, id="S3-retired-gitattributes"),
    pytest.param({"gitignore"}, id="S4-stale-gitignore"),
    pytest.param({"env-id"}, id="S5-no-env-id"),
    pytest.param({"gc-config", "gitignore"}, id="S6-partial-gc+gitignore"),
    pytest.param({"env-id", "merge-ours", "gitattributes"}, id="S7-three-behind"),
    pytest.param(ALL_UNITS, id="S8-fully-legacy"),
]


@pytest.mark.parametrize("behind", DRIFT_SCENARIOS)
def test_run_ensures_corrects_exactly_the_drift(repo: Path, behind: set[str]) -> None:
    tracker = _tracker(repo)
    _make_behind(tracker, behind)
    before = _commit_count(tracker)

    outcomes = {o.id: o.status for o in ensures.run_ensures(tracker)}

    # No unit ever fails; the store fully converges.
    assert "failed" not in outcomes.values(), outcomes
    _assert_converged(tracker)

    # Config-only units (env-id/gc-config/merge-ours) never commit; the two commit
    # units (gitattributes/gitignore) commit at most once each when they were behind.
    commit_units_behind = behind & {"gitattributes", "gitignore"}
    assert _commit_count(tracker) - before == len(commit_units_behind), outcomes

    # Every drifted unit reports `changed`; every already-converged unit reports `ok`.
    for u in behind:
        assert outcomes[u] == "changed", f"{u} should be changed in {behind}: {outcomes}"
    for u in ALL_UNITS - behind:
        assert outcomes[u] == "ok", f"{u} should be ok in {behind}: {outcomes}"

    # The marker records every (non-failed) unit.
    assert ensures.applied_ids(tracker) == ALL_UNITS

    # Idempotent: a second sweep changes nothing and makes zero new commits.
    after_first = _commit_count(tracker)
    ensures._reset_pending_cache()
    outcomes2 = {o.id: o.status for o in ensures.run_ensures(tracker)}
    assert set(outcomes2.values()) == {"ok"}, outcomes2
    assert _commit_count(tracker) == after_first, "second sweep must not commit"


# ── operator path: fsck --repair converges a fully-legacy store ──────────────
def test_fsck_repair_converges_fully_legacy_store(repo: Path, capsys) -> None:
    tracker = _tracker(repo)
    _make_behind(tracker, ALL_UNITS)

    fsck_mod.fsck_cli(["--repair"], repo_root=str(repo))
    out = capsys.readouterr().out
    assert "ensures: swept 5 unit(s)" in out
    assert "5 changed" in out  # all five were behind
    _assert_converged(tracker)
    assert ensures.applied_ids(tracker) == ALL_UNITS

    # A second --repair on the now-converged store makes no new ensure commits.
    before = _commit_count(tracker)
    fsck_mod.fsck_cli(["--repair"], repo_root=str(repo))
    out2 = capsys.readouterr().out
    assert "0 changed" in out2
    assert _commit_count(tracker) == before


# ── pre-feature store (never swept) is reported pending by the read-only line ─
def test_pre_feature_store_reports_pending(repo: Path) -> None:
    """A store an older rebar initialized has no `.ensure-applied` marker at all —
    the read-only line reports it as fully pending until a sweep runs."""
    tracker = _tracker(repo)
    (tracker / ensures.APPLIED_MARKER).unlink()  # simulate: marker never written
    ensures._reset_pending_cache()

    out = rebar.fsck(repo_root=str(repo))
    assert "ensures: 0/5 applied" in out
    assert "run `rebar fsck --repair` to converge" in out

    # After the operator repairs, the same read-only line reports fully applied.
    fsck_mod.fsck_cli(["--repair"], repo_root=str(repo))
    ensures._reset_pending_cache()
    assert "ensures: 5/5 applied" in rebar.fsck(repo_root=str(repo))
