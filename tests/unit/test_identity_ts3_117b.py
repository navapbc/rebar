"""TS3 happy-path check. A configured signing key writes an in-toto author_sig
through the event seam, and verify-identity classifies it as verified. Ledger,
revocation, forged-signature, and schema boundaries stay in the held-out suite.

Held-out oracles merged from test_identity_ts3_117b_heldout.py: Held-out TS3 checks verify
compacted events from the snapshot authorship ledger alone. Revoked-key signatures get
key_not_valid_at_era, while signatures matching no historical key get bad-signature. The separate
verify-signature schema stays unchanged, and ensure_identity_for remains the placeholder
entrypoint.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

import rebar
from rebar._commands._seam import tracker_dir
from rebar.attest import authorship, sshsig

try:
    sshsig.ensure_available()
    _SSH_OK = True
except Exception:  # noqa: BLE001 — best-effort SSHSIG availability probe; skip if unavailable
    _SSH_OK = False

pytestmark = pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required for SSHSIG")


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


def test_live_signed_event_verifies(store: Path, tmp_path: Path, monkeypatch) -> None:
    """An event written through the seam by an identity with a valid key (in-toto
    Statement author_sig) is classified `verified` by the merge-gate."""
    priv, pub = _keypair(tmp_path, "author")
    ident = rebar.create_identity("Ada", "ada@example.com", keys=[pub], repo_root=str(store))
    rebar.use_identity(ident, repo_root=str(store))
    monkeypatch.setenv("REBAR_IDENTITY_SIGNING_KEY", priv)

    # Written through the seam → author_sig is an in-toto Statement envelope.
    tid = rebar.create_ticket("task", "signed work", repo_root=str(store))
    rebar.comment(tid, "a signed comment", repo_root=str(store))

    env = subprocess_env(
        {
            "REBAR_ROOT": str(store),
            "REBAR_IDENTITY_SIGNING_KEY": priv,
            "REBAR_IDENTITY_REQUIRE_AUTHENTICATED": "1",
        }
    )
    res = subprocess.run(
        [sys.executable, "-m", "rebar", "verify-identity", "--all"],
        cwd=store,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    # require_authenticated ON + every in-scope event verified ⇒ exit 0, ≥1 verified.
    assert res.returncode == 0, res.stdout + res.stderr
    out = (res.stdout + res.stderr).lower()
    assert "verified" in out
    assert "not verified" not in out


def test_ensure_identity_for_replaces_public_create_placeholder(store: Path) -> None:
    assert not hasattr(rebar, "create_placeholder")

    first = rebar.ensure_identity_for("jira", "acct-123", "Jane Doe", repo_root=str(store))
    again = rebar.ensure_identity_for("jira", "acct-123", "Jane Doe", repo_root=str(store))

    assert first == again
    assert rebar.is_placeholder(first, repo_root=str(store))


# ── held-out oracles (merged from test_identity_ts3_117b_heldout.py) ──

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


def _verify_authorship(store: Path, priv: str | None = None) -> subprocess.CompletedProcess:
    env = subprocess_env({"REBAR_ROOT": str(store), "REBAR_IDENTITY_REQUIRE_AUTHENTICATED": "1"})
    if priv:
        env["REBAR_IDENTITY_SIGNING_KEY"] = priv
    return subprocess.run(
        [sys.executable, "-m", "rebar", "verify-identity", "--all"],
        cwd=store,
        env=env,
        capture_output=True,
        text=True,
        check=False,
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

    # verify-identity must return verified FROM THE LEDGER ALONE (raw events retired).
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


# ── AC4: signer-not-on-keyring vs bad signature (ticket df01-290f-89a2-4ce8) ──────
# A sound, event-binding signature by a key the author identity never held is
# ``signer-not-on-keyring``: reported with its signing key, not a bad signature, but it fails
# enforcement like ``key_not_valid_at_era``. Only a signature that fails cryptographic
# verification, or does not bind its event, is ``bad-signature``.


def _fingerprint(pub_path: Path) -> str:
    """The ``SHA256:`` fingerprint ``ssh-keygen -l`` prints for the public key file."""
    out = subprocess.run(
        ["ssh-keygen", "-lf", str(pub_path)], check=True, capture_output=True, text=True
    ).stdout
    return out.split()[1]


def _event(tid: str, uuid: str, ident: str) -> dict:
    return {
        "timestamp": 1,
        "uuid": uuid,
        "event_type": "CREATE",
        "env_id": "x",
        "author": "Mallory",
        "author_id": ident,
        "data": {"ticket_type": "task", "title": "off-keyring", "id": tid, "priority": 2},
    }


def _encode(envelope) -> str:
    from rebar.attest import dsse

    return dsse.encode(
        envelope.payload_type,
        envelope.payload,
        [{"keyid": s.keyid, "sig": s.sig} for s in envelope.signatures],
    )


def _commit_event(repo: Path, tid: str, event: dict) -> None:
    """Hand-write one event file into the tracker and commit it."""
    tracker = str(tracker_dir(str(repo)))
    tdir = Path(tracker) / tid
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / f"1-{event['uuid']}-CREATE.json").write_text(json.dumps(event), encoding="utf-8")
    subprocess.run(["git", "-C", tracker, "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", tracker, "commit", "-q", "--no-verify", "-m", event["uuid"]],
        check=True,
        capture_output=True,
    )


def _mallory_store(tmp_path: Path, monkeypatch, name: str, tid: str, uuid: str):
    """A store where identity Ada's event is signed (soundly, binding it) by Mallory's key."""
    repo = _init(tmp_path, monkeypatch, name)
    priv, pub = _keypair(tmp_path, "author")
    priv_m, _pub_m = _keypair(tmp_path, "mallory")
    ident = rebar.create_identity("Ada", "ada@example.com", keys=[pub], repo_root=str(repo))
    event = _event(tid, uuid, ident)
    event["author_sig"] = _encode(authorship.sign_event_authorship(event, priv_m, principal=ident))
    _commit_event(repo, tid, event)
    return repo, ident, priv, _fingerprint(tmp_path / "mallory.pub")


@_ssh
def test_off_keyring_signature_fails_enforcement(tmp_path: Path, monkeypatch) -> None:
    repo, _ident, _priv, fingerprint = _mallory_store(
        tmp_path, monkeypatch, "offkeyring", "aaaa-bbbb-cccc-dddd", "e-offkeyring"
    )

    res = _verify_authorship(repo)
    combined = res.stdout + res.stderr
    assert res.returncode == 1, combined
    assert "FAIL — 1 enforced in-scope event(s) are not verified" in combined, combined
    assert "signer-not-on-keyring: aaaa-bbbb-cccc-dddd/" in combined, combined
    assert f"(signing key {fingerprint})" in combined, combined
    assert "1 signer-not-on-keyring" in combined, combined
    assert "0 bad-signature" in combined, combined


@_ssh
def test_off_keyring_signature_stays_a_finding_after_compaction(
    tmp_path: Path, monkeypatch
) -> None:
    """Compaction records the off-keyring event with a null signer_pubkey; the ledger entry is
    still verified against its own key and classified signer-not-on-keyring, not bad."""
    tid = "eeee-ffff-0000-1111"
    repo, ident, priv, fingerprint = _mallory_store(
        tmp_path, monkeypatch, "offkeyringcompact", tid, "e-offkeyring2"
    )
    # The second event is Ada's own, signed with her keyring key, so it verifies.
    rebar.use_identity(ident, repo_root=str(repo))
    monkeypatch.setenv("REBAR_IDENTITY_SIGNING_KEY", priv)
    rebar.comment(tid, "second", repo_root=str(repo))
    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", "0")
    monkeypatch.setenv("REBAR_COMPACT_THRESHOLD", "1")
    rebar.compact(tid, repo_root=str(repo))

    ledger = _snapshot_ledger(repo, tid)
    entry = next(r for r in ledger if r["event_uuid"] == "e-offkeyring2")
    assert entry["signer_pubkey"] is None  # no keyring key verified it at compaction

    res = _verify_authorship(repo)
    combined = res.stdout + res.stderr
    assert res.returncode == 1, combined
    assert "1 signer-not-on-keyring" in combined, combined
    assert f"signer-not-on-keyring: {tid}" in combined, combined
    assert f"(signing key {fingerprint})" in combined, combined
    assert "0 bad-signature" in combined, combined


@_ssh
def test_off_keyring_signature_passes_in_advisory_mode(tmp_path: Path, monkeypatch) -> None:
    """With enforcement off, the same store is reported and exits 0."""
    repo, _ident, _priv, _fingerprint = _mallory_store(
        tmp_path, monkeypatch, "offkeyringadvisory", "abab-cdcd-efef-0101", "e-advisory"
    )
    env = subprocess_env({"REBAR_ROOT": str(repo)})
    env.pop("REBAR_IDENTITY_REQUIRE_AUTHENTICATED", None)
    res = subprocess.run(
        [sys.executable, "-m", "rebar", "verify-identity", "--all"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    combined = res.stdout + res.stderr
    assert res.returncode == 0, combined
    assert "advisory" in combined.lower(), combined


@_ssh
def test_null_signer_ledger_entry_with_a_bad_signature_is_bad_signature(
    tmp_path: Path, monkeypatch
) -> None:
    """Removing the null-signer short-circuit must not let a broken ledger signature through:
    an entry whose signature bytes were made over other bytes is still bad-signature."""
    repo = _init(tmp_path, monkeypatch, "nullsignerbad")
    priv, pub = _keypair(tmp_path, "author")
    priv_m, _pub_m = _keypair(tmp_path, "mallory")
    ident = rebar.create_identity("Ada", "ada@example.com", keys=[pub], repo_root=str(repo))
    rebar.use_identity(ident, repo_root=str(repo))
    monkeypatch.setenv("REBAR_IDENTITY_SIGNING_KEY", priv)
    tid = rebar.create_ticket("task", "signed work", repo_root=str(repo))
    rebar.comment(tid, "c1", repo_root=str(repo))
    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", "0")
    monkeypatch.setenv("REBAR_COMPACT_THRESHOLD", "1")
    rebar.compact(tid, repo_root=str(repo))

    from rebar.attest import dsse

    tracker = str(tracker_dir(str(repo)))
    snap_path = next((Path(tracker) / tid).glob("*-SNAPSHOT.json"))
    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    entry = snap["data"]["compiled_state"]["authorship_ledger"][0]
    original = dsse.decode(entry["signature"])
    foreign = authorship.sign_event_authorship(_event(tid, "e-other", ident), priv_m, ident)
    entry["signature"] = _encode(
        dsse.Envelope(original.payload_type, original.payload, list(foreign.signatures))
    )
    entry["signer_pubkey"] = None
    snap_path.write_text(json.dumps(snap), encoding="utf-8")
    subprocess.run(["git", "-C", tracker, "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", tracker, "commit", "-q", "--no-verify", "-m", "bad ledger signature"],
        check=True,
        capture_output=True,
    )

    res = _verify_authorship(repo)
    combined = res.stdout + res.stderr
    assert res.returncode == 1, combined
    assert "1 bad-signature" in combined, combined
    assert "0 signer-not-on-keyring" in combined, combined


@_ssh
def test_null_signer_ledger_entry_signed_by_a_keyring_key_verifies(
    tmp_path: Path, monkeypatch
) -> None:
    """A ledger entry whose signer_pubkey compaction left null, but whose signature verifies
    against the author's keyring, is verified rather than rejected before the check."""
    repo = _init(tmp_path, monkeypatch, "nullsigner")
    priv, pub = _keypair(tmp_path, "author")
    ident = rebar.create_identity("Ada", "ada@example.com", keys=[pub], repo_root=str(repo))
    rebar.use_identity(ident, repo_root=str(repo))
    monkeypatch.setenv("REBAR_IDENTITY_SIGNING_KEY", priv)
    tid = rebar.create_ticket("task", "signed work", repo_root=str(repo))
    rebar.comment(tid, "c1", repo_root=str(repo))
    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", "0")
    monkeypatch.setenv("REBAR_COMPACT_THRESHOLD", "1")
    rebar.compact(tid, repo_root=str(repo))

    tracker = str(tracker_dir(str(repo)))
    snap_path = next((Path(tracker) / tid).glob("*-SNAPSHOT.json"))
    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    ledger = snap["data"]["compiled_state"]["authorship_ledger"]
    assert ledger and all(r["signer_pubkey"] == pub for r in ledger)
    for r in ledger:
        r["signer_pubkey"] = None
    snap_path.write_text(json.dumps(snap), encoding="utf-8")
    subprocess.run(["git", "-C", tracker, "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", tracker, "commit", "-q", "--no-verify", "-m", "null signer"],
        check=True,
        capture_output=True,
    )

    res = _verify_authorship(repo)
    combined = res.stdout + res.stderr
    assert res.returncode == 0, combined
    assert "0 bad-signature" in combined and "0 signer-not-on-keyring" in combined, combined


