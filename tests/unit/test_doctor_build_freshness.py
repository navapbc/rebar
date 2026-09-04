"""Local detection of a stalled main-tracking updater / an over-stale rebar build.

Bug ae97-a37b-9fa3-413a. The host's hourly ``com.navapbc.rebar-dev-update`` LaunchAgent
rejected **120 consecutive** candidates on a uv ``required-version`` mismatch, leaving the
global ``rebar`` ~195 commits and five days behind ``origin/main``. Every agent session on
the box ran gates from that build. The only detector was a remote SNS alert that 403'd on
every attempt (cross-account IAM), so the streak reached 120 against a threshold of 3 with
zero operator signal — a strictly worse recurrence of the b477339a99 incident the alert was
added to prevent.

These tests pin the LOCAL detector: it must read the two signals already on disk
(``reject-streak`` and the published ``current`` build) and answer without touching any
remote sink. Absent updater state must read as "not applicable" — most developers do not
run the LaunchAgent, and a check that fails on a box that never had it is a check that gets
ignored.

Drift is decided by real git against real throwaway repos, exactly as
``tests/unit/test_build_drift.py`` does, so "behind" means what git means.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from rebar._commands import doctor_build_freshness as dbf

# ── helpers ──────────────────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A linear repo whose ``origin/main`` is 30 commits ahead of its first commit."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _git(root, "config", "commit.gpgsign", "false")
    for n in range(31):
        (root / "f.txt").write_text(str(n), encoding="utf-8")
        _git(root, "add", "f.txt")
        _git(root, "commit", "-m", f"c{n}", "--no-gpg-sign")
    # A local ref standing in for origin/main so no network or second repo is needed.
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    return root


def _shas(repo: Path) -> list[str]:
    return _git(repo, "rev-list", "--reverse", "HEAD").splitlines()


def _state_dir(tmp_path: Path, *, streak: str | None = None, published: str | None = None) -> Path:
    """A synthetic ``~/.local/state/rebar-dev`` in the shape the updater writes."""
    state = tmp_path / "rebar-dev"
    state.mkdir(parents=True, exist_ok=True)
    if streak is not None:
        (state / "reject-streak").write_text(streak, encoding="utf-8")
    if published is not None:
        release = state / "releases" / f"{published}.4242"
        release.mkdir(parents=True)
        (release / "sha").write_text(published + "\n", encoding="utf-8")
        (state / "current").symlink_to(Path("releases") / f"{published}.4242")
    return state


def _kinds(findings: list[dict]) -> set[str]:
    return {f["kind"] for f in findings}


def _of_kind(findings: list[dict], kind: str) -> dict:
    matches = [f for f in findings if f["kind"] == kind]
    assert matches, f"no {kind!r} finding in {_kinds(findings)}"
    return matches[0]


# ── "not applicable" is not a failure ────────────────────────────────────────


@pytest.mark.unit
def test_absent_updater_state_reads_as_not_applicable(tmp_path: Path) -> None:
    """A box that never ran the LaunchAgent must not be reported as broken."""
    findings = dbf.scan_build_freshness(state_dir=tmp_path / "nope", repo_root=None)

    assert _kinds(findings) == {dbf.KIND_UPDATER_ABSENT}
    assert _of_kind(findings, dbf.KIND_UPDATER_ABSENT)["severity"] == dbf.SEVERITY_UNAVAILABLE
    assert dbf.has_blocking_build_freshness(findings) is False


# ── signal 1: the reject streak ──────────────────────────────────────────────


@pytest.mark.unit
def test_reject_streak_over_threshold_is_a_blocking_finding(tmp_path: Path) -> None:
    """The 120-run streak this bug is about, detected with no remote sink involved."""
    findings = dbf.scan_build_freshness(state_dir=_state_dir(tmp_path, streak="120\n"))

    finding = _of_kind(findings, dbf.KIND_REJECT_STREAK)
    assert finding["severity"] == dbf.SEVERITY_ERROR
    assert finding["streak"] == 120
    assert finding["threshold"] == dbf.DEFAULT_REJECT_STREAK_ALERT
    # The operator must be able to act from the line alone: it names both numbers.
    assert "120" in finding["detail"] and str(dbf.DEFAULT_REJECT_STREAK_ALERT) in finding["detail"]
    assert dbf.has_blocking_build_freshness(findings) is True


@pytest.mark.unit
def test_zero_streak_is_healthy(tmp_path: Path) -> None:
    findings = dbf.scan_build_freshness(state_dir=_state_dir(tmp_path, streak="0\n"))

    assert dbf.KIND_REJECT_STREAK not in _kinds(findings)
    assert dbf.has_blocking_build_freshness(findings) is False


@pytest.mark.unit
def test_streak_at_the_threshold_fires(tmp_path: Path) -> None:
    """The updater alerts at ``>=`` its threshold; the local detector must agree."""
    streak = dbf.DEFAULT_REJECT_STREAK_ALERT
    findings = dbf.scan_build_freshness(state_dir=_state_dir(tmp_path, streak=f"{streak}\n"))

    assert _of_kind(findings, dbf.KIND_REJECT_STREAK)["streak"] == streak


@pytest.mark.unit
def test_unreadable_streak_file_is_reported_not_raised(tmp_path: Path) -> None:
    """A corrupt counter means the detector is BLIND, which is itself worth saying."""
    findings = dbf.scan_build_freshness(state_dir=_state_dir(tmp_path, streak="not-a-number"))

    assert _of_kind(findings, dbf.KIND_STATE_UNREADABLE)["severity"] == dbf.SEVERITY_WARNING


# ── signal 2: how stale the published build is ───────────────────────────────


@pytest.mark.unit
def test_published_build_far_behind_origin_main_is_a_blocking_finding(
    tmp_path: Path, repo: Path
) -> None:
    """The stale-global-build half: 30 commits behind, read off ``current``."""
    oldest = _shas(repo)[0]
    findings = dbf.scan_build_freshness(
        state_dir=_state_dir(tmp_path, streak="0\n", published=oldest), repo_root=str(repo)
    )

    finding = _of_kind(findings, dbf.KIND_BUILD_STALE)
    assert finding["severity"] == dbf.SEVERITY_ERROR
    assert finding["commits_behind"] == 30
    assert finding["build_sha"] == oldest
    assert "30" in finding["detail"]
    assert dbf.has_blocking_build_freshness(findings) is True


@pytest.mark.unit
def test_published_build_at_the_tip_is_healthy(tmp_path: Path, repo: Path) -> None:
    tip = _shas(repo)[-1]
    findings = dbf.scan_build_freshness(
        state_dir=_state_dir(tmp_path, streak="0\n", published=tip), repo_root=str(repo)
    )

    assert dbf.KIND_BUILD_STALE not in _kinds(findings)
    assert dbf.has_blocking_build_freshness(findings) is False


@pytest.mark.unit
def test_drift_below_the_threshold_is_not_reported(tmp_path: Path, repo: Path) -> None:
    """An hourly updater is routinely a commit or two behind; that is not a stall."""
    one_back = _shas(repo)[-2]
    findings = dbf.scan_build_freshness(
        state_dir=_state_dir(tmp_path, streak="0\n", published=one_back), repo_root=str(repo)
    )

    assert dbf.KIND_BUILD_STALE not in _kinds(findings)


@pytest.mark.unit
def test_a_published_sha_absent_from_the_repo_degrades_to_silence(
    tmp_path: Path, repo: Path
) -> None:
    """Foreign or garbage provenance proves nothing, so it must not invent drift."""
    findings = dbf.scan_build_freshness(
        state_dir=_state_dir(tmp_path, streak="0\n", published="0" * 40), repo_root=str(repo)
    )

    assert dbf.KIND_BUILD_STALE not in _kinds(findings)
    assert dbf.has_blocking_build_freshness(findings) is False


# ── where the state dir comes from ───────────────────────────────────────────


@pytest.mark.unit
def test_default_state_dir_is_the_path_the_updater_actually_writes(tmp_path: Path) -> None:
    """Not a generalisation over ``$XDG_STATE_HOME``: the updater does not read it, so
    following it here would describe a layout that is never written."""
    assert dbf.default_state_dir(home=tmp_path) == tmp_path / ".local" / "state" / "rebar-dev"


# ── rendering, and the reuse seam it is built on ─────────────────────────────


@pytest.mark.unit
def test_render_text_emits_a_header_and_one_line_per_finding(tmp_path: Path) -> None:
    findings = dbf.scan_build_freshness(state_dir=_state_dir(tmp_path, streak="120\n"))
    lines = dbf.render_text(findings)

    assert lines[0].startswith("doctor: ")
    assert len(lines) == len(findings) + 1
    assert any("120" in line for line in lines[1:])


@pytest.mark.unit
def test_drift_detection_accepts_an_explicit_build_sha(repo: Path) -> None:
    """The detector must REUSE ``build_drift``, not re-derive the ancestry walk.

    ``detect_drift`` previously read the sha of the RUNNING process only, which cannot
    answer "how stale is the build the updater published" from a dev checkout.
    """
    from rebar.llm import build_drift

    shas = _shas(repo)
    drift = build_drift.detect_drift(shas[-1], str(repo), build_sha=shas[0])

    assert drift is not None
    assert drift.commits_behind == 30
    assert drift.build_sha == shas[0]


# ── wiring: the report reaches the operator through ``rebar doctor`` ─────────


def _clean_tracker(tmp_path: Path) -> Path:
    origin, tracker = tmp_path / "origin", tmp_path / "tracker"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "tickets")
    _git(origin, "config", "user.email", "t@t")
    _git(origin, "config", "user.name", "t")
    (origin / ".gitignore").write_text(
        ".ticket-write.lock\n.ticket-write.lock.d/\n.cache.json\n", encoding="utf-8"
    )
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "base", "--no-gpg-sign")
    subprocess.run(["git", "clone", "-q", "-b", "tickets", str(origin), str(tracker)], check=True)
    _git(tracker, "config", "user.email", "t@t")
    _git(tracker, "config", "user.name", "t")
    return tracker


@pytest.mark.unit
def test_doctor_reports_build_freshness_in_json_and_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A finding the operator never sees is not a detector."""
    from rebar._commands import doctor

    tracker = _clean_tracker(tmp_path)
    monkeypatch.setattr(doctor, "tracker_dir", lambda _repo_root=None: tracker)
    monkeypatch.setattr(doctor, "_reconciler_in_flight", lambda *_a, **_k: False)
    monkeypatch.setattr(
        doctor_module_freshness := doctor.doctor_build_freshness,
        "scan_build_freshness",
        lambda **_kw: [
            {
                "kind": dbf.KIND_REJECT_STREAK,
                "severity": dbf.SEVERITY_ERROR,
                "detail": "120 consecutive candidate rejections (threshold 3)",
                "streak": 120,
            }
        ],
    )
    assert doctor_module_freshness is dbf

    rc_json = doctor.doctor_cli(["--output", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["build_freshness_findings"][0]["streak"] == 120

    rc_text = doctor.doctor_cli([])
    text = capsys.readouterr().out
    assert "120 consecutive candidate rejections" in text

    # Advisory, exactly like the MCP-client section: these findings describe the HOST,
    # not the store, so folding them into the exit code would make store health depend
    # on whichever LaunchAgent happens to run on the box.
    assert rc_json == 0
    assert rc_text == 0
