"""``sshsig.ssh_keygen_version`` reads the OpenSSH banner on every platform it ships on.

The probe runs ``ssh -V`` and parses the banner. Microsoft's Windows build prints
``OpenSSH_for_Windows_<ver>`` rather than ``OpenSSH_<ver>``, which the first version of the
pattern did not match, so a Windows runner that had OpenSSH 8.9 or newer was reported as
having none. These tests feed the probe fixed banners, so they need no ssh on the host
(unlike test_attest_sshsig.py, which skips when ssh-keygen is absent).
"""

from __future__ import annotations

import subprocess

import pytest

from rebar.attest import sshsig

pytestmark = pytest.mark.unit


def _fake_run(*, stdout: str = "", stderr: str = "") -> object:
    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["ssh", "-V"], returncode=0, stdout=stdout, stderr=stderr
        )

    return run


@pytest.mark.parametrize(
    ("banner", "expected"),
    [
        ("OpenSSH_9.5p1, OpenSSL 3.0.2 1 Mar 2022", (9, 5)),
        ("OpenSSH_8.8p1, OpenSSL 3.0.2 1 Mar 2022", (8, 8)),
        ("OpenSSH_for_Windows_9.5p1, LibreSSL 3.8.2", (9, 5)),
        ("OpenSSH_for_Windows_8.1p1, LibreSSL 3.0.2", (8, 1)),
        ("not an openssh banner", None),
        ("", None),
    ],
)
def test_the_banner_on_stderr_is_parsed(
    monkeypatch: pytest.MonkeyPatch, banner: str, expected: tuple[int, int] | None
) -> None:
    monkeypatch.setattr(sshsig.subprocess, "run", _fake_run(stderr=banner))

    assert sshsig.ssh_keygen_version() == expected


def test_a_banner_on_stdout_is_used_when_stderr_has_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sshsig.subprocess, "run", _fake_run(stdout="OpenSSH_for_Windows_9.5p1, LibreSSL 3.8.2")
    )

    assert sshsig.ssh_keygen_version() == (9, 5)


def test_a_missing_ssh_binary_reports_no_version(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("ssh")

    monkeypatch.setattr(sshsig.subprocess, "run", run)

    assert sshsig.ssh_keygen_version() is None


def test_windows_openssh_at_the_floor_is_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sshsig.subprocess, "run", _fake_run(stderr="OpenSSH_for_Windows_9.5p1, LibreSSL 3.8.2")
    )

    sshsig.ensure_available()


def test_windows_openssh_below_the_floor_is_refused_with_its_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sshsig.subprocess, "run", _fake_run(stderr="OpenSSH_for_Windows_8.1p1, LibreSSL 3.0.2")
    )

    with pytest.raises(sshsig.SshKeygenUnavailable, match=r"8\.1 is too old"):
        sshsig.ensure_available()
