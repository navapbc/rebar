"""Workflow contract for Gerrit's trusted, fail-closed docs-only route."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from _subprocess_env import subprocess_env

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
GERRIT = ROOT / ".github" / "workflows" / "gerrit-verify.yaml"
BUILD = ROOT / ".github" / "workflows" / "_build-and-test.yml"
DOCS_ACTION = ROOT / ".github" / "actions" / "docs-gates" / "action.yml"

FULL_JOBS = {
    "build-and-test",
    "mutation",
    "optionality",
    "artifact-probe",
    "eval-discipline",
    "golden-path",
    "verify-identity",
}
DOC_GATE_COMMANDS = {
    "scripts/check_adr_numbers.py",
    "scripts/gen_adr_index.py",
    "scripts/gen_env_registry.py",
    "scripts/check_docs_index.py",
    "scripts/check_readme_quickstart.py",
    "scripts/gen_cli_reference.py",
    "scripts/gen_mcp_reference.py",
}
DOCS_ONLY_GUARDS = {
    "scripts/check_dco_identity.py",
    "scripts/check_criteria_vocabulary.py",
}


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _normalized(value: Any) -> str:
    return "".join(str(value).split())


def test_documentation_gates_have_one_shared_definition_and_both_callers() -> None:
    action_text = DOCS_ACTION.read_text(encoding="utf-8")
    build_text = BUILD.read_text(encoding="utf-8")

    for command in DOC_GATE_COMMANDS:
        assert action_text.count(command) >= 1
        assert command not in build_text

    build = _load(BUILD)
    test_steps = build["jobs"]["test"]["steps"]
    assert sum(s.get("uses") == "./.github/actions/docs-gates" for s in test_steps) == 1

    gerrit = _load(GERRIT)
    docs_steps = gerrit["jobs"]["docs-only"]["steps"]
    assert sum(s.get("uses") == "./.github/actions/docs-gates" for s in docs_steps) == 1
    docs_action = next(s for s in docs_steps if s.get("uses") == "./.github/actions/docs-gates")
    assert docs_action["with"]["run-contract-tests"] == "true"
    docs_inline = "\n".join(str(s.get("run", "")) for s in docs_steps)
    assert not any(command in docs_inline for command in DOC_GATE_COMMANDS)

    action_steps = _load(DOCS_ACTION)["runs"]["steps"]
    assert len(action_steps) >= len(DOC_GATE_COMMANDS)


def test_docs_only_lane_keeps_documentation_guards_and_contract_tests() -> None:
    """Skipping the default suite must not skip checks whose contract is documentation."""
    action = DOCS_ACTION.read_text(encoding="utf-8")
    for command in DOCS_ONLY_GUARDS:
        assert command in action
    assert "git grep -Il" in action
    # Directory names do not define pytest tiers: unmarked modules under
    # tests/integration can still carry documentation contracts that default
    # Verify runs. Leave those eligible and let the marker expression exclude
    # the genuinely expensive integration tier.
    assert "tests/integration/*" not in action
    assert "tests/external/" in action
    assert "python -m pytest" in action
    assert "not integration and not external" in action
    assert "pre-commit run check-merge-conflict --all-files" in action
    assert "pre-commit run check-added-large-files --all-files" in action


def test_documentation_contract_selector_keeps_unmarked_integration_modules(
    tmp_path: Path,
) -> None:
    """Run the action's selector, including its real Git path and shell semantics."""
    action_steps = _load(DOCS_ACTION)["runs"]["steps"]
    selector = next(
        step["run"] for step in action_steps if step.get("name") == "Documentation contract tests"
    )

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    files = {
        "tests/unit/test_docs.py": 'REFERENCE = "docs/README.md"\n',
        "tests/integration/test_doc_contract.py": (
            'GENERATED_REFERENCE = "docs/cli-reference.md"\n'
        ),
        "tests/external/test_docs.py": 'REFERENCE = "docs/external.md"\n',
        "tests/unit/conftest.py": 'REFERENCE = "docs/fixtures.md"\n',
        "tests/unit/test_code.py": 'SOURCE = "src/rebar/store.py"\n',
    }
    for relative, content in files.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)

    capture = tmp_path / "pytest-arguments"
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    python_stub = stub_dir / "python"
    python_stub.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$CAPTURE"\n',
        encoding="utf-8",
    )
    python_stub.chmod(0o755)
    env = subprocess_env(
        {
            "CAPTURE": str(capture),
            "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
        }
    )

    subprocess.run(["bash", "-c", selector], cwd=repo, env=env, check=True)
    arguments = capture.read_text(encoding="utf-8").splitlines()

    assert "not integration and not external" in arguments
    assert "tests/unit/test_docs.py" in arguments
    assert "tests/integration/test_doc_contract.py" in arguments
    assert "tests/external/test_docs.py" not in arguments
    assert "tests/unit/conftest.py" not in arguments
    assert "tests/unit/test_code.py" not in arguments


