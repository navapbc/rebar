"""Behavioral oracle for a2c7 (coal-trainsick-heifer) — the whole-store verify-identity gate's
runtime shape.

Two independent, behavior-preserving performance fixes on the authenticated-authorship
merge-gate, expressed as observable call-graph facts (not timings):

  Fix #1 — Advisory mode (``require_authenticated`` OFF) is report-only: the gate's exit code is
  unconditionally 0 and no enforcement can trigger, so the per-event git era-verify
  (``verify_authorship_at_commit``) MUST NOT run at all. With enforcement ON it MUST still run
  (the gate still enforces).

  Fix #4 — With enforcement ON, the era-verify resolves each keyring record's / event's
  introducing commit via the batched position map built once in ``cli()``, NOT a full-history
  ``git log`` per keyring record (``resolve_event_commit``). The verdict is IDENTICAL either
  way (a signed CREATE stays ``verified`` → exit 0); only the number of git subprocesses drops.

The last test pins Fix #4's behavior-preservation directly on
``verify_authorship_at_commit``: injecting a ``position_resolver`` yields the same Verdict as
the per-event resolver, while calling ``resolve_event_commit`` zero times.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands import verify_authorship as V
from rebar._commands._seam import tracker_dir
from rebar.attest import authorship, sshsig

try:
    sshsig.ensure_available()
    _SSH_OK = True
except Exception:  # noqa: BLE001 — best-effort SSHSIG availability probe; skip if unavailable
    _SSH_OK = False

pytestmark = pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required for SSHSIG")


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


def _signed_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> tuple[Path, str]:
    """A store whose work event is SIGNED+verifiable: an identity with an in-band key, made the
    active author, then a signed CREATE. Returns (repo, identity_id)."""
    repo = _init(tmp_path, monkeypatch, name)
    priv, pub = _keypair(tmp_path, f"{name}-key")
    ident = rebar.create_identity("Ada", "ada@example.com", keys=[pub], repo_root=str(repo))
    rebar.use_identity(ident, repo_root=str(repo))
    monkeypatch.setenv("REBAR_IDENTITY_SIGNING_KEY", priv)
    rebar.create_ticket("task", "signed work", repo_root=str(repo))  # signed, verifiable CREATE
    return repo, ident


def _spy(monkeypatch: pytest.MonkeyPatch, attr: str) -> list[int]:
    """Replace ``authorship.<attr>`` with a counting pass-through; returns a 1-element call
    counter list."""
    calls = [0]
    orig = getattr(authorship, attr)

    def wrapper(*a, **k):
        calls[0] += 1
        return orig(*a, **k)

    monkeypatch.setattr(authorship, attr, wrapper)
    return calls


# ── Fix #1: advisory mode does NOT run the per-event git era-verify ───────────────────────────
def test_advisory_mode_skips_per_event_era_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _ident = _signed_store(tmp_path, monkeypatch, "advisory")
    monkeypatch.setenv("REBAR_IDENTITY_REQUIRE_AUTHENTICATED", "0")  # enforcement OFF
    era = _spy(monkeypatch, "verify_authorship_at_commit")

    rc = V.cli(["--all", "--root", str(repo)])

    assert rc == 0  # advisory is always a pass
    assert era[0] == 0, "advisory mode must not run the per-event git era-verify"


def test_enforcement_still_runs_era_verify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Contrast: with enforcement ON the gate MUST still era-verify (Fix #1 gates ONLY the
    advisory path — it does not weaken enforcement)."""
    repo, _ident = _signed_store(tmp_path, monkeypatch, "enforce")
    monkeypatch.setenv("REBAR_IDENTITY_REQUIRE_AUTHENTICATED", "1")  # enforcement ON
    era = _spy(monkeypatch, "verify_authorship_at_commit")

    rc = V.cli(["--all", "--root", str(repo)])

    assert rc == 0, "the signed CREATE is verifiable → gate passes"
    assert era[0] >= 1, "enforcement must still run the per-event era-verify"


# ── Fix #4: enforced era-verify uses the batched map, not a per-record full-history git log ────
def test_enforced_scan_does_not_git_log_per_keyring_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _ident = _signed_store(tmp_path, monkeypatch, "batched")
    monkeypatch.setenv("REBAR_IDENTITY_REQUIRE_AUTHENTICATED", "1")  # enforcement ON
    per_event_git_log = _spy(monkeypatch, "resolve_event_commit")

    rc = V.cli(["--all", "--root", str(repo)])

    assert rc == 0, "verified store passes"
    assert per_event_git_log[0] == 0, (
        "the era-verify must resolve commits via the batched position map, not a full-history "
        "git log per keyring record / event"
    )


# ── Fix #4: behavior preservation at the function level ────────────────────────────────────────
def test_position_resolver_is_behavior_preserving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, ident = _signed_store(tmp_path, monkeypatch, "equiv")
    tracker = str(tracker_dir(str(repo)))
    head = subprocess.run(
        ["git", "-C", tracker, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()

    priv, _pub = _keypair(tmp_path, "equiv-signkey")
    # A DSSE authorship envelope by the identity's own key over some payload; the era predicate's
    # key-validity outcome is what we compare (identical resolver inputs → identical Verdict).
    env = authorship.sign_authorship(b'{"uuid":"e","data":{}}', priv, principal=ident)

    # Per-event resolver (the status quo).
    baseline = authorship.verify_authorship_at_commit(env, ident, head, None, repo_root=str(repo))

    # Batched resolver: a dict-backed position→commit map (built the way cli() builds it), plus a
    # counter proving the per-record git log is bypassed entirely.
    pos_map = authorship.build_position_commit_map(repo_root=str(repo))
    per_event_git_log = _spy(monkeypatch, "resolve_event_commit")
    batched = authorship.verify_authorship_at_commit(
        env, ident, head, None, repo_root=str(repo), position_resolver=pos_map.get
    )

    assert batched.verified == baseline.verified
    assert batched.verdict == baseline.verdict
    assert per_event_git_log[0] == 0, (
        "an injected position_resolver must bypass resolve_event_commit"
    )
