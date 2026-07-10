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
    (repo / "rebar.toml").write_text("[verify]\nrequire_completion_verification_for_close = true\n")


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
# AC4 — reopen invalidates the closure via validity-on-read (records immutable); local
# never signs
# --------------------------------------------------------------------------------------
def test_reopen_invalidates_completion_via_validity_on_read(rebar_repo: Path, monkeypatch):
    """Epic dark-acme-lumen: reopen no longer CLEARS the signature (retire_attested_pin is
    gone). The completion attestation RECORD stays HMAC-certified (immutable), but
    compute_validity reads it as not-valid because the ticket was reopened — so a reopened
    closure is never trusted, without destroying the kind-keyed attestations it carries."""
    from rebar.llm.plan_review.attest import compute_validity

    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", _verdict("attested", "pinnedsha"))
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))
    rebar.claim(tid, assignee="me", repo_root=str(rebar_repo))
    rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    sig = rebar.verify_signature(tid, kind="completion-verifier", repo_root=str(rebar_repo))
    assert sig["verdict"] == "certified" and sig["verified_at_sha"] == "pinnedsha"
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert compute_validity(sig, state, "completion-verifier", repo_root=str(rebar_repo))["valid"]

    rebar.reopen(tid, repo_root=str(rebar_repo))
    # The record is NOT mutated — still HMAC-certified — but it no longer validates.
    sig2 = rebar.verify_signature(tid, kind="completion-verifier", repo_root=str(rebar_repo))
    assert sig2["verdict"] == "certified"
    state2 = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    v = compute_validity(sig2, state2, "completion-verifier", repo_root=str(rebar_repo))
    assert v["valid"] is False


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


# --------------------------------------------------------------------------------------
# Story blackbear — END-TO-END pre-sign fingerprint-mismatch: HEAD moved between verify
# and sign, so the close SKIPS signing but the ticket STILL closes (exit 0) with a
# stderr warning. Exercises the real close_ticket flow, not just the _material_drifted
# unit (that unit lives in tests/unit/test_llm_disposition_plumbing.py).
# --------------------------------------------------------------------------------------
def test_presign_sha_drift_closes_without_signature_and_warns(
    rebar_repo: Path, monkeypatch, capsys
):
    """The attested verdict pins a REAL 40-hex SHA that differs from the repo's fresh HEAD
    (simulating HEAD moving between verify and sign). ``_material_drifted`` trips on the
    pre-sign recheck, so close_ticket closes the ticket (exit 0) but does NOT write a
    completion signature and emits a stderr drift warning. (Contrast the other attested
    tests, whose synthetic non-40-hex markers are NOT drift and DO sign — see below.)"""
    _commit(rebar_repo)
    _enable(rebar_repo)
    # A plausible full 40-char hex SHA that cannot equal the freshly-read HEAD sha.
    stale_sha = "b" * 40
    assert stale_sha != signing.head_sha(rebar_repo)  # guard: genuinely a mismatch
    monkeypatch.setattr(rebar.llm, "verify_completion", _verdict("attested", stale_sha))
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))
    rebar.claim(tid, assignee="me", repo_root=str(rebar_repo))
    # (a) the ticket still closes successfully (no raise / exit 0).
    rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["status"] == "closed"
    # (b) NO completion signature was written (the drift skipped signing).
    sig = rebar.verify_signature(tid, kind="completion-verifier", repo_root=str(rebar_repo))
    assert sig["verdict"] == "unsigned"
    # (c) a stderr warning about the drift was emitted.
    err = capsys.readouterr().err
    assert "WITHOUT a completion signature" in err
    assert "drifted between verify" in err


# --------------------------------------------------------------------------------------
# Story blackbear — verify runs OUTSIDE the write lock: close_ticket runs the completion
# precheck (_completion_precheck) BEFORE txn.transition_core (the locked write). Asserts
# the verify->lock ordering directly (stronger than sign-after-close, which only orders
# the SIGNATURE commit after the STATUS commit).
# --------------------------------------------------------------------------------------
def test_completion_verify_runs_before_and_outside_the_write_lock(rebar_repo: Path, monkeypatch):
    """Spy on ``_completion_precheck`` and ``txn.transition_core`` to record call order, and —
    inside the precheck spy — assert the tickets write lock is ACQUIRABLE (hence not yet held),
    proving verify runs before AND outside the locked write."""
    import rebar._commands.transition_close as tc
    from rebar import config as _cfg
    from rebar._store import lock as _lock

    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", _verdict("attested", "syntheticmarker"))

    calls: list[str] = []
    real_precheck = tc._completion_precheck
    real_core = tc.txn.transition_core
    tracker = str(_cfg.tracker_dir(str(rebar_repo)))

    def spy_precheck(*a, **kw):
        calls.append("verify")
        # Verify is NOT inside the write lock: the lock must be free right now. If verify
        # held (or ran under) the lock, this acquire would block for its full budget.
        handle = _lock.acquire(tracker, timeout=2, attempts=1)
        handle.release()
        return real_precheck(*a, **kw)

    def spy_core(*a, **kw):
        calls.append("lock")
        return real_core(*a, **kw)

    monkeypatch.setattr(tc, "_completion_precheck", spy_precheck)
    monkeypatch.setattr(tc.txn, "transition_core", spy_core)

    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))
    rebar.claim(tid, assignee="me", repo_root=str(rebar_repo))
    rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))

    # verify was invoked, ran before the locked write, and (synthetic marker => not drift)
    # the close still signed normally afterwards.
    assert calls == ["verify", "lock"], f"verify must precede the locked write, got {calls}"
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["status"] == "closed"
    sig = rebar.verify_signature(tid, kind="completion-verifier", repo_root=str(rebar_repo))
    assert sig["verdict"] == "certified"
