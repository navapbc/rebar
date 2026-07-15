"""Contract phase: remove the legacy HMAC scheme for the op-cert kinds (story 8f1d).

Pins the acceptance criteria:

  * AC1 (dependency gate): the asymmetric op-cert producer path is present — an import check
    plus an end-to-end op-cert sign/verify smoke test.
  * no code path SIGNS an HMAC op-cert: ``plan-review`` / ``completion-verifier`` no longer
    resolve the HMAC-SHA256 scheme in ``registry.POLICY``, and ``sign_manifest`` emits an
    envelope (no HMAC ``signature``) for those kinds — while ``compute_signature`` /
    ``.signing-key`` remain for non-op-cert consumers.
  * no code path ACCEPTS an HMAC op-cert: a pre-existing HMAC-signed op-cert record reads
    NOT-certified post-contract, and re-running the gate re-issues an asymmetric op-cert that
    then certifies.
  * the migration artifact exists, is non-empty, and carries the documented sections.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar import signing


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


# ── AC1: dependency gate — asymmetric producer path present ───────────────────────────
def test_dependency_gate_asymmetric_producer_present() -> None:
    # Import check: the asymmetric op-cert producer + verifier surface exists.
    from rebar.attest import opcert
    from rebar.attest.opcert import OPCERT_KIND

    assert OPCERT_KIND == "rebar.opcert.v1"
    assert callable(opcert.sign_opcert)
    assert callable(opcert.verify_opcert)


def test_dependency_gate_sign_verify_smoke(store: Path) -> None:
    # End-to-end: a plan-review manifest signs as an op-cert and verifies as certified.
    tid = rebar.create_ticket("task", "smoke", repo_root=str(store))
    rec = signing.sign_manifest(
        tid, ["plan-review: PASS"], kind="plan-review", repo_root=str(store)
    )
    assert rec["algorithm"] == "sshsig" and rec.get("envelope")
    verdict = signing.verify_signature(tid, kind="plan-review", repo_root=str(store))
    assert verdict["verified"] is True and verdict["verdict"] == "certified"


# ── no code path SIGNS an HMAC op-cert ────────────────────────────────────────────────
def test_opcert_kinds_do_not_resolve_hmac_scheme() -> None:
    from rebar.attest import registry

    # The HMAC-SHA256 scheme is retired: it is no longer registered, and neither op-cert kind
    # pins a policy (they resolve None → fail closed), so only the asymmetric op-cert scheme is
    # reachable for them.
    assert registry.get_scheme("HMAC-SHA256") is None
    for kind in ("plan-review", "completion-verifier"):
        assert registry.resolve(kind) is None
    # The op-cert kind itself still resolves the asymmetric sshsig scheme.
    assert registry.resolve("rebar.opcert.v1").scheme == "sshsig"


def test_sign_manifest_emits_no_hmac_for_opcert_kinds(store: Path) -> None:
    for kind in ("plan-review", "completion-verifier"):
        tid = rebar.create_ticket("task", f"sign {kind}", repo_root=str(store))
        rec = signing.sign_manifest(tid, [f"{kind}: PASS"], kind=kind, repo_root=str(store))
        assert rec.get("envelope") and rec["algorithm"] == "sshsig"
        # No reachable HMAC branch: the op-cert record carries neither an HMAC signature nor a
        # key fingerprint.
        assert not rec.get("signature")
        assert not rec.get("key_id")


def test_generic_hmac_utility_still_works_for_non_opcert(store: Path) -> None:
    # The kept generic HMAC utility (compute_signature + .signing-key genesis) still certifies a
    # NON-op-cert record through the unchanged verify_record path.
    key = signing.signing_key(str(_tracker(store)))
    assert key  # a per-environment .signing-key was minted
    manifest = ["generic-note: ok", "ran tests"]
    rec = {
        "manifest": manifest,
        "algorithm": signing.ALGORITHM,
        "signature": signing.compute_signature("t", manifest, key),
        "key_id": signing.key_fingerprint(key),
    }
    verdict = signing.verify_record(rec, "t", key)
    assert verdict["verified"] is True and verdict["verdict"] == "certified"


# ── no code path ACCEPTS an HMAC op-cert ──────────────────────────────────────────────
def _write_legacy_hmac_opcert(store: Path, tid: str, kind: str) -> str:
    """Append a genuine pre-contract HMAC SIGNATURE record for an op-cert ``kind``."""
    from rebar._commands._seam import append_event

    resolved = rebar.show_ticket(tid, repo_root=str(store))["ticket_id"]
    tracker = _tracker(store)
    manifest = [f"{kind}: PASS"]
    key = signing.signing_key(str(tracker))
    append_event(
        resolved,
        "SIGNATURE",
        {
            "manifest": manifest,
            "algorithm": signing.ALGORITHM,
            "signature": signing.compute_signature(resolved, manifest, key),
            "key_id": signing.key_fingerprint(key),
            "kind": kind,
        },
        tracker,
        repo_root=str(store),
    )
    return resolved


@pytest.mark.parametrize("kind", ["plan-review", "completion-verifier"])
def test_preexisting_hmac_opcert_reads_not_certified(store: Path, kind: str) -> None:
    tid = rebar.create_ticket("task", "legacy hmac", repo_root=str(store))
    _write_legacy_hmac_opcert(store, tid, kind)

    # Validity-on-read: the HMAC op-cert no longer certifies (both the explicit-kind gate read
    # and the legacy most-recent path derive the op-cert kind and reject it).
    v_kind = signing.verify_signature(tid, kind=kind, repo_root=str(store))
    assert v_kind["verified"] is False and v_kind["verdict"] == "unknown_scheme"
    v_recent = signing.verify_signature(tid, repo_root=str(store))
    assert v_recent["verified"] is False


@pytest.mark.parametrize("kind", ["plan-review", "completion-verifier"])
def test_hmac_record_is_byte_unchanged_on_disk(store: Path, kind: str) -> None:
    # The append-only record is NOT mutated — it is byte-unchanged, still an HMAC record; only
    # the verdict is recomputed on read.
    tid = rebar.create_ticket("task", "unchanged", repo_root=str(store))
    _write_legacy_hmac_opcert(store, tid, kind)
    rec = rebar.show_ticket(tid, repo_root=str(store))["attestations"][kind]
    assert rec.get("algorithm") == "HMAC-SHA256"
    assert "envelope" not in rec


@pytest.mark.parametrize("kind", ["plan-review", "completion-verifier"])
def test_rerunning_gate_reissues_asymmetric_opcert(store: Path, kind: str) -> None:
    tid = rebar.create_ticket("task", "reissue", repo_root=str(store))
    _write_legacy_hmac_opcert(store, tid, kind)
    assert signing.verify_signature(tid, kind=kind, repo_root=str(store))["verified"] is False

    # Re-running the gate (re-signing) mints an asymmetric op-cert that then certifies.
    rec = signing.sign_manifest(tid, [f"{kind}: PASS"], kind=kind, repo_root=str(store))
    assert rec["algorithm"] == "sshsig" and rec.get("envelope")
    reissued = signing.verify_signature(tid, kind=kind, repo_root=str(store))
    assert reissued["verified"] is True and reissued["verdict"] == "certified"


# ── migration artifact ────────────────────────────────────────────────────────────────
def test_migration_doc_present_and_documented() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    doc = repo_root / "docs" / "migrations" / "hmac-opcert-removal.md"
    assert doc.exists(), f"migration artifact missing: {doc}"
    text = doc.read_text(encoding="utf-8")
    assert text.strip(), "migration artifact is empty"
    assert "## Expand" in text
    assert "## Contract" in text
    # Upgrade order + re-issue procedure are documented.
    lower = text.lower()
    assert "upgrade" in lower and "reconcile" in lower
    assert "re-issue" in lower or "re-run the gate" in lower
    # rebar.authorship.v1 is called out as non-op-cert and unaffected.
    assert "rebar.authorship.v1" in text
    assert "unaffected" in lower