@_ssh
def test_signature_over_other_bytes_is_bad_signature(tmp_path: Path, monkeypatch) -> None:
    """The payload binds the event, but the signature bytes were made over another event's
    Statement: no key verifies it, its own embedded key included."""
    repo = _init(tmp_path, monkeypatch, "badbytes")
    _priv, pub = _keypair(tmp_path, "author")
    priv_m, _pub_m = _keypair(tmp_path, "mallory")
    ident = rebar.create_identity("Ada", "ada@example.com", keys=[pub], repo_root=str(repo))
    tid = "1111-2222-3333-4444"
    event = _event(tid, "e-badbytes", ident)
    other = _event(tid, "e-other", ident)
    good = authorship.sign_event_authorship(event, priv_m, principal=ident)
    foreign = authorship.sign_event_authorship(other, priv_m, principal=ident)
    from rebar.attest import dsse

    event["author_sig"] = _encode(
        dsse.Envelope(good.payload_type, good.payload, list(foreign.signatures))
    )
    _commit_event(repo, tid, event)

    res = _verify_authorship(repo)
    combined = res.stdout + res.stderr
    assert res.returncode == 1, combined
    assert "bad-signature: 1111-2222-3333-4444/" in combined, combined
    assert "0 signer-not-on-keyring" in combined, combined


