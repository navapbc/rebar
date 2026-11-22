"""run_git sends str stdin as exact bytes (bug neglectful-acceptable-gerbil).

Text-mode stdin on Windows rewrites every ``\\n`` as ``\\r\\n`` before git sees it. run_git now
encodes str stdin itself and runs git in binary mode, then decodes the output the way text
mode would, so callers still get ``str`` with universal newlines. These tests pin that
contract on every host by spying on the subprocess layer.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rebar._store import gitutil


def _repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


def test_str_stdin_reaches_subprocess_as_encoded_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict] = []
    real = subprocess.run

    def spy(*args, **kwargs):
        seen.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(gitutil.subprocess, "run", spy)
    repo = _repo(tmp_path)
    out = gitutil.run_git(repo, "hash-object", "--stdin", input_data="a\nb\n")

    assert seen[-1]["input"] == b"a\nb\n"
    assert not seen[-1]["text"]
    expected = real(
        ["git", "-C", str(repo), "hash-object", "--stdin"], input=b"a\nb\n", capture_output=True
    ).stdout.decode()
    assert out.stdout == expected
    assert isinstance(out.stdout, str)


def test_decoded_output_matches_text_mode_newlines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, b"one\r\ntwo\rthree\n", b"warn\r\n")

    monkeypatch.setattr(gitutil.subprocess, "run", fake_run)
    out = gitutil.run_git(tmp_path, "status", input_data="x\n")
    assert out.stdout == "one\ntwo\nthree\n"
    assert out.stderr == "warn\n"


def test_check_raises_with_decoded_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, b"", b"fatal: nope\r\n")

    monkeypatch.setattr(gitutil.subprocess, "run", fake_run)
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        gitutil.run_git(tmp_path, "status", input_data="x\n", check=True)
    assert excinfo.value.stderr == "fatal: nope\n"


def test_the_bounded_path_also_sends_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``timeout=`` routes through ``_run_in_group`` (Popen); stdin must be bytes there too."""
    seen: list[dict] = []
    real = subprocess.Popen

    class SpyPopen(real):  # type: ignore[misc,valid-type]
        def __init__(self, *args, **kwargs):
            seen.append(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(gitutil.subprocess, "Popen", SpyPopen)
    repo = _repo(tmp_path)
    out = gitutil.run_git(repo, "hash-object", "--stdin", input_data="a\nb\n", timeout=60)

    assert not seen[-1]["text"]
    assert isinstance(out.stdout, str) and len(out.stdout.strip()) == 40


def test_a_timeout_carries_decoded_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_group(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1, output=b"part\r\n", stderr=b"slow\r\n")

    monkeypatch.setattr(gitutil, "_run_in_group", fake_group)
    with pytest.raises(subprocess.TimeoutExpired) as excinfo:
        gitutil.run_git(tmp_path, "status", input_data="x\n", timeout=1)
    assert excinfo.value.output == "part\n"
    assert excinfo.value.stderr == "slow\n"


def test_bytes_stdin_with_text_still_raises_type_error(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        gitutil.run_git(tmp_path, "status", input_data=b"x")
