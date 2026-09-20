"""Test the local build-freshness detector behind ``rebar doctor`` (ticket ae97-a37b).

The detector answers a question no remote sink can be trusted with: "is the hourly
updater that keeps this host's global ``rebar`` aligned to ``origin/main`` actually
working?" On 2026-09-03 the answer was no for 122 consecutive runs and 201 commits, and
the alert built to say so could not deliver, so nothing told the operator.

Two INDEPENDENT signals carry that answer -- ``reject-streak`` (the updater's own
counter) and ``build-stale`` (how far the published build trails ``origin/main``) --
because a stall that silences one can leave the other speaking.

Everything here runs against a synthetic state directory and a throwaway git repo. That
independence from live host state is deliberate: the ticket's AC1 REMOVES the condition
being detected, so a proof worded as "observe it on this host" would become unrunnable
the moment the host was repaired.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from rebar._commands import doctor_build_freshness as dbf


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(("git", *args), cwd=repo, check=True, capture_output=True, text=True)
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway repo with 40 commits on ``main`` and an ``origin/main`` tracking ref.

    40 is chosen to straddle the 25-commit default from both sides, so one fixture
    serves the above-threshold and below-threshold cases without reshaping.
    """
    work = tmp_path / "repo"
    work.mkdir()
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "t@e.com"),
        ("config", "user.name", "t"),
    ):
        _git(work, *args)
    for n in range(40):
        _git(work, "commit", "-q", "--allow-empty", "-m", f"c{n}")
    _git(work, "update-ref", "refs/remotes/origin/main", "HEAD")
    return work


def _sha_at(repo: Path, back: int) -> str:
    """The sha ``back`` commits before ``origin/main``."""
    return _git(repo, "rev-parse", f"origin/main~{back}")


def _plant_state(home: Path, *, streak: str | None = None, current_sha: str | None = None) -> Path:
    """Plant an updater state directory under ``home``, omitting either half.

    Omission is a real on-disk shape, not a test convenience: the updater writes the
    counter and the ``current`` pointer at different stages, so a run interrupted between
    them leaves exactly one of them present.
    """
    state = home / dbf.STATE_RELPATH
    state.mkdir(parents=True, exist_ok=True)
    if streak is not None:
        (state / "reject-streak").write_text(streak)
    if current_sha is not None:
        (state / "current").mkdir(exist_ok=True)
        (state / "current" / "sha").write_text(current_sha + "\n")
    return state


def _by_signal(findings: list[dict], signal: str) -> dict:
    matched = [f for f in findings if f["signal"] == signal]
    assert len(matched) == 1, f"expected exactly one {signal!r} finding, got {matched}"
    return matched[0]


# ---------------------------------------------------------------------------
# Documented defaults
# ---------------------------------------------------------------------------


def test_thresholds_are_module_constants_carrying_the_documented_defaults():
    """The thresholds are named constants, NOT config keys or env vars.

    That choice is load-bearing rather than stylistic: the mechanism-delta ratchet
    counts ``config_key``/``env_var``/``feature_flag``, and this detector needs no new
    configuration surface to do its job.
    """
    assert dbf.REJECT_STREAK_ALERT == 3
    assert dbf.MAX_COMMITS_BEHIND == 25


# ---------------------------------------------------------------------------
# Signal 1 -- reject-streak
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("streak", ["3", "4", "122"])
def test_reject_streak_at_or_above_the_threshold_warns(tmp_path, repo, streak):
    """At the threshold warns too -- ``>=`` is the updater's own alert rule."""
    home = tmp_path / "home"
    _plant_state(home, streak=streak, current_sha=_git(repo, "rev-parse", "origin/main"))
    finding = _by_signal(
        dbf.scan_build_freshness(home=home, repo_root=repo), dbf.SIGNAL_REJECT_STREAK
    )
    assert finding["severity"] == dbf.SEVERITY_WARNING
    assert finding["kind"] == dbf.KIND_REJECT_STREAK
    assert finding["streak"] == int(streak)
    assert streak in finding["detail"]


@pytest.mark.parametrize("streak", ["0", "1", "2"])
def test_reject_streak_below_the_threshold_is_ok(tmp_path, repo, streak):
    home = tmp_path / "home"
    _plant_state(home, streak=streak, current_sha=_git(repo, "rev-parse", "origin/main"))
    finding = _by_signal(
        dbf.scan_build_freshness(home=home, repo_root=repo), dbf.SIGNAL_REJECT_STREAK
    )
    assert finding["severity"] == dbf.SEVERITY_OK
    assert finding["streak"] == int(streak)