@_ssh
def test_sound_signature_for_another_event_is_bad_signature(tmp_path: Path, monkeypatch) -> None:
    """A sound Statement signature for event X, attached to event Y, does not bind Y."""
    repo = _init(tmp_path, monkeypatch, "nobind")
    _priv, pub = _keypair(tmp_path, "author")
    priv_m, _pub_m = _keypair(tmp_path, "mallory")
    ident = rebar.create_identity("Ada", "ada@example.com", keys=[pub], repo_root=str(repo))
    tid = "5555-6666-7777-8888"
    event = _event(tid, "e-target", ident)
    other = _event(tid, "e-source", ident)
    event["author_sig"] = _encode(authorship.sign_event_authorship(other, priv_m, principal=ident))
    _commit_event(repo, tid, event)

    res = _verify_authorship(repo)
    combined = res.stdout + res.stderr
    assert res.returncode == 1, combined
    assert "bad-signature: 5555-6666-7777-8888/" in combined, combined


@_ssh
def test_identity_finding_json_entry_names_the_signing_key(tmp_path: Path, monkeypatch) -> None:
    repo, _ident, _priv, fingerprint = _mallory_store(
        tmp_path, monkeypatch, "offkeyringjson", "9999-aaaa-bbbb-cccc", "e-offkeyring3"
    )
    env = subprocess_env({"REBAR_ROOT": str(repo), "REBAR_IDENTITY_REQUIRE_AUTHENTICATED": "1"})
    res = subprocess.run(
        [sys.executable, "-m", "rebar", "verify-identity", "--all", "--format", "json"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 1, res.stdout + res.stderr
    payload = json.loads(res.stdout)
    entry = next(e for e in payload if e["event_uuid"] == "e-offkeyring3")
    assert entry["verdict"] == "signer-not-on-keyring"
    assert entry["display"] == "unverified"
    assert entry["signing_key"] == fingerprint

    import jsonschema

    schema_path = (
        Path(rebar.__file__).resolve().parent / "schemas" / "verify_identity_report.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    for e in payload:
        jsonschema.validate(e, schema)


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
        # Added by the op-cert producer-signing seam (story 8d8e) — the op-cert dispatch verdicts.
        "invalid",
        "unavailable",
        "unknown_kind",
        "unknown_scheme",
        "untrusted",
    }


# ── AC6: ensure_identity_for is canonical after alias removal ─────────────────
def test_ensure_identity_for_replaces_create_placeholder(tmp_path: Path, monkeypatch) -> None:
    repo = _init(tmp_path, monkeypatch, "placeholder")
    assert not hasattr(rebar, "create_placeholder")
    first = rebar.ensure_identity_for("jira", "acct-123", "Jane Doe", repo_root=str(repo))
    again = rebar.ensure_identity_for("jira", "acct-123", "Jane Doe", repo_root=str(repo))
    assert first == again, "ensure_identity_for must be idempotent on the same mapping"
    state = rebar.show_ticket(first, repo_root=str(repo))
    assert state["ticket_type"] == "identity"
    assert not (state.get("keys") or [])  # keyless placeholder
