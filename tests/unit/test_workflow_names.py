"""Config-as-artifact gate for GitHub Actions workflow naming (ticket b550).

The Actions UI lists workflows by their ``name:`` field, so a lowercase or
filename-echoing ``name:`` is a discoverability wart. This test pins every
workflow file to a descriptive, Title-Case display name (the "locked table")
and enforces the ecosystem-standard extension convention:

* filenames are ``.yml`` — the sole allowed ``.yaml`` is ``gerrit-verify.yaml``,
  whose name is load-bearing (Gerrit's gerrit-to-platform plugin dispatches CI
  by matching the filename substring ``verify`` + ``gerrit``; renaming it — incl.
  the extension surface — is off-limits);
* every workflow present must appear in the locked table, so a newly-added
  workflow with no deliberate ``name:`` fails here until it is named.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_WORKFLOWS_DIR = Path(__file__).resolve().parents[2] / ".github" / "workflows"

# filename -> exact expected `name:` display value. This IS the locked table.
_EXPECTED_NAMES: dict[str, str] = {
    "gerrit-verify.yaml": "Gerrit Verified Gate",
    "release.yml": "Release",
    "_optionality.yml": "Optional-Dependency Isolation (reusable)",
    "optionality.yml": "Optional-Dependency Isolation (mirror)",
    "test.yml": "Test Suite (mirror)",
    "verify-identity.yml": "Verify Authorship Identity",
    "prompt-eval.yml": "Prompt Eval",
    "external-integration.yml": "Live External Integration",
    "mirror-guard.yml": "Mirror Guard",
    "lockdown.yml": "PR Lockdown",
    "terraform-drift.yml": "Terraform Drift",
    "codeql.yml": "CodeQL SAST",
    "reconcile-bridge.yml": "Reconcile Bridge",
    "reconcile-bridge-canary.yml": "Reconciler Heartbeat Canary",
}

# The one filename permitted to keep the `.yaml` extension (Gerrit g2p match).
_ALLOWED_YAML = {"gerrit-verify.yaml"}


def _present_workflows() -> set[str]:
    return {p.name for p in _WORKFLOWS_DIR.glob("*.yml")} | {
        p.name for p in _WORKFLOWS_DIR.glob("*.yaml")
    }


@pytest.mark.parametrize(("filename", "expected"), sorted(_EXPECTED_NAMES.items()))
def test_workflow_name_matches_locked_table(filename: str, expected: str) -> None:
    path = _WORKFLOWS_DIR / filename
    assert path.exists(), f"expected workflow {filename} is missing"
    doc = yaml.safe_load(path.read_text())
    assert doc.get("name") == expected, (
        f"{filename}: name: is {doc.get('name')!r}, expected {expected!r} "
        f"(update the locked table in this test if the rename is intentional)"
    )


def test_every_workflow_is_named_in_the_locked_table() -> None:
    """A new workflow with no deliberate display name fails until it's added here."""
    present = _present_workflows()
    unlisted = present - set(_EXPECTED_NAMES)
    assert not unlisted, (
        f"workflow(s) {sorted(unlisted)} have no entry in the locked naming table — "
        f"give each a descriptive Title-Case name: and register it in _EXPECTED_NAMES"
    )
    missing = set(_EXPECTED_NAMES) - present
    assert not missing, f"locked table references absent workflow file(s): {sorted(missing)}"


def test_only_gerrit_verify_uses_the_yaml_extension() -> None:
    stray_yaml = {p.name for p in _WORKFLOWS_DIR.glob("*.yaml")} - _ALLOWED_YAML
    assert not stray_yaml, (
        f"{sorted(stray_yaml)} use .yaml; workflows standardize on .yml "
        f"(only {sorted(_ALLOWED_YAML)} is exempt — Gerrit g2p filename match)"
    )