@pytest.mark.parametrize("corrupt", ["", "   ", "not-a-number", "3.5", "-\n"])
def test_a_corrupt_reject_streak_counter_is_unavailable_not_silence(tmp_path, repo, corrupt):
    """A counter that cannot be read is reported, never treated as zero.

    Reading it as zero is the failure mode that matters: it would turn the exact
    condition the detector exists to catch into a clean bill of health.
    """
    home = tmp_path / "home"
    _plant_state(home, streak=corrupt, current_sha=_git(repo, "rev-parse", "origin/main"))
    finding = _by_signal(
        dbf.scan_build_freshness(home=home, repo_root=repo), dbf.SIGNAL_REJECT_STREAK
    )
    assert finding["severity"] == dbf.SEVERITY_UNAVAILABLE
    assert finding["kind"] == dbf.KIND_STREAK_UNREADABLE


def test_a_missing_counter_beside_a_live_state_dir_is_unavailable(tmp_path, repo):
    home = tmp_path / "home"
    _plant_state(home, current_sha=_git(repo, "rev-parse", "origin/main"))
    finding = _by_signal(
        dbf.scan_build_freshness(home=home, repo_root=repo), dbf.SIGNAL_REJECT_STREAK
    )
    assert finding["severity"] == dbf.SEVERITY_UNAVAILABLE


def test_the_reject_streak_threshold_is_overridable_per_call(tmp_path, repo):
    home = tmp_path / "home"
    _plant_state(home, streak="2", current_sha=_git(repo, "rev-parse", "origin/main"))
    findings = dbf.scan_build_freshness(home=home, repo_root=repo, reject_streak_alert=2)
    assert _by_signal(findings, dbf.SIGNAL_REJECT_STREAK)["severity"] == dbf.SEVERITY_WARNING


# ---------------------------------------------------------------------------
# Signal 2 -- build-stale
# ---------------------------------------------------------------------------


def test_a_build_further_behind_than_the_threshold_warns(tmp_path, repo):
    home = tmp_path / "home"
    _plant_state(home, streak="0", current_sha=_sha_at(repo, 30))
    finding = _by_signal(
        dbf.scan_build_freshness(home=home, repo_root=repo), dbf.SIGNAL_BUILD_STALE
    )
    assert finding["severity"] == dbf.SEVERITY_WARNING
    assert finding["kind"] == dbf.KIND_BUILD_STALE
    assert finding["commits_behind"] == 30
    assert "30" in finding["detail"]


@pytest.mark.parametrize("back", [0, 1, 24, 25])
def test_a_build_at_or_within_the_threshold_is_ok(tmp_path, repo, back):
    """25 behind is OK; the rule is *more than* 25, matching the ticket's wording."""
    home = tmp_path / "home"
    _plant_state(home, streak="0", current_sha=_sha_at(repo, back))
    finding = _by_signal(
        dbf.scan_build_freshness(home=home, repo_root=repo), dbf.SIGNAL_BUILD_STALE
    )
    assert finding["severity"] == dbf.SEVERITY_OK
    assert finding["commits_behind"] == back


def test_a_sha_absent_from_the_repo_is_unavailable_not_ok(tmp_path, repo):
    """An unresolvable build sha means "cannot measure", which is not "not behind".

    Collapsing the two would let a state dir pointing at a garbage-collected or foreign
    commit read as healthy forever.
    """
    home = tmp_path / "home"
    _plant_state(home, streak="0", current_sha="0" * 40)
    finding = _by_signal(
        dbf.scan_build_freshness(home=home, repo_root=repo), dbf.SIGNAL_BUILD_STALE
    )
    assert finding["severity"] == dbf.SEVERITY_UNAVAILABLE
    assert finding["kind"] == dbf.KIND_BUILD_UNMEASURABLE


def test_a_missing_current_pointer_beside_a_live_state_dir_is_unavailable(tmp_path, repo):
    home = tmp_path / "home"
    _plant_state(home, streak="0")
    finding = _by_signal(
        dbf.scan_build_freshness(home=home, repo_root=repo), dbf.SIGNAL_BUILD_STALE
    )
    assert finding["severity"] == dbf.SEVERITY_UNAVAILABLE


def test_a_build_ahead_of_origin_main_is_ok_not_a_fault(tmp_path, repo):
    """A local commit past the tracking ref is normal on a dev box, never a finding."""
    home = tmp_path / "home"
    _git(repo, "commit", "-q", "--allow-empty", "-m", "ahead")
    _plant_state(home, streak="0", current_sha=_git(repo, "rev-parse", "HEAD"))
    finding = _by_signal(
        dbf.scan_build_freshness(home=home, repo_root=repo), dbf.SIGNAL_BUILD_STALE
    )
    assert finding["severity"] == dbf.SEVERITY_OK


