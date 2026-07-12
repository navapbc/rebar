"""HELD-OUT oracle for TS3 (117b) — the implementation MUST NOT see this file.

The approved-design behaviour the happy path cannot cover:
- the SNAPSHOT authorship ledger records {event_uuid, content_hash, signature,
  signer_pubkey, position} and a compacted signed event verifies from the ledger ALONE;
- `key_not_valid_at_era` is emitted for a valid signature by a since-revoked key (distinct
  from bad-signature / unknown-author);
- `bad-signature` for a signature that verifies against no key the identity ever held;
- `verify_signature_result.schema.json` (the SEPARATE verify-signature command) is unchanged;
- `rebar.create_placeholder` is importable and idempotent.
Observable behaviour only.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands._seam import tracker_dir
from rebar.attest import authorship, sshsig

try:
    sshsig.ensure_available()
    _SSH_OK = True
except Exception:  # noqa: BLE001 — best-effort SSHSIG availability probe; skip if unavailable
    _SSH_OK = False

_ssh = pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required for SSHSIG")


def _init(tmp_path: Path, monkeypatch, name: str = "repo") -> Path:
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
    rebar.init_repo(repo_root=str(repo))
    return repo


def _keypair(tmp_path: Path, name: str) -> tuple[str, str]:
    key = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-q", "-C", name],
        check=True,
        capture_output=True,
    )
    parts = (tmp_path / f"{name}.pub").read_text().strip().split()
    return str(key), f"{parts[0]} {parts[1]}"


def _verify_authorship(store: Path, priv: str | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "REBAR_ROOT": str(store), "REBAR_IDENTITY_REQUIRE_AUTHENTICATED": "1"}
    if priv:
        env["REBAR_IDENTITY_SIGNING_KEY"] = priv
    return subprocess.run(
        ["rebar", "verify-authorship", "--all"], cwd=store, env=env, capture_output=True, text=True
    )


def _snapshot_ledger(store: Path, ticket_id: str) -> list[dict]:
    """Read the authorship_ledger from the ticket's SNAPSHOT event file."""
    tdir = Path(tracker_dir(str(store))) / ticket_id
    for f in tdir.glob("*-SNAPSHOT.json"):
        snap = json.loads(f.read_text(encoding="utf-8"))
        led = snap.get("data", {}).get("compiled_state", {}).get("authorship_ledger")
        if led is not None:
            return led
    raise AssertionError("no SNAPSHOT authorship_ledger found")


# ── AC2: ledger schema + verify-from-ledger-alone ─────────────────────────────
@_ssh
def test_ledger_schema_and_verify_from_ledger_alone(tmp_path: Path, monkeypatch) -> None:
    repo = _init(tmp_path, monkeypatch, "ledger")
    priv, pub = _keypair(tmp_path, "author")
    ident = rebar.create_identity("Ada", "ada@example.com", keys=[pub], repo_root=str(repo))
    rebar.use_identity(ident, repo_root=str(repo))
    monkeypatch.setenv("REBAR_IDENTITY_SIGNING_KEY", priv)

    tid = rebar.create_ticket("task", "signed work", repo_root=str(repo))
    for i in range(3):
        rebar.comment(tid, f"c{i}", repo_root=str(repo))

    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", "0")
    monkeypatch.setenv("REBAR_COMPACT_THRESHOLD", "1")
    rebar.compact(tid, repo_root=str(repo))

    ledger = _snapshot_ledger(repo, tid)
    assert ledger, "ledger must record the signed folded events"
    rec = ledger[0]
    assert set(rec.keys()) == {
        "event_uuid",
        "content_hash",
        "signature",
        "signer_pubkey",
        "position",
    }
    assert rec["signer_pubkey"] == pub
    assert set(rec["position"].keys()) == {"commit_sha", "position"}

    # verify-authorship must return verified FROM THE LEDGER ALONE (raw events retired).
    res = _verify_authorship(repo, priv)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "verified" in (res.stdout + res.stderr).lower()


# ── AC3: key_not_valid_at_era for a since-revoked key ─────────────────────────
@_ssh
def test_key_not_valid_at_era_for_revoked_key(tmp_path: Path, monkeypatch) -> None:
    repo = _init(tmp_path, monkeypatch, "era")
    priv, pub = _keypair(tmp_path, "author")
    ident = rebar.create_identity("Ada", "ada@example.com", keys=[pub], repo_root=str(repo))
    rebar.use_identity(ident, repo_root=str(repo))
    monkeypatch.setenv("REBAR_IDENTITY_SIGNING_KEY", priv)

    tid = rebar.create_ticket("task", "work", repo_root=str(repo))

    # Revoke the key (signed by the still-valid key itself).
    revoke_sig = authorship.sign_authorship(
        authorship.keyop_payload("KEY_REVOKE", ident, pub), priv, principal=ident
    )
    rebar.revoke_identity_key(ident, pub, signature=revoke_sig, repo_root=str(repo))

    # Write an event AFTER revocation, still signed by the (now-revoked) key.
    rebar.comment(tid, "signed by a revoked key", repo_root=str(repo))

    res = _verify_authorship(repo, priv)
    combined = (res.stdout + res.stderr).lower()
    assert "key_not_valid_at_era" in combined, combined
    assert res.returncode != 0  # not all in-scope events verified


