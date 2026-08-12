"""Build-drift warning: a gate notices when its own binary predates the ref it pinned.

Ticket b273-e0ba-f719-4f1c. The motivating incident was silent: a rebar build predating the
``verify.overlap_enabled`` -> ``verify.suggest_duplicate_tickets`` rename read the CURRENT
base ref's config, did not recognise the current key, and ignored the operator's setting.

These tests drive the real ancestry logic against real throwaway git repos rather than
stubbing the git probes, so "behind", "ahead", and "not in this repo" are decided by git
exactly as they are in production. Only the RUNNING BUILD's sha is injected — there is no
other way to pretend the interpreter is a different build.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from rebar import _config_schema
from rebar.llm import build_drift


@pytest.fixture(autouse=True)
def _clean_drift_state() -> object:
    """Both the dedup set and the config wording flag are process-global; a leak between
    tests would make the once-per-pair assertions meaningless."""
    build_drift.reset_warned()
    yield
    build_drift.reset_warned()


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    (repo / "file.txt").write_text(message, encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-m", message, "--no-gpg-sign")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A three-commit linear repo: ``old`` -> middle -> ``tip``."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _git(root, "config", "commit.gpgsign", "false")
    for message in ("old", "middle", "tip"):
        _commit(root, message)
    return root


def _shas(repo: Path) -> tuple[str, str]:
    """``(oldest, tip)`` full shas."""
    log = _git(repo, "rev-list", "--reverse", "HEAD").splitlines()
    return log[0], log[-1]


def _run_as_build(monkeypatch: pytest.MonkeyPatch, sha: str | None, repo: Path) -> None:
    """Pretend the running build is ``sha`` and the target repo is ``repo``."""
    monkeypatch.setattr(build_drift, "running_build_sha", lambda: sha)
    monkeypatch.setattr(build_drift, "_resolve_repo_root", lambda _root: str(repo))


# ── the drift finding itself ─────────────────────────────────────────────────


def test_build_behind_pinned_ref_is_detected(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    """A build at an ANCESTOR of the pinned sha is drift, with an accurate commit count."""
    old, tip = _shas(repo)
    _run_as_build(monkeypatch, old, repo)

    drift = build_drift.detect_drift(tip, str(repo))

    assert drift is not None
    assert drift.build_sha == old
    assert drift.pinned_sha == tip
    assert drift.commits_behind == 2
    assert drift.build_date is not None and drift.build_date.count("-") == 2


def test_build_at_pinned_ref_is_silent(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    """The healthy case — build == pinned — must produce nothing at all."""
    _, tip = _shas(repo)
    _run_as_build(monkeypatch, tip, repo)

    assert build_drift.detect_drift(tip, str(repo)) is None


def test_build_ahead_of_pinned_ref_is_silent(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    """Running a NEWER build than the pinned ref is legitimate and is not 'behind'."""
    old, tip = _shas(repo)
    _run_as_build(monkeypatch, tip, repo)

    assert build_drift.detect_drift(old, str(repo)) is None


def test_diverged_build_is_silent(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    """A build on a sibling branch is not an ancestor — unknown relationship, so silent."""
    old, tip = _shas(repo)
    _git(repo, "checkout", "-q", "-b", "side", old)
    side = _commit(repo, "side-work")
    _run_as_build(monkeypatch, side, repo)

    assert build_drift.detect_drift(tip, str(repo)) is None


# ── every "cannot prove drift" path degrades to silence ──────────────────────


def test_absent_build_provenance_is_silent(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    """No baked/live sha (a source install outside git) must not warn — the dev-install
    warning-storm guard."""
    _, tip = _shas(repo)
    _run_as_build(monkeypatch, None, repo)

    assert build_drift.detect_drift(tip, str(repo)) is None


def test_absent_pinned_sha_is_silent(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    """A ``local``-source gate pins nothing, so there is no ref to be behind."""
    old, _tip = _shas(repo)
    _run_as_build(monkeypatch, old, repo)

    assert build_drift.detect_drift(None, str(repo)) is None


def test_build_sha_absent_from_target_repo_is_silent(
    monkeypatch: pytest.MonkeyPatch, repo: Path, tmp_path: Path
) -> None:
    """rebar reviewing a DIFFERENT repository: the gate-code sha is not an object there,
    so no comparison is possible and the gate stays quiet."""
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    _git(other, "config", "user.email", "t@example.com")
    _git(other, "config", "user.name", "T")
    _git(other, "config", "commit.gpgsign", "false")
    foreign_tip = _commit(other, "unrelated")

    old, _tip = _shas(repo)
    _run_as_build(monkeypatch, old, other)

    assert build_drift.detect_drift(foreign_tip, str(other)) is None


def test_dirty_build_still_compares_on_its_commit(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    """``_gate_commit_sha`` appends ``-dirty``; the suffix must not defeat rev-parse."""
    old, tip = _shas(repo)
    _run_as_build(monkeypatch, f"{old}-dirty", repo)

    drift = build_drift.detect_drift(tip, str(repo))

    assert drift is not None
    assert drift.dirty is True
    assert drift.build_sha == old


def test_git_failure_is_silent(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    """An unavailable git binary is provenance-unavailable, not a gate failure."""
    old, tip = _shas(repo)
    _run_as_build(monkeypatch, old, repo)
    monkeypatch.setattr(build_drift, "_git", lambda *_a, **_k: None)

    assert build_drift.detect_drift(tip, str(repo)) is None


def test_detection_failure_never_propagates(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    """warn_if_behind is called unguarded from the gate seam, so it must swallow."""

    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(build_drift, "detect_drift", _boom)

    assert build_drift.warn_if_behind("deadbeef", str(repo)) is None


# ── the warning: emitted, and emitted ONCE ───────────────────────────────────


def _drift_records(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING and "BEHIND" in r.getMessage()
    ]


def test_warns_once_per_pair(
    monkeypatch: pytest.MonkeyPatch, repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A run that resolves the gate handle repeatedly gets one warning, not a storm."""
    old, tip = _shas(repo)
    _run_as_build(monkeypatch, old, repo)

    with caplog.at_level(logging.WARNING, logger=build_drift.logger.name):
        for _ in range(3):
            assert build_drift.warn_if_behind(tip, str(repo)) is not None

    messages = _drift_records(caplog)
    assert len(messages) == 1, messages
    assert old[:9] in messages[0]
    assert tip[:9] in messages[0]
    assert "2 commit(s)" in messages[0]


