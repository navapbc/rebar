"""Held-out fail-closed, override, cache, and public-surface init contracts."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

import rebar
from rebar import config
from rebar._cli import _init as cli_init
from rebar._commands import _init_probe, init


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result


def _host_repo(tmp_path: Path, *, remote: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "host")
    _git(repo, "remote", "add", "origin", str(remote))
    return repo


def _bare_remote(tmp_path: Path, *, advertised: bool) -> tuple[Path, str | None]:
    remote = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", "--initial-branch=tickets", str(remote)],
        check=True,
        capture_output=True,
    )
    if not advertised:
        return remote, None
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "--initial-branch=tickets")
    _git(seed, "config", "user.email", "test@example.com")
    _git(seed, "config", "user.name", "Test")
    (seed / "event.json").write_text("{}\n", encoding="utf-8")
    _git(seed, "add", "event.json")
    _git(seed, "commit", "-q", "-m", "seed tickets")
    sha = _git(seed, "rev-parse", "HEAD").stdout.strip()
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-q", "origin", "HEAD:tickets")
    return remote, sha


def _env(repo: Path) -> dict[str, str]:
    env = subprocess_env()
    env["REBAR_ROOT"] = str(repo)
    env["REBAR_SYNC_PUSH"] = "off"
    env["REBAR_SYNC_PULL"] = "off"
    env.pop("REBAR_TRACKER_DIR", None)
    return env


def _env_with_failing_fetch(repo: Path, tmp_path: Path) -> dict[str, str]:
    real_git = shutil.which("git")
    assert real_git is not None
    shim_dir = tmp_path / "git-shim"
    shim_dir.mkdir()
    shim = shim_dir / "git"
    shim.write_text(
        f"""#!{sys.executable}
import os
import sys

if "fetch" in sys.argv[1:]:
    print("injected fetch failure", file=sys.stderr)
    raise SystemExit(1)

