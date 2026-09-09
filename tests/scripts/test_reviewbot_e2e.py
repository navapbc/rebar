from __future__ import annotations

import json
import os
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


def _contract(**env: str) -> dict[str, object]:
    merged_env = os.environ | env
    result = subprocess.run(
        ["bash", str(SCRIPT), "--describe-contract"],
        text=True,
        capture_output=True,
        check=True,
        env=merged_env,
    )
    return json.loads(result.stdout)


def test_probe_commit_has_ticket_trailer_and_signoff() -> None:
    contract = _contract()
    message = str(contract["commit_message"])

    assert message.startswith("test: reviewbot e2e probe stamp\n\n")
    assert "rebar-ticket: 0000-0000-0000-0000\n" in message


def test_e2e_targets_replicated_feature_branch() -> None:
    contract = _contract(TEST_BRANCH="feature/custom-e2e")

    assert contract["test_branch"] == "feature/custom-e2e"
    assert contract["review_ref"] == "refs/for/feature/custom-e2e"


def test_waits_for_llm_review_and_verified_before_submit() -> None:
    contract = _contract(CI_BOT_USER="ci-user")

    assert contract["llm_label"] == "LLM-Review"
    assert contract["verified_label"] == "Verified"
    assert contract["ci_bot_user"] == "ci-user"


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