def test_classifier_uses_exact_patchset_and_trusted_main_logic() -> None:
    job = _load(GERRIT)["jobs"]["classify"]
    steps = job["steps"]
    text = GERRIT.read_text(encoding="utf-8")

    patchset = next(s for s in steps if "checkout-gerrit-change-action" in str(s.get("uses", "")))
    assert patchset["with"]["gerrit-refspec"] == "${{ inputs.GERRIT_REFSPEC }}"
    assert "HEAD^" in text and "HEAD" in text and "--deepen=" in text

    trusted = next(s for s in steps if s.get("name") == "Fetch the trusted route classifier")
    assert str(trusted["uses"]).startswith("actions/checkout@")
    assert trusted["with"]["ref"] == "main"
    assert trusted["with"]["persist-credentials"] is False
    assert trusted["with"]["sparse-checkout"] == "scripts/classify_gerrit_verify_change.py"


def test_patchsets_predating_the_local_action_get_a_trusted_main_fallback() -> None:
    for workflow_path in (BUILD, GERRIT):
        workflow = _load(workflow_path)
        serialized = workflow_path.read_text(encoding="utf-8")
        assert "Restore documentation action for a patchset that predates it" in serialized
        assert "https://github.com/${{ github.repository }}" in serialized
        assert (
            "refs/remotes/gh-main-docs-action:.github/actions/docs-gates/action.yml" in serialized
        )
        assert "::error::could not fetch trusted main documentation action" in serialized

        jobs = workflow["jobs"]
        steps = jobs["test"]["steps"] if workflow_path == BUILD else jobs["docs-only"]["steps"]
        names = [step.get("name") for step in steps]
        restore = names.index("Restore documentation action for a patchset that predates it")
        action = names.index(
            "Run the shared documentation gates"
            if workflow_path == GERRIT
            else "Documentation gates (shared with Gerrit docs-only route)"
        )
        assert restore < action


def test_full_jobs_cancel_on_supersession_and_fallback_on_classifier_failure() -> None:
    jobs = _load(GERRIT)["jobs"]
    cancellation_guard = "!cancelled()"
    fallback = "needs.classify.result!='success'||needs.classify.outputs.route=='full'"
    for name in FULL_JOBS:
        job = jobs[name]
        condition = _normalized(job["if"])
        assert "classify" in job["needs"]
        assert cancellation_guard in condition, name
        assert "always()" not in condition, name
        assert fallback in condition, name

    docs = jobs["docs-only"]
    condition = _normalized(docs["if"])
    assert "needs.classify.result=='success'" in condition
    assert "needs.classify.outputs.route=='docs-only'" in condition


def test_vote_requires_classifier_docs_lane_and_exactly_one_complete_route() -> None:
    workflow = _load(GERRIT)
    vote = workflow["jobs"]["vote"]
    needs = set(vote["needs"])
    assert {"classify", "docs-only", *FULL_JOBS} <= needs

    normalize = next(s for s in vote["steps"] if s.get("id") == "normalize")
    conclusion = _normalized(normalize["env"]["CONCLUSION"])
    assert "needs.classify.result=='success'" in conclusion
    assert "needs.docs-only.result=='success'" in conclusion
    assert "needs.docs-only.result=='skipped'" in conclusion
    for name in FULL_JOBS:
        assert f"needs.{name}.result=='success'" in conclusion
        assert f"needs.{name}.result=='skipped'" in conclusion


def test_clear_vote_ticket_and_vote_key_boundaries_remain_unconditional() -> None:
    jobs = _load(GERRIT)["jobs"]
    assert "if" not in jobs["clear-vote"]
    assert "if" not in jobs["require-ticket"]
    assert jobs["require-ticket"]["needs"] == "clear-vote"

    serialized = GERRIT.read_text(encoding="utf-8")
    assert serialized.count("secrets.GERRIT_SSH_PRIVKEY") == 3
    assert "secrets:" not in str(jobs["docs-only"])
    assert "secrets:" not in str(jobs["classify"])
