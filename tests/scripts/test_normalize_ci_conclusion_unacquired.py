"""A run whose only unsuccessful job never got a runner is no test verdict (20e1-f766-929b-4951).

GitHub cancels a job it cannot place on a hosted runner ("The job was not acquired by Runner of
type hosted even after multiple attempts"). That job reports ``runner_id: 0``, an empty
``runner_name`` and no steps. Inside the reusable ``build-and-test`` workflow the cancelled
matrix cell does not surface as a ``cancelled`` caller result, so the aggregate fell through to
the fail-closed ``failure`` and cast Verified -1 although nothing failed. These tests drive the
normalizer with jobs-API payloads captured from the affected runs and from a genuine
mid-test cancellation and a genuine macOS failure, which must still vote -1.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytestmark = pytest.mark.scripts

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "normalize_ci_conclusion.py"
_FIXTURES = _REPO / "tests" / "fixtures" / "ci_jobs"
_NOT_ACQUIRED_JOB_ID = 126662869315


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("normalize_ci_conclusion_unacquired", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load(name: str) -> Any:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _not_acquired_run() -> dict[str, Any]:
    """A captured run in which every job passed except the never-acquired macOS cell."""
    return _load("run-41855161578-macos-not-acquired.json")


def _run_with_macos_job_replaced_by(fixture: str) -> dict[str, Any]:
    """The same captured run, with the macOS cell swapped for another captured job."""
    run = copy.deepcopy(_not_acquired_run())
    replacement = _load(fixture)
    run["jobs"] = [replacement if job["id"] == _NOT_ACQUIRED_JOB_ID else job for job in run["jobs"]]
    return run


def _vote(
    tmp_path: Path, payload: Any, conclusion: str = "failure", observed: str = "false"
) -> str:
    jobs_file = tmp_path / "jobs.json"
    jobs_file.write_text(json.dumps(payload), encoding="utf-8")
    cp = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        env={
            "CONCLUSION": conclusion,
            "FAILURE_OBSERVED": observed,
            "JOBS_FILE": str(jobs_file),
            "PATH": "/usr/bin:/bin",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert cp.returncode == 0, cp.stderr
    return cp.stdout.strip()


# --- classification of a single captured job ------------------------------------------


def test_captured_not_acquired_job_is_classified_as_never_acquired() -> None:
    job = next(j for j in _not_acquired_run()["jobs"] if j["id"] == _NOT_ACQUIRED_JOB_ID)
    assert _module().runner_never_acquired(job) is True


def test_captured_mid_test_cancellation_is_not_never_acquired() -> None:
    job = _load("job-126547335283-cancelled-mid-test.json")
    assert job["conclusion"] == "cancelled"
    assert _module().runner_never_acquired(job) is False


def test_captured_macos_failure_is_not_never_acquired() -> None:
    assert _module().runner_never_acquired(_load("job-126249875159-macos-failure.json")) is False


# --- the vote the captured runs produce -----------------------------------------------


def test_not_acquired_run_casts_no_verdict(tmp_path: Path) -> None:
    """The four 2026-11-04 runs logged CONCLUSION=failure, FAILURE_OBSERVED=false."""
    assert _vote(tmp_path, _not_acquired_run()) == "cancelled"


def test_mid_test_cancellation_still_votes_failure(tmp_path: Path) -> None:
    run = _run_with_macos_job_replaced_by("job-126547335283-cancelled-mid-test.json")
    assert _vote(tmp_path, run) == "failure"


def test_genuine_macos_failure_still_votes_failure(tmp_path: Path) -> None:
    run = _run_with_macos_job_replaced_by("job-126249875159-macos-failure.json")
    assert _vote(tmp_path, run) == "failure"


def test_not_acquired_plus_another_failure_votes_failure(tmp_path: Path) -> None:
    run = copy.deepcopy(_not_acquired_run())
    run["jobs"].append(_load("job-126249875159-macos-failure.json"))
    assert _vote(tmp_path, run) == "failure"


def test_failure_observed_overrides_the_not_acquired_exemption(tmp_path: Path) -> None:
    assert _vote(tmp_path, _not_acquired_run(), observed="true") == "failure"


def test_only_a_failure_conclusion_is_reclassified(tmp_path: Path) -> None:
    for conclusion, expected in (("success", "success"), ("", "failure"), ("weird", "failure")):
        assert _vote(tmp_path, _not_acquired_run(), conclusion=conclusion) == expected


def test_run_without_any_not_acquired_job_votes_failure(tmp_path: Path) -> None:
    run = copy.deepcopy(_not_acquired_run())
    run["jobs"] = [j for j in run["jobs"] if j["id"] != _NOT_ACQUIRED_JOB_ID]
    assert _vote(tmp_path, run) == "failure"


def test_bare_job_list_is_accepted(tmp_path: Path) -> None:
    assert _vote(tmp_path, _not_acquired_run()["jobs"]) == "cancelled"


# --- fail closed when the jobs evidence is unusable -----------------------------------


@pytest.mark.parametrize("raw", ["", "not json", "{}", '{"jobs": "x"}', "[1, 2]", "null"])
def test_unusable_jobs_file_fails_closed(tmp_path: Path, raw: str) -> None:
    jobs_file = tmp_path / "jobs.json"
    jobs_file.write_text(raw, encoding="utf-8")
    assert _module().normalize("failure", "false", jobs_file=str(jobs_file)) == "failure"


def test_missing_jobs_file_fails_closed(tmp_path: Path) -> None:
    missing = str(tmp_path / "absent.json")
    assert _module().normalize("failure", "false", jobs_file=missing) == "failure"


def test_unset_jobs_file_keeps_the_two_input_contract() -> None:
    assert _module().normalize("failure", "false") == "failure"
    assert _module().normalize("failure", "false", jobs_file="") == "failure"
