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

import re
from pathlib import Path
from typing import Any

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
_PRECOMMIT_CONFIG = _ROOT / ".pre-commit-config.yaml"
_REUSABLE_OPTIONALITY = "./.github/workflows/_optionality.yml"
# The reusable gate+suite workflow both CI lanes now delegate to (this refactor). Its presence
# in BOTH caller files is what makes the two lanes share one definition — no drift by construction.
_REUSABLE_BAT = "./.github/workflows/_build-and-test.yml"
_REUSABLE_MUTATION = "./.github/workflows/_mutation.yml"

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
    "security-rules freshness gate": "security_pin",
    "criteria-routing parity gate": "validate-routing",
    "server.json env-contract drift gate": "scripts/check_server_manifest.py",
    "public-types drift gate": "gen_types",
    "lint (ruff)": "make lint",
    "mypy (typecheck)": "make typecheck",
    "config-check": "make config-check",
    "pre-commit all hooks": "pre-commit run --all-files",
    "pip-audit": "pip-audit",
    "default test suite": 'pytest -m "$DEFAULT_SUITE_MARKS"',
    "integration tier": "pytest -m integration",
}

_DOCUMENTATION_GATE_SIGNATURES = {
    "ADR number and cross-reference gate": "scripts/check_adr_numbers.py",
    "ADR index drift gate": "scripts/gen_adr_index.py",
    "env-var registry drift gate": "scripts/gen_env_registry.py",
    "docs index and dead-link gate": "scripts/check_docs_index.py",
    "README quickstart gate": "scripts/check_readme_quickstart.py",
    "CLI reference drift gate": "scripts/gen_cli_reference.py",
    "MCP reference drift gate": "scripts/gen_mcp_reference.py",
}

_COVERAGE_FLAGS = "--cov=rebar --cov-report=term-missing:skip-covered"


def _expanded_test_matrix(workflow: dict[str, Any]) -> list[tuple[str, str]]:
    matrix = workflow["jobs"]["test"]["strategy"]["matrix"]
    excluded = {(item["os"], item["python-version"]) for item in matrix.get("exclude", [])}
    return [
        (os_name, python_version)
        for os_name in matrix["os"]
        for python_version in matrix["python-version"]
        if (os_name, python_version) not in excluded
    ]


