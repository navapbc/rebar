"""The per-ticket gates share one heading vocabulary (ticket bandy-name-hilt).

clarity-check and check-ac historically diverged: clarity rewarded a per-type
heading (## Success Criteria for epics, ## Why/## What for stories) and only gave
the Acceptance-Criteria bonus to tasks, while check-ac required a literal
"## Acceptance Criteria" checklist on EVERY type. So a clarity-perfect epic/story
without an AC block passed clarity but failed check-ac.

Invariant pinned here: a ticket that PASSES clarity-check (any type) also passes
check-ac. clarity-check now requires the AC floor on every type, so the two gates
agree. (RED before the fix for the story/epic/bug fixtures below.)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import rebar

_AC = "\n## Acceptance Criteria\n- [ ] first criterion\n- [ ] second criterion\n"
_FILLER = (
    "This ticket describes a cohesive unit of work in enough detail for an "
    "agent to act on it without guessing. " * 3
)

# Well-formed ticket per type: the per-type headings clarity rewards PLUS the
# universal Acceptance Criteria checklist. All should pass both gates.
WELL_FORMED = {
    "task": _FILLER + "\n\n## Notes\nTouches src/rebar/foo.py and tests/test_foo.py.\n" + _AC,
    "story": _FILLER + "\n\n## Why\nContext.\n\n## What\nThe change.\n\n## Scope\nBounded.\n" + _AC,
    "bug": _FILLER + "\n\n## Reproduction Steps\n- run it\n\nExpected X, actual Y.\n" + _AC,
    "epic": _FILLER + "\n\n## Success Criteria\n- done\n\n## Context\nWhy now.\n" + _AC,
}

# Same per-type richness but NO Acceptance Criteria block: clarity must now FAIL
# (the AC floor), so it can never pass clarity while failing check-ac.
NO_AC = {t: d.replace(_AC, "\n") for t, d in WELL_FORMED.items()}


def _cli_rc(*args: str, cwd: str) -> int:
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
    ).returncode


@pytest.mark.parametrize("ttype", sorted(WELL_FORMED))
def test_clarity_pass_implies_check_ac_pass(ttype: str, rebar_repo: Path) -> None:
    r = str(rebar_repo)
    tid = rebar.create_ticket(ttype, f"{ttype} probe", description=WELL_FORMED[ttype], repo_root=r)
    clarity_rc = _cli_rc("clarity-check", tid, cwd=r)
    assert clarity_rc == 0, f"{ttype}: expected clarity pass for a well-formed ticket"
    # The invariant: clarity-pass => check-ac-pass (exit 0).
    assert _cli_rc("check-ac", tid, cwd=r) == 0, f"{ttype}: clarity passed but check-ac failed"


@pytest.mark.parametrize("ttype", sorted(NO_AC))
def test_clarity_fails_without_acceptance_criteria(ttype: str, rebar_repo: Path) -> None:
    r = str(rebar_repo)
    tid = rebar.create_ticket(ttype, f"{ttype} no-ac", description=NO_AC[ttype], repo_root=r)
    # No AC block -> clarity must fail (the floor), matching check-ac's failure.
    assert _cli_rc("clarity-check", tid, cwd=r) == 1, f"{ttype}: clarity passed without an AC block"
    assert _cli_rc("check-ac", tid, cwd=r) == 1, f"{ttype}: check-ac passed without an AC block"
