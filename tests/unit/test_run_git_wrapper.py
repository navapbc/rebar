"""Direct contract matrix for the shared ``run_git`` wrapper (story sleepful-rubber-gyrfalcon).

``run_git`` is the one ``git`` subprocess wrapper every store path funnels through, but until
now it was only characterized indirectly through callers that monkeypatch it — its OWN branches
(``-C`` prefixing, ``check``, ``timeout``, ``env`` replacement, stdin forwarding, and the
``input_data`` type contract) were never exercised. These tests invoke the REAL wrapper against a
throwaway git repo and assert observable behavior (argv effect, exceptions, stdout), not source.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from rebar._store.gitutil import run_git


def _git_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    return repo


def test_cwd_none_omits_dash_c_and_runs_in_process_cwd(tmp_path: Path, monkeypatch) -> None:
    """cwd=None omits the ``-C`` prefix, so git runs in the PROCESS cwd, not a fixed repo."""
    repo = _git_repo(tmp_path)
    monkeypatch.chdir(repo)
    top = run_git(None, "rev-parse", "--show-toplevel").stdout.strip()
    assert Path(top).resolve() == repo.resolve()


def test_cwd_path_inserts_dash_c(tmp_path: Path, monkeypatch) -> None:
    """A path-like cwd inserts ``-C <cwd>``, so git targets THAT repo regardless of process cwd."""
    repo = _git_repo(tmp_path, "target")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # process cwd is NOT a repo
    top = run_git(repo, "rev-parse", "--show-toplevel").stdout.strip()
    assert Path(top).resolve() == repo.resolve()


def test_check_true_raises_called_process_error(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    with pytest.raises(subprocess.CalledProcessError):
        run_git(repo, "rev-parse", "--verify", "does-not-exist", check=True)


def test_check_false_returns_nonzero_result(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    result = run_git(repo, "rev-parse", "--verify", "does-not-exist", check=False)
    assert result.returncode != 0
    assert isinstance(result, subprocess.CompletedProcess)


def test_timeout_propagates_timeout_expired(tmp_path: Path) -> None:
    """A command that exceeds ``timeout`` lets subprocess.TimeoutExpired propagate. A sleeping
    pre-commit hook blocks the commit past the timeout (hooks run with no TTY, unlike the
    editor)."""
    repo = _git_repo(tmp_path)
    (repo / "f.txt").write_text("x")
    run_git(repo, "add", "f.txt")
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nsleep 5\n")
    hook.chmod(0o755)
    with pytest.raises(subprocess.TimeoutExpired):
        run_git(repo, "commit", "-m", "blocked", timeout=0.5)


def test_env_fully_replaces_environment(tmp_path: Path) -> None:
    """``env`` REPLACES (not augments) the environment: a var set in the parent but absent from
    ``env`` is invisible to git. Asserted via GIT_AUTHOR_NAME which git echoes into a commit."""
    repo = _git_repo(tmp_path)
    (repo / "f.txt").write_text("x")
    run_git(repo, "add", "f.txt")
    # Provide a full, minimal env with a distinctive author; PATH is needed for git to run.
    env = {
        "PATH": os.environ.get("PATH", ""),
        "GIT_AUTHOR_NAME": "Env Author",
        "GIT_AUTHOR_EMAIL": "env@e.com",
        "GIT_COMMITTER_NAME": "Env Author",
        "GIT_COMMITTER_EMAIL": "env@e.com",
        "HOME": str(tmp_path),
    }
    run_git(repo, "commit", "-q", "-m", "c", env=env)
    author = run_git(repo, "log", "-1", "--format=%an").stdout.strip()
    assert author == "Env Author"


def test_str_input_is_forwarded_to_stdin(tmp_path: Path) -> None:
    """str ``input_data`` is piped to git's stdin (hash-object hashes the piped content)."""
    repo = _git_repo(tmp_path)
    content = "hello from stdin\n"
    piped = run_git(repo, "hash-object", "--stdin", input_data=content).stdout.strip()
    # Cross-check against git hashing a real file with the same bytes.
    (repo / "probe").write_text(content)
    direct = run_git(repo, "hash-object", str(repo / "probe")).stdout.strip()
    assert piped == direct


def test_bytes_input_with_text_true_raises_clear_type_error(tmp_path: Path) -> None:
    """The narrowed contract: bytes stdin with the default text=True raises a CLEAR TypeError
    (naming the fix), not the opaque stdlib ``AttributeError: 'bytes' object has no attribute
    'encode'`` that bare subprocess would surface."""
    repo = _git_repo(tmp_path)
    with pytest.raises(TypeError) as exc:
        run_git(repo, "hash-object", "--stdin", input_data=b"raw bytes")
    msg = str(exc.value).lower()
    assert "bytes" in msg and "text=false" in msg


# ── transient read-fault classification (bug wrongful-chemic-squeaker, s3 doctor) ──


@pytest.mark.parametrize(
    "stderr",
    [
        "fatal: bad object HEAD",
        "fatal: bad object ae7d19a6f0a1b2c3d4e5f60718293a4b5c6d7e8f",
        "FATAL: BAD OBJECT HEAD",
    ],
)
def test_doctor_bad_object_stderr_is_classified_transient(stderr: str) -> None:
    """The exact stderr shapes the s3 doctor's bundle step hit on CI classify as transient.

    Both the ``HEAD`` and the bare-sha forms were observed for the SAME fault, so the marker
    must match the fault rather than the argument git happened to name.
    """
    from rebar._store.gitutil import _is_transient_head_error, is_transient_object_read_error

    assert is_transient_object_read_error(stderr)
    assert _is_transient_head_error(stderr)


@pytest.mark.parametrize(
    "stderr",
    [
        "fatal: loose object 4b825dc6 is corrupt",
        "error: inflate: data stream error (incorrect header check)",
        "fatal: ambiguous argument 'HEAD': unknown revision or path not in the working tree.",
        "fatal: Refusing to create empty bundle.",
        "error: Unable to create '/repo/.git/index.lock': File exists.",
    ],
)
def test_doctor_non_transient_stderr_is_not_classified_transient(stderr: str) -> None:
    """Real damage and ordinary errors are NOT retried as a transient object read.

    Corruption, an unresolvable revision and an empty-bundle refusal are all permanent: a
    retry cannot change them, and masking them behind a backoff would hide the real fault.
    (The index.lock case is a genuine transient, but it belongs to the SEPARATE lock retry.)
    """
    from rebar._store.gitutil import is_transient_object_read_error

    assert not is_transient_object_read_error(stderr)


def test_doctor_git_helper_routes_through_the_self_healing_seam() -> None:
    """The s3 doctor reaches git through ``run_git_write``, not raw ``run_git``.

    The defect this closes was structural — the module was the one store git caller outside
    rebar's self-healing seam — so the seam membership itself is pinned, not just the
    retry's effect.
    """
    from rebar._store import s3_doctor

    assert not hasattr(s3_doctor, "run_git"), "s3_doctor still binds raw run_git"
    assert hasattr(s3_doctor, "run_git_write")


def test_run_git_write_honours_a_caller_supplied_timeout(tmp_path: Path, monkeypatch) -> None:
    """A caller's own watchdog bound travels with it when it adopts the seam."""
    from rebar._store import gitutil

    seen: dict = {}

    def _fake_run_git(cwd, *args, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(["git", *args], 0, "", "")

    monkeypatch.setattr(gitutil, "run_git", _fake_run_git)
    gitutil.run_git_write(str(tmp_path), "status", timeout=17)

    assert seen["timeout"] == 17
