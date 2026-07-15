"""EV-3b: the relocated/decoupled store dir override REBAR_TRACKER_DIR.
The relocated/decoupled store dir is a supported feature.

(The scheduled TICKETS_TRACKER_DIR alias of REBAR_TRACKER_DIR was removed pre-1.0 —
DE7 — so it is now ignored; only REBAR_TRACKER_DIR is honored.)
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rebar import config as cfg

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REBAR_TRACKER_DIR", raising=False)
    monkeypatch.delenv("REBAR_ROOT", raising=False)
    cfg.reset_config_cache()  # config now feeds tracker_dir; don't leak resolution across cases


def _git_init(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    return path


def test_override_none_when_unset() -> None:
    assert cfg.tracker_dir_override() is None


def test_override_canonical(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REBAR_TRACKER_DIR", "/tmp/canon-tracker")
    assert cfg.tracker_dir_override() == "/tmp/canon-tracker"


def test_removed_legacy_alias_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # TICKETS_TRACKER_DIR is a load-bearing TOMBSTONE (story 36c7): still-setting the
    # removed store-location alias must FAIL LOUD from tracker_dir_override (the single
    # env-read source, which reconciler/read paths reach directly), NOT be silently
    # ignored into the wrong store. The autouse _clean_env does not delete it, so the
    # test manages TICKETS_TRACKER_DIR itself.
    from rebar._deprecations import RemovedInputError

    monkeypatch.delenv("REBAR_TRACKER_DIR", raising=False)
    monkeypatch.setenv("TICKETS_TRACKER_DIR", "/tmp/legacy")
    try:
        with pytest.raises(RemovedInputError):
            cfg.tracker_dir_override()
    finally:
        monkeypatch.delenv("TICKETS_TRACKER_DIR", raising=False)


def test_tracker_dir_uses_canonical(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REBAR_TRACKER_DIR", str(tmp_path / "store"))
    assert cfg.tracker_dir() == Path(str(tmp_path / "store"))


def test_tracker_dir_default_when_no_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REBAR_ROOT", str(tmp_path))
    assert cfg.tracker_dir() == tmp_path.resolve() / ".tickets-tracker"


def test_reads_tracker_dir_honors_canonical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar._engine_support import reads

    monkeypatch.setenv("REBAR_TRACKER_DIR", str(tmp_path / "rt"))
    assert reads.tracker_dir() == str(tmp_path / "rt")


# ── G6: the read-path git work-tree validation gate is preserved after unification ──
def test_reads_tracker_dir_validation_gate_rejects_non_git_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A supplied root that is NOT a git work tree must still sys.exit(1) with the
    historical message — the precondition reads.tracker_dir adds on top of the shared
    dir-name resolution (preserved when the duplicate was unified into config)."""
    from rebar._engine_support import reads

    non_git = tmp_path / "plain"  # a real dir, but no `git init`
    non_git.mkdir()
    with pytest.raises(SystemExit) as exc:
        reads.tracker_dir(repo_root=str(non_git))
    assert exc.value.code == 1
    assert "not inside a git repository" in capsys.readouterr().err


def test_reads_tracker_dir_passes_gate_for_real_git_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar._engine_support import reads

    repo = _git_init(tmp_path / "repo")
    assert reads.tracker_dir(repo_root=str(repo)) == str(repo / ".tickets-tracker")


# ── the configured custom name + branch flow through the resolvers (precedence) ──
def test_custom_tracker_dir_and_branch_from_project_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar._engine_support import reads

    repo = _git_init(tmp_path / "repo")
    (repo / "pyproject.toml").write_text(
        '[tool.rebar]\ntracker.dir = "custom-store"\ntracker.branch = "my-branch"\n',
        encoding="utf-8",
    )
    cfg.reset_config_cache()
    assert cfg.tracker_dir(root=str(repo)) == repo.resolve() / "custom-store"
    assert cfg.tickets_branch(root=str(repo)) == "my-branch"
    # reads.tracker_dir sources the same configured NAME and still passes its git gate.
    assert reads.tracker_dir(repo_root=str(repo)) == str(repo / "custom-store")


def test_tickets_branch_default() -> None:
    assert cfg.tickets_branch() == "tickets"
