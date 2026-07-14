"""Independent held-out rollback regression for the op-cert merge-gate (story 4214, Option B).

NOT shown to the implementer. This is the orchestrator's own oracle for the CVE-2026-44544-style
rollback: a holder of a REVOKED environment key freshly signs+stores a cert (storage anchor S falls
AFTER the key's revocation) but backdates the bound `merged_log_commit` to a pre-revocation commit.
The gate must judge key-era-validity at S (the introducing commit of the envelope-bearing SIGNATURE
event) — NOT at the self-chosen `merged_log_commit` — so the revoked key must FAIL as
`key_not_valid_at_era`. A control case (same cert, key NOT revoked) must PASS, isolating that the
failure is caused by revocation-before-storage, not by anything else.

Real ssh-keygen + a real rebar store + the real `rebar verify-opcert` subprocess (exit codes).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar import signing
from rebar.attest import sshsig

try:
    sshsig.ensure_available()
    _SSH_OK = True
except Exception:  # noqa: BLE001
    _SSH_OK = False

pytestmark = pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required for SSHSIG")

ENV_ID = "trusted-ci@rebar.test"
KIND = "completion-verifier"


def _keypair(tmp_path: Path, name: str) -> tuple[str, str]:
    key = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-q", "-C", name],
        check=True,
        capture_output=True,
    )
    parts = (tmp_path / f"{name}.pub").read_text().strip().split()
    return str(key), f"{parts[0]} {parts[1]}"


def _store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _tip_position(repo: Path) -> str:
    """Greatest active-event log position on the tickets branch ({ts}-{uuid})."""
    from rebar._commands._seam import tracker_dir
    from rebar.reducer._cache import is_active_event

    tracker = str(tracker_dir(str(repo)))
    best = ""
    for d in os.listdir(tracker):
        dp = os.path.join(tracker, d)
        if d.startswith(".") or not os.path.isdir(dp):
            continue
        for fn in os.listdir(dp):
            if not fn.endswith(".json") or fn.startswith(".") or not is_active_event(fn):
                continue
            pos = fn[:-5].rsplit("-", 1)[0]
            if pos > best:
                best = pos
    return best


def _write_env(repo: Path, pub: str, added_at: str, revoked_at: str | None) -> None:
    d = repo / ".rebar"
    d.mkdir(exist_ok=True)
    rev = "null" if revoked_at is None else revoked_at
    (d / "trusted_environments.yaml").write_text(
        "environments:\n"
        f"  - env_id: {ENV_ID}\n"
        "    keys:\n"
        f"      - public_key: {pub}\n"
        f"        added_at_log_position: {added_at}\n"
        f"        revoked_at_log_position: {rev}\n",
        encoding="utf-8",
    )


def _run(repo: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["rebar", "verify-opcert", "--root", str(repo), *extra], capture_output=True, text=True
    )


def _sign_and_close(repo: Path, tid: str, priv: str, merged_log_commit: str) -> None:
    from rebar.llm.plan_review.attest import current_material_fingerprint

    material = current_material_fingerprint(tid, repo_root=str(repo))
    # Signing appends the SIGNATURE event at the current (latest) tickets-branch position, so the
    # cert's storage anchor S is strictly newer than everything created before this call.
    signing.sign_opcert_manifest(
        tid,
        [f"{KIND}: PASS"],
        material_fingerprint=material,
        merged_log_commit=merged_log_commit,
        key_path=priv,
        principal=ENV_ID,
        repo_root=str(repo),
    )
    rebar.transition(tid, "open", "closed", repo_root=str(repo))


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str, str, str, str, str]:
    """Return (repo, priv, pub, tid, added_pos, revoke_pos); added_pos < revoke_pos < future S."""
    repo = _store(tmp_path, monkeypatch)
    priv, pub = _keypair(tmp_path, "env")
    tid = rebar.create_ticket("task", "gated work", repo_root=str(repo))
    added_pos = _tip_position(repo)  # key added at the gated ticket's genesis position
    rebar.create_ticket("task", "activity advancing the tickets log", repo_root=str(repo))
    revoke_pos = _tip_position(repo)  # revocation stamped strictly after `added_pos`
    return repo, priv, pub, tid, added_pos, revoke_pos


def test_revoked_key_backdated_cert_fails_at_storage_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, priv, pub, tid, added_pos, revoke_pos = _setup(tmp_path, monkeypatch)
    # Key REVOKED at revoke_pos; the cert is signed AFTER (its SIGNATURE lands at a later position),
    # so the storage anchor S is a descendant of the revocation → key invalid at S.
    _write_env(repo, pub, added_pos, revoke_pos)
    _sign_and_close(repo, tid, priv, _head(repo))  # merged_log_commit backdated to main HEAD (init)
    proc = _run(repo, "--require-environment", ENV_ID)
    assert proc.returncode == 1, (
        "ROLLBACK BYPASS: gate accepted a cert whose signing key was revoked before its storage "
        f"anchor. stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    # The specific failure must be era-invalidity at the storage anchor, not an incidental error.
    assert "key_not_valid_at_era" in proc.stdout, (
        f"expected 'key_not_valid_at_era' verdict; stdout={proc.stdout}"
    )


def test_control_not_revoked_cert_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same cert, key NOT revoked → PASSES. Isolates that the rollback failure is caused by
    revocation-before-storage-anchor, not by any incidental setup difference."""
    repo, priv, pub, tid, added_pos, _revoke_pos = _setup(tmp_path, monkeypatch)
    _write_env(repo, pub, added_pos, None)  # never revoked
    _sign_and_close(repo, tid, priv, _head(repo))
    proc = _run(repo, "--require-environment", ENV_ID)
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
