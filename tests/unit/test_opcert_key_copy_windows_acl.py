"""The op-cert key copy gets an owner-only ACL on Windows (bug plump-smelly-indianabat).

Python 3.13's ``tempfile.mkdtemp`` gives the runtime dir SYSTEM, Administrators and OWNER RIGHTS
entries and no user entry; the key copy takes that ACL, and Win32-OpenSSH's ``ssh-keygen``
refuses a key that OWNER RIGHTS can open. By operator decision (option 1 on the ticket) rebar
restricts the dir to the current user on Windows before the copy is written. POSIX is unchanged.

The real-ACL test runs only on Windows. The others fake ``whoami``/``icacls`` so the ordering,
the error paths and the POSIX no-op are checked on every platform.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from rebar.opcert_service import keyprov
from rebar.opcert_service.config import OpcertServiceConfig

pytestmark = pytest.mark.unit

_SID = "S-1-5-21-1-2-3-1001"
_WHOAMI_CSV = f'"host\\\\runner","{_SID}"\r\n'.encode()


def _key_cfg(tmp_path: Path) -> OpcertServiceConfig:
    key = tmp_path / "envkey"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-q", "-C", "env"],
        check=True,
        capture_output=True,
    )
    return OpcertServiceConfig(env_id="indianabat-env", key_path=str(key))


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty dir that ``tempfile.mkdtemp`` uses as the system temp dir for this test."""
    box = tmp_path / "tmpdir-sandbox"
    box.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(box))
    return box