# ── AC4: bad-signature for a signature by a key the identity never held ───────
@_ssh
def test_forged_signature_is_bad_signature(tmp_path: Path, monkeypatch) -> None:
    repo = _init(tmp_path, monkeypatch, "forged")
    priv, pub = _keypair(tmp_path, "author")
    priv_m, _pub_m = _keypair(tmp_path, "mallory")
    ident = rebar.create_identity("Ada", "ada@example.com", keys=[pub], repo_root=str(repo))

    # Hand-write a CREATE event authored by the identity but signed by Mallory's key
    # (which the identity never held) — a well-formed in-toto Statement, wrong signer.
    tid = "aaaa-bbbb-cccc-dddd"
    tdir = Path(tracker_dir(str(repo))) / tid
    tdir.mkdir(parents=True)
    event = {
        "timestamp": 1,
        "uuid": "e-forged",
        "event_type": "CREATE",
        "env_id": "x",
        "author": "Mallory",
        "author_id": ident,
        "data": {"ticket_type": "task", "title": "forged", "id": tid, "priority": 2},
    }
    from rebar.attest import dsse

    env_sig = authorship.sign_event_authorship(event, priv_m, principal=ident)
    event["author_sig"] = dsse.encode(
        env_sig.payload_type,
        env_sig.payload,
        [{"keyid": s.keyid, "sig": s.sig} for s in env_sig.signatures],
    )
    (tdir / "1-e-forged-CREATE.json").write_text(json.dumps(event), encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(tracker_dir(str(repo))), "add", "-A"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(tracker_dir(str(repo))), "commit", "-q", "--no-verify", "-m", "forged"],
        check=True,
        capture_output=True,
    )

    res = _verify_authorship(repo)
    combined = (res.stdout + res.stderr).lower()
    assert "bad-signature" in combined, combined


# ── AC4b: a forged event stays bad-signature AFTER compaction (null signer_pubkey) ──
@_ssh
def test_forged_signature_bad_after_compaction(tmp_path: Path, monkeypatch) -> None:
    """A forged signed event compacted into the SNAPSHOT ledger is recorded with a null
    signer_pubkey and still classified bad-signature from the ledger alone."""
    repo = _init(tmp_path, monkeypatch, "forgedcompact")
    priv, pub = _keypair(tmp_path, "author")
    priv_m, _pub_m = _keypair(tmp_path, "mallory")
    ident = rebar.create_identity("Ada", "ada@example.com", keys=[pub], repo_root=str(repo))

    tid = "eeee-ffff-0000-1111"
    tdir = Path(tracker_dir(str(repo))) / tid
    tdir.mkdir(parents=True)
    event = {
        "timestamp": 1,
        "uuid": "e-forged2",
        "event_type": "CREATE",
        "env_id": "x",
        "author": "Mallory",
        "author_id": ident,
        "data": {"ticket_type": "task", "title": "forged", "id": tid, "priority": 2},
    }
    from rebar.attest import dsse

    env_sig = authorship.sign_event_authorship(event, priv_m, principal=ident)
    event["author_sig"] = dsse.encode(
        env_sig.payload_type,
        env_sig.payload,
        [{"keyid": s.keyid, "sig": s.sig} for s in env_sig.signatures],
    )
    (tdir / "1-e-forged2-CREATE.json").write_text(json.dumps(event), encoding="utf-8")
    tracker = str(tracker_dir(str(repo)))
    subprocess.run(["git", "-C", tracker, "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", tracker, "commit", "-q", "--no-verify", "-m", "forged"],
        check=True,
        capture_output=True,
    )
    # add a second event so the ticket has >1 event, then compact.
    rebar.comment(tid, "second", repo_root=str(repo))
    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", "0")
    monkeypatch.setenv("REBAR_COMPACT_THRESHOLD", "1")
    rebar.compact(tid, repo_root=str(repo))

    ledger = _snapshot_ledger(repo, tid)
    forged = next(r for r in ledger if r["event_uuid"] == "e-forged2")
    assert forged["signer_pubkey"] is None  # no keyring key verified it
    res = _verify_authorship(repo)
    assert "bad-signature" in (res.stdout + res.stderr).lower()


# ── AC5: the SEPARATE verify-signature schema is untouched ────────────────────
def test_verify_signature_schema_includes_key_not_valid_at_era() -> None:
    """epic AC10: key_not_valid_at_era is added to the canonical verify-result verdict enum
    (the original four verdicts remain)."""
    schema_path = (
        Path(rebar.__file__).resolve().parent / "schemas" / "verify_signature_result.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    enum = schema["properties"]["verdict"]["enum"]
    assert set(enum) == {
        "unsigned",
        "foreign_key",
        "certified",
        "mismatch",
        "key_not_valid_at_era",
    }


# ── AC6: create_placeholder importable + idempotent ───────────────────────────
def test_create_placeholder_importable_and_idempotent(tmp_path: Path, monkeypatch) -> None:
    repo = _init(tmp_path, monkeypatch, "placeholder")
    assert hasattr(rebar, "create_placeholder"), "rebar.create_placeholder must be importable"
    first = rebar.create_placeholder("jira", "acct-123", "Jane Doe", repo_root=str(repo))
    again = rebar.create_placeholder("jira", "acct-123", "Jane Doe", repo_root=str(repo))
    assert first == again, "create_placeholder must be idempotent on the same mapping"
    state = rebar.show_ticket(first, repo_root=str(repo))
    assert state["ticket_type"] == "identity"
    assert not (state.get("keys") or [])  # keyless placeholder
