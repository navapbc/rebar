"""The external tier's live tests must actually REACH the live model.

Regression guard for bug ``baa8-2c41-d9b9-4642``. Two live tests exist specifically to
prove the LLM tier runs for real, and both had silently stopped doing so — each for its
own reason, and each invisible until bug ``8070`` unmasked the tier:

* ``test_execution_mode_live.py`` wrote its ``single_turn`` prompt override into the
  scratch repo's WORKING TREE and never committed it. A workflow with LLM steps runs
  inside the snapshot gate (``src/rebar/llm/workflow/runs.py``), and the suite defaults to
  ``REBAR_GATE_SOURCE=attested`` / ``REBAR_GATE_REF=HEAD`` (``tests/conftest.py``), so
  ``prompts.get_prompt`` resolves against the materialized snapshot at ``HEAD`` — where an
  uncommitted override simply does not exist. The step died on ``PromptNotFound`` and the
  run came back ``status='failed'``.
* ``test_workflow_live.py``'s plan fixture carried no ``## Testing`` / ``## Verification``
  section and no AC item with a code span or verification vocabulary, so the BLOCKING DET
  clarity check ``P10 verification-presence``
  (``src/rebar/llm/plan_review/det_clarity.py``, added 2026-07-30) failed and the DET-floor
  short-circuit in ``src/rebar/llm/plan_review/workflow_ops.py`` returned a BLOCK verdict
  with ``coverage.llm_ran=False`` and ``llm_calls=0`` — never calling the model at all.

Both guards live in the DEFAULT tier on purpose, following the precedent set by
``tests/unit/test_external_tier_gate_ref.py``: ``tests/conftest.py`` auto-marks everything
under ``tests/external/`` as ``external``, so an in-tier guard could only fail in the same
weekly credential-gated job that already went unread for days. These need no credentials
and make no billable model call — they assert the PRECONDITIONS the live tests depend on,
against the very artifacts those tests use.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from rebar.llm import gate_source
from rebar.llm.prompting import prompts

_EXTERNAL_CONFTEST = Path(__file__).resolve().parents[1] / "external" / "conftest.py"


def _load_external_conftest() -> ModuleType:
    """Import ``tests/external/conftest.py`` by path (conftests are not importable by name)."""
    spec = importlib.util.spec_from_file_location(
        "_rebar_external_live_conftest", _EXTERNAL_CONFTEST
    )
    assert spec is not None and spec.loader is not None, _EXTERNAL_CONFTEST
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_external_project_prompt_is_visible_to_an_attested_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prompt written by the external tier must resolve from the ATTESTED snapshot.

    Drives the real mechanism: build the tier's scratch repo, write a project prompt with
    the tier's own writer, materialize a gate handle under the suite's attested/``HEAD``
    default, then resolve the prompt against the snapshot root the gate re-roots onto
    (``gate_source.gate_read_root``). Before the fix the writer left the file uncommitted,
    so this raised ``PromptNotFound`` — exactly the ``unknown prompt 'live-verdict'`` that
    made the live ``single_turn`` run come back ``failed``.
    """
    monkeypatch.setenv("REBAR_GATE_SOURCE", "attested")
    monkeypatch.setenv("REBAR_GATE_REF", "HEAD")

    conftest = _load_external_conftest()
    repo = tmp_path / "repo"
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    conftest.build_scratch_rebar_repo(repo)

    prompt_id = "live-verdict"
    conftest.write_project_prompt(
        repo,
        prompt_id,
        "---\nexecution_mode: single_turn\noutputs: completion_verdict\n---\nBody.",
    )
    # Fixture precondition: the override really is on disk in the working tree.
    assert (repo / ".rebar" / "prompts" / f"{prompt_id}.md").is_file()

    handle = gate_source.resolve_gate_handle(None, None, str(repo))
    assert handle.source == "attested", handle

    # The postcondition the live test depends on: the prompt the gate will actually read
    # (from the pinned snapshot, NOT the mutable checkout) is the project override.
    resolved = prompts.get_prompt(prompt_id, repo_root=str(handle.path))
    assert resolved.execution_mode == "single_turn", resolved
    assert resolved.text.strip() == "Body.", (
        "the gate resolved SOME prompt, but not the project override written above — "
        f"got {resolved.text!r}"
    )


def test_external_plan_review_fixture_clears_the_blocking_det_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live plan-review guard's plan must not be blocked by the DET floor.

    ``review_plan`` short-circuits BEFORE the LLM tier on any blocking DET finding, and
    reports ``coverage.llm_ran=False``. So a plan fixture that trips a blocking DET check
    turns the live "did we reach a real model?" assertion into a guaranteed failure while
    spending nothing — the live test can never pass. Asserts the shared fixture plan the
    live test reviews produces NO blocking DET findings.
    """
    monkeypatch.setenv("REBAR_ROOT", str(tmp_path / "repo"))
    conftest = _load_external_conftest()
    plan = conftest.PLAN_REVIEW_FIXTURE_PLAN

    from rebar.llm.plan_review import det_clarity, det_floor

    ctx = _plan_context(plan)
    p10 = det_clarity.p10_verification_presence(ctx)
    assert p10.status == "pass", (
        "the live plan-review fixture states no verification, so the BLOCKING P10 check "
        "short-circuits review_plan before the LLM tier and coverage.llm_ran can only ever "
        f"be False. Add a '## Testing' section to PLAN_REVIEW_FIXTURE_PLAN. Got: {p10}"
    )
    blocking = det_floor.det_blocking_findings(det_floor.run_det_floor(ctx))
    assert blocking == [], f"blocking DET findings would short-circuit the live review: {blocking}"


def _plan_context(plan: str):
    """Build the minimal :class:`PlanContext` the DET clarity checks read."""
    from rebar.llm.plan_review.det_floor import PlanContext

    return PlanContext(
        ticket_id="baa8-2c41-d9b9-4642",
        ticket_type="story",
        title="Persist the review cache to disk",
        description=plan,
    )


def test_git_helper_commits_are_reachable_from_head(tmp_path: Path) -> None:
    """Sanity: the tier's scratch repo keeps a resolvable HEAD after a prompt write."""
    conftest = _load_external_conftest()
    repo = tmp_path / "repo"
    conftest.build_scratch_rebar_repo(repo)
    conftest.write_project_prompt(repo, "some-prompt", "---\n---\nBody.")
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    assert head.returncode == 0, head.stderr
