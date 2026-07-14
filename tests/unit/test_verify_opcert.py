"""Happy-path spec for the op-cert merge-gate CLI (story 4214 / unfair-mindless-halcyon).

The ONLY tests the implementation subagent sees. Pins the approved design: `rebar verify-opcert`
walks the merged log, and a CLOSED ticket carrying a valid required-environment
`completion-verifier` op-cert (pinned in `.rebar/trusted_environments.yaml`) PASSES the gate.

Held-out (missing cert → exit 1, foreign cert → exit 1, advisory when no required env,
grandfathered ticket, the workflow-file step + CI-trigger audit) lives in the held-out companion.

Real ssh-keygen + a real rebar store + the real `rebar verify-opcert` subprocess (exit codes).
"""

from __future__ import annotations

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
MATERIAL = "0123456789abcdef"
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
    """The current tickets-branch tip log position (greatest {ts}-{uuid} over active events) — an
    early ancestor of any later storage anchor, to stamp as the key's added_at_log_position."""
    import os

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


def _write_trusted_env(repo: Path, env_id: str, pub: str, added_at_position: str) -> None:
    d = repo / ".rebar"
    d.mkdir(exist_ok=True)
    (d / "trusted_environments.yaml").write_text(
        "environments:\n"
        f"  - env_id: {env_id}\n"
        "    keys:\n"
        f"      - public_key: {pub}\n"
        f"        added_at_log_position: {added_at_position}\n"
        "        revoked_at_log_position: null\n",
        encoding="utf-8",
    )


def _run_verify_opcert(repo: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["rebar", "verify-opcert", "--root", str(repo), *extra],
        capture_output=True,
        text=True,
    )


def test_verify_opcert_passes_when_required_env_cert_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A closed ticket carrying a valid required-environment op-cert passes the gate (exit 0)."""
    repo = _store(tmp_path, monkeypatch)
    priv, pub = _keypair(tmp_path, "env")
    commit = _head(repo)

    tid = rebar.create_ticket("task", "gated work", repo_root=str(repo))
    # Pin the key from an EARLY tickets-branch log position (the ticket's CREATE), an ancestor of
    # the cert's storage anchor (its SIGNATURE event, written below) → valid at that anchor.
    _write_trusted_env(repo, ENV_ID, pub, _tip_position(repo))
    # The trusted signer binds the ticket's AUTHORITATIVE material fingerprint (the gate recomputes
    # the same value from live state — it never trusts a self-reported one).
    from rebar.llm.plan_review.attest import current_material_fingerprint

    material = current_material_fingerprint(tid, repo_root=str(repo))
    signing.sign_opcert_manifest(
        tid,
        [f"{KIND}: PASS"],
        material_fingerprint=material,
        merged_log_commit=commit,
        key_path=priv,
        principal=ENV_ID,
        repo_root=str(repo),
    )
    rebar.transition(tid, "open", "closed", repo_root=str(repo))

    # Require env ENV_ID; the ticket carries a valid cert from it → gate passes.
    proc = _run_verify_opcert(repo, "--require-environment", ENV_ID)
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
