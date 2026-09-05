"""The Verified gate's run NAME must carry the patchset its ``head_sha`` cannot (ticket 2a21).

``.github/workflows/gerrit-verify.yaml`` is dispatched from ``main``, so GitHub stamps every
run with the sha of the WORKFLOW FILE — then the job checks out a Gerrit patchset ref. The API
therefore reports a ``head_sha`` that has nothing to do with what was tested, and ``gh run
list`` presents runs of unrelated patchsets as same-sha contradictions. GitHub gives a
``workflow_dispatch`` run no way to restate its own ``head_sha``, so the identity has to ride
on the one field the workflow does control and ``gh run list`` does show: the top-level
``run-name``.

These tests pin that identity structurally — parsed from the YAML document, not matched
against the file's text — so a future edit that drops the change/patchset fragment, or that
interpolates an input the workflow does not declare (which renders as an empty fragment rather
than failing), is caught here instead of silently regressing the signal.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "gerrit-verify.yaml"

# ``${{ ... }}`` expression bodies, and the ``inputs.NAME`` references inside one.
_EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
_INPUT_REF = re.compile(r"\binputs\.([A-Za-z_][A-Za-z0-9_-]*)")

# The two fragments that make two runs of different patchsets distinguishable.
REQUIRED_INPUT_REFS = frozenset({"GERRIT_CHANGE_NUMBER", "GERRIT_PATCHSET_NUMBER"})


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def run_name(workflow: dict[str, Any]) -> str:
    name = workflow.get("run-name")
    assert isinstance(name, str) and name.strip(), (
        "gerrit-verify.yaml declares no top-level `run-name:`, so every run's title is the "
        "workflow name and `gh run list` cannot tell two patchsets apart"
    )
    return name


@pytest.fixture(scope="module")
def declared_inputs(workflow: dict[str, Any]) -> set[str]:
    # PyYAML resolves the bare key `on` to the boolean True (YAML 1.1 truthiness).
    triggers = workflow.get(True, workflow.get("on"))
    assert isinstance(triggers, dict), "gerrit-verify.yaml declares no triggers"
    return set(triggers["workflow_dispatch"]["inputs"])


def _expressions(run_name: str) -> list[str]:
    return _EXPRESSION.findall(run_name)


def _referenced_inputs(run_name: str) -> set[str]:
    return {name for body in _expressions(run_name) for name in _INPUT_REF.findall(body)}


def test_run_name_identifies_the_change_and_patchset(run_name: str) -> None:
    """AC1: the run's own name names the change + patchset it tested."""
    missing = REQUIRED_INPUT_REFS - _referenced_inputs(run_name)
    assert not missing, (
        f"run-name does not interpolate {sorted(missing)}, so two runs of different "
        f"patchsets are indistinguishable in `gh run list`: {run_name!r}"
    )


def test_run_name_only_reads_declared_dispatch_inputs(
    run_name: str, declared_inputs: set[str]
) -> None:
    """AC2: an undeclared input renders as an EMPTY fragment, not an error — pin it here."""
    undeclared = _referenced_inputs(run_name) - declared_inputs
    assert not undeclared, (
        f"run-name interpolates {sorted(undeclared)}, which gerrit-verify.yaml does not "
        "declare as a workflow_dispatch input; GitHub renders it as an empty fragment"
    )


def test_run_name_adds_no_new_required_input(declared_inputs: set[str]) -> None:
    """AC2: the identity is derived from inputs the dispatcher ALREADY sends."""
    assert REQUIRED_INPUT_REFS <= declared_inputs, (
        "run-name must be built from the existing GERRIT_* dispatch inputs; "
        f"declared inputs are {sorted(declared_inputs)}"
    )


def test_run_name_has_a_literal_fallback_branch(run_name: str) -> None:
    """AC2/AC4: a dispatch without the Gerrit inputs must still render a sensible name.

    Structural check: at least one alternative of a ``||`` in the expression must be free of
    ``inputs.`` references, i.e. a literal the run falls back to instead of rendering nothing.
    """
    alternatives = [
        alternative
        for body in _expressions(run_name)
        for alternative in body.split("||")
        if alternative.strip()
    ]
    assert any(not _INPUT_REF.search(alt) for alt in alternatives), (
        "run-name has no `||` fallback free of `inputs.` references, so a manual or "
        f"input-less dispatch renders an empty or null fragment: {run_name!r}"
    )
