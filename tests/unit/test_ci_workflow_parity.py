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

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_TEST_YML = _ROOT / ".github" / "workflows" / "test.yml"
_GERRIT_YML = _ROOT / ".github" / "workflows" / "gerrit-verify.yaml"
_OPTIONALITY_YML = _ROOT / ".github" / "workflows" / "optionality.yml"
_OPTIONALITY_REUSABLE_YML = _ROOT / ".github" / "workflows" / "_optionality.yml"
# The job that replaced the 5-cell `per-extra` matrix + the `union` job with one venv loop
# (6 concurrent slots -> 1, under the org-wide 20-job ceiling).
_OPTIONALITY_LOOP_JOB = "optional-extras"
_BAT_YML = _ROOT / ".github" / "workflows" / "_build-and-test.yml"
_PRECOMMIT_CONFIG = _ROOT / ".pre-commit-config.yaml"
_REUSABLE_OPTIONALITY = "./.github/workflows/_optionality.yml"
# The reusable gate+suite workflow both CI lanes now delegate to (this refactor). Its presence
# in BOTH caller files is what makes the two lanes share one definition — no drift by construction.
_REUSABLE_BAT = "./.github/workflows/_build-and-test.yml"

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

# Each gating check keyed by a STABLE command signature (not the step name — names differ
# in wording, e.g. the pip-audit step). Every signature here must appear in the reusable
# `_build-and-test.yml`, which both the pre-merge Verified gate and the post-merge branch CI run.
_SHARED_GATE_SIGNATURES = {
    "module-size gate": ".github/module-size-limit.txt",
    "prompt-index drift gate": "regenerate-index",
    "env-var registry drift gate": "scripts/gen_env_registry.py",
    "security-rules freshness gate": "security_pin",
    "criteria-routing parity gate": "validate-routing",
    "server.json env-contract drift gate": "scripts/check_server_manifest.py",
    "public-types drift gate": "gen_types",
    "lint (ruff)": "make lint",
    "mypy (typecheck)": "make typecheck",
    "config-check": "make config-check",
    "pre-commit all hooks": "pre-commit run --all-files",
    "pip-audit": "pip-audit",
    "default test suite": 'pytest -m "not integration and not external"',
    "integration tier": "pytest -m integration",
}

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


def test_shared_gate_signatures_live_in_the_reusable() -> None:
    """Every known gate signature is present in the reusable both lanes run — so the gate exists
    AND (via the delegation test above) runs pre-merge and post-merge from one definition."""
    bat = _read(_BAT_YML)
    for label, sig in _SHARED_GATE_SIGNATURES.items():
        assert sig in bat, (
            f"gate {label!r} signature {sig!r} not found in _build-and-test.yml — either the "
            "gate was dropped from the shared workflow (it would stop gating BOTH lanes) or its "
            "signature is stale; update _SHARED_GATE_SIGNATURES to match the renamed/removed gate."
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


def test_optionality_contract_gates_the_verified_path() -> None:
    """The lean-runtime / clean-core-wheel / packaging (optionality) contract must run in the
    Verified gate ON THE PATCHSET, not only post-merge. A module-scope heavy-import regression
    (pydantic_ai/httpx) once reached main precisely because gerrit-verify installs .[dev] (heavy
    stack present) and never exercised the no-extras clean wheel. Lock in the wiring: gerrit-verify
    invokes the reusable optionality workflow with the Gerrit refspec (so it checks out the
    patchset), and the push/PR lane delegates to the SAME reusable workflow (no drift)."""
    gerrit_yml = _read(_GERRIT_YML)
    optionality_yml = _read(_OPTIONALITY_YML)
    assert _REUSABLE_OPTIONALITY in gerrit_yml, (
        "gerrit-verify.yaml does not invoke the reusable optionality workflow "
        f"({_REUSABLE_OPTIONALITY}) — the clean-wheel/packaging contract would only fail "
        "post-merge. Add an `optionality` job that `uses` it with the Gerrit refspec."
    )
    # The patchset (not the branch head / main) must be what optionality verifies in the gate.
    assert "GERRIT_REFSPEC" in gerrit_yml, (
        "the Verified-lane optionality job must pass GERRIT_REFSPEC so it checks out the exact "
        "patchset (a plain checkout under workflow_dispatch resolves to main → silent false PASS)."
    )
    # The vote must wait for optionality so the run-conclusion snapshot sees a terminal result.
    vote_needs_optionality = (
        "build-and-test, optionality" in gerrit_yml or "optionality, build-and-test" in gerrit_yml
    )
    assert vote_needs_optionality, (
        "the `vote` job must list `optionality` in its `needs` so its conclusion is folded into "
        "the Verified vote (otherwise the run-conclusion snapshot can miss it)."
    )
    # Both lanes share one definition — no drift between push/PR and Verified.
    assert _REUSABLE_OPTIONALITY in optionality_yml, (
        "optionality.yml (push/PR lane) must delegate to the same reusable workflow so its checks "
        "cannot drift from the Verified-lane checks."
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
_OPTIONALITY_SUITE_ITEM_COUNT = 9
# The default suite selection, verbatim from the reusable `_build-and-test.yml` test step.
_DEFAULT_SELECTION = "not integration and not external"


def _collect_node_ids(paths: tuple[str, ...], selection: str) -> list[str]:
    """Node ids pytest actually collects for `paths` under `-m selection`.

    Runs the REAL selection in a subprocess rather than grepping the files for marker text:
    that is what makes this see a mark applied anywhere — a file-level ``pytestmark``, a
    per-test decorator, or one applied from a ``conftest.py`` hook.
    """
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-m", selection, *paths],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    # rc 0 = items collected; rc 5 = nothing collected (a full exclusion) — both are outcomes
    # this guard must be able to report on, so only an unexpected rc is an error.
    assert proc.returncode in (0, 5), (
        f"pytest collection failed (rc={proc.returncode}) for {list(paths)}:\n"
        f"{proc.stdout}\n{proc.stderr}"
    )
    return [line.strip() for line in proc.stdout.splitlines() if "::" in line]


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
    labels = {m.group(1) for m in re.finditer(r'"([a-z_]+):([a-z_,]+)"', run)}
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