def test_the_staleness_threshold_is_overridable_per_call(tmp_path, repo):
    home = tmp_path / "home"
    _plant_state(home, streak="0", current_sha=_sha_at(repo, 10))
    findings = dbf.scan_build_freshness(home=home, repo_root=repo, max_commits_behind=5)
    assert _by_signal(findings, dbf.SIGNAL_BUILD_STALE)["severity"] == dbf.SEVERITY_WARNING


def test_the_two_signals_are_independent(tmp_path, repo):
    """A healthy counter must not suppress a stale build, nor the reverse.

    This is the whole reason there are two: the recorded incident had both, but a stall
    that resets the counter while leaving the build pinned would show only one.
    """
    home = tmp_path / "home"
    _plant_state(home, streak="0", current_sha=_sha_at(repo, 30))
    findings = dbf.scan_build_freshness(home=home, repo_root=repo)
    assert _by_signal(findings, dbf.SIGNAL_REJECT_STREAK)["severity"] == dbf.SEVERITY_OK
    assert _by_signal(findings, dbf.SIGNAL_BUILD_STALE)["severity"] == dbf.SEVERITY_WARNING


def test_the_recorded_incident_fires_both_signals(tmp_path, repo):
    """Replay 2026-09-03: reject-streak 122, build 201 commits behind.

    The live-host observation cannot be re-run -- the ticket's AC1 repairs the host, and
    a repaired host no longer exhibits the condition. Pinning the incident's own numbers
    against a synthetic state directory is what keeps the evidence reproducible: it says
    the detector fires on the real thing, not merely on a number someone chose.
    """
    home = tmp_path / "home"
    for _ in range(162):  # 40 from the fixture + 162 = 202 commits, so ~201 behind
        _git(repo, "commit", "-q", "--allow-empty", "-m", "drift")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _plant_state(home, streak="122", current_sha=_sha_at(repo, 201))
    findings = dbf.scan_build_freshness(home=home, repo_root=repo)

    streak = _by_signal(findings, dbf.SIGNAL_REJECT_STREAK)
    assert streak["severity"] == dbf.SEVERITY_WARNING
    assert streak["streak"] == 122

    stale = _by_signal(findings, dbf.SIGNAL_BUILD_STALE)
    assert stale["severity"] == dbf.SEVERITY_WARNING
    assert stale["commits_behind"] == 201

    assert dbf.has_stale_build(findings)


# ---------------------------------------------------------------------------
# Absent state -- a box running no such updater
# ---------------------------------------------------------------------------


def test_no_updater_on_this_box_emits_exactly_one_unavailable_finding(tmp_path, repo):
    """Most boxes run no such updater. That is not a fault, and not two faults either."""
    home = tmp_path / "home"
    home.mkdir()
    findings = dbf.scan_build_freshness(home=home, repo_root=repo)
    assert len(findings) == 1
    assert findings[0]["severity"] == dbf.SEVERITY_UNAVAILABLE
    assert findings[0]["kind"] == dbf.KIND_UPDATER_ABSENT


def test_an_unreadable_state_dir_degrades_to_unavailable_rather_than_raising(tmp_path, repo):
    """A file where the state DIRECTORY should be is malformed, not absent."""
    home = tmp_path / "home"
    home.mkdir()
    state = home / dbf.STATE_RELPATH
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("not a directory")
    findings = dbf.scan_build_freshness(home=home, repo_root=repo)
    assert len(findings) == 1
    assert findings[0]["severity"] == dbf.SEVERITY_UNAVAILABLE


def test_an_unresolvable_repo_root_never_raises(tmp_path):
    """No git data is "cannot measure" on both signals -- never an exception."""
    home = tmp_path / "home"
    _plant_state(home, streak="0", current_sha="0" * 40)
    findings = dbf.scan_build_freshness(home=home, repo_root=tmp_path / "nonexistent")
    assert _by_signal(findings, dbf.SIGNAL_BUILD_STALE)["severity"] == dbf.SEVERITY_UNAVAILABLE


# ---------------------------------------------------------------------------
# Severity predicate and rendering
# ---------------------------------------------------------------------------


def test_no_finding_is_ever_an_error_severity(tmp_path, repo):
    """Every finding is ADVISORY. ``error`` is the severity doctor gates its exit on."""
    home = tmp_path / "home"
    _plant_state(home, streak="122", current_sha=_sha_at(repo, 30))
    findings = dbf.scan_build_freshness(home=home, repo_root=repo)
    assert findings
    assert all(f["severity"] != "error" for f in findings)


def test_has_stale_build_is_the_seam_for_a_caller_that_wants_to_gate(tmp_path, repo):
    home = tmp_path / "home"
    _plant_state(home, streak="122", current_sha=_sha_at(repo, 30))
    assert dbf.has_stale_build(dbf.scan_build_freshness(home=home, repo_root=repo))
    _plant_state(home, streak="0", current_sha=_git(repo, "rev-parse", "origin/main"))
    assert not dbf.has_stale_build(dbf.scan_build_freshness(home=home, repo_root=repo))