def test_current_build_warns_not_at_all(
    monkeypatch: pytest.MonkeyPatch, repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _, tip = _shas(repo)
    _run_as_build(monkeypatch, tip, repo)

    with caplog.at_level(logging.WARNING, logger=build_drift.logger.name):
        assert build_drift.warn_if_behind(tip, str(repo)) is None

    assert _drift_records(caplog) == []


# ── AC4: the config unknown-key wording follows the drift state ──────────────


def _unknown_key_message(caplog: pytest.LogCaptureFixture) -> str:
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="rebar.config"):
        _config_schema._warn_unknown("verify", {"suggest_duplicate_tickets": True}, "")
    return caplog.records[0].getMessage()


def test_unknown_key_wording_default_is_the_typo_hint(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With no drift detected the long-standing wording is unchanged."""
    message = _unknown_key_message(caplog)

    assert "typo? see docs/config.md" in message
    assert "may predate" not in message


def test_unknown_key_wording_changes_when_build_is_behind(
    monkeypatch: pytest.MonkeyPatch, repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """After a gate proves drift, 'typo?' would misdirect — the incident's exact trap."""
    old, tip = _shas(repo)
    _run_as_build(monkeypatch, old, repo)
    build_drift.warn_if_behind(tip, str(repo))

    message = _unknown_key_message(caplog)

    assert "this build may predate it; see docs/config.md" in message
    assert "typo?" not in message


def test_unknown_key_wording_resets_for_a_current_build(
    monkeypatch: pytest.MonkeyPatch, repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A later, non-drifted gate in the same process must not inherit the drift wording."""
    old, tip = _shas(repo)
    _run_as_build(monkeypatch, old, repo)
    build_drift.warn_if_behind(tip, str(repo))

    _run_as_build(monkeypatch, tip, repo)
    build_drift.warn_if_behind(tip, str(repo))

    assert "typo? see docs/config.md" in _unknown_key_message(caplog)
