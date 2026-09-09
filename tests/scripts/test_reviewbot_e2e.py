from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "infra" / "gerrit" / "reviewbot-e2e.sh"


def _script() -> str:
    return SCRIPT.read_text()


def _embedded_python(function_name: str, terminator: str) -> str:
    text = _script()
    function_pos = text.index(f"{function_name}()")
    start = text.index("| python3 -c '\n", function_pos) + len("| python3 -c '\n")
    end = text.index(terminator, start)
    return text[start:end]


def _run_embedded(script: str, payload: dict[str, object], *args: str) -> str:
    result = subprocess.run(
        ["python3", "-c", script, *args],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


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
    assert 'rebar transition "$DISPOSABLE_TICKET" "$status" closed --class obsolete' in text
    assert "refs/for/${TEST_BRANCH}" in text


def test_waits_for_llm_review_and_verified_before_submit() -> None:
    text = _script()
    submit_pos = text.index("submitting change")

    assert "VERIFIED_MAX" in text
    assert "CI_BOT_NAME" in text
    assert "CI_BOT_USER" in text
    assert "poll_both_votes" in text
    gate_section = text[text.index("polling up to ${POLL_TIMEOUT_SECONDS}s") : submit_pos]
    assert "LLM-Review" in gate_section
    assert "Verified" in gate_section
    assert re.search(r'\\[ "\\$llm" != "NONE" \\] \\|\\| harness', text)
    assert re.search(r'\\[ "\\$ver" != "NONE" \\] \\|\\| harness', text)
    assert 'is_int "$llm" && [ "$llm" -lt "$LLM_REVIEW_MAX" ]' in text
    assert 'is_int "$ver" && [ "$ver" -lt "$VERIFIED_MAX" ]' in text
    assert "current_label_vote" in text
    assert 'llm_req="$(sr_status "LLM-Review")"' in text
    assert 'ver_req="$(sr_status "Verified")"' in text


def test_current_label_vote_reads_current_exact_account_value() -> None:
    script = _embedded_python("current_label_vote", '\n\' "$1" "$2" "$3"')
    payload = {
        "labels": {
            "Verified": {
                "approved": {"username": "rebar-ci-bot", "name": "rebar CI bot"},
                "all": [
                    {"username": "other-bot", "name": "other", "value": -1},
                    {"username": "rebar-ci-bot", "name": "rebar CI bot", "value": 1},
                ],
            }
        }
    }

    assert _run_embedded(script, payload, "Verified", "rebar-ci-bot", "rebar CI bot") == "1"


def test_current_label_vote_requires_current_shortcut_and_integer_value() -> None:
    script = _embedded_python("current_label_vote", '\n\' "$1" "$2" "$3"')

    stale_only = {
        "labels": {
            "LLM-Review": {
                "all": [{"username": "rebar-review-bot", "name": "rebar-review-bot", "value": 1}]
            }
        }
    }
    no_integer_value = {
        "labels": {
            "LLM-Review": {
                "approved": {"username": "rebar-review-bot", "name": "rebar-review-bot"},
                "all": [{"username": "rebar-review-bot", "name": "rebar-review-bot"}],
            }
        }
    }

    assert (
        _run_embedded(script, stale_only, "LLM-Review", "rebar-review-bot", "rebar-review-bot")
        == "NONE"
    )
    assert (
        _run_embedded(
            script, no_integer_value, "LLM-Review", "rebar-review-bot", "rebar-review-bot"
        )
        == "NONE"
    )


def test_sr_status_reads_submit_requirement_state() -> None:
    script = _embedded_python("sr_status", '\n\' "$1"')
    payload = {"submit_requirements": [{"name": "Verified", "status": "SATISFIED"}]}

    assert _run_embedded(script, payload, "Verified") == "SATISFIED"
    assert _run_embedded(script, payload, "LLM-Review") == "ABSENT"
