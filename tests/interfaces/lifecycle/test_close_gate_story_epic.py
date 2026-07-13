"""Story/epic close behavior after the signature close-gate was retired (28f1).

The former ``verify.require_signature_for_close`` gate (which required a certified
signature BEFORE closing a story/epic) has been removed — it was the documented
ALTERNATIVE to the completion-verifier close gate this project actually uses, and was
never enabled here. These tests pin the resulting behavior:

  * a story/epic closes WITHOUT any signature (no gate to satisfy), for both types;
  * ``--force-close="<reason>"`` still closes and still records the FORCE_CLOSE audit
    comment (that path is owned by the force-close flow, not by the removed gate).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import rebar
from rebar import _cli

_TYPES = ("story", "epic")


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
def test_story_epic_closes_without_signature(rebar_repo: Path, ttype: str) -> None:
    tid = _make(rebar_repo, ttype)
    out = rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert out["to"] == "closed"


@pytest.mark.parametrize("ttype", _TYPES)
def test_force_close_still_closes_with_audit_comment(
    rebar_repo: Path, ttype: str, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _make(rebar_repo, ttype)

    capsys.readouterr()
    rc = _cli.main(["transition", tid, "in_progress", "closed", "--force-close=verifier offline"])
    assert rc == 0
    assert _status(tid, rebar_repo) == "closed"

    # The force-close flow records a FORCE_CLOSE audit comment on the ticket.
    comments = rebar.show_ticket(tid, repo_root=str(rebar_repo))["comments"]
    bodies = "\n".join(c.get("body", "") for c in comments)
    assert "FORCE_CLOSE: close gate(s) bypassed by user approval" in bodies
    assert 'Reason: "verifier offline"' in bodies
