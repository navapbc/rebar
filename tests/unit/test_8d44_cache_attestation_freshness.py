"""Ensure reducer cache attestations agree with signed events on disk.

A ``dir_hash`` hit can reuse a ``.cache.json`` written with a different attestation
projection. The reducer compares cached attestation keys with disk evidence and treats
disagreement as a cache miss under the read-freshness policy. Tests assert the
projected attestation, while ``verify_signature`` supplies an independently derived
reference.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar import signing
from rebar.reducer import reduce_ticket


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@e.com"),
        ("git", "config", "user.name", "t"),
        ("git", "commit", "-q", "--allow-empty", "-m", "init"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    # Scratch/tmp store: no remote, both sync directions off, forced HMAC key.
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("REBAR_SYNC_PULL", "off")
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    monkeypatch.setenv("REBAR_SIGNING_KEY", "test-signing-key-8d44")
    rebar.init_repo(repo_root=str(repo))
    return repo


def _ticket_dir(store: Path, tid: str) -> Path:
    return store / ".tickets-tracker" / tid


def _read_cache_file(ticket_dir: Path) -> dict:
    return json.loads((ticket_dir / ".cache.json").read_text(encoding="utf-8"))


def test_reduce_reflects_ondisk_attestation_after_stale_cache(store: Path) -> None:
    tid = rebar.create_ticket("task", "signed ticket", repo_root=str(store))
    signing.sign_manifest(
        tid,
        ["completion-verifier: PASS", f"ticket: {tid}"],
        kind="completion-verifier",
        repo_root=str(store),
    )
    ticket_dir = _ticket_dir(store, tid)

    # Populate the reducer cache with the signed attestation and matching dir_hash.
    warm = reduce_ticket(str(ticket_dir))
    assert warm is not None
    assert "completion-verifier" in (warm.get("attestations") or {})
    assert (ticket_dir / ".cache.json").exists()

    # verify-signature (the re-deriving reader) certifies the SIGNATURE event on disk.
    assert (
        signing.verify_signature(tid, kind="completion-verifier", repo_root=str(store))["verdict"]
        == "certified"
    )

    # Remove only the cached projection while preserving its hash and the disk event.
    cache = _read_cache_file(ticket_dir)
    cache["state"].pop("attestations", None)
    cache["state"].pop("signature", None)
    (ticket_dir / ".cache.json").write_text(json.dumps(cache), encoding="utf-8")
    # The SIGNATURE evidence is untouched on disk.
    assert any(p.name.endswith("-SIGNATURE.json") for p in ticket_dir.iterdir())

    # Detect the mismatch, rebuild the projection, and agree with verification.
    state = reduce_ticket(str(ticket_dir))
    assert state is not None
    assert "completion-verifier" in (state.get("attestations") or {}), (
        "reduce_ticket served a stale cache whose attestations contradict the on-disk "
        "SIGNATURE event; show and verify-signature now disagree"
    )
    # show (reduce) and verify-signature agree once more.
    assert state["attestations"]["completion-verifier"]["manifest"][0].startswith(
        "completion-verifier"
    )


def test_unsigned_ticket_cache_hit_is_preserved(store: Path) -> None:
    """The validity check must NOT weaken caching for a ticket with no attestation
    evidence: an untampered warm cache is still served on the next reduce."""
    tid = rebar.create_ticket("task", "plain ticket", repo_root=str(store))
    ticket_dir = _ticket_dir(store, tid)
    first = reduce_ticket(str(ticket_dir))
    assert first is not None
    assert not (first.get("attestations") or {})

    # Mark the on-disk cache so we can prove the SECOND reduce was served FROM it (a
    # re-derive would overwrite the marker; a hit returns it verbatim).
    cache = _read_cache_file(ticket_dir)
    cache["state"]["_served_from_cache_marker"] = True
    (ticket_dir / ".cache.json").write_text(json.dumps(cache), encoding="utf-8")

    second = reduce_ticket(str(ticket_dir))
    assert second is not None
    assert second.get("_served_from_cache_marker") is True, (
        "an untampered, attestation-free cache entry must still be a HIT"
    )
