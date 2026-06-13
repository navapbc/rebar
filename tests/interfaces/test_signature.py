"""Cross-interface tests for ticket manifest signing (library / CLI / MCP).

Pins that all three interfaces sign a manifest of verified steps with the
environment-specific key, that ``verify-signature`` certifies a clean manifest
and rejects tampering / foreign-environment keys / unsigned tickets, that the
signature survives compaction, and that the MCP write tool is gated by
REBAR_MCP_READONLY while the read tool is not.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import rebar

MANIFEST = ["ran unit tests: PASS", "lint clean", "manual smoke OK"]


def _cli(*args: str, cwd: Path, **env: str) -> subprocess.CompletedProcess:
    e = dict(os.environ)
    e.update(env)
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args],
        capture_output=True, text=True, cwd=str(cwd), env=e,
    )


def _seed(repo: Path) -> str:
    return rebar.create_ticket(
        "task", "Sign me",
        description="Body.\n\n## Acceptance Criteria\n- [ ] a",
        repo_root=str(repo),
    )


# ── library ───────────────────────────────────────────────────────────────────
def test_library_sign_then_certify(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    rec = rebar.sign_manifest(tid, MANIFEST, repo_root=str(rebar_repo))
    assert rec["ticket_id"] == tid
    assert rec["algorithm"] == "HMAC-SHA256"
    assert rec["manifest"] == MANIFEST
    assert rec["signature"] and rec["key_id"]

    out = rebar.verify_signature(tid, repo_root=str(rebar_repo))
    assert out["verified"] is True
    assert out["verdict"] == "certified"
    assert out["manifest"] == MANIFEST


def test_signature_appears_in_show(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    rebar.sign_manifest(tid, MANIFEST, repo_root=str(rebar_repo))
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert state["signature"]["manifest"] == MANIFEST


def test_library_unsigned_ticket(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    out = rebar.verify_signature(tid, repo_root=str(rebar_repo))
    assert out["verified"] is False and out["verdict"] == "unsigned"


def test_library_sign_bad_manifest_raises(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    with pytest.raises(rebar.RebarError):
        rebar.sign_manifest(tid, "not json", repo_root=str(rebar_repo))


def test_verify_unresolvable_ticket_raises(rebar_repo: Path) -> None:
    with pytest.raises(rebar.RebarError):
        rebar.verify_signature("nope-nope-nope", repo_root=str(rebar_repo))


# ── tamper / foreign-key detection ────────────────────────────────────────────
def _forge_signature_event(repo: Path, tid: str, new_manifest: list[str]) -> None:
    """Append a fresh SIGNATURE event whose manifest was altered but whose
    signature is copied from the genuine one — i.e. a tampered record."""
    import glob
    import uuid as _uuid

    tdir = repo / ".tickets-tracker" / tid
    latest = sorted(glob.glob(str(tdir / "*-SIGNATURE.json")))[-1]
    ev = json.loads(Path(latest).read_text())
    ev["uuid"] = str(_uuid.uuid4())
    ev["timestamp"] = int(ev["timestamp"]) + 1000
    ev["data"] = {**ev["data"], "manifest": new_manifest}
    (tdir / f'{ev["timestamp"]}-{ev["uuid"]}-SIGNATURE.json').write_text(
        json.dumps(ev, ensure_ascii=False)
    )
    for cache in glob.glob(str(tdir / ".cache.json")):
        os.remove(cache)


def test_tampered_manifest_is_rejected(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    rebar.sign_manifest(tid, MANIFEST, repo_root=str(rebar_repo))
    _forge_signature_event(rebar_repo, tid, MANIFEST + ["SECRETLY ADDED STEP"])
    out = rebar.verify_signature(tid, repo_root=str(rebar_repo))
    assert out["verified"] is False
    assert out["verdict"] == "mismatch"


def test_foreign_environment_key_cannot_certify(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tid = _seed(rebar_repo)
    rebar.sign_manifest(tid, MANIFEST, repo_root=str(rebar_repo))
    # A different environment (different signing key) must not be able to certify.
    monkeypatch.setenv("REBAR_SIGNING_KEY", "a-totally-different-environment-key")
    out = rebar.verify_signature(tid, repo_root=str(rebar_repo))
    assert out["verified"] is False
    assert out["verdict"] == "foreign_key"


def test_injected_env_key_round_trips(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The shared-deployment path: the key is injected via REBAR_SIGNING_KEY rather
    # than read from disk. Signing and certifying under the same injected key works.
    monkeypatch.setenv("REBAR_SIGNING_KEY", "shared-deployment-key")
    tid = _seed(rebar_repo)
    rebar.sign_manifest(tid, MANIFEST, repo_root=str(rebar_repo))
    assert rebar.verify_signature(tid, repo_root=str(rebar_repo))["verdict"] == "certified"


# ── compaction survival ───────────────────────────────────────────────────────
def test_signature_survives_compaction(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    rebar.sign_manifest(tid, MANIFEST, repo_root=str(rebar_repo))
    cp = _cli("compact", tid, "--threshold=0", cwd=rebar_repo, TICKET_SYNC_CMD="true")
    assert cp.returncode == 0, cp.stderr
    snaps = list((rebar_repo / ".tickets-tracker" / tid).glob("*-SNAPSHOT.json"))
    assert snaps, "expected a SNAPSHOT after compaction"
    out = rebar.verify_signature(tid, repo_root=str(rebar_repo))
    assert out["verdict"] == "certified"
    assert out["manifest"] == MANIFEST


# ── sign / compact interplay ──────────────────────────────────────────────────
def test_sign_compact_sign_compact_latest_wins(rebar_repo: Path) -> None:
    # The latest signature must win and verify across TWO snapshot round-trips —
    # exercises the SNAPSHOT fold + post-snapshot replay for the signature field.
    tid = _seed(rebar_repo)
    rebar.sign_manifest(tid, ["v1: first"], repo_root=str(rebar_repo))
    assert _cli("compact", tid, "--threshold=0", cwd=rebar_repo, TICKET_SYNC_CMD="true").returncode == 0
    rebar.sign_manifest(tid, ["v2: second", "v2: more"], repo_root=str(rebar_repo))
    assert _cli("compact", tid, "--threshold=0", cwd=rebar_repo, TICKET_SYNC_CMD="true").returncode == 0

    out = rebar.verify_signature(tid, repo_root=str(rebar_repo))
    assert out["verdict"] == "certified"
    assert out["manifest"] == ["v2: second", "v2: more"]


def test_concurrent_signatures_converge_by_basename(rebar_repo: Path) -> None:
    # Two SIGNATURE events at the SAME timestamp, different uuids: the reducer
    # sorts event files by basename, so the lexicographically-greater uuid is
    # applied last (wins) — deterministically, independent of write order.
    from rebar import signing

    tid = _seed(rebar_repo)
    tracker = str(rebar_repo / ".tickets-tracker")
    tdir = rebar_repo / ".tickets-tracker" / tid
    key = signing.signing_key(tracker)
    ts = 1_781_000_000_000_000_000

    def _write(uid: str, manifest: list[str]) -> None:
        ev = {
            "timestamp": ts, "uuid": uid, "event_type": "SIGNATURE",
            "env_id": "e", "author": "a",
            "data": {
                "manifest": manifest, "algorithm": signing.ALGORITHM,
                "signature": signing.compute_signature(tid, manifest, key),
                "key_id": signing.key_fingerprint(key), "head_sha": "x", "signed_at": ts,
            },
        }
        (tdir / f"{ts}-{uid}-SIGNATURE.json").write_text(json.dumps(ev))

    # Write the higher-uuid one FIRST so disk discovery order != winner order.
    _write("ffffffff-0000-4000-8000-000000000002", ["WINNER"])
    _write("00000000-0000-4000-8000-000000000001", ["loser"])
    for c in tdir.glob(".cache.json"):
        c.unlink()

    out = rebar.verify_signature(tid, repo_root=str(rebar_repo))
    assert out["manifest"] == ["WINNER"]
    assert out["verdict"] == "certified"


# ── cross-environment via a REAL key-file swap (not just env override) ─────────
def test_foreign_key_round_trip_via_file_swap(rebar_repo: Path) -> None:
    import uuid as _uuid

    tid = _seed(rebar_repo)
    keyfile = rebar_repo / ".tickets-tracker" / ".signing-key"
    env_a = keyfile.read_text()

    rebar.sign_manifest(tid, MANIFEST, repo_root=str(rebar_repo))            # signed by A
    assert rebar.verify_signature(tid, repo_root=str(rebar_repo))["verdict"] == "certified"

    # Become environment B (different key on disk).
    keyfile.write_text(str(_uuid.uuid4()) + "\n")
    assert rebar.verify_signature(tid, repo_root=str(rebar_repo))["verdict"] == "foreign_key"

    # B re-signs → certified in B; A's restored key then sees B's signature foreign.
    rebar.sign_manifest(tid, MANIFEST, repo_root=str(rebar_repo))
    assert rebar.verify_signature(tid, repo_root=str(rebar_repo))["verdict"] == "certified"
    keyfile.write_text(env_a)
    assert rebar.verify_signature(tid, repo_root=str(rebar_repo))["verdict"] == "foreign_key"


def test_readonly_verify_does_not_mint_key(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A verify on a key-less environment must not write a .signing-key (a read
    # tool persisting a secret a read-only deployment never asked for).
    tid = _seed(rebar_repo)
    keyfile = rebar_repo / ".tickets-tracker" / ".signing-key"
    keyfile.unlink()
    monkeypatch.delenv("REBAR_SIGNING_KEY", raising=False)
    out = rebar.verify_signature(tid, repo_root=str(rebar_repo))
    assert out["verdict"] == "unsigned"
    assert not keyfile.exists(), "verify minted a signing key as a side effect"


# ── malformed reduced state must not crash verify (fail closed) ────────────────
def test_verify_survives_non_dict_signature_state(rebar_repo: Path) -> None:
    import time as _time
    import uuid as _uuid

    tid = _seed(rebar_repo)
    rebar.sign_manifest(tid, MANIFEST, repo_root=str(rebar_repo))
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    # Forge a latest SNAPSHOT whose compiled_state.signature is a NON-dict string
    # (a corrupt / forward-compat snapshot). It must verify cleanly, not crash.
    state["signature"] = "totally-not-a-dict"
    ts = _time.time_ns()
    uid = str(_uuid.uuid4())
    tdir = rebar_repo / ".tickets-tracker" / tid
    snap = {
        "timestamp": ts, "uuid": uid, "event_type": "SNAPSHOT",
        "env_id": "e", "author": "a",
        "data": {"compiled_state": state, "source_event_uuids": []},
    }
    (tdir / f"{ts}-{uid}-SNAPSHOT.json").write_text(json.dumps(snap))
    for c in tdir.glob(".cache.json"):
        c.unlink()

    out = rebar.verify_signature(tid, repo_root=str(rebar_repo))
    assert out["verified"] is False
    assert out["verdict"] in ("unsigned", "mismatch")
    # And the CLI surfaces a clean exit, not a traceback.
    cp = _cli("verify-signature", tid, cwd=rebar_repo)
    assert cp.returncode == 1
    assert "Traceback" not in cp.stderr


def test_verify_ghost_and_archived_tickets(rebar_repo: Path) -> None:
    # Archived (and never-signed) tickets verify as unsigned, never crash.
    tid = _seed(rebar_repo)
    rebar.archive(tid, repo_root=str(rebar_repo))
    out = rebar.verify_signature(tid, repo_root=str(rebar_repo))
    assert out["verdict"] == "unsigned"


# ── client-facing display: hex stripped, facts kept, llm compacted ────────────
def test_show_strips_raw_hex_but_keeps_facts(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    rebar.sign_manifest(tid, MANIFEST, repo_root=str(rebar_repo))
    sig = rebar.show_ticket(tid, repo_root=str(rebar_repo))["signature"]
    # The raw HMAC hex (the "signature itself") is not in client output ...
    assert "signature" not in sig
    # ... but the facts a client needs ARE: the verified-steps manifest + key fp.
    assert sig["manifest"] == MANIFEST
    assert sig["key_id"]


def test_llm_view_compacts_signature(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    rebar.sign_manifest(tid, MANIFEST, repo_root=str(rebar_repo))
    llm = rebar.to_llm(rebar.show_ticket(tid, repo_root=str(rebar_repo)))
    assert "sig" in llm
    assert llm["sig"]["present"] is True
    assert llm["sig"]["steps"] == len(MANIFEST)
    assert "signature" not in llm["sig"]  # never the raw hex


# ── close gate: story/epic require a certified signature (opt-in) ──────────────
def _enable_gate(repo: Path) -> None:
    (repo / ".rebar").mkdir(exist_ok=True)
    (repo / ".rebar" / "config.conf").write_text("verify.require_signature_for_close=true\n")


def _story(repo: Path) -> str:
    tid = rebar.create_ticket(
        "story", "Gate story",
        description="B.\n\n## Acceptance Criteria\n- [ ] a", repo_root=str(repo),
    )
    rebar.transition(tid, "open", "in_progress", repo_root=str(repo))
    return tid


def test_close_gate_off_by_default(rebar_repo: Path) -> None:
    tid = _story(rebar_repo)
    out = rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert out["to"] == "closed"  # no gate, no signature needed


def test_close_gate_blocks_without_signature_then_allows_after_sign(rebar_repo: Path) -> None:
    _enable_gate(rebar_repo)
    tid = _story(rebar_repo)
    with pytest.raises(rebar.RebarError) as ei:
        rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert "certified signature" in ei.value.stderr
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["status"] == "in_progress"

    rebar.sign_manifest(tid, MANIFEST, repo_root=str(rebar_repo))
    out = rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert out["to"] == "closed"


def test_close_gate_stale_head_blocks(rebar_repo: Path) -> None:
    _enable_gate(rebar_repo)
    tid = _story(rebar_repo)
    rebar.sign_manifest(tid, MANIFEST, repo_root=str(rebar_repo))
    # Advance the CODE repo HEAD after signing → the attestation is now stale.
    subprocess.run(["git", "commit", "--allow-empty", "-q", "-m", "advance"],
                   cwd=str(rebar_repo), check=True, capture_output=True)
    with pytest.raises(rebar.RebarError) as ei:
        rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert "different commit" in ei.value.stderr
    # Re-signing at the new HEAD unblocks the close.
    rebar.sign_manifest(tid, MANIFEST, repo_root=str(rebar_repo))
    assert rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))["to"] == "closed"


def test_close_gate_force_close_bypass(rebar_repo: Path) -> None:
    _enable_gate(rebar_repo)
    tid = _story(rebar_repo)
    cp = _cli("transition", tid, "in_progress", "closed", "--force-close=verifier offline", cwd=rebar_repo)
    assert cp.returncode == 0, cp.stderr
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["status"] == "closed"


def test_close_gate_does_not_apply_to_tasks(rebar_repo: Path) -> None:
    _enable_gate(rebar_repo)
    tid = rebar.create_ticket("task", "T", repo_root=str(rebar_repo))
    rebar.transition(tid, "open", "in_progress", repo_root=str(rebar_repo))
    out = rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert out["to"] == "closed"  # gate is story/epic only


# ── validate: store-wide signature integrity ──────────────────────────────────
def test_validate_flags_tampered_signature(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    rebar.sign_manifest(tid, MANIFEST, repo_root=str(rebar_repo))
    # A clean signature is not flagged.
    clean = rebar.validate(repo_root=str(rebar_repo))
    assert not any("[SIGNATURE]" in m for m in clean["major_issues"])
    # Tamper, then validate flags it MAJOR and names the ticket.
    _forge_signature_event(rebar_repo, tid, MANIFEST + ["SECRETLY ADDED"])
    rep = rebar.validate(repo_root=str(rebar_repo))
    sig_majors = [m for m in rep["major_issues"] if "[SIGNATURE]" in m]
    assert sig_majors, f"tampered signature not flagged: {rep['major_issues']}"
    assert any(tid in m for m in sig_majors)


# ── CLI ───────────────────────────────────────────────────────────────────────
def test_cli_sign_and_verify(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    s = _cli("sign", tid, json.dumps(MANIFEST), cwd=rebar_repo)
    assert s.returncode == 0, s.stderr
    assert s.stdout.startswith("SIGNED ")

    v = _cli("verify-signature", tid, cwd=rebar_repo)
    assert v.returncode == 0
    assert "certified" in v.stdout

    vj = _cli("verify-signature", tid, "--output", "json", cwd=rebar_repo)
    assert vj.returncode == 0
    assert json.loads(vj.stdout)["verified"] is True


def test_cli_verify_unsigned_exits_1(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    v = _cli("verify-signature", tid, cwd=rebar_repo)
    assert v.returncode == 1
    assert "unsigned" in v.stdout


def test_cli_sign_usage_on_missing_args(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    s = _cli("sign", tid, cwd=rebar_repo)
    assert s.returncode == 1
    assert s.stderr.startswith("Usage: rebar sign")


# ── MCP ───────────────────────────────────────────────────────────────────────
def _mcp_call(tool: str, args: dict):
    pytest.importorskip("mcp")
    from adapters import _unwrap  # tests/interfaces on sys.path

    from rebar.mcp_server import build_server

    srv = build_server()
    return _unwrap(asyncio.run(srv.call_tool(tool, args)))


def _mcp_tools() -> set[str]:
    pytest.importorskip("mcp")
    from rebar.mcp_server import build_server

    return {t.name for t in asyncio.run(build_server().list_tools())}


def test_mcp_sign_and_verify(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    rec = _mcp_call("sign_manifest", {"ticket_id": tid, "manifest": MANIFEST})
    assert rec["manifest"] == MANIFEST
    out = _mcp_call("verify_signature", {"ticket_id": tid})
    assert out["verified"] is True and out["verdict"] == "certified"


def test_mcp_readonly_gates_sign_but_not_verify(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _seed(rebar_repo)
    rebar.sign_manifest(tid, MANIFEST, repo_root=str(rebar_repo))
    monkeypatch.setenv("REBAR_MCP_READONLY", "1")
    tools = _mcp_tools()
    assert "sign_manifest" not in tools, "write tool must be hidden on a read-only server"
    assert "verify_signature" in tools, "verify is a read and must stay available"
    # verify still works read-only
    out = _mcp_call("verify_signature", {"ticket_id": tid})
    assert out["verdict"] == "certified"
