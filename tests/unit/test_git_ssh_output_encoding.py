"""git and ssh output is decoded as UTF-8, never with the locale codec (bug 39d8-253b-8d41-4f88).

Without an explicit ``encoding``, ``subprocess`` decodes child output (and encodes ``str``
stdin) with the locale codec: cp1252 on Windows, which garbles non-ASCII git output or raises on
the bytes cp1252 leaves undefined. Each case runs in a child interpreter started with
``-X warn_default_encoding -W error::EncodingWarning``, so a call that falls back to the locale
codec fails the child, and the child reports its result with ``ascii()`` so the parent can
compare it exactly whatever the console codec is.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SUBJECT = "café ✓"


def _run_strict(code: str) -> subprocess.CompletedProcess[str]:
    """Run *code* in a child that turns any locale-codec fallback into an error."""
    return subprocess.run(
        [sys.executable, "-X", "warn_default_encoding", "-W", "error::EncodingWarning", "-c", code],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    for argv in (
        ["git", "init", "-q", str(r)],
        ["git", "-C", str(r), "config", "user.email", "t@e.com"],
        ["git", "-C", str(r), "config", "user.name", "T"],
        ["git", "-C", str(r), "commit", "-q", "--allow-empty", "-m", _SUBJECT],
    ):
        subprocess.run(argv, check=True, capture_output=True)
    return r


def _child_result(code: str) -> str:
    proc = _run_strict(code)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.mark.parametrize("timeout", [None, 30], ids=["subprocess-run", "process-group"])
def test_run_git_decodes_a_non_ascii_subject(repo: Path, timeout: int | None) -> None:
    out = _child_result(
        "from rebar._store import gitutil\n"
        f"r = gitutil.run_git({str(repo)!r}, 'log', '-1', '--format=%s', timeout={timeout})\n"
        "print(ascii(r.stdout.strip()))\n"
    )

    assert out == ascii(_SUBJECT)


@pytest.mark.parametrize("timeout", [None, 30], ids=["subprocess-run", "process-group"])
def test_run_git_encodes_str_stdin_as_utf8(repo: Path, timeout: int | None) -> None:
    text = _SUBJECT + "\n"
    expected = (
        subprocess.run(
            ["git", "-C", str(repo), "hash-object", "--stdin"],
            input=text.encode("utf-8"),
            capture_output=True,
            check=True,
        )
        .stdout.decode("ascii")
        .strip()
    )

    out = _child_result(
        "from rebar._store import gitutil\n"
        f"r = gitutil.run_git({str(repo)!r}, 'hash-object', '--stdin', input_data={text!r},"
        f" timeout={timeout})\n"
        "print(ascii(r.stdout.strip()))\n"
    )

    assert out == ascii(expected)


def test_run_git_binary_mode_stays_binary(repo: Path) -> None:
    out = _child_result(
        "from rebar._store import gitutil\n"
        f"r = gitutil.run_git({str(repo)!r}, 'log', '-1', '--format=%s', text=False)\n"
        "print(type(r.stdout).__name__, ascii(r.stdout.strip()))\n"
    )

    assert out == f"bytes {_SUBJECT.encode('utf-8')!a}"


@pytest.mark.skipif(shutil.which("ssh") is None, reason="ssh is not on PATH; nothing is decoded")
def test_ssh_keygen_version_probe_names_its_encoding() -> None:
    out = _child_result(
        "from rebar.attest import sshsig\nprint(ascii(sshsig.ssh_keygen_version()))\n"
    )

    # The child exiting 0 under -W error::EncodingWarning is the check; the banner parse is
    # covered by test_attest_sshsig_version_banner.py.
    assert out
