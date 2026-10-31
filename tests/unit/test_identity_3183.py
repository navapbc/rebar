"""Happy-path authenticated-authorship gate tests.

With enforcement on, verify-identity flags unsigned events; with it off,
the gate is advisory. Write-gate, replay, and snapshot cases are held out.

Held-out oracles merged from test_identity_3183_heldout.py: Held-out authenticated-authorship
checks: replay folds bad signatures while counting signed/unsigned events; required unsigned writes
fail fast; signed snapshot ledger entries still verify.
"""

from __future__ import annotations

import json
import subprocess
import uuid as _uuid
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

import rebar
from rebar._commands._seam import tracker_dir
from rebar.attest import sshsig


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "dev@example.com"),
        ("git", "config", "user.name", "Dev"),
        ("git", "commit", "-q", "--allow-empty", "-m", "init"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    # a normal (unsigned) ticket exists in the store
    rebar.create_ticket("task", "unsigned work", repo_root=str(repo))
    return repo


def _verify_authorship(store: Path, *, required: bool) -> subprocess.CompletedProcess:
    env = subprocess_env({"REBAR_ROOT": str(store)})
    env["REBAR_IDENTITY_REQUIRE_AUTHENTICATED"] = "1" if required else "0"
    return subprocess.run(
        ["rebar", "verify-identity", "--all"],
        cwd=store,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_verify_authorship_flags_unsigned_when_required(store: Path) -> None:
    """With require_authenticated ON, the merge-gate flags an unsigned event and
    fails (non-zero exit)."""
    res = _verify_authorship(store, required=True)
    assert res.returncode != 0, res.stdout + res.stderr
    assert "unsigned" in (res.stdout + res.stderr).lower()


def test_verify_authorship_advisory_when_off(store: Path) -> None:
    """With require_authenticated OFF, verify-identity is advisory (exit 0)."""
    res = _verify_authorship(store, required=False)
    assert res.returncode == 0, res.stdout + res.stderr


# ── held-out oracles (merged from test_identity_3183_heldout.py) ──

try:
    sshsig.ensure_available()
    _SSH_OK = True
except Exception:  # noqa: BLE001 — best-effort SSHSIG availability probe
    _SSH_OK = False


def _init_store(tmp_path: Path, monkeypatch, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "dev@example.com"),
        ("git", "config", "user.name", "Dev"),
        ("git", "commit", "-q", "--allow-empty", "-m", "init"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", "9" * 18)
    rebar.init_repo(repo_root=str(repo))
    return repo


@pytest.fixture
def store_heldout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return _init_store(tmp_path, monkeypatch)


def _keypair(tmp_path: Path, name: str) -> tuple[str, str]:
    key = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-q", "-C", name],
        check=True,
        capture_output=True,
    )
    parts = (tmp_path / f"{name}.pub").read_text().strip().split()
    return str(key), f"{parts[0]} {parts[1]}"


# ------------------------------------------------------ replay never rejects


def test_reducer_never_rejects_bad_signature(store_heldout: Path) -> None:
    """A CREATE event with a BOGUS author_sig still folds into state (never rejected)."""
    from rebar.reducer import reduce_ticket

    tid = "0000-aaaa-bbbb-cccc"
    tdir = Path(tracker_dir(str(store_heldout))) / tid
    tdir.mkdir(parents=True)
    ev_uuid = str(_uuid.uuid4())
    event = {
        "timestamp": 1,
        "uuid": ev_uuid,
        "event_type": "CREATE",
        "env_id": "envx",
        "author": "Mallory",
        "author_email": "mallory@example.com",
        "author_id": "some-identity",
        "author_sig": "-----BEGIN SSH SIGNATURE-----\nTOTALLY BOGUS\n-----END SSH SIGNATURE-----",
        "data": {"ticket_type": "task", "title": "Tampered", "id": tid, "priority": 2},
    }
    (tdir / f"1-{ev_uuid}-CREATE.json").write_text(json.dumps(event))

    state = reduce_ticket(str(tdir))
    assert state is not None, "replay must not reject a bad-signature event"
    assert state["status"] == "open"
    assert state["title"] == "Tampered"


def test_reduced_state_records_signed_unsigned_counts(store_heldout: Path) -> None:
    """The reducer records per-ticket signed/unsigned PRESENCE counts (not a crypto
    verdict)."""
    tid = rebar.create_ticket("task", "unsigned t", repo_root=str(store_heldout))
    st = rebar.show_ticket(tid, repo_root=str(store_heldout))
    authorship = st.get("authorship")
    assert isinstance(authorship, dict)
    assert authorship.get("unsigned", 0) >= 1
    assert "signed" in authorship


# ------------------------------------------------------------- write-gate (UX)


def test_write_gate_fails_fast_when_unsignable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With require_authenticated ON and no resolvable identity/key, a write fails
    fast (UX-only convenience)."""
    repo = _init_store(tmp_path, monkeypatch, name="gated")
    monkeypatch.setenv("REBAR_IDENTITY_REQUIRE_AUTHENTICATED", "1")
    # no identity, no signing key configured → the write cannot be signed
    with pytest.raises(rebar.RebarError):
        rebar.create_ticket("task", "should be refused", repo_root=str(repo))


# ------------------------------------------------------- snapshot round-trip


@pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required")
def test_snapshot_roundtrip_signed_events_still_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Signed events compacted into a SNAPSHOT still verify from the snapshot's
    authorship ledger after the raw event files are gone."""
    repo = _init_store(tmp_path, monkeypatch, name="snaproundtrip")
    priv, pub = _keypair(tmp_path, "author")
    # create_identity(keys=[pub]) genesis-seeds the keyring at epoch 0 (e165 bootstrap),
    # so no separate add_identity_key is needed.
    ident = rebar.create_identity("Ada", "ada@example.com", keys=[pub], repo_root=str(repo))
    rebar.use_identity(ident, repo_root=str(repo))
    monkeypatch.setenv("REBAR_IDENTITY_SIGNING_KEY", priv)

    tid = rebar.create_ticket("task", "signed work", repo_root=str(repo))
    for i in range(3):
        rebar.comment(tid, f"c{i}", repo_root=str(repo))

    # compact → the signed events fold into a SNAPSHOT carrying the authorship ledger
    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", "0")
    monkeypatch.setenv("REBAR_COMPACT_THRESHOLD", "1")
    rebar.compact(tid, repo_root=str(repo))

    env = subprocess_env({"REBAR_ROOT": str(repo), "REBAR_IDENTITY_REQUIRE_AUTHENTICATED": "1"})
    res = subprocess.run(
        ["rebar", "verify-identity", "--all"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    # the signed ticket's compacted events still verify from the snapshot ledger
    assert res.returncode == 0, f"post-compaction verify failed: {res.stdout}{res.stderr}"


def test_verify_authorship_flags_unknown_author(store_heldout: Path) -> None:
    """An event whose author_id references a NON-identity is classified unknown-author
    and fails the merge-gate when require_authenticated is on."""
    tid = "1111-dddd-eeee-ffff"
    tdir = Path(tracker_dir(str(store_heldout))) / tid
    tdir.mkdir(parents=True)
    ev_uuid = str(_uuid.uuid4())
    event = {
        "timestamp": 1,
        "uuid": ev_uuid,
        "event_type": "CREATE",
        "env_id": "envx",
        "author": "Ghost",
        "author_email": "ghost@example.com",
        "author_id": "not-a-real-identity",  # present but not an identity ticket
        "author_sig": "-----BEGIN SSH SIGNATURE-----\nX\n-----END SSH SIGNATURE-----",
        "data": {"ticket_type": "task", "title": "Ghosted", "id": tid, "priority": 2},
    }
    (tdir / f"1-{ev_uuid}-CREATE.json").write_text(json.dumps(event))

    env = subprocess_env(
        {"REBAR_ROOT": str(store_heldout), "REBAR_IDENTITY_REQUIRE_AUTHENTICATED": "1"}
    )
    res = subprocess.run(
        ["rebar", "verify-identity", "--all"],
        cwd=store_heldout,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode != 0
    assert "unknown-author" in (res.stdout + res.stderr).lower()


def test_fsck_surfaces_unsigned_count(store_heldout: Path) -> None:
    """fsck surfaces a store-wide unsigned-event count (AC2 'show/fsck surface')."""
    rebar.create_ticket("task", "unsigned t", repo_root=str(store_heldout))
    env = subprocess_env({"REBAR_ROOT": str(store_heldout)})
    res = subprocess.run(
        ["rebar", "store", "fsck"],
        cwd=store_heldout,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert "authorship:" in res.stdout.lower()
    assert "unsigned" in res.stdout.lower()
