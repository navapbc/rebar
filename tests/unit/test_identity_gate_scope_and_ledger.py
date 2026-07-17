"""Regression tests for two authenticated-authorship gate false-rejections
(branch ``fix/identity-gate-false-rejections``).

Bug A — reducer-IGNORED observability sidecars (``COMPLETION_VERDICT`` et al.) are
        NOT in ``rebar.reducer.KNOWN_EVENT_TYPES``, are non-state-bearing, and may be
        emitted unsigned. They must be OUT of the authorship gate's scope so they do
        not false-fail enforcement — while KNOWN state-bearing events with no
        ``author_sig`` are STILL classified ``unsigned`` (the exemption is scoped to
        non-KNOWN types only, not a blanket pass).

Bug B — a compacted SNAPSHOT ``authorship_ledger`` entry can carry a null
        ``commit_sha`` (compaction's ``resolve_event_commit`` returned ``None`` at
        fold time). The gate must RE-RESOLVE that null from the recorded ``position``
        (which resolves to the real, era-valid introducing commit) so a validly-signed
        event is classified ``verified`` — not ``key_not_valid_at_era``. Compaction
        itself must also fall back to the global position resolver so it persists the
        real commit rather than a null.

All assertions are on OBSERVABLE outcomes (JSON-report verdicts, exit codes, persisted
ledger values), never internal classifier names.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands._seam import tracker_dir
from rebar.attest import sshsig

try:
    sshsig.ensure_available()
    _SSH_OK = True
except Exception:  # noqa: BLE001 — best-effort SSHSIG availability probe; skip if unavailable
    _SSH_OK = False

_ssh = pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required for SSHSIG")


# ── shared harness ────────────────────────────────────────────────────────────
def _init(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str = "repo") -> Path:
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


def _gate(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run the merge-gate with enforcement ON via the config/env path (no ``--since``,
    so EVERY in-scope event is enforced)."""
    env = {
        **os.environ,
        "REBAR_ROOT": str(repo),
        "REBAR_IDENTITY_REQUIRE_AUTHENTICATED": "1",
    }
    return subprocess.run(
        ["rebar", "verify-identity", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )


def _write_and_commit_event(repo: Path, ticket_id: str, event: dict, suffix: str) -> None:
    """Hand-write one raw event file into ``ticket_id``'s dir and commit it to the
    tracker branch (the gate scans working-tree ticket dirs; committing also gives the
    file a resolvable introducing commit)."""
    tracker = str(tracker_dir(str(repo)))
    tdir = Path(tracker) / ticket_id
    fname = f"{event['timestamp']}-{event['uuid']}-{suffix}.json"
    (tdir / fname).write_text(json.dumps(event), encoding="utf-8")
    subprocess.run(["git", "-C", tracker, "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", tracker, "commit", "-q", "--no-verify", "-m", f"sidecar {suffix}"],
        check=True,
        capture_output=True,
    )


# ── Bug A: reducer-ignored sidecars are out of the gate's scope ───────────────
def test_completion_verdict_sidecar_out_of_scope_but_known_unsigned_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``COMPLETION_VERDICT`` (non-KNOWN, reducer-ignored) sidecar on a normal work
    ticket must NOT appear in the gate's report as an unsigned event; the ticket's own
    unsigned KNOWN ``CREATE`` MUST still be reported ``unsigned`` (exemption is scoped
    to non-KNOWN types only)."""
    repo = _init(tmp_path, monkeypatch, "scope")
    tid = rebar.create_ticket("task", "work", repo_root=str(repo))

    cv_uuid = "cv-sidecar-0001"
    verdict_event = {
        "timestamp": 9_000_000_000_000_000_001,
        "uuid": cv_uuid,
        "event_type": "COMPLETION_VERDICT",
        "env_id": "x",
        "data": {"verdict": "PASS", "ticket_id": tid},
    }
    _write_and_commit_event(repo, tid, verdict_event, "COMPLETION_VERDICT")

    res = _gate(repo, "--all", "--format", "json")
    report = json.loads(res.stdout)

    # The reducer-ignored sidecar must be OUT of scope: no report entry cites it.
    cv_entries = [e for e in report if e.get("event_uuid") == cv_uuid]
    assert cv_entries == [], f"COMPLETION_VERDICT sidecar must be out of scope, got {cv_entries}"

    # The KNOWN, state-bearing CREATE (no author_sig) must STILL be flagged unsigned.
    known_unsigned = [
        e
        for e in report
        if e.get("ticket_id") == tid
        and e.get("verdict") == "unsigned"
        and e.get("event_uuid") != cv_uuid
    ]
    assert known_unsigned, f"unsigned KNOWN CREATE must still be flagged, report={report}"


@_ssh
def test_completion_verdict_sidecar_does_not_fail_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With every state-bearing event signed (verified), an unsigned reducer-ignored
    ``COMPLETION_VERDICT`` sidecar must NOT fail the gate — it is out of scope, so the
    run exits 0."""
    repo = _init(tmp_path, monkeypatch, "scope_pass")
    priv, pub = _keypair(tmp_path, "author")
    ident = rebar.create_identity("Ada", "ada@example.com", keys=[pub], repo_root=str(repo))
    rebar.use_identity(ident, repo_root=str(repo))
    monkeypatch.setenv("REBAR_IDENTITY_SIGNING_KEY", priv)

    tid = rebar.create_ticket("task", "signed work", repo_root=str(repo))  # signed CREATE

    verdict_event = {
        "timestamp": 9_000_000_000_000_000_002,
        "uuid": "cv-sidecar-0002",
        "event_type": "COMPLETION_VERDICT",
        "env_id": "x",
        "data": {"verdict": "PASS", "ticket_id": tid},
    }
    _write_and_commit_event(repo, tid, verdict_event, "COMPLETION_VERDICT")

    res = _gate(repo, "--all")
    combined = res.stdout + res.stderr
    assert res.returncode == 0, combined  # sidecar out of scope → nothing unverified


# ── Bug B: gate re-resolves a ledger's null commit_sha ────────────────────────
@_ssh
def test_ledger_null_commit_sha_reresolves_to_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A SNAPSHOT ``authorship_ledger`` entry whose recorded ``commit_sha`` is null but
    whose ``position`` resolves to a real, era-valid commit must be classified
    ``verified`` — NOT ``key_not_valid_at_era``."""
    repo = _init(tmp_path, monkeypatch, "ledger_null")
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

    # Corrupt the persisted ledger the way compaction's null-commit_sha bug does: every
    # entry keeps its real ``position`` string but loses its resolved commit_sha.
    tracker = str(tracker_dir(str(repo)))
    tdir = Path(tracker) / tid
    snapf = next(tdir.glob("*-SNAPSHOT.json"))
    snap = json.loads(snapf.read_text(encoding="utf-8"))
    ledger = snap["data"]["compiled_state"]["authorship_ledger"]
    assert ledger, "precondition: compaction recorded a signed ledger"
    for entry in ledger:
        entry["position"]["commit_sha"] = None
    snapf.write_text(json.dumps(snap), encoding="utf-8")
    subprocess.run(["git", "-C", tracker, "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", tracker, "commit", "-q", "--no-verify", "-m", "null-out ledger commit_sha"],
        check=True,
        capture_output=True,
    )

    res = _gate(repo, "--all", "--format", "json")
    combined = (res.stdout + res.stderr).lower()
    report = json.loads(res.stdout)

    assert "key_not_valid_at_era" not in combined, combined
    assert not report, f"a re-resolved, era-valid signed event must be verified, got {report}"
    assert res.returncode == 0, combined


# ── Bug B: compaction falls back to the global position resolver ──────────────
def test_compaction_falls_back_to_position_resolver_for_null_event_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``resolve_event_commit`` returns ``None`` at fold time,
    ``_build_authorship_ledger`` must fall back to ``resolve_position_commit``
    (scoped to the ticket's parent tracker dir) and persist that non-null commit."""
    from rebar._commands.compact import _build_authorship_ledger
    from rebar.attest import authorship

    tracker = tmp_path / "tracker"
    ticket_dir = tracker / "ticket-aaaa"
    ticket_dir.mkdir(parents=True)
    event = {
        "timestamp": 1234,
        "uuid": "evt-1",
        "event_type": "COMMENT",
        "env_id": "x",
        "author_sig": "bogus-envelope",  # truthy so it's recorded; decode fails → null signer
        "data": {"body": "hi"},
    }
    epath = ticket_dir / "1234-evt-1-COMMENT.json"
    epath.write_text(json.dumps(event), encoding="utf-8")

    calls: list[tuple] = []

    monkeypatch.setattr(authorship, "resolve_event_commit", lambda *a, **k: None)

    def _fake_position(position, trk, *, repo_root=None):
        calls.append((position, trk, repo_root))
        return "cafef00dcafef00d"

    monkeypatch.setattr(authorship, "resolve_position_commit", _fake_position)

    ledger = _build_authorship_ledger([str(epath)], repo_root=str(tmp_path))
    assert len(ledger) == 1
    assert ledger[0]["position"]["commit_sha"] == "cafef00dcafef00d"
    # fallback resolver was called with the position string and the PARENT tracker dir.
    assert calls == [("1234-evt-1", str(ticket_dir.parent), str(tmp_path))]


def test_compaction_persists_null_only_when_both_resolvers_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If BOTH ``resolve_event_commit`` and ``resolve_position_commit`` return ``None``,
    the ledger persists a null ``commit_sha`` (unchanged fail-closed behavior)."""
    from rebar._commands.compact import _build_authorship_ledger
    from rebar.attest import authorship

    ticket_dir = tmp_path / "tracker" / "ticket-bbbb"
    ticket_dir.mkdir(parents=True)
    event = {
        "timestamp": 5678,
        "uuid": "evt-2",
        "event_type": "COMMENT",
        "env_id": "x",
        "author_sig": "bogus-envelope",
        "data": {"body": "hi"},
    }
    epath = ticket_dir / "5678-evt-2-COMMENT.json"
    epath.write_text(json.dumps(event), encoding="utf-8")

    monkeypatch.setattr(authorship, "resolve_event_commit", lambda *a, **k: None)
    monkeypatch.setattr(authorship, "resolve_position_commit", lambda *a, **k: None)

    ledger = _build_authorship_ledger([str(epath)], repo_root=str(tmp_path))
    assert len(ledger) == 1
    assert ledger[0]["position"]["commit_sha"] is None
