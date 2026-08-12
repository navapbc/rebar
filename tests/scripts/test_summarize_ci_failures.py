"""Contract for scripts/summarize_ci_failures.py (ticket be9d-2f2d-47ca-45d2).

Tier: scripts (imports the module directly; the pure summarize() seam needs no subprocess).

The Gerrit `Verified` vote message is built by lfreleng-actions/gerrit-review-action as
`"<STATUS>: <run URL>"`, with no input for custom text. So every -1 reads `CANCELLED: <url>`
regardless of cause: a job that blew its `timeout-minutes` and a job that failed on stale-base
version skew are indistinguishable from the vote, though only the second needs a rebase.

This script renders the run's own job list into a triage summary that the vote job posts as a
comment-only review. The load-bearing distinction is timeout-vs-cancel: GitHub reports a
TIMED-OUT job as `conclusion: cancelled` and says so only in the check-run annotation text,
so the annotation is what separates "the gate never finished" from "a newer patchset
superseded this run".
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "summarize_ci_failures.py"

_spec = importlib.util.spec_from_file_location("summarize_ci_failures", SCRIPT)
assert _spec is not None and _spec.loader is not None
summarize_ci_failures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(summarize_ci_failures)

summarize = summarize_ci_failures.summarize
format_duration = summarize_ci_failures.format_duration

TIMEOUT_ANNOTATION = [{"message": "The job has exceeded the maximum execution time of 30m0s"}]


def _job(
    name: str,
    conclusion: str,
    job_id: int = 1,
    started_at: str = "2026-08-12T03:39:25Z",
    completed_at: str = "2026-08-12T04:09:41Z",
) -> dict[str, Any]:
    return {
        "id": job_id,
        "name": name,
        "conclusion": conclusion,
        "started_at": started_at,
        "completed_at": completed_at,
        "html_url": f"https://github.com/navapbc/rebar/actions/runs/1/job/{job_id}",
    }


def test_script_exists() -> None:
    assert SCRIPT.is_file()


# --- AC1: a timed-out job is named as timed out, with its duration -------------------


def test_timed_out_job_is_named_and_reports_duration() -> None:
    """The shape of the original bug: a mutation job that ran past its 30-minute cap and was
    reported only as `cancelled`, with nothing anywhere saying it had timed out."""
    jobs = [_job("bounded mutation gate", "cancelled", job_id=94002208506)]
    annotations = {"94002208506": TIMEOUT_ANNOTATION}

    summary = summarize(jobs, annotations)

    assert "bounded mutation gate" in summary
    assert "TIMED OUT" in summary
    assert "30m16s" in summary


def test_timed_out_summary_explains_that_a_timeout_is_not_a_verdict() -> None:
    summary = summarize(
        [_job("bounded mutation gate", "cancelled", job_id=7)],
        {"7": TIMEOUT_ANNOTATION},
    )
    assert "not a test verdict" in summary or "timed out" in summary.lower()


def test_explicit_timed_out_conclusion_needs_no_annotation() -> None:
    """GitHub sometimes reports `timed_out` directly; it must not fall through to FAILED."""
    summary = summarize([_job("slow job", "timed_out")], {})
    assert "TIMED OUT" in summary
    assert "FAILED" not in summary


# --- AC2: the three non-success outcomes are worded differently ----------------------


def test_cancelled_without_timeout_annotation_is_not_called_a_timeout() -> None:
    summary = summarize([_job("superseded job", "cancelled", job_id=3)], {})
    assert "CANCELLED" in summary
    assert "TIMED OUT" not in summary


def test_failed_job_is_reported_as_failed() -> None:
    summary = summarize([_job("select mutation shards", "failure", job_id=4)], {})
    assert "select mutation shards" in summary
    assert "FAILED" in summary
    assert "TIMED OUT" not in summary


def test_the_three_outcomes_render_distinctly() -> None:
    """Timeout, cancellation and failure must not collapse into one another (the whole bug)."""
    jobs = [
        _job("timed out job", "cancelled", job_id=1),
        _job("superseded job", "cancelled", job_id=2),
        _job("broken job", "failure", job_id=3),
    ]
    summary = summarize(jobs, {"1": TIMEOUT_ANNOTATION})

    lines = {line.split(":", 1)[0]: line for line in summary.splitlines() if line.startswith("- ")}
    assert "- timed out job" in lines
    assert "TIMED OUT" in lines["- timed out job"]
    assert "CANCELLED" in lines["- superseded job"]
    assert "FAILED" in lines["- broken job"]


def test_each_reported_job_carries_its_own_link() -> None:
    summary = summarize([_job("broken job", "failure", job_id=42)], {})
    assert "/job/42" in summary


# --- AC3: a fully green run produces no comment at all ------------------------------


def test_all_successful_jobs_produce_no_summary() -> None:
    jobs = [_job("lint", "success", job_id=1), _job("pytest", "success", job_id=2)]
    assert summarize(jobs, {}) == ""


def test_empty_job_list_produces_no_summary() -> None:
    assert summarize([], {}) == ""


def test_skipped_jobs_are_not_reported() -> None:
    """A skipped job is normal (conditional gates); reporting it would be noise."""
    assert summarize([_job("bounded mutation gate", "skipped")], {}) == ""


def test_successful_jobs_are_omitted_from_a_mixed_run() -> None:
    jobs = [_job("lint", "success", job_id=1), _job("broken job", "failure", job_id=2)]
    summary = summarize(jobs, {})
    assert "broken job" in summary
    assert "lint" not in summary


# --- the summary is embedded in a single-quoted ssh argument -------------------------


def test_a_quote_in_a_job_name_cannot_break_out_of_the_quoting() -> None:
    """Matrix job names come from mutation-shards.toml, which a patchset controls."""
    hostile = "shard'; touch /tmp/pwned; echo '"
    summary = summarize([_job(hostile, "failure", job_id=1)], {})
    assert "'" not in summary


def test_backslashes_are_stripped_from_the_summary() -> None:
    summary = summarize([_job("odd\\name", "failure", job_id=1)], {})
    assert "\\" not in summary


def test_control_characters_are_stripped_but_newlines_survive() -> None:
    summary = summarize([_job("bell\x07job", "failure", job_id=1)], {})
    assert "\x07" not in summary
    assert "\n" in summary


# --- duration formatting -------------------------------------------------------------


@pytest.mark.parametrize(
    ("started", "completed", "expected"),
    [
        ("2026-08-12T03:39:25Z", "2026-08-12T04:09:41Z", "30m16s"),
        ("2026-08-12T04:40:18Z", "2026-08-12T04:40:57Z", "0m39s"),
        ("2026-08-12T04:39:43Z", "2026-08-12T05:00:05Z", "20m22s"),
    ],
)
def test_format_duration(started: str, completed: str, expected: str) -> None:
    assert format_duration(started, completed) == expected


def test_missing_timestamps_do_not_crash() -> None:
    """A still-running or malformed job must not take the whole summary down."""
    job = {"id": 1, "name": "odd job", "conclusion": "failure", "html_url": "u"}
    summary = summarize([job], {})
    assert "odd job" in summary
    assert "FAILED" in summary


# --- the script entrypoint -----------------------------------------------------------


def _run(jobs_json: str, annotations_json: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "JOBS_JSON": jobs_json,
            "ANNOTATIONS_JSON": annotations_json,
        },
        check=False,
    )


def test_entrypoint_accepts_the_list_jobs_payload_shape() -> None:
    """The workflow pipes the GitHub API response straight in, i.e. {"jobs": [...]}."""
    payload = '{"jobs": [{"id": 1, "name": "broken job", "conclusion": "failure"}]}'
    result = _run(payload)
    assert result.returncode == 0
    assert "broken job" in result.stdout


def test_entrypoint_is_silent_on_a_green_run() -> None:
    result = _run('{"jobs": [{"id": 1, "name": "lint", "conclusion": "success"}]}')
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_entrypoint_survives_empty_input() -> None:
    """A failed API read must not turn into a crash in the vote job."""
    result = _run("")
    assert result.returncode == 0
    assert result.stdout.strip() == ""