def _coverage_flags_by_cell(run: str, cells: list[tuple[str, str]]) -> dict[tuple[str, str], str]:
    expression = re.search(
        r"\$\{\{\s*(?P<condition>[^}]*?)\s*&&\s*'(?P<truthy>--cov=rebar "
        r"--cov-report=term-missing:skip-covered)'\s*\|\|\s*'(?P<falsy>[^']*)'\s*\}\}",
        run,
    )
    assert expression is not None, (
        "the default-suite coverage expression must keep the non-empty flags in the truthy "
        "`&&` slot and the empty string in the final `||` slot"
    )
    assert expression.group("truthy") == _COVERAGE_FLAGS
    assert expression.group("falsy") == ""

    predicates = [part.strip() for part in expression.group("condition").split("&&")]

    def enabled(os_name: str, python_version: str) -> bool:
        values: list[bool] = []
        for predicate in predicates:
            if predicate == "!startsWith(matrix.os, 'macos')":
                values.append(not os_name.startswith("macos"))
            elif predicate == "matrix.python-version == '3.13'":
                values.append(python_version == "3.13")
            elif predicate == "false":
                values.append(False)
            else:
                raise AssertionError(f"unsupported coverage predicate: {predicate!r}")
        return all(values)

    return {
        cell: expression.group("truthy") if enabled(*cell) else expression.group("falsy")
        for cell in cells
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


def test_matrix_keeps_every_test_tier_but_collects_coverage_once() -> None:
    """Every cell runs both tiers; only primary Ubuntu traces coverage and policy."""
    import yaml

    workflow = yaml.safe_load(_read(_BAT_YML))
    cells = _expanded_test_matrix(workflow)
    assert cells == [
        ("ubuntu-latest", "3.11"),
        ("ubuntu-latest", "3.12"),
        ("ubuntu-latest", "3.13"),
        ("macos-latest", "3.13"),
    ]

    steps = workflow["jobs"]["test"]["steps"]
    default_steps = [
        step
        for step in steps
        if step.get("name", "").startswith("Run the default suite")
        and 'pytest -m "$DEFAULT_SUITE_MARKS"' in step.get("run", "")
    ]
    integration_steps = [step for step in steps if "pytest -m integration" in step.get("run", "")]
    assert len(default_steps) == 1
    assert len(integration_steps) == 1

    tiers_by_cell = {cell: {"default", "integration"} for cell in cells}
    assert all(tiers == {"default", "integration"} for tiers in tiers_by_cell.values())

    flags_by_cell = _coverage_flags_by_cell(default_steps[0]["run"], cells)
    assert flags_by_cell == {
        ("ubuntu-latest", "3.11"): "",
        ("ubuntu-latest", "3.12"): "",
        ("ubuntu-latest", "3.13"): _COVERAGE_FLAGS,
        ("macos-latest", "3.13"): "",
    }
    assert _COVERAGE_FLAGS not in integration_steps[0]["run"]


def test_mutation_gate_uses_one_reusable_in_both_lanes_and_votes() -> None:
    """Targeted mutation checks must run on branch/PR plus the exact Gerrit patchset."""
    test_yml = _read(_TEST_YML)
    gerrit_yml = _read(_GERRIT_YML)

    assert _REUSABLE_MUTATION in test_yml
    assert _REUSABLE_MUTATION in gerrit_yml
    assert "gerrit-refspec: ${{ inputs.GERRIT_REFSPEC }}" in gerrit_yml
    assert "mutation" in gerrit_yml.split("  vote:", 1)[1].split("runs-on:", 1)[0]


def test_mutation_workflows_are_bounded_and_publish_results() -> None:
    reusable = _read(_ROOT / ".github" / "workflows" / "_mutation.yml")
    broad = _read(_ROOT / ".github" / "workflows" / "mutation.yml")

    assert "timeout-minutes: 30" in reusable
    assert "if: always()" in reusable
    assert "upload-artifact" in reusable
    assert "--all-shards" in reusable
    assert _REUSABLE_MUTATION in broad
    assert "schedule:" in broad
    assert "workflow_dispatch:" in broad


def test_mutation_selector_skips_pre_gate_trees_without_weakening_current_trees(
    tmp_path: Path,
) -> None:
    """Definition/tree skew skips only when the patchset lacks the selector script."""
    import os
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
        env={**os.environ, "GITHUB_OUTPUT": str(absent_output)},
        capture_output=True,
        text=True,
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
        env={**os.environ, "GITHUB_OUTPUT": str(current_output)},
        capture_output=True,
        text=True,
    )

    assert marker.read_text(encoding="utf-8") == "ran"
    assert current.returncode == 17, current.stdout + current.stderr


