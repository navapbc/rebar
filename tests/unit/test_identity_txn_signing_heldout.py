"""HELD-OUT oracle for bug 0ba4 (txn signing gap).

The transactional write path (transition / claim / close, and delete) must sign its STATUS /
EDIT events through the SAME shared finalize+sign seam that ``append_event`` uses — so
CREATE/COMMENT and transitions/claims are signed identically, and enabling
``identity.require_authenticated`` does not break the ticket workflow. Before the fix these
events carried attribution (``author_id``) but no ``author_sig``.

These assertions target observable event bytes (``author_sig`` presence) and the merge-gate
verdict — never internal function names — so they survive a behaviour-preserving refactor.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands._seam import tracker_dir

pytestmark = pytest.mark.skipif(
    subprocess.run(["ssh-keygen", "-Y", "sign", "-h"], capture_output=True).returncode
    not in (0, 1, 255),
    reason="ssh-keygen with -Y sign (OpenSSH >= 8.9) required",
)


def _init_store(tmp_path: Path, monkeypatch, email: str) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", email),
        ("git", "config", "user.name", "Dev"),
        ("git", "commit", "-q", "--allow-empty", "-m", "init"),
    ):
        subprocess.run(args, cwd=r, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(r))
    rebar.init_repo(repo_root=str(r))
    return r


@pytest.fixture
def signed_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A store with local signing wired: an identity whose email matches git, and a signing key."""
    # Disable compaction (conftest sets the horizon to 0, which compact-on-close would use to
    # retire the just-written close STATUS into a SNAPSHOT before we can inspect the raw event).
    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", str(10**19))
    r = _init_store(tmp_path, monkeypatch, "ada@example.com")
    key = tmp_path / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-q"],
        check=True,
        capture_output=True,
    )
    parts = (tmp_path / "id_ed25519.pub").read_text().strip().split()
    ident = rebar.create_identity(
        "Ada", "ada@example.com", keys=[f"{parts[0]} {parts[1]}"], repo_root=str(r)
    )
    rebar.use_identity(ident, repo_root=str(r))
    monkeypatch.setenv("REBAR_IDENTITY_SIGNING_KEY", str(key))
    return r


def _events(repo: Path, ticket_id: str, event_type: str) -> list[dict]:
    d = Path(tracker_dir(str(repo))) / ticket_id
    return [
        json.loads(p.read_text(encoding="utf-8")) for p in sorted(d.glob(f"*-{event_type}.json"))
    ]


def test_transition_status_event_is_signed(signed_repo: Path) -> None:
    """An ungated transition (open -> idea) writes a SIGNED STATUS event."""
    t = rebar.create_ticket("task", "t", repo_root=str(signed_repo))
    rebar.transition(t, "open", "idea", repo_root=str(signed_repo))
    st = _events(signed_repo, t, "STATUS")
    assert st, "transition wrote no STATUS event"
    assert all(e.get("author_sig") for e in st), "transition STATUS event(s) must be signed"


def test_claim_status_and_edit_events_are_signed(signed_repo: Path) -> None:
    """A claim writes a SIGNED STATUS(in_progress) AND a SIGNED EDIT(assignee) in one commit."""
    b = rebar.create_ticket("bug", "b", repo_root=str(signed_repo))  # bug: plan-review-exempt
    rebar.claim(b, assignee="ada", repo_root=str(signed_repo))
    st = _events(signed_repo, b, "STATUS")
    ed = _events(signed_repo, b, "EDIT")
    assert st and all(e.get("author_sig") for e in st), "claim STATUS must be signed"
    assert ed and all(e.get("author_sig") for e in ed), "claim EDIT(assignee) must be signed"


def test_close_status_event_is_signed(signed_repo: Path) -> None:
    """Closing a ticket writes a SIGNED STATUS(closed) event."""
    b = rebar.create_ticket("bug", "b", repo_root=str(signed_repo))
    rebar.claim(b, assignee="ada", repo_root=str(signed_repo))
    rebar.transition(b, "in_progress", "closed", reason="Fixed: done", repo_root=str(signed_repo))
    closed = [
        e for e in _events(signed_repo, b, "STATUS") if e.get("data", {}).get("status") == "closed"
    ]
    assert closed, "no close STATUS event found"
    assert all(e.get("author_sig") for e in closed), "close STATUS must be signed"


def test_transition_event_passes_the_merge_gate(signed_repo: Path) -> None:
    """The integration that matters: a signed transition STATUS event VERIFIES under the
    authenticated-authorship merge-gate with enforcement on (exit 0)."""
    t = rebar.create_ticket("task", "t", repo_root=str(signed_repo))
    rebar.transition(t, "open", "idea", repo_root=str(signed_repo))
    env = {
        **os.environ,
        "REBAR_ROOT": str(signed_repo),
        "REBAR_IDENTITY_REQUIRE_AUTHENTICATED": "1",
    }
    res = subprocess.run(
        ["rebar", "verify-identity", "--all"],
        cwd=signed_repo,
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stdout + res.stderr


def test_unsignable_transition_is_refused_under_enforcement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parity with append_event's write-gate: under ``require_authenticated`` with NO resolvable
    identity/key, a non-exempt transition is REFUSED and NO STATUS event is written."""
    r = _init_store(tmp_path, monkeypatch, "nobody@example.com")  # no matching identity
    t = rebar.create_ticket("task", "t", repo_root=str(r))  # created with enforcement off
    monkeypatch.setenv("REBAR_IDENTITY_REQUIRE_AUTHENTICATED", "1")
    with pytest.raises(rebar.RebarError):
        rebar.transition(t, "open", "idea", repo_root=str(r))
    assert not _events(r, t, "STATUS"), "an unsignable transition must write no STATUS event"
