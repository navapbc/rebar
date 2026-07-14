"""Held-out oracle for the op-cert merge-gate (story 4214). NOT shown to the implementer.

* enforced + missing cert → exit 1;
* enforced + foreign (non-pinned) cert → exit 1;
* no required environment → advisory (exit 0) even with a missing cert;
* grandfathered ticket (closed before the `--since` boundary) → exit 0 despite a missing cert;
* the shipped `verify-identity.yaml` gains a `rebar verify-opcert` step;
* the workflow carries a CI-trigger audit comment enumerating every `on:` trigger.

Real ssh-keygen + a real rebar store + the real `rebar verify-opcert` subprocess.
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
_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "verify-identity.yaml"


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


def _tracker_head(repo: Path) -> str:
    from rebar._commands._seam import tracker_dir

    tr = str(tracker_dir(str(repo)))
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tr, capture_output=True, text=True, check=True
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


def _run(repo: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["rebar", "verify-opcert", "--root", str(repo), *extra], capture_output=True, text=True
    )


def _sign(repo: Path, tid: str, priv: str, commit: str) -> None:
    signing.sign_opcert_manifest(
        tid,
        [f"{KIND}: PASS"],
        material_fingerprint=MATERIAL,
        merged_log_commit=commit,
        key_path=priv,
        principal=ENV_ID,
        repo_root=str(repo),
    )


# ---- enforced + missing / foreign → exit 1 ----------------------------------------------------


def test_missing_opcert_fails_when_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _store(tmp_path, monkeypatch)
    priv, pub = _keypair(tmp_path, "env")
    tid = rebar.create_ticket("task", "ungated", repo_root=str(repo))
    _write_trusted_env(repo, ENV_ID, pub, _tip_position(repo))
    rebar.transition(tid, "open", "closed", repo_root=str(repo))  # closed, but NO op-cert
    # No --since → all in-scope tickets enforced; required env set → missing cert fails.
    proc = _run(repo, "--require-environment", ENV_ID)
    assert proc.returncode == 1, f"stdout={proc.stdout}\nstderr={proc.stderr}"


def test_foreign_opcert_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _store(tmp_path, monkeypatch)
    _pinned_priv, pinned_pub = _keypair(tmp_path, "pinned")
    foreign_priv, _foreign_pub = _keypair(tmp_path, "foreign")
    commit = _head(repo)
    tid = rebar.create_ticket("task", "foreign-signed", repo_root=str(repo))
    _write_trusted_env(repo, ENV_ID, pinned_pub, _tip_position(repo))
    _sign(repo, tid, foreign_priv, commit)  # op-cert signed by a NON-pinned key
    rebar.transition(tid, "open", "closed", repo_root=str(repo))
    proc = _run(repo, "--require-environment", ENV_ID)
    assert proc.returncode == 1, f"stdout={proc.stdout}\nstderr={proc.stderr}"


# ---- advisory posture (no required environment) → exit 0 --------------------------------------


def test_advisory_when_no_required_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _store(tmp_path, monkeypatch)
    tid = rebar.create_ticket("task", "ungated", repo_root=str(repo))
    rebar.transition(tid, "open", "closed", repo_root=str(repo))  # closed, no cert
    # No --require-environment and no config → advisory everywhere → exit 0.
    proc = _run(repo)
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"


# ---- grandfathered ticket (closed before the boundary) → exit 0 -------------------------------


def test_grandfathered_ticket_passes_without_cert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _store(tmp_path, monkeypatch)
    priv, pub = _keypair(tmp_path, "env")
    tid = rebar.create_ticket("task", "old work", repo_root=str(repo))
    _write_trusted_env(repo, ENV_ID, pub, _tip_position(repo))
    rebar.transition(tid, "open", "closed", repo_root=str(repo))  # closed, no cert
    # A LATER tracker commit becomes the enforce-since boundary, so the ticket's close-STATUS
    # event predates it → grandfathered → advisory pass despite the missing cert.
    rebar.create_ticket("task", "later activity", repo_root=str(repo))
    since = _tracker_head(repo)
    proc = _run(repo, "--require-environment", ENV_ID, "--since", since)
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"


# NOTE (fail-closed on an unresolvable anchor — LLM-Review security finding, verify_opcert.py):
# a CLOSED ticket whose terminal close-STATUS event has been compacted into a SNAPSHOT has an
# unresolvable enforcement anchor. The fix does NOT drop it from scope (that was fail-OPEN); it
# leaves close_commit=None and lets `_is_enforced(None, since_ref, tracker)` FAIL CLOSED (its
# documented + separately-tested contract: `commit_sha is None → return True`). We deliberately do
# NOT add an end-to-end test here: reliably forcing `compact()` to fold the *terminal* STATUS event
# is env/threshold-dependent and would yield a flaky-or-vacuous test (worse than none). The
# behaviour is covered by (a) this code path routing to `_is_enforced`, and (b) that function's own
# fail-closed test in the identity suite.


# ---- authoritative material: self-reported material is not trusted (security regression) ------


def test_self_reported_stale_material_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A self-consistent op-cert (signed by the genuine pinned key) whose bound material_fingerprint
    is NOT the ticket's authoritative material must FAIL. The record + envelope live on the
    auto-pushed tickets branch, so the gate must recompute the material from live ticket state and
    never trust the self-reported value. Regression for the verify-side authoritative-input gap."""
    repo = _store(tmp_path, monkeypatch)
    priv, pub = _keypair(tmp_path, "env")
    commit = _head(repo)
    tid = rebar.create_ticket("task", "gated", repo_root=str(repo))
    _write_trusted_env(repo, ENV_ID, pub, _tip_position(repo))
    # MATERIAL is a fixed dummy, NOT the ticket's real (sha256-derived) material fingerprint.
    signing.sign_opcert_manifest(
        tid,
        [f"{KIND}: PASS"],
        material_fingerprint=MATERIAL,
        merged_log_commit=commit,
        key_path=priv,
        principal=ENV_ID,
        repo_root=str(repo),
    )
    rebar.transition(tid, "open", "closed", repo_root=str(repo))
    proc = _run(repo, "--require-environment", ENV_ID)
    assert proc.returncode == 1, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    # The failure is specifically a subject-digest mismatch (recomputed material != bound material),
    # surfaced as the `mismatch` verdict — not some incidental error.
    assert "mismatch" in proc.stdout, f"expected 'mismatch' verdict; stdout={proc.stdout}"