def test_render_text_always_emits_its_header_even_with_no_findings():
    """The header is what distinguishes a healthy box from a check that did not run."""
    lines = dbf.render_text([])
    assert lines and lines[0].startswith("doctor: build freshness")


def test_render_text_names_each_signal_its_severity_and_its_detail(tmp_path, repo):
    home = tmp_path / "home"
    _plant_state(home, streak="122", current_sha=_sha_at(repo, 30))
    rendered = "\n".join(dbf.render_text(dbf.scan_build_freshness(home=home, repo_root=repo)))
    assert dbf.SIGNAL_REJECT_STREAK in rendered
    assert dbf.SIGNAL_BUILD_STALE in rendered
    assert dbf.SEVERITY_WARNING in rendered
    assert "122" in rendered and "30" in rendered


# ---------------------------------------------------------------------------
# Wiring into `rebar doctor`
# ---------------------------------------------------------------------------


@pytest.fixture
def doctor_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real rebar store, so ``doctor_cli`` runs its store scans for real."""
    import rebar

    work = tmp_path / "store"
    work.mkdir()
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "t@e.com"),
        ("config", "user.name", "t"),
        ("commit", "-q", "--allow-empty", "-m", "init"),
    ):
        _git(work, *args)
    monkeypatch.setenv("REBAR_ROOT", str(work))
    monkeypatch.setenv("REBAR_SYNC_PULL", "off")
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    monkeypatch.setenv("REBAR_SIGNING_KEY", "test-signing-key-ae97")
    rebar.init_repo(repo_root=str(work))
    return work


def test_doctor_json_carries_the_build_freshness_findings(doctor_repo, capsys):
    from rebar._commands.doctor import doctor_cli

    assert doctor_cli(["--output", "json"], repo_root=str(doctor_repo)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "build_freshness_findings" in payload
    assert isinstance(payload["build_freshness_findings"], list)


def test_doctor_text_renders_the_build_freshness_section(doctor_repo, capsys):
    from rebar._commands.doctor import doctor_cli

    assert doctor_cli([], repo_root=str(doctor_repo)) == 0
    assert "doctor: build freshness" in capsys.readouterr().out


def test_a_warning_finding_does_not_change_doctors_exit_code(doctor_repo, capsys, monkeypatch):
    """The whole advisory claim, asserted where it matters: on the exit code.

    Folding a HOME-sourced signal into a store-health exit would make a CI gate depend on
    whichever updater happens to sit on the box running it -- the same ground on which
    doctor already excludes its MCP-client findings.
    """
    from rebar._commands import doctor as doctor_mod

    warning = {
        "signal": dbf.SIGNAL_BUILD_STALE,
        "severity": dbf.SEVERITY_WARNING,
        "kind": dbf.KIND_BUILD_STALE,
        "detail": "201 commits behind",
        "commits_behind": 201,
    }
    monkeypatch.setattr(
        doctor_mod.doctor_build_freshness, "scan_build_freshness", lambda **_: [warning]
    )
    assert doctor_cli_exit(doctor_mod, doctor_repo) == 0
    assert "201 commits behind" in capsys.readouterr().out


def doctor_cli_exit(doctor_mod, repo_root: Path) -> int:
    return doctor_mod.doctor_cli([], repo_root=str(repo_root))


def test_repair_never_acts_on_a_build_freshness_finding(doctor_repo, capsys, monkeypatch):
    """``--repair`` iterates ``findings``; these must never reach that list.

    Nothing here describes the store, so there is nothing for a repair pass to convert --
    and a repair loop that tried would be acting on the operator's home directory.
    """
    from rebar._commands import doctor as doctor_mod

    seen: list[object] = []
    real_repair = doctor_mod.run_repair

    def _spy(findings, tracker, **kw):
        seen.extend(findings)
        return real_repair(findings, tracker, **kw)

    monkeypatch.setattr(doctor_mod, "run_repair", _spy)
    monkeypatch.setattr(
        doctor_mod.doctor_build_freshness,
        "scan_build_freshness",
        lambda **_: [
            {
                "signal": dbf.SIGNAL_BUILD_STALE,
                "severity": dbf.SEVERITY_WARNING,
                "kind": dbf.KIND_BUILD_STALE,
                "detail": "stale",
            }
        ],
    )
    doctor_mod.doctor_cli(["--repair"], repo_root=str(doctor_repo))
    capsys.readouterr()
    assert not [f for f in seen if f.get("signal") == dbf.SIGNAL_BUILD_STALE]
