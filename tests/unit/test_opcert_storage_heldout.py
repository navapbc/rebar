"""Held-out adversarial/robustness oracle for op-cert storage (keystone e4df). NOT shown to the
implementation subagent.

* compaction survival — an op-cert stored on a ticket survives a compact→SNAPSHOT round-trip
  (the reducer's attestations fold preserves the envelope + bound fields);
* legacy-HMAC → None — `opcert_from_record` returns None for a plain HMAC `sign_manifest` record;
* additive invariance — a legacy HMAC record is byte-unchanged (no `envelope`/bound keys), so
  older clones preserve-and-ignore;
* fold-through — an op-cert survives further post-signature events (a comment) in a full reduce.

Real ssh-keygen + a real rebar store.
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
    key = signing.signing_key(str(tracker))
    append_event(
        resolved,
        "SIGNATURE",
        {
            "manifest": manifest,
            "algorithm": signing.ALGORITHM,
            "signature": signing.compute_signature(resolved, manifest, key),
            "key_id": signing.key_fingerprint(key),
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
    # The op-cert extension is additive: an HMAC op-cert still CERTIFIES via the HMAC verify path
    # (verify_signature reads the raw record, not the hex-stripped public_state view).
    verdict = signing.verify_signature(tid, kind=KIND, repo_root=str(store))
    assert verdict["verified"] is True
    assert verdict["verdict"] == "certified"


def test_opcert_survives_later_events(store: Path, tmp_path: Path) -> None:
    priv, _pub = _keypair(tmp_path, "env")
    commit = _head(store)
    tid = rebar.create_ticket("task", "op-cert me", repo_root=str(store))
    _sign_opcert(store, tid, priv, commit)
    rebar.comment(tid, "a later event", repo_root=str(store))  # more history after the op-cert

    rec = rebar.show_ticket(tid, repo_root=str(store))["attestations"][KIND]
    assert rec.get("envelope")
    assert rec["merged_log_commit"] == commit
