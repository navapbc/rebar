"""CI parity: every gate that runs against `main` post-merge must also gate pre-merge.

The Gerrit `Verified` vote is cast by ``.github/workflows/gerrit-verify.yaml`` BEFORE a change
can land; ``.github/workflows/test.yml`` runs the same gates AFTER the fact on the pushed `main`.
A gate present only in ``test.yml`` can *only* fail post-merge, letting a red change land with a
green Verified vote (this is exactly how a stale ``docs/env-vars.md`` reached `main`).

The two lanes used to hand-copy the gate+suite step list, and these tests grepped both files to
catch drift. That copy is now factored into ONE reusable workflow, ``_build-and-test.yml``, which
BOTH callers invoke (the branch-head lane via ``test.yml``, the patchset/Verified lane via
``gerrit-verify.yaml``) — so drift is impossible by construction, the same way ``_optionality.yml``
is shared. These tests therefore assert the new invariant: (1) both callers delegate to the shared
reusable, (2) every gate/script lives in that reusable so it gates both lanes, and (3) the
independent safety properties the old file also enforced — the pre-commit SKIP set and the
optionality wiring — are preserved, now checked against the reusable that owns those steps.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
from _nested_pytest import run_nested_pytest
from _subprocess_env import subprocess_env

_ROOT = Path(__file__).resolve().parents[2]
_TEST_YML = _ROOT / ".github" / "workflows" / "test.yml"
_GERRIT_YML = _ROOT / ".github" / "workflows" / "gerrit-verify.yaml"
_OPTIONALITY_YML = _ROOT / ".github" / "workflows" / "optionality.yml"
_OPTIONALITY_REUSABLE_YML = _ROOT / ".github" / "workflows" / "_optionality.yml"
_DOCS_ACTION = _ROOT / ".github" / "actions" / "docs-gates" / "action.yml"
# The job that replaced the 5-cell `per-extra` matrix + the `union` job with one venv loop
# (6 concurrent slots -> 1, under the org-wide 20-job ceiling).
_OPTIONALITY_LOOP_JOB = "optional-extras"
_BAT_YML = _ROOT / ".github" / "workflows" / "_build-and-test.yml"
_HISTORICAL_C57057F41 = _ROOT / "tests" / "fixtures" / "ci_workflow_parity" / "c57057f41"
_HISTORICAL_C57057F41_BAT = _HISTORICAL_C57057F41 / "_build-and-test.yml"
_HISTORICAL_C57057F41_TEST = _HISTORICAL_C57057F41 / "test.yml"
_PRECOMMIT_CONFIG = _ROOT / ".pre-commit-config.yaml"
_REUSABLE_OPTIONALITY = "./.github/workflows/_optionality.yml"
# The reusable gate+suite workflow both CI lanes now delegate to (this refactor). Its presence
# in BOTH caller files is what makes the two lanes share one definition — no drift by construction.
_REUSABLE_BAT = "./.github/workflows/_build-and-test.yml"
_REUSABLE_MUTATION = "./.github/workflows/_mutation.yml"
_BAT_TEST_INSTALL_STEP = "Install rebar from the committed lock (dev + reviewbot + ui extras)"
_SWEEP_INSTALL_STEP = "Create the interpreter venv and install"
_INTENTIONAL_SWEEP_EXTRA_DIVERGENCES = {
    "grounding-terraform": (
        "The interpreter sweep is intentionally lean; the grounding-terraform "
        "extra is covered by the Verified gate and optionality lane."
    )
}

# Pre-commit hooks whose PASS/FAIL depends on the current git branch / HEAD state rather
# than on file content. `pre-commit run --all-files` therefore behaves DIFFERENTLY between
# the two CI runners even when they run the identical command: gerrit-verify.yaml checks the
# change out at a `refs/changes/*` ref (detached HEAD, hook silent) while test.yml runs on the
# pushed `main` branch (hook fires). Because both lanes now run the SINGLE pre-commit step in
# the reusable, each such hook MUST be listed in that step's `SKIP` — otherwise the identical
# command reddens post-merge branch CI on `main` while the pre-merge Verified gate stays green
# (bug `pillared-doddering-fawn`). The "don't commit to main" intent is enforced server-side by
# the GitHub ruleset + Gerrit votes, not by this CI hook.
_BRANCH_SENSITIVE_HOOKS = {"no-commit-to-branch"}

# Pre-commit hooks that are REDUNDANT in CI because they only re-invoke a check that already
# runs as its own named step in the reusable. The local `lint`/`typecheck` hooks shell out to
# `make lint` / `make typecheck`, which the reusable runs directly; letting `pre-commit run
# --all-files` run them again would execute each linter TWICE per job. They must therefore be
# listed in the reusable's pre-commit `SKIP` so each linter runs exactly once (the direct steps).
_CI_REDUNDANT_HOOKS = {"lint", "typecheck"}


# Scripts referenced by a gate in the branch-CI lane that are DELIBERATELY not run in the
# Verified lane. Empty by design: a derive-and-diff drift gate (scripts/gen_*.py / check_*.py)
# that gates `main` must also gate pre-merge, or a broken artifact lands green. Add an entry
# only with a written reason.
_INTENTIONAL_SCRIPT_ASYMMETRIES: set[str] = {
    # NOT a gate — it cannot fail a build, so there is nothing for the Verified vote to
    # mirror. It renders the branch-health run summary (last known-green SHA + bisect recipe)
    # for a run that has ALREADY finished, and only on the 6-hourly schedule / manual dispatch,
    # never on the push/PR critical path. Its whole input is `needs.<gate>.result`, and the
    # Verified lane has no equivalent to describe: Gerrit's vote is per-patchset, so it is
    # attributable to one commit and needs no bisect window (ticket 03ef-6fb5-158b-4abd).
    "scripts/main_health_report.py",
}


def _read(path: Path) -> str:
    assert path.exists(), f"workflow not found: {path}"
    return path.read_text()


def _historical_c57057f41_workflows() -> tuple[str, str]:
    return _read(_HISTORICAL_C57057F41_BAT), _read(_HISTORICAL_C57057F41_TEST)


def _extras_from_install_run(run: str) -> set[str]:
    """Extract Python extras from the uv install spellings used by CI."""
    extras = set(re.findall(r"--extra\s+([A-Za-z0-9_-]+)", run))
    for bracketed in re.findall(r"\.\[([A-Za-z0-9_, -]+)\]", run):
        extras.update(part.strip() for part in bracketed.split(",") if part.strip())
    return extras


def _step_run(workflow_text: str, job_name: str, step_name: str) -> str:
    import yaml

    workflow = yaml.safe_load(workflow_text)
    job = (workflow.get("jobs") or {}).get(job_name)
    assert job, f"workflow is missing job {job_name!r}"
    matches = [
        str(step.get("run") or "")
        for step in (job.get("steps") or [])
        if step.get("name") == step_name
    ]
    assert len(matches) == 1, (
        f"expected exactly one {step_name!r} step in job {job_name!r}; found {len(matches)}"
    )
    return matches[0]


def _assert_sweep_extra_parity(
    build_and_test_yml: str,
    test_yml: str,
    *,
    intentional_gating_only: dict[str, str],
) -> None:
    gating = _extras_from_install_run(_step_run(build_and_test_yml, "test", _BAT_TEST_INSTALL_STEP))
    sweep = _extras_from_install_run(_step_run(test_yml, "sweep-interpreters", _SWEEP_INSTALL_STEP))
    allowed = set(intentional_gating_only)
    unexpected_gating_only = sorted((gating - sweep) - allowed)
    stale_declarations = sorted(allowed - (gating - sweep))
    sweep_only = sorted(sweep - gating)
    problems = []
    if unexpected_gating_only:
        problems.append(
            "gating-only extra(s) missing from sweep-interpreters and not declared intentional: "
            + ", ".join(unexpected_gating_only)
        )
    if sweep_only:
        problems.append(
            "sweep-only extra(s) absent from the Verified gating lane: " + ", ".join(sweep_only)
        )
    if stale_declarations:
        problems.append(
            "stale intentional gating-only declaration(s): " + ", ".join(stale_declarations)
        )
    assert not problems, "; ".join(problems)


def test_both_lanes_delegate_to_the_shared_gate_workflow() -> None:
    """The anti-drift invariant: both the branch-head lane (test.yml) and the Verified lane
    (gerrit-verify.yaml) run the gate+suite by invoking the SAME reusable workflow, so their
    checks cannot diverge. This replaces the old "grep both files for identical step text"."""
    test_yml = _read(_TEST_YML)
    gerrit_yml = _read(_GERRIT_YML)
    assert _REUSABLE_BAT in test_yml, (
        f"test.yml no longer delegates to the shared gate workflow ({_REUSABLE_BAT}) — the "
        "post-merge branch CI would drift from the Verified gate. Call the reusable, don't inline."
    )
    assert _REUSABLE_BAT in gerrit_yml, (
        f"gerrit-verify.yaml no longer delegates to the shared gate workflow ({_REUSABLE_BAT}) — "
        "the Verified gate would drift from branch CI. Call the reusable, don't inline the gates."
    )


def test_sweep_install_extra_drift_is_declared_and_actionable() -> None:
    """Sweep-lane extra drift must be intentional and named."""
    _assert_sweep_extra_parity(
        _read(_BAT_YML),
        _read(_TEST_YML),
        intentional_gating_only=_INTENTIONAL_SWEEP_EXTRA_DIVERGENCES,
    )

    divergent_bat = _read(_BAT_YML).replace(
        "--extra dev --extra reviewbot --extra ui",
        "--extra dev --extra reviewbot --extra ui --extra example",
    )
    try:
        _assert_sweep_extra_parity(divergent_bat, _read(_TEST_YML), intentional_gating_only={})
    except AssertionError as exc:
        message = str(exc)
    else:
        raise AssertionError("undeclared gating-only extra did not fail")
    assert "example" in message
    assert "gating-only" in message
    assert "sweep-interpreters" in message


def test_sweep_extra_parity_would_have_caught_c57057f41() -> None:
    """The known historical workflow drift fails without an in-tree declaration."""
    historical_bat, historical_test = _historical_c57057f41_workflows()

    try:
        _assert_sweep_extra_parity(
            historical_bat,
            historical_test,
            intentional_gating_only={},
        )
    except AssertionError as exc:
        message = str(exc)
    else:
        raise AssertionError("c57057f41's undeclared extra drift did not fail")
    assert "grounding-terraform" in message
    assert "gating-only" in message


def test_module_size_gate_retries_trusted_main_fetch_before_failing_closed() -> None:
    """A transient trusted-main fetch failure must not redden Verified on the first try."""
    body = _step_run(_read(_BAT_YML), "test", "Module-size gate")

    assert "for attempt in 1 2 3; do" in body
    assert "fetch_main_rc=0" in body
    assert "sleep 2" in body
    assert 'if [ "$fetch_main_rc" -eq 0 ]; then' in body
    assert "could not fetch main to verify the module-size limit lock; failing closed" in body
    assert body.index("for attempt in 1 2 3; do") < body.index(
        "could not fetch main to verify the module-size limit lock; failing closed"
    )


def test_mutation_selector_skips_pre_gate_trees_without_weakening_current_trees(
    tmp_path: Path,
) -> None:
    """Definition/tree skew skips only when the patchset lacks the selector script."""
    import shlex
    import subprocess
    import sys

    import yaml

    workflow = yaml.safe_load(_read(_ROOT / ".github" / "workflows" / "_mutation.yml"))
    selector_steps = workflow["jobs"]["selector"]["steps"]
    selector = next(step for step in selector_steps if step.get("id") == "select")
    run = str(selector["run"])

    absent_tree = tmp_path / "predates-mutation-gate"
    absent_tree.mkdir()
    absent_output = absent_tree / "github-output"
    absent = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", run],
        cwd=absent_tree,
        env=subprocess_env({"GITHUB_OUTPUT": str(absent_output)}),
        capture_output=True,
        text=True,
        check=False,
    )

    assert absent.returncode == 0, absent.stdout + absent.stderr
    assert "change predates the gate" in absent.stdout
    assert absent_output.read_text(encoding="utf-8").splitlines() == [
        'matrix={"shard":[]}',
        "has-shards=false",
    ]

    current_tree = tmp_path / "current-tree"
    (current_tree / "scripts").mkdir(parents=True)
    marker = current_tree / "selector-ran"
    (current_tree / "scripts" / "mutation_gate.py").write_text(
        f"import pathlib, sys\npathlib.Path({str(marker)!r}).write_text('ran')\nsys.exit(17)\n",
        encoding="utf-8",
    )
    current_output = current_tree / "github-output"
    executable_run = run.replace("${{ inputs.all-shards }}", "false").replace(
        "uv run --locked python", shlex.quote(sys.executable)
    )
    current = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", executable_run],
        cwd=current_tree,
        env=subprocess_env({"GITHUB_OUTPUT": str(current_output)}),
        capture_output=True,
        text=True,
        check=False,
    )

    assert marker.read_text(encoding="utf-8") == "ran"
    assert current.returncode == 17, current.stdout + current.stderr


def test_degradation_step_runs_only_the_present_test_files(tmp_path: Path) -> None:
    """Each degradation-path test file is guarded on ITS OWN presence (d93e-92ce-e2d0-4033).

    All four presence combinations — the property a joint guard would break (bug
    ``5bc6-5496-4e17-403a``): an absent file skips with the canonical message, a present one
    still reaches pytest, so a tree with one file and not the other still runs the one it has.
    The clean-lane no-agents-extra assertion must also still run whenever pytest does.
    """
    import subprocess

    import yaml

    workflow = yaml.safe_load(_read(_OPTIONALITY_REUSABLE_YML))
    steps = workflow["jobs"]["clean-core-wheel"]["steps"]
    step = next(
        s for s in steps if s.get("name") == "degradation-path tests run with NO agents extra"
    )
    run = str(step["run"])

    optionality = "tests/interfaces/store/test_llm_optionality.py"
    completion = "tests/interfaces/lifecycle/test_completion_gate.py"
    node_id = {
        optionality: optionality,
        completion: f"{completion}::test_gate_missing_llm_fails_closed",
    }

    combos: tuple[tuple[str, ...], ...] = (
        (),
        (optionality,),
        (completion,),
        (optionality, completion),
    )
    for index, present in enumerate(combos):
        tree = tmp_path / f"combo-{index}"
        stub_bin = tree / "stub-bin"
        stub_bin.mkdir(parents=True)
        calls = tree / "calls.log"
        for tool in ("pip", "python"):
            stub = stub_bin / tool
            stub.write_text(
                f"#!/usr/bin/env bash\nprintf '%s\\n' \"{tool} $*\" >> {str(calls)!r}\n",
                encoding="utf-8",
            )
            stub.chmod(0o755)
        for path in present:
            target = tree / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("def test_placeholder() -> None: ...\n", encoding="utf-8")

        result = subprocess.run(
            [
                "bash",
                "--noprofile",
                "--norc",
                "-eo",
                "pipefail",
                "-c",
                run.replace("/tmp/clean/bin/", f"{stub_bin}/"),
            ],
            cwd=tree,
            env=subprocess_env({}),
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"{present}: {result.stdout}{result.stderr}"

        for path in (optionality, completion):
            skipped = f"{path} not in checked-out tree" in result.stdout
            assert skipped == (path not in present), (
                f"{present}: expected the canonical skip message for exactly the absent "
                f"files; {path} {'was' if skipped else 'was not'} reported skipped.\n"
                f"{result.stdout}"
            )

        logged = calls.read_text(encoding="utf-8") if calls.exists() else ""
        pytest_lines = [line for line in logged.splitlines() if "-m pytest" in line]
        if not present:
            assert logged == "", (
                f"a fully pre-gate tree must do no work at all, but the step ran: {logged}"
            )
            continue
        assert len(pytest_lines) == 1, f"{present}: expected one pytest run, got: {logged}"
        assert "agents_extra_installed" in logged, (
            f"{present}: the clean-lane agents-extra-ABSENT assertion no longer runs: {logged}"
        )
        for path in (optionality, completion):
            selected = node_id[path] in pytest_lines[0]
            assert selected == (path in present), (
                f"{present}: pytest must run exactly the present files, got: {pytest_lines[0]}"
            )


def test_shared_pytest_job_has_incident_timeout() -> None:
    """A blocked pytest worker must not occupy a matrix slot until GitHub's 6-hour default.

    Both the Gerrit Verified lane and post-merge branch CI consume this reusable job, so the
    bound belongs here rather than in either caller.  Sixty minutes leaves more than 3x the
    workflow's documented 10–16 minute macOS runtime while making hangs self-clearing.
    """
    import yaml

    workflow = yaml.safe_load(_read(_BAT_YML))
    test_job = (workflow.get("jobs") or {}).get("test")
    assert test_job, "_build-and-test.yml no longer defines the shared pytest matrix job"
    assert test_job.get("timeout-minutes") == 60, (
        "_build-and-test.yml jobs.test must set timeout-minutes: 60 so a blocked pytest "
        "worker cannot retain each hosted matrix slot for GitHub's 360-minute default"
    )


# Matches a pytest invocation in COMMAND position on one shell line: start-of-line or a
# `|`/`&&`/`;` separator, an optional path prefix, then either `python -m pytest` or `pytest`.
# Deliberately independent of `-m` and of `--dist` — the predicate this replaced required both,
# which made it blind to every non-xdist lane AND to the `python -m pytest` form. The
# command-boundary requirement is what keeps `pip install pytest pytest-timeout` (where pytest
# is an ARGUMENT) from being mistaken for a pytest lane.
# re.MULTILINE is load-bearing, not decoration: a run block is multi-line and the invocation is
# rarely its first line (_optionality.yml runs pip install, then a python -c probe, and only
# then pytest). Without it `^` would anchor to the start of the whole block and miss that lane
# entirely — which the liveness test below caught while this guard was being written.
_PYTEST_INVOCATION = re.compile(
    r"(?:^|\|\s*|&&\s*|;\s*)\s*(?:[\w./-]*/)?(?:python[\d.]*\s+-m\s+pytest|pytest)\b",
    re.MULTILINE,
)

# Every invocation FORM that exists in this repo's workflows, each keyed to a lane that uses it.
# Asserted as a subset of what the sweep discovers, so a future refactor that breaks the matcher
# fails loudly instead of passing vacuously over an empty lane set. This is a liveness floor,
# NOT an allowlist: a new lane needs no entry here, it just has to carry the guard.
_EXPECTED_PYTEST_FORMS = {
    "_build-and-test.yml": 'pytest -m "$DEFAULT_SUITE_MARKS"',  # bare, with -m
    "structured-output-baseline.yml": "pytest tests/external/",  # bare, NO -m
    "_eval-discipline.yml": "python -m pytest",  # module form
    "_optionality.yml": "/tmp/clean/bin/python -m pytest",  # path-prefixed module form
}


def _all_workflow_pytest_steps() -> list[tuple[str, str, str]]:
    """Every ``run`` block in .github/workflows that invokes pytest, as (file, step name, body).

    Shell comment lines are dropped before matching: ``_build-and-test.yml`` carries a
    ``# --timeout (pytest-timeout) ...`` comment that names pytest without invoking it, and a
    substring matcher would demand a hang guard on the comment's step regardless of its command.
    """
    import yaml

    found: list[tuple[str, str, str]] = []
    for path in sorted((_ROOT / ".github" / "workflows").iterdir()):
        if path.suffix not in {".yml", ".yaml"}:
            continue
        workflow = yaml.safe_load(_read(path)) or {}
        for job in (workflow.get("jobs") or {}).values():
            for step in (job or {}).get("steps") or []:
                body = str((step or {}).get("run", ""))
                if not body:
                    continue
                live = "\n".join(
                    line for line in body.splitlines() if not line.lstrip().startswith("#")
                )
                if _PYTEST_INVOCATION.search(live):
                    found.append((path.name, str((step or {}).get("name", "<unnamed>")), body))
    assert found, "the pytest-invocation sweep found NO lanes at all — the matcher is broken"
    return found


def test_pytest_invocation_sweep_sees_every_form() -> None:
    """The sweep's liveness floor: it must still find a lane of each invocation form present.

    Without this, a regression in ``_PYTEST_INVOCATION`` that stopped matching (say) the
    ``python -m pytest`` form would leave the guard below asserting over a silently smaller set
    and passing — the precise failure mode of the predicate this replaced, which required
    ``--dist`` and so never saw a single one of the eight non-xdist lanes.
    """
    discovered = _all_workflow_pytest_steps()
    for filename, snippet in _EXPECTED_PYTEST_FORMS.items():
        assert any(name == filename and snippet in body for name, _step, body in discovered), (
            f"the pytest-invocation sweep no longer detects {snippet!r} in {filename} — the "
            "matcher regressed and lanes are going unchecked. Fix _PYTEST_INVOCATION; do not "
            "relax this expectation."
        )


def test_pip_install_of_pytest_is_not_mistaken_for_a_lane() -> None:
    """`pip install pytest ...` names pytest but does not RUN it — it must not demand a guard.

    ``_optionality.yml`` installs pytest (and pytest-timeout) by name into its minimal clean
    venv. A matcher keyed on the bare substring would flag that install step as an unguarded
    pytest lane and force a nonsensical ``--timeout`` onto pip.
    """
    for line in (
        "          /tmp/clean/bin/pip install pytest pytest-timeout jsonschema 'mcp>=1.28.1,<2'",
        "          python -m pip install pytest",
    ):
        assert not _PYTEST_INVOCATION.search(line), (
            f"_PYTEST_INVOCATION matched an INSTALL line, not an invocation: {line!r}. The "
            "command-boundary requirement is what separates the two; do not drop it."
        )


def test_every_ci_pytest_lane_has_a_per_test_hang_guard() -> None:
    """A single deadlocked test must name itself in seconds, not eat the whole job.

    ``timeout-minutes`` — where a job even declares it — bounds only the WHOLE job: a test that
    blocks forever under ``-q`` stays invisible behind the last dot and is killed much later
    with no traceback and no culprit named, which is exactly the ubuntu/py3.11 incident (ticket
    ``89d5-61da-b621-47f8``). ``pytest-timeout``'s ``--timeout`` is that instrument.

    ``--timeout-method=thread`` is the load-bearing half: the default ``signal`` method arms
    ``SIGALRM`` in the main thread and cannot fire while a worker is blocked in a C-level
    syscall (a stuck ``fcntl.flock``, socket ``recv``, or ``subprocess`` pipe) — precisely the
    hang shapes these suites are full of, and the ONLY shape the live-service lanes have. The
    ``thread`` watchdog dumps every thread's stack and aborts the worker, so a recurrence points
    straight at the offending test.

    This sweeps EVERY workflow rather than just the shared reusable (ticket
    ``d835-1846-95d1-4d84``): the guard originally shipped only in ``_build-and-test.yml``, and
    the eight other lanes — the live-LLM matrix, live Jira Cloud, the Langfuse round trip, the
    Jira DC harness, the structured-output sweep, ``test.yml``'s external tier, and the two
    offline lanes — each went unguarded because nothing asserted over them. Budgets are
    per-lane and calibrated from observed runtime (see each step's own comment); this asserts
    only that a budget EXISTS, so a new lane cannot ship without one.

    EXCEPTION: the ``_build-and-test.yml`` shared-tier lanes satisfy the guard WITHOUT an
    explicit ``--timeout=`` flag by inheriting the ``[tool.pytest.ini_options]`` budget
    (ticket ``5a30-d423-b1f5-4e33``): they run pytest from this repo's root, so the ini keys
    apply, and a CLI flag would OVERRIDE them — which is exactly how a stale tighter flag on
    trusted ``main`` killed workers on every Verified run and made the raising patchset unable
    to pass its own gate (bug ``3fa7-94ba-42aa-4623``; the Verified gate executes workflow
    files from trusted main, not the patchset). The ini fallback is asserted REAL below
    (budget present and positive, ``thread`` method), and the negative arms in
    ``test_timeout_budget.py`` prove those keys actually expire a hung test. Every OTHER
    workflow's lanes must still carry an explicit flag — several run against generated
    projects or from a clean venv where this repo's ini is not reliably in play, and each
    carries its own calibrated budget — and a lane that sets a flag must use the ``thread``
    method.
    """
    import tomllib

    ini = tomllib.loads(_read(_ROOT / "pyproject.toml"))["tool"]["pytest"]["ini_options"]
    assert float(ini.get("timeout", 0)) > 0, (
        "[tool.pytest.ini_options].timeout is absent or zero — the ini fallback that guards "
        "the flag-free _build-and-test.yml lanes is not real, so those lanes are unguarded"
    )
    assert ini.get("timeout_method") == "thread", (
        "[tool.pytest.ini_options].timeout_method must be 'thread' for the ini fallback to "
        "interrupt a worker blocked in a C-level flock/socket/subprocess call"
    )
    for filename, step, body in _all_workflow_pytest_steps():
        where = f"{filename} step {step!r}"
        if filename == "_build-and-test.yml" and "--timeout=" not in body:
            # Single-sourced from the ini keys asserted above (a flag would override them
            # and reopen bug 3fa7-94ba-42aa-4623); the flag-free shape is enforced by
            # test_the_shared_tier_lanes_are_flag_free_and_governed_by_the_ini below.
            continue
        assert "--timeout=" in body, (
            f"{where} invokes pytest with no `--timeout=<seconds>` per-test hang guard: a "
            "deadlocked test is invisible under -q until the job cap, with no traceback "
            "(ticket 89d5-61da-b621-47f8). Add pytest-timeout's --timeout, sized from this "
            f"lane's observed runtime rather than copied:\n{body}"
        )
        assert "--timeout-method=thread" in body, (
            f"{where} sets --timeout without --timeout-method=thread; the default `signal` "
            "method cannot interrupt a worker blocked in a C-level flock/socket/subprocess "
            f"call, so the hang would still go silent. Use the thread method:\n{body}"
        )


def test_the_shared_tier_lanes_are_flag_free_and_governed_by_the_ini() -> None:
    """`_build-and-test.yml`'s two tiers must carry NO ``--timeout``/``--timeout-method`` flags.

    The budget lives in ``[tool.pytest.ini_options]`` (story ``73b8-2d79-3046-44ce``) so `make
    test` and a bare local run are guarded too. pytest-timeout precedence is CLI > env > ini,
    so a command-line ``--timeout=`` OVERRIDES the ini — and because the Gerrit Verified gate
    executes workflow files from trusted ``main``, not the patchset, any flag here permanently
    shadows a patchset's ini change: bug ``3fa7-94ba-42aa-4623``'s budget fix could never be
    exercised by its own Verified run (change 2115 PS1 ran main's stale ``--timeout=20`` and
    killed its own workers). Single-sourcing makes that drift IMPOSSIBLE rather than
    checked-for: the lanes carry no flag and the ini — which travels WITH a patchset — is the
    one authority (ticket ``5a30-d423-b1f5-4e33``).

    Scope is deliberately these two lanes only. The other ``--timeout=`` lanes
    (``external-integration.yml``, ``_optionality.yml``, ``_eval-discipline.yml``, ``test.yml``,
    ``structured-output-baseline.yml``) drive live services, clean-venv installs and eval
    suites whose per-test runtime is legitimately minutes; each carries its own calibrated
    budget and is asserted only by the hang-guard sweep above.
    """
    import tomllib

    ini = tomllib.loads(_read(_ROOT / "pyproject.toml"))["tool"]["pytest"]["ini_options"]
    assert float(ini.get("timeout", 0)) > 0, (
        "[tool.pytest.ini_options].timeout is absent or zero: with the shared-tier lanes "
        "deliberately flag-free, the ini is the ONLY per-test hang guard they have"
    )
    lanes = [
        (step, body)
        for filename, step, body in _all_workflow_pytest_steps()
        if filename == "_build-and-test.yml"
    ]
    # Named rather than counted, so adding a lane is a deliberate edit here rather than
    # a number that silently drifts.
    assert {step for step, _ in lanes} == {
        "Run the default suite (Python tiers; excludes integration + external)",
        "Run the integration tier (concurrency + reconciler; network-guarded)",
        "Run the sandbox enforcement tests under a real bwrap",
    }, (
        "unexpected pytest lane set in _build-and-test.yml; every lane must stay "
        f"flag-free so the ini remains the only hang guard. found {[s for s, _ in lanes]}"
    )
    for step, body in lanes:
        found = re.findall(r"--timeout(?:-method)?=(\S+)", body)
        assert not found, (
            f"_build-and-test.yml step {step!r} sets --timeout/--timeout-method ({found}) on "
            "the command line. These lanes are single-sourced from "
            "[tool.pytest.ini_options] (ticket 5a30-d423-b1f5-4e33): a CLI flag OVERRIDES the "
            "ini, reopens budget drift between CI and a local run, and — because the Verified "
            "gate runs trusted-main workflow files — a stale flag here undercuts every "
            "patchset's budget including the one trying to fix it (bug 3fa7-94ba-42aa-4623). "
            "Change the ini instead."
        )


def test_integration_lane_fails_fast_on_worker_crash_and_worksteal_lane_does_not() -> None:
    """The loadgroup lane must carry ``--max-worker-restart=0``; the worksteal lane must NOT.

    Under ``--dist loadgroup`` (and loadscope) a dead worker's pending workload — INCLUDING
    the item that crashed it — is re-dispatched to respawned replacement workers, which die on
    the same item, up to xdist's default restart cap of ``numprocesses * 4`` (16 here). Each
    cycle re-pays session setup, so a single crashing test burns toward the 60-minute job
    ceiling instead of failing in seconds (bug ``3fa7``'s RCA; upstream pytest-dev/pytest-xdist
    #1094/#1189, whose ``docs/crash.rst`` sanctions ``=0``). ``--max-worker-restart=0`` makes
    the first worker death abort the whole session immediately and cleanly (triggershutdown).

    Exactly ``=0`` rather than a small cap: the deterministic cause of worker deaths (the
    over-tight 20s budget) was fixed by the budget restore (change 2115, ``--timeout=300``),
    so any residual worker death is UNEXPECTED and should fail fast and loudly, not be
    absorbed by a retry allowance that hides it.

    The worksteal (default-suite) lane must NOT get the flag: worksteal never re-dispatches
    the crashing item, so xdist's default restart allowance there only replaces lost capacity
    and lets the rest of the suite finish — one crash still fails the run, with more signal.
    """
    lanes = {
        ("loadgroup" if "--dist loadgroup" in body else "worksteal"): (step, body)
        for filename, step, body in _all_workflow_pytest_steps()
        if filename == "_build-and-test.yml"
    }
    assert set(lanes) == {"loadgroup", "worksteal"}, (
        "expected exactly one loadgroup (integration) and one worksteal (default-suite) lane "
        f"in _build-and-test.yml; found {sorted(lanes)}"
    )
    step, body = lanes["loadgroup"]
    assert "--max-worker-restart=0" in body, (
        f"_build-and-test.yml step {step!r} runs `--dist loadgroup` without "
        "`--max-worker-restart=0`: on a worker crash, loadgroup re-dispatches the crashing "
        "item to respawned workers up to numprocesses*4 restarts, re-paying session setup "
        "each cycle and burning toward the 60-min job cap (bug 3fa7). Fail fast instead:\n"
        f"{body}"
    )
    step, body = lanes["worksteal"]
    assert "--max-worker-restart" not in body, (
        f"_build-and-test.yml step {step!r} runs `--dist worksteal` with a "
        "--max-worker-restart cap. Worksteal does not re-dispatch a crashing item, so the "
        "default restart allowance only replaces lost capacity and preserves suite-wide "
        f"signal; do not add the flag here:\n{body}"
    )


def test_no_drift_script_gate_is_verified_only_in_branch_ci() -> None:
    """Auto-catch the drift class: any scripts/*.py a gate runs in the branch-CI lane (test.yml
    plus the reusable it calls) must also run in the Verified lane (gerrit-verify.yaml plus the
    reusable). Shared-reusable scripts satisfy this on both sides; the check still catches a
    script gate hiding in a caller-only job (this is how the env-vars.md gate slipped through)."""
    import re

    bat = _read(_BAT_YML)
    branch = _read(_TEST_YML) + bat
    verified = _read(_GERRIT_YML) + bat
    scripts_in_branch = set(re.findall(r"scripts/[A-Za-z0-9_]+\.py", branch))
    scripts_in_verified = set(re.findall(r"scripts/[A-Za-z0-9_]+\.py", verified))
    missing = scripts_in_branch - scripts_in_verified - _INTENTIONAL_SCRIPT_ASYMMETRIES
    assert not missing, (
        "script-driven gate(s) run in the branch-CI lane (post-merge) but not in the Verified "
        f"lane (gerrit-verify.yaml): {sorted(missing)}. Move them into the shared reusable, add "
        "them to the Verified lane, or record a reason in _INTENTIONAL_SCRIPT_ASYMMETRIES."
    )


def _verified_lane_reusable_jobs() -> list[tuple[str, str]]:
    """Every ``gerrit-verify.yaml`` job delegating to a ``.github/workflows/_*.yml`` reusable."""
    import yaml

    jobs = yaml.safe_load(_read(_GERRIT_YML))["jobs"]
    return sorted(
        (name, job["uses"])
        for name, job in jobs.items()
        if str(job.get("uses", "")).startswith("./.github/workflows/_")
    )


def test_verified_lane_reusable_discovery_finds_the_gating_reusables() -> None:
    """Non-emptiness floor for the parametrized rule below: if discovery found nothing (the
    workflow renamed, its ``jobs`` shape changed, or the ``uses:`` prefix drifted), that test
    would collect zero cases and pass vacuously. These four reusables carry the Verified gate's
    build/test, mutation, optionality and scanner checks."""
    jobs = dict(_verified_lane_reusable_jobs())
    for job in ("build-and-test", "mutation", "optionality", "scanner-integration"):
        assert job in jobs, f"gerrit-verify job `{job}` no longer delegates to a _*.yml reusable"


@pytest.mark.parametrize(
    ("job", "reusable"), _verified_lane_reusable_jobs(), ids=lambda v: str(v).rsplit("/", 1)[-1]
)
def test_optionality_contract_gates_the_verified_path(job: str, reusable: str) -> None:
    """Every Verified-lane job that delegates to a reusable workflow must run ON THE PATCHSET,
    fold into the vote, and share its definition with a push/PR lane. A module-scope
    heavy-import regression (pydantic_ai/httpx) once reached main precisely because a check ran
    only post-merge; a reusable called without the Gerrit refspec checks out main under
    workflow_dispatch and passes silently. This replaces the per-gate wiring snapshots
    (optionality, scanner, mutation) with one rule over every reusable job."""
    import yaml

    gerrit = yaml.safe_load(_read(_GERRIT_YML))["jobs"]
    assert gerrit[job].get("with", {}).get("gerrit-refspec") == "${{ inputs.GERRIT_REFSPEC }}", (
        f"gerrit-verify job `{job}` must pass `gerrit-refspec: ${{{{ inputs.GERRIT_REFSPEC }}}}` "
        f"to {reusable}, or it verifies main instead of the patchset."
    )
    needs = gerrit["vote"]["needs"]
    needs = [needs] if isinstance(needs, str) else needs
    assert job in needs, f"the `vote` job must list `{job}` in `needs` so its result is voted."
    workflows = Path(_GERRIT_YML).parent
    callers = []
    for path in sorted(workflows.glob("*.y*ml")):
        if path.name == Path(_GERRIT_YML).name or path.name.startswith("_"):
            continue
        lane_jobs = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("jobs") or {}
        if any(isinstance(j, dict) and j.get("uses") == reusable for j in lane_jobs.values()):
            callers.append(path.name)
    assert callers, (
        f"no push/PR workflow calls {reusable}; the Verified lane's `{job}` checks would drift "
        "from branch CI."
    )


# The optionality suite that used to have its OWN dedicated CI job (`import-linter` in
# _optionality.yml). That job was deleted as redundant: it installed `-e .[dev]` — byte-identical
# to the reusable's install — and ran exactly these files, which carry no marker and therefore
# already run in every `test` matrix cell (3.11/3.12/3.13 + macOS), strictly more coverage than
# one ubuntu/3.12 run. The deletion is safe ONLY while these files stay in the default selection,
# and NOTHING else in the repo can see that: the parity checks above read caller-level wiring
# strings and `scripts/check_verify_gate_parity.py` reads caller job keys — neither looks inside
# `_optionality.yml`. So the guard below is what replaces the deleted job (ticket
# `hominoid-awestruck-goshawk`): adding a `@pytest.mark.integration`, moving the files, or
# marking them from a conftest would otherwise silently drop the optionality contract on a
# fully GREEN build. Bump this count when a test is legitimately added to either file.
_OPTIONALITY_SUITE = ("tests/unit/test_core_optionality.py", "tests/unit/test_optional.py")
_OPTIONALITY_SUITE_ITEM_COUNT = 10
# The default suite selection, verbatim from the reusable `_build-and-test.yml` test step.
_DEFAULT_SELECTION = "not integration and not external"


def _collect_node_ids(paths: tuple[str, ...], selection: str) -> list[str]:
    """Node ids pytest actually collects for `paths` under `-m selection`.

    Runs the REAL selection in a subprocess rather than grepping the files for marker text:
    that is what makes this see a mark applied anywhere — a file-level ``pytestmark``, a
    per-test decorator, or one applied from a ``conftest.py`` hook.
    """
    import tempfile

    with tempfile.TemporaryDirectory(prefix="rebar-pytest-collect-") as temp_dir:
        proc = run_nested_pytest(
            Path(temp_dir),
            "--collect-only",
            "-q",
            "-m",
            selection,
            *paths,
            # subprocess_env() rather than a minimal mapping: this collector inherited the
            # ambient environment before it was routed through the helper, and
            # test_ci_workflow_parity_basetemp.py pins that PYTEST_DEBUG_TEMPROOT reaches
            # the child.
            env=subprocess_env(),
            timeout=300,
            cwd=_ROOT,
            no_cacheprovider=False,
        )
    # rc 0 = items collected; rc 5 = nothing collected (a full exclusion) — both are outcomes
    # this guard must be able to report on, so only an unexpected rc is an error.
    assert proc.returncode in (0, 5), (
        f"pytest collection failed (rc={proc.returncode}) for {list(paths)}:\n"
        f"{proc.stdout}\n{proc.stderr}"
    )
    return [line.strip() for line in proc.stdout.splitlines() if "::" in line]


def test_repo_policy_nodes_run_only_in_the_existing_primary_cell() -> None:
    """Route policy nodes by selector, without a new job or pytest session."""
    import yaml

    workflow = yaml.safe_load(_read(_BAT_YML))

    steps = workflow["jobs"]["test"]["steps"]
    default_step = next(
        step for step in steps if step.get("name", "").startswith("Run the default suite")
    )
    selector_key, selector_expression = next(
        (key, value)
        for key, value in default_step.get("env", {}).items()
        if isinstance(value, str) and _DEFAULT_SELECTION in value and "repo_policy" in value
    )
    expression = re.fullmatch(
        r"\$\{\{\s*matrix\.os == 'macos-latest'\s*&&\s*"
        r"'(?P<macos>[^']+)'\s*\|\|\s*\(\s*"
        r"matrix\.python-version == '3\.13'\s*&&\s*"
        r"'(?P<primary>[^']+)'\s*\|\|\s*'(?P<non_primary>[^']+)'\s*\)\s*\}\}",
        selector_expression,
    )
    assert expression is not None
    assert expression.group("macos") == "platform_compat and not external"
    assert expression.group("primary") == _DEFAULT_SELECTION
    assert expression.group("non_primary") == f"{_DEFAULT_SELECTION} and not repo_policy"
    assert f'pytest -m "${selector_key}"' in default_step["run"]

    pytest_steps = [
        step for step in steps if re.search(r"(?m)^\s*pytest\s", step.get("run", "")) is not None
    ]
    assert len(pytest_steps) == 2, "routing must not add a pytest collection/fixture session"
    integration_step = next(
        step for step in steps if step.get("name", "").startswith("Run the integration tier")
    )
    assert "pytest -m integration" in integration_step["run"]


# Core OS-sensitive files that MUST remain in the reduced macOS compatibility suite
# (story ci-runtime-round2). macOS runs ONLY `-m "platform_compat and not external"` +
# the integration tier; if pattern drift in tests/conftest.py's platform_compat
# auto-marking (or a file rename) silently drops these, the macOS cell would go green
# while testing NONE of the platform behaviour it exists to protect (file locking,
# atomic rename, timeout, cross-process git contention). This guard fails loudly first.
_PLATFORM_COMPAT_CORE_MODULES = (
    "tests/unit/store/test_stamped_lock.py",
    "tests/unit/test_lock_stale_reclaim.py",
    "tests/unit/test_timeout_budget.py",
    "tests/unit/test_repo_snapshot.py",
    "tests/interfaces/store/test_event_append_index_lock.py",
    "tests/interfaces/store/test_atomic_ticket_dir_create_021d.py",
)


def test_platform_compat_suite_covers_the_core_os_sensitive_seams() -> None:
    """The macOS compat subset must actually collect its load-bearing platform tests."""
    node_ids = _collect_node_ids(_PLATFORM_COMPAT_CORE_MODULES, "platform_compat and not external")
    missing = [
        p for p in _PLATFORM_COMPAT_CORE_MODULES if not any(n.startswith(p) for n in node_ids)
    ]
    assert not missing, (
        f"platform-sensitive test file(s) {missing} collect NOTHING under "
        '-m "platform_compat and not external". macOS runs only this subset + the integration '
        "tier, so a file that stops matching tests/conftest.py's _PLATFORM_COMPAT_PATH_FRAGMENTS "
        "(a rename, or a fragment edit) silently removes its platform coverage from CI on a green "
        "build. Restore the match or update the fragment list deliberately."
    )


def _render_default_suite_body(body: str, os_name: str, python_version: str, temp: str) -> str:
    """Resolve the GitHub ``${{ ... }}`` expressions in the default-suite run body for one cell.

    The Verified gate never hands this text to a shell verbatim — Actions resolves the
    expressions first — so to execute the lane's REAL exit-code logic under bash we resolve the
    same handful of expressions ourselves. An unrecognised expression raises rather than passing
    an unrendered ``${{...}}`` to bash, so a newly-added expression cannot silently slip past
    this oracle.
    """

    def resolve(match: re.Match[str]) -> str:
        inner = " ".join(match.group(1).split())
        if inner == "matrix.os == 'macos-latest' && '3' || '4'":
            return "3" if os_name == "macos-latest" else "4"
        if inner == "runner.temp":
            return temp
        if inner == "matrix.os":
            return os_name
        if inner == (
            "!startsWith(matrix.os, 'macos') && matrix.python-version == '3.13' "
            "&& '--cov=rebar --cov-report=term-missing:skip-covered "
            "--cov-report=xml:coverage.xml' || ''"
        ):
            return (
                "--cov=rebar --cov-report=term-missing:skip-covered --cov-report=xml:coverage.xml"
                if (not os_name.startswith("macos") and python_version == "3.13")
                else ""
            )
        raise AssertionError(f"unrendered workflow expression: {match.group(0)!r}")

    return re.sub(r"\$\{\{(.*?)\}\}", resolve, body)


def test_macos_default_suite_lane_passes_when_platform_compat_collects_nothing(
    tmp_path: Path,
) -> None:
    """A stale-tree macOS cell (0 platform_compat nodes -> pytest rc 5) must not fail the gate.

    The Verified gate runs THIS workflow from trusted main against the raw patchset tree
    (reusable-workflow ref binding). A patchset whose tree predates the ``tests/conftest.py``
    platform_compat auto-marker (story omphacite-desertlike-coney) carries zero nodes with that
    mark, so the
    macOS selection ``-m "platform_compat and not external"`` collects nothing and pytest exits
    5. That is a provenance artifact, not a defect in the change — the ubuntu cells (whole-suite,
    marker-independent) still fully exercise it — so the macOS lane must treat an empty
    collection as pass. Every OTHER cell selects the whole suite, where rc 5 is a real
    empty-suite failure that MUST stay red. This runs the lane's real shell with a stub
    ``pytest`` that exits 5, per cell.
    """
    import yaml

    workflow = yaml.safe_load(_read(_BAT_YML))
    steps = workflow["jobs"]["test"]["steps"]
    body = next(step for step in steps if step.get("name", "").startswith("Run the default suite"))[
        "run"
    ]

    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    stub = stub_bin / "pytest"
    stub.write_text("#!/usr/bin/env bash\nexit 5\n", encoding="utf-8")
    stub.chmod(0o755)

    def run_lane(os_name: str, python_version: str) -> int:
        rendered = _render_default_suite_body(body, os_name, python_version, str(tmp_path))
        marks = (
            "platform_compat and not external"
            if os_name == "macos-latest"
            else "not integration and not external"
        )
        proc = subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", rendered],
            env={
                "PATH": f"{stub_bin}{os.pathsep}{os.environ['PATH']}",
                "DEFAULT_SUITE_MARKS": marks,
                # Pinned: this hand-built env drops the tier's inherited isolation
                # root, and a child without it falls back to the real checkout.
                "REBAR_ROOT": str(tmp_path),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode

    assert run_lane("macos-latest", "3.13") == 0, (
        "the macOS default-suite lane still FAILS when the platform_compat selection collects "
        "nothing (pytest rc 5). A patchset whose tree predates the conftest auto-marker has no "
        "platform_compat nodes, so this spuriously blocks Verified even though the ubuntu cells "
        "fully test the change. Tolerate rc 5 on the macOS cell only."
    )
    for ubuntu_cell in (
        ("ubuntu-latest", "3.11"),
        ("ubuntu-latest", "3.12"),
        ("ubuntu-latest", "3.13"),
    ):
        assert run_lane(*ubuntu_cell) == 5, (
            f"the {ubuntu_cell} default-suite lane must still FAIL on an empty collection "
            "(rc 5): there the selection is the whole suite, so nothing collected is a real "
            "broken-suite signal and must not be masked."
        )


def test_optionality_suite_still_runs_in_the_default_selection() -> None:
    """The replacement guard for the deleted `import-linter` job: the optionality tests must
    remain collected by the default suite selection, which is now their ONLY CI home."""
    node_ids = _collect_node_ids(_OPTIONALITY_SUITE, _DEFAULT_SELECTION)
    missing = [p for p in _OPTIONALITY_SUITE if not any(n.startswith(p) for n in node_ids)]
    assert not missing, (
        f"optionality test file(s) {missing} collect NOTHING under the default selection "
        f'-m "{_DEFAULT_SELECTION}". These files no longer have a dedicated CI job — the default '
        "suite is the only thing running them, so excluding them (a marker, a move, a rename) "
        "silently deletes the lean-runtime / optionality contract on a green build. Either keep "
        "them in the default selection or give them back a dedicated gate."
    )
    assert len(node_ids) == _OPTIONALITY_SUITE_ITEM_COUNT, (
        f"expected {_OPTIONALITY_SUITE_ITEM_COUNT} optionality tests in the default selection, "
        f"collected {len(node_ids)}: {node_ids}. If a test was legitimately added or removed, "
        "update _OPTIONALITY_SUITE_ITEM_COUNT; if it was DESELECTED by a marker, that drops the "
        "optionality contract from CI entirely — see the deleted `import-linter` job."
    )


def _optionality_loop_step_run() -> str:
    """The `run:` text of the venv-loop step that installs every optional extra."""
    import yaml

    wf = yaml.safe_load(_read(_OPTIONALITY_REUSABLE_YML))
    job = (wf.get("jobs") or {}).get(_OPTIONALITY_LOOP_JOB)
    assert job, (
        f"_optionality.yml has no {_OPTIONALITY_LOOP_JOB!r} job — the per-extra/union venv loop "
        "was renamed or removed. Update _OPTIONALITY_LOOP_JOB (and "
        "infra/github/main-protection.snapshot.json, which names the job's check context)."
    )
    steps = [s.get("run") or "" for s in (job.get("steps") or [])]
    runs = [run for run in steps if "specs=" in run]
    assert len(runs) == 1, (
        f"expected exactly one venv-loop step (a `specs=` list) in the {_OPTIONALITY_LOOP_JOB!r} "
        f"job, found {len(runs)} — this guard's anchor is stale."
    )
    return runs[0]


def test_optionality_loop_aggregates_failures_and_never_activates() -> None:
    """The venv loop replaced a `fail-fast: false` matrix, so it must keep BOTH properties the
    matrix gave for free, each of which fails SILENTLY if dropped:

    * failure aggregation — Actions runs `run:` under `bash -e`, so a naive loop dies on the
      first bad extra (losing the other extras' diagnostics), while `|| true` / `set +e` without
      an accumulator makes the whole contract a no-op that ALWAYS passes. The `rc` accumulator
      (`continue` per failure, `exit $rc` at the end) is what keeps it honest.
    * no `activate` — venv activations STACK across loop iterations, so PATH (not the loop
      variable) would decide which interpreter runs and every later iteration could silently
      re-test the FIRST extra while reporting its own name. Absolute `"$v/bin/..."` only.
    """
    run = _optionality_loop_step_run()
    for shape in ("rc=0", "rc=1", "continue", "exit $rc"):
        assert shape in run, (
            f"_optionality.yml {_OPTIONALITY_LOOP_JOB!r}: the venv loop no longer contains "
            f"{shape!r}. Without the full `rc` accumulator shape a failing extra either aborts "
            "the loop (losing the other extras' diagnostics) or is swallowed entirely, making "
            "this whole contract a check that can never fail."
        )
    assert "activate" not in run, (
        f"_optionality.yml {_OPTIONALITY_LOOP_JOB!r}: the venv loop sources an `activate` script. "
        "Activations stack across iterations, so PATH decides which interpreter runs and later "
        'iterations can silently re-test the first extra. Invoke by absolute "$v/bin/..." path.'
    )


def test_optionality_loop_covers_every_declared_extra() -> None:
    """Every key of ``rebar._optional.EXTRAS`` must appear in the loop's extra list, so a newly
    declared extra cannot ship with ZERO CI. The old hand-maintained matrix list had drifted
    exactly this way: it ran `mcp` (which is not in EXTRAS) but never `metrics` (which is)."""
    import re

    from rebar import _optional

    run = _optionality_loop_step_run()
    # Entries are "<label>:<pip extras>"; the union entry's label is not an extra name.
    # Extra/pip-extra names may contain digits (e.g. `s3`), so allow them in both groups.
    labels = {m.group(1) for m in re.finditer(r'"([a-z0-9_-]+):([a-z0-9_,-]+)"', run)}
    missing = set(_optional.EXTRAS) - labels
    assert not missing, (
        f"optional extra(s) {sorted(missing)} are declared in rebar._optional.EXTRAS but are not "
        f"installed by the {_OPTIONALITY_LOOP_JOB!r} venv loop in _optionality.yml — they would "
        "ship with no CI proving the extra installs and is detected. Add them to `specs`."
    )
    # `mcp` has no _optional probe but is still a shipped extra, and the joint UNION install is
    # the only iteration that can surface a `ResolutionImpossible` no single extra reaches.
    assert {"mcp", "union"} <= labels, (
        f"the {_OPTIONALITY_LOOP_JOB!r} venv loop must keep the `mcp` iteration (server transport "
        "+ its `rebar-mcp --help` boot check) and the joint `union` iteration (joint dependency "
        "resolution); one of them is missing."
    )


def test_every_optionality_spec_is_a_declared_extra() -> None:
    """The loop must not install an extra that does not exist (story 271c).

    The reverse of the test above, and a real drift found by it: the loop carried a
    stale ``eval:eval`` iteration long after the ``eval`` extra was gone from
    ``[project.optional-dependencies]``, so that lane installed a non-existent extra
    and proved nothing. ``mcp`` is exempt — it is a shipped extra with no
    ``_optional`` probe — and ``union`` is the joint-resolution label, not an extra.
    """
    import re

    import tomllib

    from rebar import _optional

    pyproject = tomllib.loads(_read(_ROOT / "pyproject.toml"))
    declared = set(pyproject["project"]["optional-dependencies"])
    run = _optionality_loop_step_run()
    specs = {m.group(1): m.group(2) for m in re.finditer(r'"([a-z0-9_-]+):([a-z0-9_,-]+)"', run)}

    unknown = {
        label: target
        for label, target in specs.items()
        if label != "union"
        for part in target.split(",")
        if part not in declared
    }
    assert not unknown, (
        f"the {_OPTIONALITY_LOOP_JOB!r} venv loop installs extra(s) that are not declared in "
        f"[project.optional-dependencies]: {sorted(unknown)}. A lane installing a non-existent "
        "extra proves nothing — remove it or declare the extra."
    )
    assert "eval" not in specs, (
        "the stale `eval:eval` iteration is back: there is no `eval` extra in pyproject.toml."
    )
    # Every declared extra with an _optional probe is still covered by the loop.
    assert set(_optional.EXTRAS) <= set(specs)


def _precommit_config_hook_ids() -> set[str]:
    """The set of hook ids declared in .pre-commit-config.yaml."""
    import yaml

    cfg = yaml.safe_load(_read(_PRECOMMIT_CONFIG))
    return {
        hook["id"]
        for repo in (cfg.get("repos") or [])
        for hook in (repo.get("hooks") or [])
        if "id" in hook
    }


def _precommit_all_files_steps(workflow_text: str) -> list[dict]:
    """Every step in a workflow whose `run` invokes `pre-commit run --all-files`,
    with `env` resolved to the merged workflow-/job-/step-level environment (SKIP may be
    set at any of those scopes)."""
    import yaml

    wf = yaml.safe_load(workflow_text)
    wf_env = wf.get("env") or {}
    steps: list[dict] = []
    for job in (wf.get("jobs") or {}).values():
        job_env = job.get("env") or {}
        for step in job.get("steps") or []:
            if "pre-commit run --all-files" in (step.get("run") or ""):
                merged = {**wf_env, **job_env, **(step.get("env") or {})}
                steps.append({"run": step.get("run") or "", "env": merged})
    return steps


def _hook_is_skipped(hook: str, step: dict) -> bool:
    """True if `hook` is skipped for this pre-commit step, via the SKIP env var (a
    comma/space-separated list of hook ids) or an inline `SKIP=<...>` in the run script."""
    import re

    skip_env = str(step["env"].get("SKIP", ""))
    if hook in re.split(r"[,\s]+", skip_env.strip()):
        return True
    # Inline form, e.g. `SKIP=no-commit-to-branch pre-commit run --all-files`.
    for m in re.finditer(r"SKIP=([^\s]+)", step["run"]):
        if hook in re.split(r"[,]+", m.group(1)):
            return True
    return False


def test_branch_sensitive_precommit_hooks_skipped_in_ci() -> None:
    """A branch-sensitive pre-commit hook must be SKIPped in the reusable's single `pre-commit
    run --all-files` step (both lanes run it). Otherwise the identical command passes pre-merge
    on the detached Gerrit change ref (Verified +1 → the change lands) but fails post-merge on
    the `main` branch, reddening branch CI with no way for the pre-merge gate to catch it. This
    closes the *context/behavioral* drift gap that the command-signature checks cannot see
    (bug `pillared-doddering-fawn`)."""
    active = _BRANCH_SENSITIVE_HOOKS & _precommit_config_hook_ids()
    steps = _precommit_all_files_steps(_read(_BAT_YML))
    assert steps, (
        "_build-and-test.yml: no `pre-commit run --all-files` step found — the parity signature "
        "is stale (the step was renamed/removed/moved out of the reusable). Update this guard."
    )
    for hook in sorted(active):
        for step in steps:
            assert _hook_is_skipped(hook, step), (
                f"_build-and-test.yml: the `pre-commit run --all-files` step does not SKIP the "
                f"branch-sensitive hook {hook!r}. That hook passes on a detached Gerrit change "
                f"ref (Verified gate) but FAILS on the `main` branch (post-merge branch CI), so "
                f"it reddens `main` while the pre-merge Verified vote stays green — the drift "
                f"that caused bug `pillared-doddering-fawn`. Add `SKIP: {hook}` to the step env."
            )


def test_ci_redundant_hooks_skipped_in_precommit() -> None:
    """The `lint`/`typecheck` pre-commit hooks (which just re-invoke `make lint` / `make
    typecheck`) must be SKIPped in the reusable's `pre-commit run --all-files` step, because
    each of those `make` targets already runs as its own named step in the reusable. Without the
    skip, every CI job runs each linter TWICE (once directly, once via the hook) — pure duplicate
    work (ticket `ecumenical-equal-sidewinder`). This guard fails the build if the double-run is
    reintroduced. It also asserts the direct step still exists, so a linter runs exactly once —
    never zero times."""
    active = _CI_REDUNDANT_HOOKS & _precommit_config_hook_ids()
    # Each redundant hook's stand-alone step is what keeps it running once after the skip.
    _direct_step_sig = {"lint": "make lint", "typecheck": "make typecheck"}
    text = _read(_BAT_YML)
    steps = _precommit_all_files_steps(text)
    assert steps, (
        "_build-and-test.yml: no `pre-commit run --all-files` step found — the parity signature "
        "is stale (the step was renamed/removed/moved out of the reusable). Update this guard."
    )
    for hook in sorted(active):
        for step in steps:
            assert _hook_is_skipped(hook, step), (
                f"_build-and-test.yml: the `pre-commit run --all-files` step does not SKIP the "
                f"redundant hook {hook!r}. It only re-invokes `{_direct_step_sig[hook]}`, which "
                f"already runs as its own named step — so CI runs the linter TWICE per job. Add "
                f"{hook!r} to the step's `SKIP` env, per ticket `ecumenical-equal-sidewinder`."
            )
        assert _direct_step_sig[hook] in text, (
            f"_build-and-test.yml: `{_direct_step_sig[hook]}` no longer runs as its own step, yet "
            f"the {hook!r} hook is skipped in the pre-commit run — the linter would run ZERO times "
            f"in CI. Keep the direct `{_direct_step_sig[hook]}` step in the reusable."
        )


# --------------------------------------------------------------------------- #
# Config-ownership + config-read gates run through the portable `make lint`    #
# trigger (RP-04 S7.2, ticket 735b). Both gates live in exactly one place —    #
# the Makefile `lint` target — and CI inherits them via its existing          #
# `make lint` step, so neither runs twice and the trigger needs no CI provider.#
# --------------------------------------------------------------------------- #

_MAKEFILE = _ROOT / "Makefile"
_CONFIG_GATE_SCRIPTS = (
    "scripts/check_config_ownership.py",
    "scripts/check_config_reads.py",
)


def _lint_target_body(makefile_text: str) -> str:
    """The recipe lines of the `lint:` target — from the `lint:` header up to the next
    top-level `target:` header. Isolating the target keeps the assertions from matching a
    gate invocation that lives in some OTHER target."""
    lines = makefile_text.splitlines()
    body: list[str] = []
    in_target = False
    for line in lines:
        if re.match(r"^lint:", line):
            in_target = True
            continue
        if in_target and re.match(r"^[A-Za-z0-9_.-]+:", line):
            break
        if in_target:
            body.append(line)
    return "\n".join(body)


_MAKE_WORKFLOW_CALL = re.compile(r"(?<![\w$])make(?P<args>(?:\s+[^;&|]+)+)")
_MAKE_TARGET = re.compile(r"^[A-Za-z0-9_.-]+$")
_MAKE_RECIPE_EDGE = re.compile(r"\$\(\s*MAKE\s*\)\s+(?P<target>[A-Za-z0-9_.-]+)")
_MAKE_RECIPE_HEADER = re.compile(r"^([A-Za-z0-9_.-]+):(?:\s|$)")
_MAKE_NON_POSIX_CONSTRUCTS = (
    ("set -o pipefail", re.compile(r"\bset\s+-o\s+pipefail\b")),
    ("double-bracket test", re.compile(r"\[\[")),
    (
        "indexed/associative array",
        re.compile(
            r"\b(?:declare|local|typeset|readonly)\s+-(?:a|A)\b|"
            r"(?<![\w$])[A-Za-z_][A-Za-z0-9_]*\[[^\]\n]+\]="
        ),
    ),
    ("here-string", re.compile(r"<<<")),
    ("case-modification", re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]+\])?[\^,]{1,2}\}")),
)


def _live_make_lines(text: str) -> str:
    """Recipe text with comment-only lines removed before static scans."""
    live: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        while stripped[:1] in {"@", "+", "-"}:
            stripped = stripped[1:].lstrip()
        if stripped.startswith("#"):
            continue
        live.append(line)
    return "\n".join(live)


def _targets_from_make_invocations(text: str) -> set[str]:
    """Make targets named by every ``make`` invocation in ``text``."""
    targets: set[str] = set()
    for match in _MAKE_WORKFLOW_CALL.finditer(_live_make_lines(text)):
        for token in match.group("args").split():
            if token.startswith("-") or "=" in token:
                continue
            if _MAKE_TARGET.fullmatch(token):
                targets.add(token)
    return targets


def _workflow_make_targets(*workflow_paths: Path) -> set[str]:
    """The ``make`` targets invoked by the workflow commands in the supplied files."""
    import yaml

    targets: set[str] = set()
    for path in workflow_paths:
        workflow = yaml.safe_load(_read(path)) or {}
        for job in (workflow.get("jobs") or {}).values():
            for step in (job or {}).get("steps") or []:
                body = str((step or {}).get("run", ""))
                if not body:
                    continue
                targets.update(_targets_from_make_invocations(body))
    return targets


def _make_recipe_bodies(makefile_text: str) -> dict[str, str]:
    """Top-level Makefile recipes keyed by target name."""
    bodies: dict[str, list[str]] = {}
    current_target: str | None = None
    current_lines: list[str] = []
    for line in makefile_text.splitlines():
        header = _MAKE_RECIPE_HEADER.match(line)
        if header and not line.startswith((" ", "\t")):
            if current_target is not None:
                bodies[current_target] = current_lines
            current_target = header.group(1)
            current_lines = []
            continue
        if current_target is not None:
            if line and not line.startswith((" ", "\t")):
                bodies[current_target] = current_lines
                current_target = None
                current_lines = []
                continue
            current_lines.append(line)
    if current_target is not None:
        bodies[current_target] = current_lines
    return {target: "\n".join(lines) for target, lines in bodies.items()}


def _reachable_make_targets(seed_targets: set[str], makefile_text: str) -> set[str]:
    """The CI-reachable Makefile targets from workflow calls plus transitive ``$(MAKE)`` edges."""
    bodies = _make_recipe_bodies(makefile_text)
    seen: set[str] = set()
    stack = list(seed_targets)
    while stack:
        target = stack.pop()
        if target in seen:
            continue
        seen.add(target)
        stack.extend(
            match.group("target")
            for match in _MAKE_RECIPE_EDGE.finditer(_live_make_lines(bodies.get(target, "")))
        )
    return seen


def _bash_only_make_recipe_violations(
    recipes: dict[str, str], *, include_match: bool = False
) -> list[str]:
    """Static bash-only constructs in already-selected recipe bodies."""
    violations: list[str] = []
    for target, body in sorted(recipes.items()):
        live = _live_make_lines(body)
        for label, pattern in _MAKE_NON_POSIX_CONSTRUCTS:
            if match := pattern.search(live):
                suffix = f": {match.group(0)}" if include_match else ""
                violations.append(f"{target}: {label}{suffix}")
    return violations


def _make_recipe_violations(makefile_text: str, targets: set[str]) -> list[str]:
    """Static non-POSIX-dash constructs in the recipes reachable from CI."""
    bodies = _make_recipe_bodies(makefile_text)
    recipes = {target: bodies[target] for target in sorted(targets) if target in bodies}
    return _bash_only_make_recipe_violations(recipes, include_match=True)


def _ci_reachable_make_recipes(workflow_text: str, makefile_text: str) -> dict[str, str]:
    """CI-reachable Makefile recipe bodies, keyed by target name."""
    import yaml

    workflow = yaml.safe_load(workflow_text) or {}
    seed_targets: set[str] = set()
    for job in (workflow.get("jobs") or {}).values():
        for step in (job or {}).get("steps") or []:
            body = str((step or {}).get("run", ""))
            if body:
                seed_targets.update(_targets_from_make_invocations(body))
    targets = _reachable_make_targets(seed_targets, makefile_text)
    bodies = _make_recipe_bodies(makefile_text)
    return {target: bodies[target] for target in sorted(targets) if target in bodies}


def test_make_target_closure_follows_transitive_make_edges(tmp_path: Path) -> None:
    """The closure helper follows nested ``$(MAKE)`` edges, not just direct children."""
    repo = tmp_path / "repo"
    workflows = repo / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "root.yml").write_text(
        "jobs:\n  gate:\n    runs-on: ubuntu-latest\n    steps:\n      - run: make root\n",
        encoding="utf-8",
    )
    (repo / "Makefile").write_text(
        "root:\n\t$(MAKE) mid\n\nmid:\n\t$(MAKE) leaf\n\nleaf:\n\t@printf ok\n",
        encoding="utf-8",
    )
    assert _reachable_make_targets(
        _workflow_make_targets(workflows / "root.yml"), _read(repo / "Makefile")
    ) == {
        "root",
        "mid",
        "leaf",
    }


def test_make_target_discovery_sees_multiple_targets_in_one_invocation() -> None:
    """A single workflow ``make`` command can seed more than one target."""
    assert _targets_from_make_invocations("make lint typecheck && make config-check") == {
        "lint",
        "typecheck",
        "config-check",
    }


def test_ci_reachable_make_targets_follow_workflow_invocations_and_make_edges() -> None:
    """The gating/mirror lanes seed Make targets from workflow commands, then follow
    transitive ``$(MAKE)`` edges inside the Makefile."""
    makefile = _read(_MAKEFILE)
    reachable = _reachable_make_targets(_workflow_make_targets(_BAT_YML), makefile)
    assert {"lint", "typecheck", "config-check"}.issubset(reachable), (
        "the CI-reachable Makefile closure no longer includes one of the lane-invoked "
        f"targets: {sorted(reachable)}"
    )
    assert "actionlint-bin" in reachable, (
        "the CI-reachable Makefile closure no longer follows lint's transitive `$(MAKE)` "
        "edge to actionlint-bin"
    )
    assert "worktree" not in reachable, (
        "the CI-reachable Makefile closure now includes the dev-only worktree target, which "
        "CI does not invoke"
    )


def test_ci_reachable_make_recipes_stay_posix_dash_static_text() -> None:
    """Static recipe scans catch non-POSIX-dash shell constructs on the CI-reachable path."""
    makefile = _read(_MAKEFILE)
    reachable = _reachable_make_targets(_workflow_make_targets(_BAT_YML), makefile)
    violations = _make_recipe_violations(makefile, reachable)
    assert not violations, (
        "the CI-reachable Makefile recipes use non-POSIX-dash shell constructs:\n  "
        + "\n  ".join(violations)
    )


def test_static_recipe_scan_reaches_transitive_make_targets() -> None:
    """A violation in a ``$(MAKE)`` child target is still on the CI-reachable path."""
    makefile = _read(_MAKEFILE)
    mutated = makefile.replace(
        "actionlint-bin:  ## Ensure a pinned actionlint is available",
        "actionlint-bin:  ## Ensure a pinned actionlint is available\n\tset -o pipefail",
    )
    reachable = _reachable_make_targets(_workflow_make_targets(_BAT_YML), mutated)
    assert any(
        violation.startswith("actionlint-bin: set -o pipefail")
        for violation in _make_recipe_violations(mutated, reachable)
    )


def test_ci_reachable_make_recipes_reject_bash_only_constructs() -> None:
    """CI-reachable Makefile recipes stay executable by POSIX /bin/sh."""
    recipes = _ci_reachable_make_recipes(_read(_BAT_YML), _read(_MAKEFILE))
    assert {"lint", "typecheck", "config-check", "actionlint-bin"} <= set(recipes)
    assert "worktree" not in recipes

    seeded = dict(recipes)
    seeded["lint"] = "\tset -o pipefail\n" + seeded["lint"]
    violations = _bash_only_make_recipe_violations(seeded)
    assert violations == ["lint: set -o pipefail"]
    assert not _bash_only_make_recipe_violations(recipes)
