"""Local same-environment op-cert verify path: SIGNED subject binding + payload-authoritative
material/commit (security findings A + B on story 8d8e's local verify path).

The epic's "verify-then-extract" principle (finding #4): a verifier trusts ONLY the signed DSSE
envelope payload, never the record's plaintext mirror fields (which live on the auto-pushed,
non-Gerrit-gated tickets branch and are attacker-writable). The merge-gate path already followed
this; these tests pin that the LOCAL path (``verify_opcert_record`` / ``verify_signature`` +
``compute_validity``) now does too:

  * Finding A — a valid op-cert the environment signed for ticket X is REJECTED when replayed onto
    ticket Y's record, and a cert signed for kind K1 is REJECTED under kind K2's slot (the signature
    still verifies; the subject binding does not).
  * Finding B — corrupting the plaintext ``material_fingerprint`` / ``merged_log_commit`` /
    ``head_sha`` mirrors (and the plaintext manifest ``material:`` line) on an envelope record does
    NOT change the local verify verdict and does NOT flip ``compute_validity`` — the SIGNED payload
    is authoritative.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar import signing
from rebar._opcert_signing import verify_opcert_record
from rebar.llm.plan_review.attest import (
    compute_validity,
    current_material_fingerprint,
    registry_version,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@e.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "i")
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.delenv("REBAR_OPCERT_ENV_ID", raising=False)
    rebar.init_repo(repo_root=str(repo))
    return repo


def _tracker(store: Path) -> Path:
    from rebar._commands._seam import tracker_dir

    return Path(tracker_dir(str(store)))


def _sign_opcert(store: Path, tid: str, manifest, *, material: str, commit: str) -> dict:
    """Mint an op-cert for ``tid`` via the lower-level asymmetric seam and return the record."""
    key_path = signing.ensure_opcert_key(str(_tracker(store)))
    principal = signing.opcert_principal(str(_tracker(store)))
    return signing.sign_opcert_manifest(
        tid,
        manifest,
        material_fingerprint=material,
        merged_log_commit=commit,
        key_path=key_path,
        principal=principal,
        repo_root=str(store),
    )


# ── Finding A: SIGNED subject binding (cross-ticket / cross-kind replay) ──────────────
def test_valid_cert_for_ticket_x_rejected_on_ticket_y(store: Path) -> None:
    """A cert the environment VALIDLY signed for ticket X, copied onto ticket Y's record, is
    rejected: the SSHSIG signature still verifies, but the signed subject binds X, not Y."""
    x = rebar.create_ticket("task", "ticket X", repo_root=str(store))
    resolved_x = rebar.show_ticket(x, repo_root=str(store))["ticket_id"]
    rec = _sign_opcert(
        store, x, ["plan-review: PASS", "material: mx"], material="mx", commit="deadbeef"
    )
    principal = signing.opcert_principal(str(_tracker(store)))

    # Same VALID envelope, filed under a different ticket id (the replay).
    replayed = {
        "envelope": rec["envelope"],
        "algorithm": "sshsig",
        "principal": principal,
        "manifest": rec["manifest"],
        "material_fingerprint": rec["material_fingerprint"],
        "merged_log_commit": rec["merged_log_commit"],
        "kind": "plan-review",
    }
    # Sanity: it genuinely verifies for the ticket it was signed for.
    ok = verify_opcert_record(replayed, resolved_x, kind="plan-review", repo_root=str(store))
    assert ok["verified"] is True and ok["verdict"] == "certified"

    # But replayed onto a DIFFERENT ticket it is rejected (not certified).
    bad = verify_opcert_record(
        replayed, "ffff-ffff-ffff-ffff", kind="plan-review", repo_root=str(store)
    )
    assert bad["verified"] is False
    assert bad["verdict"] == "mismatch"
    assert "cross-ticket" in bad["reason"]


def test_valid_cert_for_kind_k1_rejected_under_kind_k2(store: Path) -> None:
    """A cert signed for kind ``plan-review`` is rejected when verified under the
    ``completion-verifier`` kind slot (kind-confusion), even though the ticket + signature match."""
    x = rebar.create_ticket("task", "ticket X", repo_root=str(store))
    resolved_x = rebar.show_ticket(x, repo_root=str(store))["ticket_id"]
    rec = _sign_opcert(
        store, x, ["plan-review: PASS", "material: mx"], material="mx", commit="deadbeef"
    )

    # Verified under the WRONG kind slot → mismatch (cross-kind replay).
    bad = verify_opcert_record(rec, resolved_x, kind="completion-verifier", repo_root=str(store))
    assert bad["verified"] is False
    assert bad["verdict"] == "mismatch"
    assert "cross-kind" in bad["reason"]

    # Verified under the CORRECT kind slot → certified.
    ok = verify_opcert_record(rec, resolved_x, kind="plan-review", repo_root=str(store))
    assert ok["verified"] is True and ok["verdict"] == "certified"


def test_kind_confusion_caught_via_manifest_derived_slot_when_kind_none(store: Path) -> None:
    """With ``kind=None`` (the legacy most-recent path) the expected kind falls back to the
    manifest-derived slot key. A cert signed for kind ``plan-review`` whose record carries a
    ``completion-verifier``-prefixed manifest[0] (the reducer's slot key) is rejected — the signed
    kind and the filed slot disagree."""
    x = rebar.create_ticket("task", "ticket X", repo_root=str(store))
    resolved_x = rebar.show_ticket(x, repo_root=str(store))["ticket_id"]
    # Signed as plan-review …
    rec = _sign_opcert(
        store, x, ["plan-review: PASS", "material: mx"], material="mx", commit="deadbeef"
    )
    # … but filed under a completion-verifier-prefixed manifest (attacker-mutated plaintext).
    forged = {**rec, "manifest": ["completion-verifier: PASS", "material: mx"]}
    bad = verify_opcert_record(forged, resolved_x, repo_root=str(store))  # kind=None
    assert bad["verified"] is False
    assert bad["verdict"] == "mismatch"
    assert "cross-kind" in bad["reason"]


def test_cross_ticket_replay_rejected_end_to_end_via_verify_signature(store: Path) -> None:
    """The full ``verify_signature`` path (reads the kind-keyed attestations map) also rejects a
    cross-ticket replay: X's valid envelope appended to ticket Y's log is not certified for Y."""
    from rebar._commands._seam import append_event, require_id

    x = rebar.create_ticket("task", "ticket X", repo_root=str(store))
    y = rebar.create_ticket("task", "ticket Y", repo_root=str(store))
    rec = _sign_opcert(
        store, x, ["plan-review: PASS", "material: mx"], material="mx", commit="deadbeef"
    )
    tracker = _tracker(store)
    resolved_y = require_id(y, tracker)
    # Append X's envelope onto Y's log verbatim (an attacker with tickets-branch write access).
    append_event(
        resolved_y,
        "SIGNATURE",
        {
            "manifest": rec["manifest"],
            "algorithm": "sshsig",
            "envelope": rec["envelope"],
            "material_fingerprint": rec["material_fingerprint"],
            "merged_log_commit": rec["merged_log_commit"],
            "principal": signing.opcert_principal(str(tracker)),
            "signed_at": rec.get("signed_at"),
        },
        tracker,
        repo_root=str(store),
    )
    res = signing.verify_signature(y, kind="plan-review", repo_root=str(store))
    assert res["verified"] is False
    assert res["verdict"] == "mismatch"


# ── Finding B: SIGNED payload is authoritative, not the plaintext mirror ──────────────
def test_verify_verdict_and_surfaced_fields_ignore_plaintext_mirror(store: Path) -> None:
    """Corrupting the plaintext ``material_fingerprint`` / ``merged_log_commit`` mirrors AND the
    plaintext manifest ``material:`` line does NOT change the verify verdict, and the surfaced
    material/commit come from the SIGNED payload, never the plaintext mirror."""
    x = rebar.create_ticket("task", "ticket X", repo_root=str(store))
    resolved_x = rebar.show_ticket(x, repo_root=str(store))["ticket_id"]
    rec = _sign_opcert(
        store, x, ["plan-review: PASS", "material: mx"], material="mx", commit="c0ffee"
    )

    clean = verify_opcert_record(rec, resolved_x, kind="plan-review", repo_root=str(store))
    assert clean["verified"] is True and clean["verdict"] == "certified"

    # Attacker mutates every plaintext mirror + the manifest material line; envelope untouched.
    tampered = {
        **rec,
        "material_fingerprint": "EVIL",
        "merged_log_commit": "EVIL",
        "head_sha": "EVIL",
        "manifest": ["plan-review: PASS", "material: EVIL"],
    }
    res = verify_opcert_record(tampered, resolved_x, kind="plan-review", repo_root=str(store))
    # Verdict is invariant under plaintext-mirror mutation …
    assert res["verified"] is True and res["verdict"] == "certified"
    # … and the surfaced material/commit are the SIGNED values, not the plaintext "EVIL".
    assert res["material_fingerprint"] == "mx"
    assert res["merged_log_commit"] == "c0ffee"


def test_compute_validity_uses_signed_material_not_plaintext(store: Path) -> None:
    """``compute_validity`` gates material-edit invalidation on the SIGNED payload fingerprint. With
    the ticket UNCHANGED, an attestation whose plaintext material mirror + manifest line have been
    corrupted still validates (payload authoritative); the corruption does not flip the verdict."""
    x = rebar.create_ticket(
        "task",
        "ticket X with enough body for a real material fingerprint to be computed here",
        repo_root=str(store),
    )
    resolved_x = rebar.show_ticket(x, repo_root=str(store))["ticket_id"]
    fp = current_material_fingerprint(resolved_x, repo_root=str(store))
    assert fp  # the fingerprint is computable
    head = signing.head_sha(str(store))
    regver = registry_version(repo_root=str(store))
    # An UNSCOPED plan-review op-cert (no dep lines): material bound = the ticket's real
    # fingerprint, merged_log_commit bound = current HEAD, so it is genuinely fresh + matching.
    manifest = ["plan-review: PASS", f"material: {fp}", f"regver: {regver}"]
    rec = _sign_opcert(store, x, manifest, material=fp, commit=head)
    state = rebar.show_ticket(resolved_x, repo_root=str(store))

    clean = verify_opcert_record(rec, resolved_x, kind="plan-review", repo_root=str(store))
    v_clean = compute_validity(clean, state, "plan-review", repo_root=str(store))
    assert v_clean["valid"] is True and v_clean["verdict"] == "certified"

    # Corrupt the plaintext mirrors + manifest material line; the ticket itself is unchanged, so
    # a verifier trusting the plaintext would see material EVIL != current fp → stale-material.
    tampered = {
        **rec,
        "material_fingerprint": "EVIL",
        "head_sha": "EVIL",
        "merged_log_commit": "EVIL",
        "manifest": ["plan-review: PASS", "material: EVIL", f"regver: {regver}"],
    }
    res = verify_opcert_record(tampered, resolved_x, kind="plan-review", repo_root=str(store))
    v_tampered = compute_validity(res, state, "plan-review", repo_root=str(store))
    # Verdict is invariant: the SIGNED payload material (fp) + commit (head) are used, not "EVIL".
    assert v_tampered["valid"] is True
    assert v_tampered["verdict"] == v_clean["verdict"] == "certified"


def test_compute_validity_still_catches_a_real_material_edit(store: Path) -> None:
    """Sanity that the fix does not weaken the material gate: a genuine material edit (change the
    ticket body) DOES invalidate the attestation, because the SIGNED payload material no longer
    matches the ticket's recomputed current fingerprint."""
    x = rebar.create_ticket(
        "task",
        "ticket X with enough body for a real material fingerprint to be computed here",
        repo_root=str(store),
    )
    resolved_x = rebar.show_ticket(x, repo_root=str(store))["ticket_id"]
    fp = current_material_fingerprint(resolved_x, repo_root=str(store))
    head = signing.head_sha(str(store))
    regver = registry_version(repo_root=str(store))
    manifest = ["plan-review: PASS", f"material: {fp}", f"regver: {regver}"]
    rec = _sign_opcert(store, x, manifest, material=fp, commit=head)

    # Materially edit the ticket → its current fingerprint changes.
    rebar.edit_ticket(
        resolved_x,
        description="a substantially different description than before edit",
        repo_root=str(store),
    )
    state = rebar.show_ticket(resolved_x, repo_root=str(store))
    res = verify_opcert_record(rec, resolved_x, kind="plan-review", repo_root=str(store))
    v = compute_validity(res, state, "plan-review", repo_root=str(store))
    assert v["valid"] is False
    assert v["verdict"] == "stale-material"
