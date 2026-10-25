"""Scenario 4 of the feature-branch e2e must not claim a Verified rerun it did not assert (3781).

``scenario_4`` records the Verified vote it reads right after the re-merge push, but asserts
only the change kind and the LLM-Review carry. Its pass line used to say "Verified re-runs"
for any recorded value, including a carried +1 that means no rerun happened. The real
scenario mutates configured Gerrit and GitHub, so these tests source the scenarios file and
replace every Gerrit and git helper with a stub, then check the output and exit code.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

_SCENARIOS = (
    Path(__file__).resolve().parents[2] / "infra" / "gerrit" / "feature-branch-e2e-scenarios.sh"
)

# Everything scenario_4 reaches outside itself, stubbed. The vote and kind come from the
# environment so each test controls what "Gerrit" reports after the re-merge push.
_HARNESS = r"""
set -euo pipefail
cd "$WORKDIR"
log()  { echo "log: $*" >&2; }
pass() { echo "PASS: $*"; }
sleep() { :; }
git() { case "$1" in ls-remote) echo "0123456789abcdef refs/heads/x" ;; *) return 0 ;; esac; }
api_get() { echo '{"change_id": "I0000000000000000000000000000000000000001"}'; }
push_for_review() { echo 4242; }
poll_both_votes() { echo "LLM=1 VER=1"; }
submit_change() { echo MERGED; }
change_status() { echo MERGED; }
change_kind() { echo "$STUB_KIND"; }
account_vote() { case "$2" in LLM-Review) echo "$STUB_LLM" ;; Verified) echo "$STUB_VER" ;; esac; }
GIT_HTTPS_URL=https://example.invalid/a/rebar FB=feature/e2e-test TICKET=leafy-vogue-ingot
TRAILER=$'\n\nrebar-ticket: leafy-vogue-ingot'
GERRIT_HOST=example.invalid PROJECT=rebar LLM_REVIEW_MAX=1 VERIFIED_MAX=1
REVIEW_BOT_NAME="rebar review bot" CI_BOT_NAME="rebar CI bot"
CREATED_CHANGES=()
# shellcheck disable=SC1090
source "$SCENARIOS"
rc=0
scenario_4 || rc=$?
echo "RC=$rc"
"""


def _run(tmp_path: Path, *, kind: str, llm: str, ver: str) -> tuple[int, list[str], list[str]]:
    work = tmp_path / "work"
    work.mkdir()
    done = subprocess.run(
        ["bash", "-c", _HARNESS],
        env=subprocess_env(
            WORKDIR=str(work),
            SCENARIOS=str(_SCENARIOS),
            STUB_KIND=kind,
            STUB_LLM=llm,
            STUB_VER=ver,
        ),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    lines = done.stdout.splitlines()
    rc_lines = [ln for ln in lines if ln.startswith("RC=")]
    assert rc_lines, f"the scenario never reported an exit code:\n{done.stdout}\n{done.stderr}"
    return (
        int(rc_lines[-1].removeprefix("RC=")),
        [ln for ln in lines if ln.startswith("PASS:")],
        [ln for ln in lines if ln.startswith("EVIDENCE:")],
    )


@pytest.mark.parametrize("ver_after", ["NONE", "0", "1"])
def test_the_pass_line_never_claims_a_verified_rerun(tmp_path: Path, ver_after: str) -> None:
    """Whatever Verified value was observed, including a carried +1, no rerun is claimed."""
    rc, passes, _evidence = _run(tmp_path, kind="MERGE_FIRST_PARENT_UPDATE", llm="1", ver=ver_after)

    assert rc == 0
    assert len(passes) == 1, passes
    line = passes[0]
    assert "re-runs" not in line and "reran" not in line.lower(), line
    assert "recorded, not asserted" in line, line
    assert ver_after in line, f"the observed vote must still be reported: {line}"


def test_the_evidence_lines_are_intact(tmp_path: Path) -> None:
    _rc, _passes, evidence = _run(tmp_path, kind="MERGE_FIRST_PARENT_UPDATE", llm="1", ver="NONE")

    joined = "\n".join(evidence)
    for field in ("merge_change=", "ps1_votes=", "unrelated_landed=", "remerge_kind="):
        assert field in joined, f"EVIDENCE lacks {field}:\n{joined}"
    assert "after_remerge LLM=1 VER=NONE expect=LLM_carried_VER_rerun" in joined, joined


def test_a_wrong_change_kind_still_fails_the_scenario(tmp_path: Path) -> None:
    rc, passes, _evidence = _run(tmp_path, kind="REWORK", llm="1", ver="NONE")

    assert rc == 1
    assert passes == []


def test_an_uncarried_llm_review_vote_still_fails_the_scenario(tmp_path: Path) -> None:
    rc, passes, _evidence = _run(tmp_path, kind="MERGE_FIRST_PARENT_UPDATE", llm="0", ver="NONE")

    assert rc == 1
    assert passes == []
