"""The branch-health run summary pays back the attribution the schedule gave up.

`main` CI on the GitHub mirror is scheduled, not push-triggered (ticket 03ef-6fb5-158b-4abd),
so one red tick covers every commit since the last green one. The summary the run publishes is
therefore load-bearing: it has to hand the responder a lower bound and a runnable bisect, and
it has to do that on a run that is ALREADY failing — which is exactly the moment a
shell-quoting bug or an unfilled placeholder would be discovered for the first time.

These tests drive the renderer directly (workflow YAML is not reachable from the suite) and
assert observable output: what the summary says, not how it is built.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "main_health_report.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("main_health_report", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def report() -> ModuleType:
    return _load()


_ALL_GREEN = {"build-and-test": "success", "golden-path": "success", "artifact-probe": "success"}
_ONE_RED = {"build-and-test": "failure", "golden-path": "success", "artifact-probe": "success"}


# --- the green path publishes the lower bound BEFORE it is needed -------------------------


def test_green_run_names_its_own_sha_as_the_new_last_known_green(report: ModuleType) -> None:
    out = report.render(ref_name="main", head_sha="abc123def456", jobs=_ALL_GREEN)
    assert "GREEN" in out
    assert "abc123def456" in out
    assert "git bisect" not in out, "a green run has nothing to bisect"


def test_a_cancelled_or_skipped_gate_is_never_reported_as_green(report: ModuleType) -> None:
    for unproven in ("cancelled", "skipped"):
        out = report.render(
            ref_name="main",
            head_sha="abc123def456",
            jobs={**_ALL_GREEN, "golden-path": unproven},
            last_green_sha="0f0f0f0f",
        )
        assert "RED" in out, f"a {unproven} gate is unproven, not passing"


def test_an_empty_job_set_is_not_treated_as_green(report: ModuleType) -> None:
    out = report.render(ref_name="main", head_sha="abc123def456", jobs={})
    assert "GREEN" not in out, "no observed gate results cannot mean 'healthy'"


# --- the red path hands over a runnable bisect ---------------------------------------------


def test_red_run_emits_a_bisect_recipe_bounded_by_both_shas(report: ModuleType) -> None:
    out = report.render(
        ref_name="main",
        head_sha="badbad0000",
        jobs=_ONE_RED,
        last_green_sha="900d900d00",
        last_green_url="https://example.invalid/runs/1",
    )
    assert "RED" in out
    assert "git bisect start badbad0000 900d900d00" in out, (
        "the bisect window must be fully filled in — bad first, then the known-good lower bound"
    )
    assert re.search(r"^git bisect run sh -c '.+'$", out, re.MULTILINE), (
        "the recipe must be a single copy-pasteable `git bisect run` line"
    )
    assert "https://example.invalid/runs/1" in out, "cite where the lower bound came from"
    assert "| build-and-test | failure |" in out, "name which gate failed"


def test_the_bisect_payload_reproduces_ci(report: ModuleType) -> None:
    out = report.render(ref_name="main", head_sha="bad", jobs=_ONE_RED, last_green_sha="good")
    # A bisect that runs something other than what CI ran converges on the wrong commit.
    for fragment in ("uv sync --locked --extra dev", "make check", "make test"):
        assert fragment in out, f"the bisect payload must run {fragment!r}, as CI does"
    [recipe] = [line for line in out.splitlines() if line.startswith("git bisect run ")]
    assert recipe.count("'") == 2 and recipe.endswith("'"), (
        f"the payload must sit in exactly one balanced single-quoted argument: {recipe!r}"
    )
    assert "'" not in recipe[recipe.index("'") + 1 : recipe.rindex("'")], (
        "a single quote inside the payload would terminate the sh -c argument early"
    )


# --- graceful degradation: a missing lower bound must not fail the run ---------------------


def test_missing_last_known_green_degrades_instead_of_raising(report: ModuleType) -> None:
    out = report.render(ref_name="main", head_sha="badbad0000", jobs=_ONE_RED, last_green_sha="")
    assert "RED" in out
    assert report.NO_LOWER_BOUND in out, (
        "an unresolvable lower bound must be a VISIBLE placeholder — a plausible-looking wrong "
        "SHA would send the bisect through commits that were never green"
    )
    assert "by hand" in out, "the reader must be told the bound is theirs to choose"
    assert "git bisect start badbad0000" in out, "the recipe is still offered, minus its bound"


# --- resolving the lower bound: every failure mode must fail SOFT --------------------------


def _runner(returncode: int = 0, stdout: str = "", stderr: str = "") -> object:
    def run(argv: list[str]) -> tuple[int, str, str]:
        run.argv = argv  # type: ignore[attr-defined]
        return returncode, stdout, stderr

    return run


_ONE_RUN = json.dumps(
    {"workflow_runs": [{"head_sha": "900d900d00", "html_url": "https://example.invalid/r/1"}]}
)


def test_resolve_reads_the_newest_success_for_this_workflow_and_branch(
    report: ModuleType,
) -> None:
    run = _runner(stdout=_ONE_RUN)
    sha, url = report.resolve_last_green(run, repo="o/r", workflow_file="test.yml", branch="main")
    assert (sha, url) == ("900d900d00", "https://example.invalid/r/1")
    endpoint = run.argv[2]  # type: ignore[attr-defined]
    assert "repos/o/r/actions/workflows/test.yml/runs" in endpoint
    assert "status=success" in endpoint and "branch=main" in endpoint, (
        "a run that was not successful, or was on another branch, is not a lower bound"
    )


@pytest.mark.parametrize(
    ("case", "run_kwargs"),
    [
        # 403 is the one that bites: the job needs `actions: read`, and without it the lookup
        # fails on EVERY run rather than visibly once.
        ("api error (403/5xx/rate limit/timeout)", {"returncode": 1, "stderr": "HTTP 403"}),
        ("empty history", {"stdout": '{"workflow_runs": []}'}),
        ("malformed body", {"stdout": "not json at all"}),
        ("unexpected shape", {"stdout": '{"unexpected": true}'}),
    ],
)
def test_every_lookup_failure_mode_fails_soft(
    report: ModuleType, case: str, run_kwargs: dict[str, object]
) -> None:
    sha, url = report.resolve_last_green(
        _runner(**run_kwargs), repo="o/r", workflow_file="test.yml", branch="main"
    )
    assert (sha, url) == ("", ""), f"{case} must yield no bound rather than raise or invent one"


# --- the CLI seam the workflow actually invokes --------------------------------------------


def test_cli_resolves_the_bound_and_exits_zero(
    report: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = report.main(
        [
            "--ref-name",
            "main",
            "--head-sha",
            "badbad0000",
            "--jobs",
            json.dumps(_ONE_RED),
            "--repo",
            "o/r",
        ],
        runner=_runner(stdout=_ONE_RUN),
    )
    assert rc == 0, "the report must never fail a run it is only describing"
    assert "git bisect start badbad0000 900d900d00" in capsys.readouterr().out


def test_cli_still_exits_zero_when_the_lookup_fails(
    report: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = report.main(
        [
            "--ref-name",
            "main",
            "--head-sha",
            "badbad0000",
            "--jobs",
            json.dumps(_ONE_RED),
            "--repo",
            "o/r",
        ],
        runner=_runner(returncode=1, stderr="HTTP 403"),
    )
    assert rc == 0, "a failed lower-bound lookup must never turn a report into a red job"
    assert report.NO_LOWER_BOUND in capsys.readouterr().out