def test_mutation_reusable_expands_selector_json_into_one_bounded_job_per_shard() -> None:
    """One selector topology serves push, PR, Gerrit, and weekly mutation lanes."""
    import re

    import yaml

    def trigger_map(document: dict[Any, Any]) -> dict[str, Any] | None:
        # PyYAML's YAML-1.1 resolver parses an unquoted ``on`` key as boolean True.
        key: str | bool | None = "on" if "on" in document else True if True in document else None
        triggers = document.get(key) if key is not None else None
        return triggers if isinstance(triggers, dict) else None

    def normalized_expression(value: Any) -> str:
        expression = str(value).strip()
        if expression.startswith("${{") and expression.endswith("}}"):
            expression = expression[3:-2]
        return "".join(expression.split())

    def executable_lines(run: Any) -> list[str]:
        lines = []
        for raw_line in str(run).splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            lines.append(line.split(" #", 1)[0].rstrip())
        return lines

    def selector_dataflow_contract(lines: list[str]) -> dict[str, Any]:
        canonical = [
            "selector_args=(select --base HEAD^ --head HEAD)",
            'if [[ "${{ inputs.all-shards }}" == "true" ]]; then',
            "selector_args+=(--all-shards)",
            "fi",
            'selection_json="$(uv run --locked python scripts/mutation_gate.py '
            '"${selector_args[@]}")"',
            'matrix="$(jq -c \'{shard: .selected_shards}\' <<<"$selection_json")"',
            'has_shards="$(jq -r \'.empty_selection | not\' <<<"$selection_json")"',
            'echo "matrix=$matrix" >> "$GITHUB_OUTPUT"',
            'echo "has-shards=$has_shards" >> "$GITHUB_OUTPUT"',
        ]
        start = lines.index(canonical[0]) if lines.count(canonical[0]) == 1 else len(lines)
        dataflow_lines = lines[start:]
        positions = [
            dataflow_lines.index(line) if dataflow_lines.count(line) == 1 else None
            for line in canonical
        ]
        present_positions = [position for position in positions if position is not None]
        assignment_write = re.compile(
            r"(?<![A-Za-z0-9_$\"'])"
            r"(?:selector_args|selection_json|matrix|has_shards)"
            r"(?:\[[^]\n]+\])?\+?=(?!=)"
        )

        return {
            "canonical_sequence_is_unique_and_ordered": len(present_positions) == len(canonical)
            and present_positions == sorted(present_positions),
            "assignment_lines": [line for line in dataflow_lines if assignment_write.search(line)],
        }

    def checkout_contract(steps: list[dict[str, Any]]) -> dict[str, bool]:
        checkout_steps = [step for step in steps if "checkout" in str(step.get("uses", "")).lower()]
        branch_or_pr = [
            step
            for step in checkout_steps
            if str(step.get("uses", "")).startswith("actions/checkout@")
            and normalized_expression(step.get("if")) == "inputs.gerrit-refspec==''"
        ]
        exact_patchset = [
            step
            for step in checkout_steps
            if str(step.get("uses", "")).startswith(
                "lfreleng-actions/checkout-gerrit-change-action@"
            )
            and normalized_expression(step.get("if")) == "inputs.gerrit-refspec!=''"
        ]
        destructive_ref_replacement = re.compile(r"\bgit\s+(?:checkout|switch|reset)\b")
        return {
            "complete_action_set": len(checkout_steps) == 2,
            "branch_or_pr": len(branch_or_pr) == 1
            and (branch_or_pr[0].get("with") or {}).get("fetch-depth") == 2
            and (branch_or_pr[0].get("with") or {}).get("persist-credentials") is False,
            "exact_patchset": len(exact_patchset) == 1
            and all(
                (exact_patchset[0].get("with") or {}).get(key) == f"${{{{ inputs.{key} }}}}"
                for key in ("gerrit-refspec", "gerrit-project", "gerrit-url")
            ),
            "no_destructive_ref_replacement": not any(
                destructive_ref_replacement.search(line)
                for step in steps
                for line in executable_lines(step.get("run", ""))
            ),
        }

    github_expression = re.compile(r"\$\{\{(?P<body>.*?)\}\}", re.DOTALL)
    secret_context = re.compile(r"\bsecrets\b")

    def has_secret_expression(value: str) -> bool:
        return any(
            secret_context.search(match.group("body"))
            for match in github_expression.finditer(value)
        )

    def secret_paths(node: Any, path: str = "$") -> list[str]:
        found: list[str] = []
        if isinstance(node, dict):
            for key, value in node.items():
                child_path = f"{path}.{key}"
                if key == "secrets":
                    found.append(child_path)
                found.extend(secret_paths(value, child_path))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                found.extend(secret_paths(value, f"{path}[{index}]"))
        elif isinstance(node, str) and has_secret_expression(node):
            found.append(path)
        return found

    def mutation_call_contract(job: Any) -> dict[str, Any]:
        return {
            "uses_reusable": isinstance(job, dict) and job.get("uses") == _REUSABLE_MUTATION,
            "secret_paths": secret_paths(job),
        }

    def active_by_default(job: Any) -> bool:
        return isinstance(job, dict) and (
            "if" not in job or normalized_expression(job.get("if")).lower() == "true"
        )

    def full_route_or_classifier_fallback(job: Any) -> bool:
        condition = normalized_expression(job.get("if")) if isinstance(job, dict) else ""
        return "needs.classify.result!='success'||needs.classify.outputs.route=='full'" in condition

    reusable_raw = _read(_ROOT / ".github" / "workflows" / "_mutation.yml")
    workflow = yaml.safe_load(reusable_raw)
    test_workflow = yaml.safe_load(_read(_TEST_YML))
    gerrit_workflow = yaml.safe_load(_read(_GERRIT_YML))
    sweep_workflow = yaml.safe_load(_read(_ROOT / ".github" / "workflows" / "mutation.yml"))

    jobs = workflow.get("jobs") or {}
    selector = jobs.get("selector") or {}
    mutation = jobs.get("mutation") or {}
    selector_steps = selector.get("steps") or []
    mutation_steps = mutation.get("steps") or []
    select_steps = [
        step
        for step in selector_steps
        if any("scripts/mutation_gate.py" in line for line in executable_lines(step.get("run", "")))
    ]
    driver_steps = [
        step for step in mutation_steps if "mutation_gate.py run" in str(step.get("run", ""))
    ]
    artifact_steps = [
        step for step in mutation_steps if "actions/upload-artifact@" in str(step.get("uses", ""))
    ]
    workflow_triggers = trigger_map(workflow)
    workflow_call = (
        workflow_triggers.get("workflow_call") if workflow_triggers is not None else None
    )
    select_lines = executable_lines(select_steps[0].get("run")) if len(select_steps) == 1 else []
    driver_run = str(driver_steps[0].get("run", "")) if len(driver_steps) == 1 else ""
    artifact = artifact_steps[0] if len(artifact_steps) == 1 else {}
    outputs = selector.get("outputs") or {}
    strategy = mutation.get("strategy") or {}
    needs = mutation.get("needs") or []
    needs = [needs] if isinstance(needs, str) else needs
    test_triggers = trigger_map(test_workflow)
    test_mutation = (test_workflow.get("jobs") or {}).get("mutation")
    gerrit_mutation = (gerrit_workflow.get("jobs") or {}).get("mutation")
    gerrit_vote = (gerrit_workflow.get("jobs") or {}).get("vote") or {}
    vote_needs = gerrit_vote.get("needs") or []
    vote_needs = [vote_needs] if isinstance(vote_needs, str) else vote_needs
    sweep_triggers = trigger_map(sweep_workflow)
    sweep_mutation = (sweep_workflow.get("jobs") or {}).get("mutation")
    contract = {
        "reusable_workflow_call_mapping": isinstance(workflow_call, dict),
        "selector_outputs_from_step": all(
            outputs.get(name) == f"${{{{ steps.select.outputs.{name} }}}}"
            for name in ("matrix", "has-shards")
        ),
        "selector_step_count": len(select_steps),
        "selector_step_id": select_steps[0].get("id") if len(select_steps) == 1 else None,
        "selector_json_dataflow": selector_dataflow_contract(select_lines),
        "selector_checkout": checkout_contract(selector_steps),
        "mutation_needs_selector": "selector" in needs,
        "mutation_guarded_by_has_shards": normalized_expression(mutation.get("if"))
        == "needs.selector.outputs.has-shards=='true'",
        "matrix_from_selector_json": strategy.get("matrix")
        == "${{ fromJSON(needs.selector.outputs.matrix) }}",
        "fail_fast": strategy.get("fail-fast"),
        "timeout_minutes": mutation.get("timeout-minutes"),
        "mutation_checkout": checkout_contract(mutation_steps),
        "driver_step_count": len(driver_steps),
        "one_matrix_shard": driver_run.count("--shard") == 1
        and "${{ matrix.shard }}" in driver_run
        and "--all-shards" not in driver_run,
        "per_shard_artifact": normalized_expression(artifact.get("if")) == "always()"
        and "${{ matrix.shard }}" in str((artifact.get("with") or {}).get("name", "")),
        "reusable_secret_paths": secret_paths(workflow),
        "callers": {
            "push_and_pull_request": isinstance(test_triggers, dict)
            and "push" in test_triggers
            and "pull_request" in test_triggers,
            "test_mutation": {
                **mutation_call_contract(test_mutation),
                "eligible_on_push_and_pull_request": isinstance(test_mutation, dict)
                and normalized_expression(test_mutation.get("if"))
                == "github.event_name!='schedule'",
            },
            "gerrit_mutation": {
                **mutation_call_contract(gerrit_mutation),
                "full_route_or_classifier_fallback": full_route_or_classifier_fallback(
                    gerrit_mutation
                ),
                "exact_inputs": isinstance(gerrit_mutation, dict)
                and all(
                    (gerrit_mutation.get("with") or {}).get(name) == value
                    for name, value in {
                        "gerrit-refspec": "${{ inputs.GERRIT_REFSPEC }}",
                        "gerrit-project": "${{ inputs.GERRIT_PROJECT }}",
                        "gerrit-url": "https://${{ vars.GERRIT_SERVER }}",
                    }.items()
                ),
                "included_in_vote_needs": "mutation" in vote_needs,
            },
            "weekly_and_manual": isinstance(sweep_triggers, dict)
            and "schedule" in sweep_triggers
            and "workflow_dispatch" in sweep_triggers,
            "sweep_mutation": {
                **mutation_call_contract(sweep_mutation),
                "active_by_default": active_by_default(sweep_mutation),
                "all_shards": isinstance(sweep_mutation, dict)
                and (sweep_mutation.get("with") or {}).get("all-shards") is True,
            },
        },
    }

    assert contract == {
        "reusable_workflow_call_mapping": True,
        "selector_outputs_from_step": True,
        "selector_step_count": 1,
        "selector_step_id": "select",
        "selector_json_dataflow": {
            "canonical_sequence_is_unique_and_ordered": True,
            "assignment_lines": [
                "selector_args=(select --base HEAD^ --head HEAD)",
                "selector_args+=(--all-shards)",
                'selection_json="$(uv run --locked python scripts/mutation_gate.py '
                '"${selector_args[@]}")"',
                'matrix="$(jq -c \'{shard: .selected_shards}\' <<<"$selection_json")"',
                'has_shards="$(jq -r \'.empty_selection | not\' <<<"$selection_json")"',
            ],
        },
        "selector_checkout": {
            "complete_action_set": True,
            "branch_or_pr": True,
            "exact_patchset": True,
            "no_destructive_ref_replacement": True,
        },
        "mutation_needs_selector": True,
        "mutation_guarded_by_has_shards": True,
        "matrix_from_selector_json": True,
        "fail_fast": False,
        "timeout_minutes": 30,
        "mutation_checkout": {
            "complete_action_set": True,
            "branch_or_pr": True,
            "exact_patchset": True,
            "no_destructive_ref_replacement": True,
        },
        "driver_step_count": 1,
        "one_matrix_shard": True,
        "per_shard_artifact": True,
        "reusable_secret_paths": [],
        "callers": {
            "push_and_pull_request": True,
            "test_mutation": {
                "uses_reusable": True,
                "secret_paths": [],
                "eligible_on_push_and_pull_request": True,
            },
            "gerrit_mutation": {
                "uses_reusable": True,
                "secret_paths": [],
                "full_route_or_classifier_fallback": True,
                "exact_inputs": True,
                "included_in_vote_needs": True,
            },
            "weekly_and_manual": True,
            "sweep_mutation": {
                "uses_reusable": True,
                "secret_paths": [],
                "active_by_default": True,
                "all_shards": True,
            },
        },
    }


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