real_git = os.environ["REBAR_TEST_REAL_GIT"]
os.execv(real_git, [real_git, *sys.argv[1:]])
""",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    env = _env(repo)
    env["REBAR_TEST_REAL_GIT"] = real_git
    env["PATH"] = f"{shim_dir}{os.pathsep}{env['PATH']}"
    return env


@pytest.fixture(autouse=True)
def _reset_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REBAR_TRACKER_DIR", raising=False)
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    monkeypatch.setenv("REBAR_SYNC_PULL", "off")
    config.reset_config_cache()


def test_unreachable_explicit_init_fails_closed_without_orphan(tmp_path: Path) -> None:
    repo = _host_repo(tmp_path, remote=tmp_path / "missing-origin.git")

    result = subprocess.run(
        [sys.executable, "-m", "rebar", "init"],
        cwd=repo,
        env=_env(repo),
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode != 0
    assert not (repo / ".tickets-tracker").exists()
    assert _git(repo, "show-ref", "--verify", "refs/heads/tickets", check=False).returncode != 0
    assert "origin" in result.stderr and "tickets" in result.stderr
    assert "force-new-store" in result.stderr


def test_force_new_store_is_required_for_unreachable_cli_remote(tmp_path: Path) -> None:
    repo = _host_repo(tmp_path, remote=tmp_path / "missing-origin.git")

    refused = subprocess.run(
        [sys.executable, "-m", "rebar", "init"],
        cwd=repo,
        env=_env(repo),
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert refused.returncode != 0
    assert not (repo / ".tickets-tracker").exists()

    forced = subprocess.run(
        [sys.executable, "-m", "rebar", "init", "--force-new-store"],
        cwd=repo,
        env=_env(repo),
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert forced.returncode == 0, forced.stderr
    assert (repo / ".tickets-tracker").is_dir()


def test_library_override_is_required_for_unreachable_remote(tmp_path: Path) -> None:
    repo = _host_repo(tmp_path, remote=tmp_path / "missing-origin.git")

    with pytest.raises(rebar.RebarError):
        rebar.init_repo(repo_root=repo)
    assert not (repo / ".tickets-tracker").exists()

    rebar.init_repo(repo_root=repo, force_new_store=True)
    assert (repo / ".tickets-tracker").is_dir()


def test_force_cannot_override_an_advertised_branch_fetch_failure(
    tmp_path: Path,
) -> None:
    remote, _sha = _bare_remote(tmp_path, advertised=True)
    repo = _host_repo(tmp_path, remote=remote)

    result = subprocess.run(
        [sys.executable, "-m", "rebar", "init", "--force-new-store"],
        cwd=repo,
        env=_env_with_failing_fetch(repo, tmp_path),
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode != 0
    assert not (repo / ".tickets-tracker").exists()
    assert _git(repo, "show-ref", "--verify", "refs/heads/tickets", check=False).returncode != 0
    assert "no effect" in result.stderr and "origin" in result.stderr
    assert "tickets" in result.stderr and "fetch" in result.stderr
    assert "retry" in result.stderr


def test_force_warns_but_preserves_reachable_absent_bootstrap(
    tmp_path: Path,
) -> None:
    remote, _sha = _bare_remote(tmp_path, advertised=False)
    repo = _host_repo(tmp_path, remote=remote)

    result = subprocess.run(
        [sys.executable, "-m", "rebar", "init", "--force-new-store"],
        cwd=repo,
        env=_env(repo),
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    assert (repo / ".tickets-tracker").is_dir()
    assert "no effect" in result.stderr


def test_best_effort_unreachable_does_not_mount_or_prompt_and_strict_reuses_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _host_repo(tmp_path, remote=tmp_path / "missing-origin.git")
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.chdir(repo)
    config.reset_config_cache()
    calls = 0
    real_run_git = init.run_git

    def _counting_run_git(cwd: str, *args: str, **kwargs: object):
        nonlocal calls
        if args and args[0] == "ls-remote":
            calls += 1
        return real_run_git(cwd, *args, **kwargs)

    monkeypatch.setattr(init, "run_git", _counting_run_git)
    monkeypatch.setattr(
        cli_init,
        "_confirm_and_init",
        lambda _root: pytest.fail("unreachable auto-init entered the consent prompt"),
    )

    cli_init.ensure_store_mounted_best_effort()
    assert not (repo / ".tickets-tracker").exists()
    with pytest.raises(SystemExit) as exc:
        cli_init._create_tracker(str(repo))
    assert exc.value.code == 1
    assert calls == 1
    err = capsys.readouterr().err
    assert "origin" in err and "tickets" in err and "10s" in err
    assert "force-new-store" in err


def test_probe_cache_expires_via_monotonic_clock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _host_repo(tmp_path, remote=tmp_path / "missing-origin.git")
    now = [100.0]
    calls = 0
    real_run_git = init.run_git

    def _counting_run_git(cwd: str, *args: str, **kwargs: object):
        nonlocal calls
        if args and args[0] == "ls-remote":
            calls += 1
        return real_run_git(cwd, *args, **kwargs)

    monkeypatch.setattr(init, "run_git", _counting_run_git)
    monkeypatch.setattr(init.time, "monotonic", lambda: now[0])

    assert not init.pending_init_attaches_to_existing(repo)
    assert not init.pending_init_attaches_to_existing(repo)
    now[0] += 60.0
    assert not init.pending_init_attaches_to_existing(repo)
    assert calls == 2


def test_probe_timeout_ttl_starts_after_the_network_call() -> None:
    now = [100.0]
    calls = 0

    def _timing_out_probe(_cwd: str, *args: str, **_kwargs: object):
        nonlocal calls
        calls += 1
        now[0] += 10.0
        return subprocess.CompletedProcess(["git", *args], 124, "", "timed out")

    _init_probe.clear_probe_cache()
    try:
        assert (
            _init_probe.probe_remote_branch(
                "/repo",
                "origin",
                "tickets",
                run_git_fn=_timing_out_probe,
                monotonic_clock=lambda: now[0],
            )
            == _init_probe.UNREACHABLE
        )
        assert (
            _init_probe.probe_remote_branch(
                "/repo",
                "origin",
                "tickets",
                run_git_fn=_timing_out_probe,
                monotonic_clock=lambda: now[0],
            )
            == _init_probe.UNREACHABLE
        )
        assert calls == 1
    finally:
        _init_probe.clear_probe_cache()


def test_probe_cache_is_bounded_for_long_lived_library_processes() -> None:
    def _absent(_cwd: str, *args: str, **_kwargs: object):
        return subprocess.CompletedProcess(["git", *args], 2, "", "")

    _init_probe.clear_probe_cache()
    try:
        for index in range(130):
            assert (
                _init_probe.probe_remote_branch(
                    f"/repo/{index}",
                    "origin",
                    "tickets",
                    run_git_fn=_absent,
                    monotonic_clock=lambda: 100.0,
                )
                == _init_probe.ABSENT
            )
        assert len(_init_probe._CACHE) <= 128
    finally:
        _init_probe.clear_probe_cache()


def test_probe_timeout_folds_to_bounded_actionable_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    remote, _sha = _bare_remote(tmp_path, advertised=False)
    repo = _host_repo(tmp_path, remote=remote)

    real_run_git = init.run_git

    def _timeout_with_fallback(cwd: str, *args: str, **kwargs: object):
        if args and args[0] == "ls-remote":
            raise subprocess.TimeoutExpired(["git", *args], kwargs.get("timeout", 1))
        return real_run_git(cwd, *args, **kwargs)

    monkeypatch.setattr(init, "run_git", _timeout_with_fallback)

    assert init.init_core(repo) != 0
    assert not (repo / ".tickets-tracker").exists()
    err = capsys.readouterr().err
    assert "10s" in err and "could not determine" in err
    assert "origin" in err and "tickets" in err


def test_existing_store_is_idempotent_and_override_does_not_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, _sha = _bare_remote(tmp_path, advertised=False)
    repo = _host_repo(tmp_path, remote=remote)
    assert init.init_core(repo) == 0

    real_run_git = init.run_git

    def _reject_probe(cwd: str, *args: str, **kwargs: object):
        if args and args[0] == "ls-remote":
            pytest.fail("healthy idempotent init invoked the advertisement probe")
        return real_run_git(cwd, *args, **kwargs)

    monkeypatch.setattr(init, "run_git", _reject_probe)
    assert init.init_core(repo, force_new_store=True) == 0


def test_force_new_store_is_documented_in_help_and_generated_reference() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "rebar", "help", "init"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--force-new-store" in result.stdout
    reference = Path("docs/cli-reference.md").read_text(encoding="utf-8")
    assert "--force-new-store" in reference
