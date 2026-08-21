"""Portable reconcile-bridge provider and runner contract tests."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft7Validator

# `_child_diag` lives in `tests/`, which the ROOT `tests/conftest.py` normally puts on
# `sys.path`. That is not enough here: this module is loaded as a `pytest_plugins` entry by a
# NESTED pytest run rooted OUTSIDE `tests/` (tests/unit/test_fixture_env_repr_security.py),
# where the root conftest never loads and a bare import raises ImportError at collection.
# Same explicit bootstrap the sibling oracle uses (tests/unit/test_live_dc_pass_health.py).
_TESTS_DIR = Path(__file__).resolve().parents[2]
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from _child_diag import assert_child_was_not_signal_killed  # noqa: E402

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[3]
_GITHUB = _ROOT / ".github" / "workflows" / "reconcile-bridge.yml"
_CANARY = _ROOT / ".github" / "workflows" / "reconcile-bridge-canary.yml"
_JENKINS = _ROOT / "Jenkinsfile"
_GITLAB = _ROOT / ".gitlab-ci.yml"
_GITLAB_SCHEMA = _ROOT / ".github" / "schemas" / "gitlab-ci.schema.json"
_GITLAB_PROVENANCE = _ROOT / ".github" / "schemas" / "gitlab-ci.schema.provenance.json"


def _workflow(path: Path) -> dict:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _steps(path: Path, job: str) -> list[dict]:
    return _workflow(path)["jobs"][job]["steps"]


def _commands(step: dict) -> list[str]:
    return [
        line.strip()
        for line in str(step.get("run", "")).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _step_index(steps: list[dict], command: str) -> int:
    matches = [index for index, step in enumerate(steps) if command in _commands(step)]
    assert len(matches) == 1, f"expected one workflow step running {command!r}, found {matches}"
    return matches[0]


def test_reconcile_workflows_provision_the_ours_driver_before_delivery() -> None:
    """The canary and primary adapters retain their proven merge-driver ordering."""
    canary = _steps(_CANARY, "canary")
    canary_mount = _step_index(
        canary, "git worktree add -B tickets .tickets-tracker origin/tickets"
    )
    canary_init = _step_index(canary, "rebar init")
    canary_push = next(
        index
        for index, step in enumerate(canary)
        if any("python -m rebar._store.push" in line for line in _commands(step))
    )
    assert canary_mount < canary_init < canary_push
    assert not any(
        "git config merge.ours.driver" in line for step in canary for line in _commands(step)
    )

    production = _steps(_GITHUB, "reconcile")
    mount = _step_index(production, "git worktree add -B tickets .tickets-tracker origin/tickets")
    configure = _step_index(production, "git config merge.ours.driver true")
    runner = _step_index(production, "rebar bridge run")
    assert mount < configure < runner


def test_github_wrapper_delegates_once_without_reimplementing_runner_policy() -> None:
    steps = _steps(_GITHUB, "reconcile")
    matches = [step for step in steps if step.get("name") == "Run reconciler"]
    assert len(matches) == 1
    assert matches[0]["id"] == "reconcile"
    assert matches[0]["run"] == "rebar bridge run"
    workflow_text = _GITHUB.read_text(encoding="utf-8")
    assert 'case "$MODE"' not in workflow_text
    assert "python -m rebar._store.push commit-and-push" not in workflow_text


def test_gitlab_workflow_validates_offline_against_pinned_schema() -> None:
    schema = json.loads(_GITLAB_SCHEMA.read_text(encoding="utf-8"))
    Draft7Validator.check_schema(schema)
    errors = sorted(
        Draft7Validator(schema).iter_errors(_workflow(_GITLAB)),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    assert not errors, "\n".join(error.message for error in errors)

    provenance = json.loads(_GITLAB_PROVENANCE.read_text(encoding="utf-8"))
    digest = hashlib.sha256(_GITLAB_SCHEMA.read_bytes()).hexdigest()
    assert provenance["revision"] == "776c20ad5675eecea0c2010433a94d98d745e921"
    assert provenance["dialect"] == "http://json-schema.org/draft-07/schema#"
    assert provenance["sha256"] == digest


def _github_shell() -> str:
    step = next(
        step for step in _steps(_GITHUB, "reconcile") if step.get("name") == "Run reconciler"
    )
    return str(step["run"])


def _jenkins_shell() -> str:
    blocks = re.findall(r"sh\s+'''\n(.*?)\n\s*'''", _JENKINS.read_text(), re.DOTALL)
    assert blocks
    return "\n".join(textwrap.dedent(block) for block in blocks)


def _gitlab_shell() -> str:
    job = _workflow(_GITLAB)["reconcile_bridge"]
    blocks = [*job.get("before_script", []), *job.get("script", [])]
    assert blocks
    return "\n".join(str(block) for block in blocks)


@pytest.mark.parametrize(
    ("provider", "source"),
    [("github", _github_shell), ("jenkins", _jenkins_shell), ("gitlab", _gitlab_shell)],
)
def test_provider_shell_bodies_pass_pinned_shellcheck(
    tmp_path: Path, provider: str, source: object
) -> None:
    version = subprocess.run(
        ["shellcheck", "--version"], capture_output=True, text=True, check=True
    ).stdout
    assert re.search(r"^version: 0\.11\.0$", version, re.MULTILINE)

    script = tmp_path / f"{provider}.sh"
    body = source()  # type: ignore[operator]
    script.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body + "\n", encoding="utf-8")
    completed = subprocess.run(
        ["shellcheck", "--shell=bash", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def _redispatch_script() -> str:
    """The opt-in continuous-loop re-dispatch step's shell, with Actions templating bound.

    Bug 8aed: this step is the LAST thing an already-converged pass runs, so its failure
    policy decides whether a transient re-dispatch problem reds an otherwise-good run.
    """
    step = next(
        step
        for step in _steps(_GITHUB, "reconcile")
        if str(step.get("name", "")).startswith("Re-dispatch to sustain the continuous loop")
    )
    return str(step["run"]).replace("${{ github.ref_name }}", "main")


def _run_redispatch(
    tmp_path: Path, gh_body: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Execute the re-dispatch step with a stubbed ``gh`` (and ``sleep``) on PATH.

    ``sleep`` is stubbed so the chain-pacing branch records the duration it asked for
    instead of actually waiting. The default environment deliberately omits
    ``PASS_STARTED_EPOCH`` — the step runs under ``set -euo pipefail``, so that absence
    is the guard every pre-existing test in this module silently depends on.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh_stub = bin_dir / "gh"
    gh_stub.write_text("#!/usr/bin/env bash\n" + gh_body + "\n", encoding="utf-8")
    gh_stub.chmod(0o755)
    sleep_stub = bin_dir / "sleep"
    sleep_stub.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$1" >> "{tmp_path / "slept"}"\n', encoding="utf-8"
    )
    sleep_stub.chmod(0o755)

    script = tmp_path / "redispatch.sh"
    script.write_text("#!/usr/bin/env bash\n" + _redispatch_script() + "\n", encoding="utf-8")
    return subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "MODE": "live", **(extra_env or {})},
    )


def _slept(tmp_path: Path) -> list[int]:
    record = tmp_path / "slept"
    if not record.exists():
        return []
    return [int(line) for line in record.read_text(encoding="utf-8").split()]


_DISABLED_422 = (
    'echo "could not create workflow dispatch event: HTTP 422: Cannot trigger a "\n'
    "echo \"'workflow_dispatch' on a disabled workflow\" >&2\n"
    "exit 1"
)


def test_redispatch_treats_a_disabled_workflow_422_as_a_benign_no_op(tmp_path: Path) -> None:
    """A converged pass stays GREEN when the loop cannot re-seed onto a disabled workflow.

    The hourly schedule documented in the workflow header is the backstop, so failing the
    job adds no recovery — only a false red that trips heartbeat alerting (runs
    31129551929 / 31129431096).
    """
    completed = _run_redispatch(tmp_path, _DISABLED_422)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    combined = completed.stdout + completed.stderr
    assert "::warning::" in combined
    assert "::error::" not in combined


def test_redispatch_keeps_every_other_failure_fatal(tmp_path: Path) -> None:
    """The downgrade is narrow: a non-disabled-workflow failure still reds the run."""
    completed = _run_redispatch(
        tmp_path, 'echo "HTTP 403: Resource not accessible by integration" >&2\nexit 1'
    )

    # No completion guard here, deliberately: unlike its sibling below, this oracle also
    # asserts a string is PRESENT (`"::error::" in combined`). A signal-killed child writes
    # nothing, so that assertion cannot pass on an empty capture — the fail-open gap is
    # already closed. The asymmetry is intentional, not an oversight.
    assert completed.returncode != 0
    combined = completed.stdout + completed.stderr
    assert "::error::" in combined
    assert "::warning::" not in combined


def test_redispatch_requires_both_the_422_and_the_disabled_workflow_signal(
    tmp_path: Path,
) -> None:
    """An unrelated HTTP 422 must NOT be swallowed — both tokens are required."""
    completed = _run_redispatch(
        tmp_path, 'echo "HTTP 422: Unprocessable Entity: no ref found" >&2\nexit 1'
    )

    assert completed.returncode != 0
    # `returncode != 0` alone is satisfied by a SIGNAL death (CPython reports -9), and a
    # killed child writes nothing, so the absent-`::warning::` verdict below fails OPEN.
    # This guard restores it without loosening the non-zero requirement above.
    assert_child_was_not_signal_killed(completed, what="the re-dispatch step")
    assert "::warning::" not in completed.stdout + completed.stderr


def test_redispatch_succeeds_quietly_when_the_dispatch_is_accepted(tmp_path: Path) -> None:
    """The happy path stays a silent success — no warning, no error."""
    completed = _run_redispatch(tmp_path, 'echo "queued"\nexit 0')

    assert completed.returncode == 0, completed.stdout + completed.stderr
    combined = completed.stdout + completed.stderr
    assert "::warning::" not in combined
    assert "::error::" not in combined


# --- Chain pacing (ticket f59a-2d16-68c5-450c) ---------------------------------------------
#
# Re-dispatching immediately made the chain's inter-invocation period one pass duration.
# Sleeping for the pass's own elapsed time first makes it two — the doubling the operator
# asked for. These cover the three branches the pacing block can take.

_DISPATCH_OK = "exit 0"


def test_pacing_sleeps_for_the_passs_own_elapsed_duration(tmp_path: Path) -> None:
    """A 100-second pass pauses ~100 seconds, so the chain's period is two pass durations."""
    started = int(time.time()) - 100
    completed = _run_redispatch(tmp_path, _DISPATCH_OK, {"PASS_STARTED_EPOCH": str(started)})

    assert completed.returncode == 0, completed.stdout + completed.stderr
    slept = _slept(tmp_path)
    assert len(slept) == 1, f"expected exactly one pacing sleep, got {slept}"
    assert 100 <= slept[0] <= 130, f"pacing sleep {slept[0]}s does not mirror the ~100s pass"


def test_pacing_is_skipped_once_a_pass_is_already_self_pacing(tmp_path: Path) -> None:
    """Past 1800s the pass paces itself; sleeping would eat the timeout-minutes: 60 budget."""
    started = int(time.time()) - 2000
    completed = _run_redispatch(tmp_path, _DISPATCH_OK, {"PASS_STARTED_EPOCH": str(started)})

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert _slept(tmp_path) == [], "a >=1800s pass must not add a pacing sleep"
    assert "no pacing sleep" in completed.stdout


def test_pacing_is_skipped_when_the_pass_start_stamp_is_missing(tmp_path: Path) -> None:
    """`set -euo pipefail` + arithmetic on an unset variable would abort the whole step.

    The step must degrade to an immediate dispatch instead — that is what keeps every
    other test in this module (which sets no PASS_STARTED_EPOCH) passing unchanged.
    """
    completed = _run_redispatch(tmp_path, _DISPATCH_OK)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert _slept(tmp_path) == []
    assert "chain pacing skipped" in completed.stdout
    assert "::error::" not in completed.stdout + completed.stderr
