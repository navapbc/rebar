"""Independent held-out oracle for op-cert key genesis (story 8d8e). NOT shown to the implementer.

Two security-critical properties of the ambient-environment Ed25519 key:
  1. Concurrent first-sign is race-safe: many signers hitting a fresh tracker at once converge on
     EXACTLY ONE keypair (the os.link exclusive-create commit point; losers adopt the winner).
  2. The verify side NEVER creates a key (`create_if_missing=False`): a fresh tracker stays keyless.
"""

from __future__ import annotations

import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import rebar
from rebar._commands._seam import tracker_dir
from rebar._opcert_signing import (
    OpcertKeyUnavailable,
    ensure_opcert_key,
    opcert_key_path,
)
from rebar.attest import sshsig

try:
    sshsig.ensure_available()
    _SSH_OK = True
except Exception:  # noqa: BLE001
    _SSH_OK = False

pytestmark = pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required for SSHSIG")


def _store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", "9" * 18)
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "d@e.test"),
        ("git", "config", "user.name", "D"),
        ("git", "commit", "-q", "--allow-empty", "-m", "init"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return str(repo)


def _pubkey_of(priv_path: str) -> str:
    return (
        subprocess.run(
            ["ssh-keygen", "-y", "-f", priv_path], capture_output=True, text=True, check=True
        )
        .stdout.strip()
        .split()[1]
    )  # the base64 blob, identity-independent of comment


def test_concurrent_first_sign_converges_on_one_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _store(tmp_path, monkeypatch)
    tracker = str(tracker_dir(repo))
    key_file = opcert_key_path(tracker)
    assert not os.path.exists(key_file), "precondition: tracker starts with no op-cert key"

    n = 8
    barrier = threading.Barrier(n)
    seen: list[bytes] = []
    lock = threading.Lock()

    def _attempt() -> None:
        barrier.wait()  # release all threads into genesis simultaneously
        path = ensure_opcert_key(tracker)
        # The key material this signer would sign with, observed right after genesis returns.
        observed = Path(path).read_bytes()
        with lock:
            seen.append(observed)

    with ThreadPoolExecutor(max_workers=n) as ex:
        list(ex.map(lambda _: _attempt(), range(n)))

    # TEETH: every signer must observe the SAME key material, and it must equal the surviving
    # on-disk key. Under the os.link EEXIST commit point the file is immutable after the winner
    # writes it (losers adopt by re-reading) → all identical. A clobbering commit point
    # (os.replace) mutates the file under later threads → observations diverge.
    assert len(seen) == n
    final = Path(key_file).read_bytes()
    assert all(obs == final for obs in seen), (
        "RACE: signers observed differing key material — the exclusive-create commit point did "
        "not hold (a clobber overwrote a key another signer had already adopted)."
    )
    # No stray sibling private keys were left behind by the losers' staging dirs.
    strays = [
        f for f in os.listdir(tracker) if f.startswith(".opcert-key") and not f.endswith(".pub")
    ]
    assert strays == [".opcert-key"], f"unexpected key files: {strays}"


def test_verify_side_never_creates_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _store(tmp_path, monkeypatch)
    tracker = str(tracker_dir(repo))
    key_file = opcert_key_path(tracker)
    assert not os.path.exists(key_file)
    # The verify-side resolution (create_if_missing=False) must not mint a key on a fresh tracker.
    with pytest.raises(OpcertKeyUnavailable):
        ensure_opcert_key(tracker, create_if_missing=False)
    assert not os.path.exists(key_file), "verify side must NEVER create the op-cert key"
    assert not os.path.exists(key_file + ".pub")