class _FakeAclTools:
    """Records ``whoami``/``icacls`` calls and answers them; every other command runs for real."""

    def __init__(self, whoami_out: bytes = _WHOAMI_CSV, icacls_rc: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.timeouts: list[object] = []
        self.whoami_out = whoami_out
        self.icacls_rc = icacls_rc
        self._real = subprocess.run

    def __call__(self, argv, *args, **kwargs):
        if argv[0] not in ("whoami", "icacls"):
            return self._real(argv, *args, **kwargs)
        self.calls.append(list(argv))
        self.timeouts.append(kwargs.get("timeout"))
        if argv[0] == "whoami":
            return subprocess.CompletedProcess(argv, 0, self.whoami_out, b"")
        return subprocess.CompletedProcess(argv, self.icacls_rc, b"", b"Access is denied.")


def _windows_branch(monkeypatch: pytest.MonkeyPatch, fake: _FakeAclTools) -> None:
    """Take the Windows branch with faked ACL tools. The fake never changes a real ACL, so on a
    real Windows host ``ssh-keygen`` would refuse the copy; key validation is stubbed out here
    and covered for real by the Windows-only test below."""
    monkeypatch.setattr(keyprov, "_POSIX_MODE_BITS", False)
    monkeypatch.setattr(keyprov.subprocess, "run", fake)
    monkeypatch.setattr(keyprov, "_validate_key_file", lambda copy_path: None)


def test_acl_is_restricted_before_the_copy_is_written(
    tmp_path: Path, sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeAclTools()
    _windows_branch(monkeypatch, fake)
    seen_at_write: list[int] = []
    real_write = keyprov._write_copy

    def recording_write(copy_path: str, key_bytes: bytes) -> None:
        seen_at_write.append(len(fake.calls))
        real_write(copy_path, key_bytes)

    monkeypatch.setattr(keyprov, "_write_copy", recording_write)
    signer = keyprov.compose_signer(_key_cfg(tmp_path))
    try:
        d = signer.runtime_dir
        assert fake.calls == [
            ["whoami", "/user", "/fo", "csv", "/nh"],
            ["icacls", d, "/inheritance:r", "/grant:r", f"*{_SID}:(OI)(CI)F"],
            ["icacls", d, "/remove:g", "*S-1-3-4", "*S-1-5-18", "*S-1-5-32-544"],
        ]
        assert seen_at_write == [3], "the ACL must be written before the key copy exists"
        assert fake.timeouts == [keyprov._ACL_TOOL_TIMEOUT_S] * 3
    finally:
        signer.cleanup()


def test_the_current_user_is_never_removed(
    tmp_path: Path, sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A service running as SYSTEM keeps its own grant: its SID is not in the remove list."""
    fake = _FakeAclTools(whoami_out=b'"nt authority\\\\system","S-1-5-18"\r\n')
    _windows_branch(monkeypatch, fake)
    signer = keyprov.compose_signer(_key_cfg(tmp_path))
    try:
        remove = fake.calls[-1]
        assert remove[2:] == ["/remove:g", "*S-1-3-4", "*S-1-5-32-544"]
    finally:
        signer.cleanup()


@pytest.mark.parametrize(
    ("fake", "match"),
    [
        (_FakeAclTools(icacls_rc=5), r"icacls.*Access is denied"),
        (_FakeAclTools(whoami_out=b"garbage\r\n"), "SID"),
        (_FakeAclTools(whoami_out=b""), "SID"),
    ],
    ids=["icacls-fails", "whoami-unparseable", "whoami-empty"],
)
def test_an_acl_failure_refuses_and_leaves_no_copy(
    tmp_path: Path,
    sandbox: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake: _FakeAclTools,
    match: str,
) -> None:
    cfg = _key_cfg(tmp_path)
    _windows_branch(monkeypatch, fake)
    with pytest.raises(keyprov.OpcertKeyError, match=match):
        keyprov.compose_signer(cfg)
    assert list(sandbox.glob("rebar-opcert-*")) == []


def test_a_missing_acl_tool_refuses_and_leaves_no_copy(
    tmp_path: Path, sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _key_cfg(tmp_path)
    real = subprocess.run

    def no_whoami(argv, *args, **kwargs):
        if argv[0] == "whoami":
            raise FileNotFoundError("whoami")
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(keyprov, "_POSIX_MODE_BITS", False)
    monkeypatch.setattr(keyprov.subprocess, "run", no_whoami)
    with pytest.raises(keyprov.OpcertKeyError, match="whoami"):
        keyprov.compose_signer(cfg)
    assert list(sandbox.glob("rebar-opcert-*")) == []


def test_a_hung_acl_tool_refuses_and_leaves_no_copy(
    tmp_path: Path, sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _key_cfg(tmp_path)
    real = subprocess.run

    def hung_icacls(argv, *args, **kwargs):
        if argv[0] == "whoami":
            return subprocess.CompletedProcess(argv, 0, _WHOAMI_CSV, b"")
        if argv[0] == "icacls":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(keyprov, "_POSIX_MODE_BITS", False)
    monkeypatch.setattr(keyprov.subprocess, "run", hung_icacls)
    with pytest.raises(keyprov.OpcertKeyError, match="cannot restrict the op-cert key copy"):
        keyprov.compose_signer(cfg)
    assert list(sandbox.glob("rebar-opcert-*")) == []


def test_a_non_ascii_user_name_still_yields_the_sid(
    tmp_path: Path, sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """whoami writes in the console code page; a cp850 name is not valid UTF-8."""
    out = '"host\\\\jos\u00e9","S-1-5-21-9-9-9-500"\r\n'.encode("cp850")
    fake = _FakeAclTools(whoami_out=out)
    _windows_branch(monkeypatch, fake)
    signer = keyprov.compose_signer(_key_cfg(tmp_path))
    signer.cleanup()
    assert fake.calls[1][4] == "*S-1-5-21-9-9-9-500:(OI)(CI)F"


def test_posix_never_calls_the_windows_acl_tools(
    tmp_path: Path, sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeAclTools()
    monkeypatch.setattr(keyprov, "_POSIX_MODE_BITS", True)
    monkeypatch.setattr(keyprov.subprocess, "run", fake)
    if os.name == "nt":
        pytest.skip("the POSIX branch's 0600 source check cannot pass on Windows")
    cfg = _key_cfg(tmp_path)
    Path(cfg.key_path or "").chmod(0o600)
    signer = keyprov.compose_signer(cfg)
    signer.cleanup()
    assert fake.calls == []


@pytest.mark.skipif(os.name != "nt", reason="reads a real Windows ACL with icacls")
def test_windows_copy_acl_names_only_the_current_user(tmp_path: Path, sandbox: Path) -> None:
    """compose_signer itself proves ssh-keygen accepted the copy; this pins the ACL behind it."""
    signer = keyprov.compose_signer(_key_cfg(tmp_path))
    try:
        user = subprocess.run(["whoami"], capture_output=True, text=True, check=True).stdout.strip()
        acl = subprocess.run(
            ["icacls", signer.key_path], capture_output=True, text=True, check=True
        ).stdout
        assert f"{user}:" in acl.lower(), acl
        for other in ("OWNER RIGHTS", "NT AUTHORITY\\SYSTEM", "BUILTIN\\Administrators"):
            assert other.lower() not in acl.lower(), acl
    finally:
        signer.cleanup()
