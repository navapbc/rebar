from __future__ import annotations

import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "infra" / "gerrit" / "reviewbot-e2e.sh"


def _script() -> str:
    return SCRIPT.read_text()


def test_probe_commit_has_ticket_trailer_and_signoff() -> None:
    text = _script()

    assert 'TICKET="${TICKET:-}"' in text
    assert "create_ticket()" in text
    assert 'TICKET_ID="$(ticket_id)"' in text
    assert 'TICKET_ID="$(create_ticket)"' in text
    assert "TRAILER=$'\\n\\nrebar-ticket: '\"${TICKET_ID}\"" in text
    assert 'rebar comment "$TICKET_ID"' in text
    assert 'git commit -q -s -m "test: reviewbot e2e probe ${stamp}${TRAILER}"' in text


def test_e2e_targets_replicated_feature_branch_and_cleans_it_up() -> None:
    text = _script()

    assert 'TEST_BRANCH="${TEST_BRANCH:-feature/e2e-gate-smoke}"' in text
    assert "test branch not visible on GitHub mirror" in text
    assert '":refs/heads/${TEST_BRANCH}"' in text
    assert 'rebar transition "$DISPOSABLE_TICKET" open closed --class obsolete' in text
    assert "refs/for/${TEST_BRANCH}" in text


def test_waits_for_llm_review_and_verified_before_submit() -> None:
    text = _script()
    submit_pos = text.index("submitting change")

    assert "VERIFIED_MAX" in text
    assert "CI_BOT_NAME" in text
    assert "poll_both_votes" in text
    assert text.index("LLM-Review") < submit_pos
    assert text.index("Verified") < submit_pos
    assert re.search(r'\\[ "\\$llm" != "NONE" \\] \\|\\| harness', text)
    assert re.search(r'\\[ "\\$ver" != "NONE" \\] \\|\\| harness', text)
    assert re.search(r'\\[ "\\$llm" -lt "\\$LLM_REVIEW_MAX" \\]', text)
    assert re.search(r'\\[ "\\$ver" -lt "\\$VERIFIED_MAX" \\]', text)
    assert 'llm_req="$(sr_status "LLM-Review")"' in text
    assert 'ver_req="$(sr_status "Verified")"' in text
