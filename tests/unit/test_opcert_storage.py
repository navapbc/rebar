"""Happy-path specification for op-cert storage.

``signing.sign_opcert_manifest`` writes an envelope-bearing signature event whose reduced kind
record retains the DSSE envelope, material fingerprint, and merged-log commit.
``opcert.opcert_from_record`` reconstructs and verifies it against a pinned keyring. Adversarial
compatibility cases remain in the held-out oracles below.

Held-out oracles merged from test_opcert_storage_heldout.py: Held-out op-cert storage oracles cover
compaction survival, legacy HMAC mapping to ``None``, byte-preserving additive compatibility, and
reduction through later events. Tests use ``ssh-keygen`` and a git-backed store.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar import signing
from rebar.attest import opcert, sshsig

try:
    sshsig.ensure_available()
    _SSH_OK = True
except Exception:  # noqa: BLE001
    _SSH_OK = False

pytestmark = pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required for SSHSIG")

ENV_ID = "trusted-ci@rebar.test"
MATERIAL = "0123456789abcdef"
KIND = "completion-verifier"


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _positions(repo: Path) -> list[tuple[str, str]]:
    """Sorted (log_position, tickets_branch_commit) over the store's event files, oldest-first —
    so callers can pick an early add-position and a later storage anchor S (Option B)."""
    import os

    from rebar._commands._seam import tracker_dir
    from rebar.attest import authorship

    tracker = str(tracker_dir(str(repo)))
    commit_map = authorship.build_introducing_commit_map(repo_root=str(repo))
    out: list[tuple[str, str]] = []
    for d in sorted(os.listdir(tracker)):
        dp = os.path.join(tracker, d)
        if d.startswith(".") or not os.path.isdir(dp):
            continue
        for fn in sorted(os.listdir(dp)):
            if not fn.endswith(".json") or fn.startswith("."):
                continue
            commit = commit_map.get(f"{d}/{fn}")
            if commit:
                out.append((fn[:-5].rsplit("-", 1)[0], commit))
    out.sort()
    return out


def test_sign_opcert_stores_envelope_on_ticket(store: Path, tmp_path: Path) -> None:
    """The producer writes an envelope-bearing SIGNATURE event; the ticket's reduced
    attestations[kind] record carries the encoded envelope + bound fields, and no HMAC."""
    priv, _pub = _keypair(tmp_path, "env")
    commit = _head(store)
    tid = rebar.create_ticket("task", "op-cert me", repo_root=str(store))

    signing.sign_opcert_manifest(
        tid,
        [f"{KIND}: PASS", f"ticket: {tid}"],
        material_fingerprint=MATERIAL,
        merged_log_commit=commit,
        key_path=priv,
        principal=ENV_ID,
        repo_root=str(store),
    )

    state = rebar.show_ticket(tid, repo_root=str(store))
    rec = state["attestations"][KIND]
    assert rec.get("envelope")  # non-empty encoded DSSE envelope
    assert rec["material_fingerprint"] == MATERIAL
    assert rec["merged_log_commit"] == commit
    assert not rec.get("signature")  # asymmetric op-cert carries no HMAC


def test_opcert_from_record_roundtrips_and_verifies(store: Path, tmp_path: Path) -> None:
    """The read helper reconstructs the envelope, which verifies against the pinned keyring."""
    priv, pub = _keypair(tmp_path, "env")
    commit = _head(store)
    tid = rebar.create_ticket("task", "op-cert me", repo_root=str(store))
    signing.sign_opcert_manifest(
        tid,
        [f"{KIND}: PASS"],
        material_fingerprint=MATERIAL,
        merged_log_commit=commit,
        key_path=priv,
        principal=ENV_ID,
        repo_root=str(store),
    )

    rec = rebar.show_ticket(tid, repo_root=str(store))["attestations"][KIND]
    got = opcert.opcert_from_record(rec)
    assert got is not None
    envelope, bound = got
    assert bound["material_fingerprint"] == MATERIAL
    assert bound["merged_log_commit"] == commit

    # Option B: the key era boundary is a TICKETS-BRANCH log position and validity is judged at the
    # cert's STORAGE ANCHOR S (a tickets-branch commit). Pin the key from an early position and use
    # the latest tickets-branch commit (the SIGNATURE event just written) as S.
    pos = _positions(store)
    keyring = [
        {"public_key": pub, "added_at_log_position": pos[0][0], "revoked_at_log_position": None}
    ]
    verdict = opcert.verify_opcert(
        envelope,
        tid,
        MATERIAL,
        commit,
        keyring,
        kind=KIND,
        principal=ENV_ID,
        storage_anchor_commit=pos[-1][1],
        storage_anchor_position=pos[-1][0],
        repo_root=str(store),
    )
    assert verdict.verified is True
    assert verdict.verdict == "certified"


# ── held-out oracles (merged from test_opcert_storage_heldout.py) ──

try:
    sshsig.ensure_available()
    _SSH_OK = True
except Exception:  # noqa: BLE001
    _SSH_OK = False


def _sign_opcert(store: Path, tid: str, priv: str, commit: str) -> None:
    signing.sign_opcert_manifest(
        tid,
        [f"{KIND}: PASS"],
        material_fingerprint=MATERIAL,
        merged_log_commit=commit,
        key_path=priv,
        principal=ENV_ID,
        repo_root=str(store),
    )


def test_opcert_survives_compaction(store: Path, tmp_path: Path) -> None:
    priv, _pub = _keypair(tmp_path, "env")
    commit = _head(store)
    tid = rebar.create_ticket("task", "op-cert me", repo_root=str(store))
    _sign_opcert(store, tid, priv, commit)

    rebar.compact(tid, repo_root=str(store))  # fold events into a SNAPSHOT

    rec = rebar.show_ticket(tid, repo_root=str(store))["attestations"][KIND]
    assert rec.get("envelope")
    assert rec["material_fingerprint"] == MATERIAL
    assert rec["merged_log_commit"] == commit


def _write_legacy_hmac_attestation(store: Path, tid: str) -> None:
    """Append a genuine LEGACY HMAC SIGNATURE event (as clones did before story 8d8e repointed the
    seam to op-certs). Constructed directly rather than via ``sign_manifest`` — which now mints an
    op-cert — so these tests exercise the read-both path on a real HMAC record."""
    from rebar._commands._seam import append_event

    resolved = rebar.show_ticket(tid, repo_root=str(store))["ticket_id"]
    tracker = Path(store) / ".tickets-tracker"
    manifest = [f"{KIND}: PASS"]
    append_event(
        resolved,
        "SIGNATURE",
        {
            "manifest": manifest,
            "algorithm": "HMAC-SHA256",
            "signature": "ab" * 32,
            "key_id": "412fa5372ad877e0",
            "kind": KIND,
        },
        tracker,
        repo_root=str(store),
    )


def test_opcert_from_record_none_for_hmac(store: Path) -> None:
    tid = rebar.create_ticket("task", "hmac me", repo_root=str(store))
    _write_legacy_hmac_attestation(store, tid)
    rec = rebar.show_ticket(tid, repo_root=str(store))["attestations"][KIND]
    assert opcert.opcert_from_record(rec) is None  # a plain HMAC record is not an op-cert


def test_hmac_record_is_additively_unchanged(store: Path) -> None:
    """A legacy HMAC record carries none of the op-cert fields — the extension is
    present-only, so older clones preserve-and-ignore."""
    tid = rebar.create_ticket("task", "hmac me", repo_root=str(store))
    _write_legacy_hmac_attestation(store, tid)
    rec = rebar.show_ticket(tid, repo_root=str(store))["attestations"][KIND]
    # The op-cert fields are folded present-only, so an HMAC record gains none of them.
    # (The raw HMAC `signature` hex is stripped from show reads by public_state, so we assert the
    # HMAC verify path separately below rather than off the stripped record.)
    assert "envelope" not in rec
    assert "material_fingerprint" not in rec
    assert "merged_log_commit" not in rec
    assert rec.get("algorithm") == "HMAC-SHA256"  # still an HMAC record, unchanged in kind
    # Contract phase (story 8f1d): HMAC is a RETIRED scheme for the op-cert kinds. The record is
    # byte-unchanged (append-only history untouched), but validity-on-read now reports it
    # NOT-certified — a forgeable HMAC op-cert can no longer certify.
    verdict = signing.verify_signature(tid, kind=KIND, repo_root=str(store))
    assert verdict["verified"] is False
    assert verdict["verdict"] == "unsigned"


def test_opcert_survives_later_events(store: Path, tmp_path: Path) -> None:
    priv, _pub = _keypair(tmp_path, "env")
    commit = _head(store)
    tid = rebar.create_ticket("task", "op-cert me", repo_root=str(store))
    _sign_opcert(store, tid, priv, commit)
    rebar.comment(tid, "a later event", repo_root=str(store))  # more history after the op-cert

    rec = rebar.show_ticket(tid, repo_root=str(store))["attestations"][KIND]
    assert rec.get("envelope")
    assert rec["merged_log_commit"] == commit