def test_documentation_gate_signatures_live_in_the_shared_action() -> None:
    """Extracted documentation checks remain one definition used by both CI routes."""
    action = _read(_DOCS_ACTION)
    bat = _read(_BAT_YML)
    assert "./.github/actions/docs-gates" in bat
    for label, signature in _DOCUMENTATION_GATE_SIGNATURES.items():
        assert signature in action, f"documentation gate {label!r} was dropped from the action"
        assert signature not in bat, f"documentation gate {label!r} was copied back inline"


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
    """
    for filename, step, body in _all_workflow_pytest_steps():
        where = f"{filename} step {step!r}"
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
    import yaml

    vote_needs = yaml.safe_load(gerrit_yml)["jobs"]["vote"]["needs"]
    assert "optionality" in vote_needs, (
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
    import tempfile

    with tempfile.TemporaryDirectory(prefix="rebar-pytest-collect-") as temp_dir:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "--collect-only",
                "-q",
                "-m",
                selection,
                "--basetemp",
                str(Path(temp_dir) / "pytest"),
                *paths,
            ],
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


_REPO_POLICY_MODULES = (
    "tests/unit/test_comment_hygiene_guard.py",
    "tests/scripts/test_check_config_reads_heldout.py",
    "tests/unit/test_tests_import_convention.py",
    "tests/unit/test_external_isolation.py",
    "tests/unit/test_compute_validity_callers.py",
    "tests/unit/test_canonical.py",
    "tests/unit/test_subcall_class_selection.py",
)
_REPO_POLICY_NODE_IDS = {
    "tests/unit/test_comment_hygiene_guard.py::test_the_real_tree_is_clean",
    "tests/scripts/test_check_config_reads_heldout.py::test_real_repo_is_clean_with_default_paths",
    "tests/scripts/test_check_config_reads_heldout.py::test_real_schema_markers_all_carry_pointers",
    "tests/scripts/test_check_config_reads_heldout.py::test_string_read_fields_keep_their_marker_and_do_not_fire[lock_lease_secs-getattr]",
    "tests/scripts/test_check_config_reads_heldout.py::test_string_read_fields_keep_their_marker_and_do_not_fire[require_plan_review_for_close-gate_enabled]",
    "tests/scripts/test_check_config_reads_heldout.py::"
    "test_string_read_fields_keep_their_marker_and_do_not_fire"
    "[require_plan_review_for_claim-string key]",
    "tests/scripts/test_check_config_reads_heldout.py::"
    "test_string_read_fields_keep_their_marker_and_do_not_fire"
    "[require_completion_verification_for_close-string key]",
    "tests/scripts/test_check_config_reads_heldout.py::test_the_real_schema_parks_no_markers_against_a_ticket",
    "tests/scripts/test_check_config_reads_heldout.py::test_ci_wires_the_gate",
    "tests/unit/test_tests_import_convention.py::test_no_tests_rooted_imports_anywhere_under_tests",
    "tests/unit/test_external_isolation.py::test_no_external_marked_test_outside_external_dir",
    "tests/unit/test_compute_validity_callers.py::test_every_compute_validity_call_consumes_a_mapping_not_a_tuple",
    "tests/unit/test_compute_validity_callers.py::test_plan_validity_profiles_are_selected_by_named_keyword",
    "tests/unit/test_canonical.py::test_no_raw_event_serializers_in_src",
    "tests/unit/test_subcall_class_selection.py::test_provenance_scan_preserves_verdicts_with_linear_whole_tree_work",
    "tests/unit/test_subcall_class_selection.py::test_the_provenance_analysis_can_see_the_sites_it_judges",
    "tests/unit/test_subcall_class_selection.py::test_no_run_request_inherits_the_raw_config_model",
    "tests/unit/test_subcall_class_selection.py::test_every_unfollowable_site_is_registered_with_a_reason",
    "tests/unit/test_subcall_class_selection.py::test_neither_registry_has_stale_entries",
}


def test_repo_policy_nodes_partition_the_default_selection_exactly() -> None:
    """The 19 OS-invariant guards run once without hiding mixed-module behavior."""
    default_nodes = set(_collect_node_ids(_REPO_POLICY_MODULES, _DEFAULT_SELECTION))
    policy_nodes = set(_collect_node_ids(_REPO_POLICY_MODULES, "repo_policy"))
    non_policy_nodes = set(
        _collect_node_ids(_REPO_POLICY_MODULES, f"{_DEFAULT_SELECTION} and not repo_policy")
    )

    assert policy_nodes == _REPO_POLICY_NODE_IDS
    assert policy_nodes.isdisjoint(non_policy_nodes)
    assert policy_nodes | non_policy_nodes == default_nodes


def test_repo_policy_nodes_run_only_in_the_existing_primary_cell() -> None:
    """Route policy nodes by selector, without a new job or pytest session."""
    import yaml

    workflow = yaml.safe_load(_read(_BAT_YML))
    assert set(workflow["jobs"]) == {"lint", "pip-audit", "test"}
    cells = _expanded_test_matrix(workflow)
    assert cells == [
        ("ubuntu-latest", "3.11"),
        ("ubuntu-latest", "3.12"),
        ("ubuntu-latest", "3.13"),
        ("macos-latest", "3.13"),
    ]

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
        r"\$\{\{\s*matrix\.os == 'ubuntu-latest'\s*&&\s*"
        r"matrix\.python-version == '3\.13'\s*&&\s*"
        r"'(?P<primary>[^']+)'\s*\|\|\s*'(?P<non_primary>[^']+)'\s*\}\}",
        selector_expression,
    )
    assert expression is not None
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
    labels = {m.group(1) for m in re.finditer(r'"([a-z0-9_]+):([a-z0-9_,]+)"', run)}
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
    specs = {m.group(1): m.group(2) for m in re.finditer(r'"([a-z0-9_]+):([a-z0-9_,]+)"', run)}

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


def test_makefile_lint_runs_both_config_gates() -> None:
    """The portable, no-CI-required `make lint` path runs BOTH the ownership-direction gate
    and the field-consumption gate, so the trigger is operation-linked, not CI-only."""
    body = _lint_target_body(_read(_MAKEFILE))
    for script in _CONFIG_GATE_SCRIPTS:
        assert script in body, (
            f"`make lint` no longer invokes {script} in its `lint` target — the portable "
            "config gate would stop running for contributors without a CI provider."
        )


def test_ci_runs_the_config_gates_through_make_lint_not_a_duplicate_step() -> None:
    """CI inherits both gates via its `make lint` step; the field-consumption gate is no
    longer ALSO a standalone workflow step (which would run it twice per job)."""
    bat = _read(_BAT_YML)
    assert "make lint" in bat, "the reusable workflow must still run `make lint` (runs the gates)"
    assert "scripts/check_config_reads.py" not in bat, (
        "the field-consumption gate is still a standalone step in _build-and-test.yml — it now "
        "runs via `make lint`, so the standalone step double-runs it; remove the standalone step."
    )
    assert "scripts/check_config_ownership.py" not in bat, (
        "the ownership gate must run via `make lint`, not as a duplicate standalone workflow step."
    )
