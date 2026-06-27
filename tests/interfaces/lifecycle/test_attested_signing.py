"""S4 — attested signing: pin verified_at_sha via the manifest channel (epic raze-vet-ditch).

The SHA a gate verified is bound through the EXISTING manifest channel (a signed
``verified-at-sha:<sha>`` step), NOT a new signed-payload field — so no PAYLOAD_VERSION
bump and prior signatures still verify. Covers: the signed pin + queryable field, no
prior-signature invalidation, reproducibility, overwrite/idempotency, reopen-retires,
local-never-signs, unresolvable-ref-fails-closed, and the in-toto envelope shape.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
import rebar.llm  # noqa: F401 — ensures rebar.llm is importable for monkeypatch
from rebar import signing


def _git(repo: Path, *a: str) -> None:
    subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)


def _enable(repo: Path) -> None:
    (repo / ".rebar").mkdir(exist_ok=True)
    (repo / ".rebar" / "config.conf").write_text(
        "verify.require_completion_verification_for_close = true\n"
    )


def _commit(repo: Path) -> None:
    (repo / "f.txt").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-q", "-m", "c")


def _verdict(source="attested", sha="deadbeef"):
    def _fn(ticket_id, **kw):
        return {
            "verdict": "PASS",
            "findings": [],
            "target": {"kind": "ticket", "ticket_ids": [ticket_id]},
            "reviewers": ["completion-verifier"],
            "runner": "fake",
            "model": "m",
            "trace_id": None,
            "source": source,
            "verified_at_sha": sha if source == "attested" else None,
            "signable": source == "attested",
        }

    return _fn


# --------------------------------------------------------------------------------------
# AC1 — verified-at-sha is a SIGNED manifest step; no PAYLOAD_VERSION bump; prior sigs verify
# --------------------------------------------------------------------------------------
def test_payload_version_unchanged():
    assert signing.PAYLOAD_VERSION == 1


def test_verified_at_sha_step_is_signed_and_certifies(rebar_repo: Path):
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))
    manifest = [
        "completion-verifier: PASS",
        f"ticket: {tid}",
        signing.verified_at_sha_step("abc123"),
    ]
    signing.sign_manifest(tid, manifest, repo_root=str(rebar_repo))
    v = rebar.verify_signature(tid, repo_root=str(rebar_repo))
    assert v["verdict"] == "certified"
    assert v["verified_at_sha"] == "abc123"


def test_prior_signature_without_pin_still_verifies(rebar_repo: Path):
    """A manifest WITHOUT a verified-at-sha step (a pre-S4 signature shape) still certifies —
    the additive step did not change the canonical payload / invalidate old signatures."""
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))
    signing.sign_manifest(
        tid, ["completion-verifier: PASS", f"ticket: {tid}"], repo_root=str(rebar_repo)
    )
    v = rebar.verify_signature(tid, repo_root=str(rebar_repo))
    assert v["verdict"] == "certified"
    assert v["verified_at_sha"] is None


def test_pin_enters_signed_bytes(rebar_repo: Path):
    """Changing the pinned SHA changes the signature — proof the step is inside the signed
    bytes (bound), not mere unsigned metadata."""
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))
    base = ["completion-verifier: PASS", f"ticket: {tid}"]
    from rebar import config as _cfg

    k = signing.signing_key(str(_cfg.tracker_dir(str(rebar_repo))))
    sig_a = signing.compute_signature(tid, [*base, signing.verified_at_sha_step("aaa")], k)
    sig_b = signing.compute_signature(tid, [*base, signing.verified_at_sha_step("bbb")], k)
    assert sig_a != sig_b


# --------------------------------------------------------------------------------------
# AC2/AC3 — queryable + reproducible + overwrite-on-reverify (compare-and-set boundary)
# --------------------------------------------------------------------------------------
def test_reverify_different_sha_overwrites_no_stale_pin(rebar_repo: Path):
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))
    signing.sign_manifest(
        tid,
        ["completion-verifier: PASS", f"ticket: {tid}", signing.verified_at_sha_step("sha1")],
        repo_root=str(rebar_repo),
    )
    assert rebar.verify_signature(tid, repo_root=str(rebar_repo))["verified_at_sha"] == "sha1"
    # Re-verify against a DIFFERENT sha → the latest SIGNATURE event wins (no stale/double pin).
    signing.sign_manifest(
        tid,
        ["completion-verifier: PASS", f"ticket: {tid}", signing.verified_at_sha_step("sha2")],
        repo_root=str(rebar_repo),
    )
    v = rebar.verify_signature(tid, repo_root=str(rebar_repo))
    assert v["verdict"] == "certified"
    assert v["verified_at_sha"] == "sha2"


def test_concurrent_writers_no_stale_or_double_pin(rebar_repo: Path):
    """AC3 (concurrency, NOT inspection): two writers race to pin DIFFERENT SHAs. The pin
    write is serialized through the ticket's locked event-append boundary, so the outcome is
    consistent — exactly ONE verified-at-sha step survives (no double pin), it certifies, and
    the pin is one of the racers' SHAs (last-writer-wins, never an interleaved/corrupt value)."""
    from concurrent.futures import ThreadPoolExecutor

    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))
    shas = [f"racesha{i:02d}" for i in range(12)]

    def _sign(sha: str):
        signing.sign_manifest(
            tid,
            ["completion-verifier: PASS", f"ticket: {tid}", signing.verified_at_sha_step(sha)],
            repo_root=str(rebar_repo),
        )

    with ThreadPoolExecutor(max_workers=12) as ex:
        list(ex.map(_sign, shas))

    v = rebar.verify_signature(tid, repo_root=str(rebar_repo))
    assert v["verdict"] == "certified"  # not corrupted by interleaving
    pins = [s for s in v["manifest"] if s.startswith(signing.VERIFIED_AT_SHA_PREFIX)]
    assert len(pins) == 1, f"exactly one pin must survive, got {pins}"  # no double pin
    assert v["verified_at_sha"] in shas  # a real racer's SHA, not a torn value


