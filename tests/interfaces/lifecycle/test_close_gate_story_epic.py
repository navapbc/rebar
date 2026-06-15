"""Signature close-gate coverage for BOTH story and epic ticket types.

The existing close-gate tests in test_signature.py exercise the gate on *story*
tickets only. The gate is story/epic-scoped, so this parametrizes the same
contract over both types and additionally pins the ``--force-close`` audit trail
the wrapper emits (the stderr warning bytes + the FORCE_CLOSE audit comment),
which the existing suite asserts the *exit code* of but not its side effects.

Contract (see rebar._commands.txn._signature_gate +
rebar._commands.transition.transition_compute):

  * gate OFF (default, no .rebar/config.conf) → story/epic close without a signature;
  * gate ON → an unsigned story/epic close is rejected (exit 1, "certified
    signature"); closing after a valid sign_manifest at the current HEAD succeeds;
  * a signature certified against an OLDER store/code HEAD is rejected once the
    HEAD advances ("different commit");
  * ``--force-close="<reason>"`` closes despite the gate, emitting the
    "via --force-close (signature gate bypassed)" warning to stderr AND a
    FORCE_CLOSE audit comment on the ticket.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar import _cli

MANIFEST = ["ran unit tests: PASS", "lint clean"]

_TYPES = ("story", "epic")


def _enable_gate(repo: Path) -> None:
    (repo / ".rebar").mkdir(exist_ok=True)
    (repo / ".rebar" / "config.conf").write_text("verify.require_signature_for_close=true\n")


def _commit(repo: Path, msg: str = "c") -> None:
    """Give the code repo a resolvable HEAD (the fixture inits with an unborn HEAD;
    the gate's freshness binding requires one)."""
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", msg],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


def _make(repo: Path, ttype: str) -> str:
    desc = (
        "Body long enough for the gates.\n\n"
        "## Acceptance Criteria\n- [ ] done\n\n"
        "## Success Criteria\n- [ ] shipped\n\n## Context\nfoo\n"
    )
    tid = rebar.create_ticket(ttype, f"Gate {ttype}", description=desc, repo_root=str(repo))
    rebar.transition(tid, "open", "in_progress", repo_root=str(repo))
    return tid


def _status(tid: str, repo: Path) -> str:
    return rebar.show_ticket(tid, repo_root=str(repo))["status"]


@pytest.mark.parametrize("ttype", _TYPES)
def test_gate_off_by_default_closes_without_signature(rebar_repo: Path, ttype: str) -> None:
    tid = _make(rebar_repo, ttype)
    out = rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert out["to"] == "closed"


@pytest.mark.parametrize("ttype", _TYPES)
def test_gate_on_blocks_unsigned_then_allows_after_sign(rebar_repo: Path, ttype: str) -> None:
    _commit(rebar_repo)
    _enable_gate(rebar_repo)
    tid = _make(rebar_repo, ttype)

    with pytest.raises(rebar.RebarError) as ei:
        rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert "certified signature" in ei.value.stderr
    assert _status(tid, rebar_repo) == "in_progress"  # not closed

    rebar.sign_manifest(tid, MANIFEST, repo_root=str(rebar_repo))
    out = rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert out["to"] == "closed"


@pytest.mark.parametrize("ttype", _TYPES)
def test_gate_stale_head_is_rejected(rebar_repo: Path, ttype: str) -> None:
    _commit(rebar_repo, "base")
    _enable_gate(rebar_repo)
    tid = _make(rebar_repo, ttype)
    rebar.sign_manifest(tid, MANIFEST, repo_root=str(rebar_repo))
    # Advance the code HEAD after signing → the attestation is now stale.
    _commit(rebar_repo, "advance")

    with pytest.raises(rebar.RebarError) as ei:
        rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert "different commit" in ei.value.stderr
    assert _status(tid, rebar_repo) == "in_progress"

    # Re-signing at the new HEAD unblocks the close.
    rebar.sign_manifest(tid, MANIFEST, repo_root=str(rebar_repo))
    assert (
        rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))["to"] == "closed"
    )


@pytest.mark.parametrize("ttype", _TYPES)
def test_gate_force_close_bypasses_with_warning_and_audit_comment(
    rebar_repo: Path, ttype: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _enable_gate(rebar_repo)
    tid = _make(rebar_repo, ttype)

    capsys.readouterr()
    rc = _cli.main(["transition", tid, "in_progress", "closed", "--force-close=verifier offline"])
    err = capsys.readouterr().err
    assert rc == 0
    # Warning bytes from the signature gate's force-close branch.
    assert "via --force-close (signature gate bypassed)" in err
    assert "Reason: verifier offline" in err
    assert _status(tid, rebar_repo) == "closed"

    # The wrapper records a FORCE_CLOSE audit comment on the ticket.
    comments = rebar.show_ticket(tid, repo_root=str(rebar_repo))["comments"]
    bodies = "\n".join(c.get("body", "") for c in comments)
    assert "FORCE_CLOSE: signature gate bypassed by user approval" in bodies
    assert 'Reason: "verifier offline"' in bodies
