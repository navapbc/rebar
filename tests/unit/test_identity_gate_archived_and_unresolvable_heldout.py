"""Regression tests for two verify-identity gate defects (ticket f482-309a-7d61-4d78).

Defect 1 (archived scope) — an ARCHIVED ticket's events must be OUT of the authorship
        gate's scope: archived tickets are retired work, and their (possibly unsigned or
        unresolvable) events must not fail enforcement. A non-archived ticket's unsigned
        KNOWN event is STILL flagged (the exemption is scoped to archived, not a blanket
        pass).

Defect 2 (unresolvable null-commit ledger) — the whole-store CI failure. A SNAPSHOT
        ``authorship_ledger`` entry can carry a null ``commit_sha`` (a pre-fix snapshot),
        and at gate time its ``position`` may fail to resolve to an introducing commit
        (topology/environment-dependent — this is what happened in CI, producing 206
        ``key_not_valid_at_era``). A validly-SIGNED entry whose commit cannot be resolved
        must NOT be force-failed: the gate must not fail-close it to an ENFORCED
        ``key_not_valid_at_era``. It is authentic (its signature verifies against a real
        key the identity holds), so it must be treated as verified/grandfathered, and the
        run must exit 0.

All assertions are on OBSERVABLE outcomes (JSON-report verdicts, exit codes), never on
internal classifier names. This file is held out from the fix implementer.
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


# ── shared harness (mirrors test_identity_gate_scope_and_ledger.py) ─────────────
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
    """Run the merge-gate with enforcement ON (no ``--since`` → every in-scope event is
    enforced)."""
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


# ── Defect 1: archived tickets are out of the gate's scope ─────────────────────
def test_archived_ticket_out_of_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An ARCHIVED ticket carrying an unsigned KNOWN event must NOT appear in the gate's
    report; a live (non-archived) ticket's unsigned CREATE MUST still be flagged."""
    repo = _init(tmp_path, monkeypatch, "arch")

    # Live ticket with an unsigned CREATE (no signing identity configured) — must be flagged.
    live = rebar.create_ticket("task", "live work", repo_root=str(repo))
    # A second ticket that we then archive — its unsigned events must drop out of scope.
    doomed = rebar.create_ticket("task", "to be archived", repo_root=str(repo))
    rebar.comment(doomed, "some note", repo_root=str(repo))
    rebar.archive(doomed, repo_root=str(repo))

    res = _gate(repo, "--all", "--format", "json")
    report = json.loads(res.stdout)

    archived_entries = [e for e in report if e.get("ticket_id") == doomed]
    assert archived_entries == [], f"archived ticket must be out of scope, got {archived_entries}"

    live_flagged = [e for e in report if e.get("ticket_id") == live]
    assert live_flagged, f"live ticket's unsigned CREATE must still be flagged, report={report}"


# ── Defect 2: a validly-signed ledger entry with an UNRESOLVABLE commit ────────
@_ssh
def test_unresolvable_null_commit_ledger_does_not_fail_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A signed SNAPSHOT ledger entry whose ``commit_sha`` is null AND whose ``position``
    does NOT resolve to any introducing commit (the CI condition) must NOT be force-failed
    to an ENFORCED ``key_not_valid_at_era``. It is authentic, so the gate must exit 0.

    The signature binds the event's uuid + content_hash (not its position), so we corrupt
    ONLY the position (to an id that no committed file matches) — resolution then returns
    None while the signature still verifies against the identity's real key.
    """
    repo = _init(tmp_path, monkeypatch, "unresolvable")
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

    tracker = str(tracker_dir(str(repo)))
    tdir = Path(tracker) / tid
    snapf = next(tdir.glob("*-SNAPSHOT.json"))
    snap = json.loads(snapf.read_text(encoding="utf-8"))
    ledger = snap["data"]["compiled_state"]["authorship_ledger"]
    assert ledger, "precondition: compaction recorded a signed ledger"
    for entry in ledger:
        # null commit_sha (the pre-fix snapshot state) AND an unresolvable position
        # (no committed file matches) → resolve_position_commit returns None in the gate.
        euuid = entry["event_uuid"]
        entry["position"]["commit_sha"] = None
        entry["position"]["position"] = f"9999999999999999999-{euuid}-NEVER-COMMITTED"
    snapf.write_text(json.dumps(snap), encoding="utf-8")
    subprocess.run(["git", "-C", tracker, "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", tracker, "commit", "-q", "--no-verify", "-m", "unresolvable ledger"],
        check=True,
        capture_output=True,
    )

    res = _gate(repo, "--all", "--format", "json")
    combined = (res.stdout + res.stderr).lower()
    report = json.loads(res.stdout)

    assert "key_not_valid_at_era" not in combined, combined
    assert not report, f"an authentic but unresolvable signed event must not fail, got {report}"
    assert res.returncode == 0, combined