# --------------------------------------------------------------------------------------
# AC4 — reopen retires the pin; local never signs
# --------------------------------------------------------------------------------------
def test_reopen_retires_attested_pin(rebar_repo: Path, monkeypatch):
    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", _verdict("attested", "pinnedsha"))
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))
    rebar.claim(tid, assignee="me", repo_root=str(rebar_repo))
    rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    v = rebar.verify_signature(tid, repo_root=str(rebar_repo))
    assert v["verdict"] == "certified" and v["verified_at_sha"] == "pinnedsha"
    # Reopen → the attested pin is retired (the closure it attested is undone).
    rebar.reopen(tid, repo_root=str(rebar_repo))
    v2 = rebar.verify_signature(tid, repo_root=str(rebar_repo))
    assert v2["verdict"] == "unsigned"
    assert v2["verified_at_sha"] is None


def test_retire_leaves_legacy_signature_intact(rebar_repo: Path):
    """retire_attested_pin only retires an ATTESTED signature; a legacy signature (no
    verified-at-sha step) is left certified — so reopen behavior is unchanged for it."""
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))
    signing.sign_manifest(
        tid, ["completion-verifier: PASS", f"ticket: {tid}"], repo_root=str(rebar_repo)
    )
    assert signing.retire_attested_pin(tid, repo_root=str(rebar_repo)) is False
    assert rebar.verify_signature(tid, repo_root=str(rebar_repo))["verdict"] == "certified"


def test_local_source_close_never_signs(rebar_repo: Path, monkeypatch):
    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", _verdict("local"))
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))
    rebar.claim(tid, assignee="me", repo_root=str(rebar_repo))
    rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    # Verified + passed, but a LOCAL run is never signed (closed-without-signature signal).
    assert rebar.verify_signature(tid, repo_root=str(rebar_repo))["verdict"] == "unsigned"


def test_attested_close_signs_with_pin(rebar_repo: Path, monkeypatch):
    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", _verdict("attested", "headsha9"))
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))
    rebar.claim(tid, assignee="me", repo_root=str(rebar_repo))
    rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    v = rebar.verify_signature(tid, repo_root=str(rebar_repo))
    assert v["verdict"] == "certified"
    assert v["verified_at_sha"] == "headsha9"
    assert signing.verified_at_sha_step("headsha9") in v["manifest"]


# --------------------------------------------------------------------------------------
# AC5 — an unresolvable ref in attested mode fails closed (no signature)
# --------------------------------------------------------------------------------------
def test_unresolvable_ref_attested_fails_closed(rebar_repo: Path):
    _commit(rebar_repo)
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))
    from rebar._snapshot import SnapshotRefError

    with pytest.raises(SnapshotRefError):
        rebar.llm.review_ticket(
            tid,
            "ticket-quality",
            ref="no-such-ref-xyz",
            source="attested",
            repo_root=str(rebar_repo),
        )


# --------------------------------------------------------------------------------------
# AC6 — the pin is shaped as an in-toto statement (DSSE envelope swap, not a rewrite)
# --------------------------------------------------------------------------------------
def test_in_toto_subject_shape():
    subj = signing.verified_at_sha_subject("abc123", "tkt-1", "completion-verifier")
    assert subj["subject"] == [{"name": "tkt-1", "digest": {"sha1": "abc123"}}]
    assert subj["predicateType"] == "completion-verifier"
    # The HMAC manifest step round-trips to the same SHA the in-toto subject carries.
    step = signing.verified_at_sha_step("abc123")
    assert signing.verified_at_sha_from_manifest([step]) == subj["subject"][0]["digest"]["sha1"]
