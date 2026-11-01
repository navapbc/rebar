"""No op-cert signer key copy outlives its owner (bug balladic-fattening-tortoise).

``compose_signer`` copies the private key into a fresh ``rebar-opcert-*`` dir under the system
temp dir, and only ``OpcertSigner.cleanup()`` removes it. Two test modules handed a composed
signer straight to ``run_job`` and never cleaned it up, leaving 9 private-key copies in the host
temp dir per run; and ``compose_signer`` itself removed the dir only on ``OpcertKeyError``, so
any other exception or an interrupt after ``mkdtemp`` left one behind. Every test here points
the temp dir at an empty sandbox and asserts no ``rebar-opcert-*`` entry remains.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest
from _nested_pytest import run_nested_pytest
from _subprocess_env import subprocess_env

from rebar.opcert_service import keyprov
from rebar.opcert_service.config import OpcertServiceConfig

pytestmark = pytest.mark.unit

#: The modules that composed signers without cleaning them up before the fix.
_SIGNER_MODULES = (
    "tests/unit/test_opcert_service.py",
    "tests/unit/test_opcert_service_integrity.py",
)


def _leftovers(sandbox: Path) -> list[str]:
    """Names of key-copy dirs ``compose_signer`` left in ``sandbox`` (never their contents)."""
    return sorted(p.name for p in sandbox.glob("rebar-opcert-*"))


def _key_cfg(tmp_path: Path) -> OpcertServiceConfig:
    key = tmp_path / "envkey"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-q", "-C", "env"],
        check=True,
        capture_output=True,
    )
    key.chmod(0o600)
    return OpcertServiceConfig(env_id="tortoise-env", key_path=str(key))


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty dir that ``tempfile.mkdtemp`` uses as the system temp dir for this test."""
    box = tmp_path / "tmpdir-sandbox"
    box.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(box))
    return box


def test_signer_test_modules_leave_no_key_copy_in_the_temp_dir(tmp_path: Path) -> None:
    box = tmp_path / "tmpdir-sandbox"
    box.mkdir()
    result = run_nested_pytest(
        tmp_path / "run", "-q", *_SIGNER_MODULES, env=subprocess_env(TMPDIR=str(box))
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    assert _leftovers(box) == [], "signer test modules left private-key copies in the temp dir"


def test_compose_signer_removes_its_dir_when_writing_the_copy_fails(
    tmp_path: Path, sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _key_cfg(tmp_path)

    def fail_write(copy_path: str, key_bytes: bytes) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(keyprov, "_write_copy", fail_write)
    with pytest.raises(OSError, match="disk full"):
        keyprov.compose_signer(cfg)
    assert _leftovers(sandbox) == []


def test_compose_signer_removes_its_dir_when_interrupted_during_validation(
    tmp_path: Path, sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _key_cfg(tmp_path)

    def interrupted(copy_path: str) -> None:
        assert Path(copy_path).is_file()  # the copy exists when the interrupt lands
        raise KeyboardInterrupt

    monkeypatch.setattr(keyprov, "_validate_key_file", interrupted)
    with pytest.raises(KeyboardInterrupt):
        keyprov.compose_signer(cfg)
    assert _leftovers(sandbox) == []


def test_composed_signer_copy_lives_until_cleanup(tmp_path: Path, sandbox: Path) -> None:
    signer = keyprov.compose_signer(_key_cfg(tmp_path))
    try:
        assert Path(signer.runtime_dir).parent == sandbox
        assert Path(signer.key_path).is_file()
    finally:
        signer.cleanup()
    assert _leftovers(sandbox) == []