def test_authoritative_material_cert_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive counterpart to the stale-material case: the SAME signing key + ticket, but the cert
    binds the ticket's AUTHORITATIVE (recomputed) material fingerprint, so the gate certifies it
    (exit 0). Pairs with test_self_reported_stale_material_rejected to prove the gate distinguishes
    a matching bound material from a mismatched one — not that it rejects everything."""
    from rebar.llm.plan_review.attest import current_material_fingerprint

    repo = _store(tmp_path, monkeypatch)
    priv, pub = _keypair(tmp_path, "env")
    commit = _head(repo)
    tid = rebar.create_ticket("task", "gated", repo_root=str(repo))
    _write_trusted_env(repo, ENV_ID, pub, _tip_position(repo))
    material = current_material_fingerprint(tid, repo_root=str(repo))  # the REAL material
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
    proc = _run(repo, "--require-environment", ENV_ID)
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"


# ---- workflow file: op-cert step + CI-trigger audit -------------------------------------------


def test_workflow_has_verify_opcert_step() -> None:
    text = _WORKFLOW.read_text(encoding="utf-8")
    assert "rebar verify-opcert" in text  # the op-cert lane runs in the shipped merge-gate job


def test_workflow_ci_trigger_audit_lists_every_trigger() -> None:
    text = _WORKFLOW.read_text(encoding="utf-8").lower()
    # A concrete audit artifact: a comment classifying each on: trigger INCLUDED/NO_FILTER.
    assert "included" in text or "no_filter" in text
    for trigger in ("push", "pull_request", "workflow_dispatch"):
        assert trigger in text
