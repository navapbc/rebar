"""Verify each code-review step resolves its declared model before reaching the runner.

`RunnerAgentStep` remains in the execution path while an injected underlying runner records
`req.config.model`. Distinct class-table sentinels separate declared classes from `cfg.model`.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
import yaml

from rebar.llm import usage_log
from rebar.llm.code_review.batch_runner import CodeReviewBatchRunner
from rebar.llm.config import LLMConfig
from rebar.llm.workflow import executor as _ex
from rebar.llm.workflow import migrate as _migrate
from rebar.llm.workflow import steps as _steps  # noqa: F401 — registers the code_review ops
from rebar.llm.workflow.runs import RunnerAgentStep

pytestmark = pytest.mark.unit

_GATE = pathlib.Path("src/rebar/llm/workflow/gates/code-review.yaml")

# Deliberately unlike any real id, and unlike each other, so an assertion can only pass by
# resolving the declared class — never by coincidence with the config default.
_CFG_MODEL = "test:cfg-default"
_FRONTIER = "test:frontier-model"
_STANDARD = "test:standard-model"
_TRIVIAL = "test:trivial-model"

_ENV_VARS = tuple(
    f"REBAR_LLM_{cls}_{field}"
    for cls in ("TRIVIAL", "STANDARD", "FRONTIER")
    for field in ("MODEL", "PROVIDER", "ENDPOINT")
)

_DIFF = "diff --git a/src/auth/login.py b/src/auth/login.py\n+++ b/src/auth/login.py\n+x\n"


@pytest.fixture
def class_table(monkeypatch):
    """Install distinct class sentinels after clearing ambient model overrides."""
    from rebar.llm import config as llm_config

    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        llm_config,
        "_read_llm_file_table",
        lambda repo_root=None: {
            "model_classes": {
                "trivial": {"model": _TRIVIAL},
                "standard": {"model": _STANDARD},
                "frontier": {"model": _FRONTIER},
            }
        },
    )


class _RecordingRunner:
    """Record each post-resolution model with its executor-bound step ID."""

    name = "recording"

    def __init__(self) -> None:
        self.calls: list[tuple[str | None, str]] = []

    def run(self, req: Any) -> dict:
        active = usage_log.active_step()
        self.calls.append((active[0] if active else None, req.config.model))
        return _canned(req.output_schema)

    def by_step(self) -> dict[str | None, set[str]]:
        """step id -> the set of models its calls ran on."""
        out: dict[str | None, set[str]] = {}
        for step_id, model in self.calls:
            out.setdefault(step_id, set()).add(model)
        return out


def _canned(schema: str | None) -> dict:
    """A schema-shaped payload, so the workflow reaches every LLM step rather than short-
    circuiting on a missing output. The CONTENT is irrelevant here; only the models are asserted.
    """
    if schema == "code_review_base_output":
        return {
            "findings": [
                {
                    "finding": "base note",
                    "criteria": ["correctness"],
                    "evidence": ["a.py:1"],
                    "location": "a.py:1",
                }
            ],
            # Escalate two overlays so Round-B has non-empty membership and therefore runs.
            "recommend_overlays": [
                {"overlay_id": "tests", "reason": "escalate"},
                {"overlay_id": "performance", "reason": "escalate"},
            ],
        }
    if schema == "code_review_findings":  # an overlay finder (Round-A / Round-B batch work)
        return {
            "findings": [
                {
                    "finding": "overlay finding",
                    "criteria": ["overlay"],
                    "evidence": ["o.py:1"],
                    "location": "o.py:1",
                }
            ]
        }
    if schema in ("verification", "code_review_verification"):
        return {"verifications": []}
    if schema == "code_review_coach":
        return {"notes": []}
    return {}


class _Rec(_ex.RunRecorder):
    def __init__(self) -> None:
        self.store: dict = {}

    def run_started(self, record): ...
    def run_finished(self, record): ...

    def step_recorded(self, record):
        if record.get("status") == "running":
            return
        self.store[record.get("frame_key") or record.get("step_id")] = dict(record)

    def completed_step(self, run_id, frame_key):
        rec = self.store.get(frame_key)
        return rec if rec and rec.get("status") == "succeeded" else None


def _run_gate() -> _RecordingRunner:
    """Run the REAL gate document offline, with the production agent step in place."""
    doc = _migrate.migrate_to_current(yaml.safe_load(_GATE.read_text()))
    runner = _RecordingRunner()
    cfg = LLMConfig(model=_CFG_MODEL, runner="fake")
    _ex.run_workflow(
        doc,
        {"base": "HEAD~1", "head": "HEAD", "diff_text": _DIFF, "changed_files": []},
        recorder=_Rec(),
        scripted_registry=dict(_ex.STEP_REGISTRY),
        # The production agent step, with the fake UNDERNEATH it — not instead of it.
        agent_runner=RunnerAgentStep(runner=runner, config=cfg),
        batch_runner=CodeReviewBatchRunner(context="## Diff\n(fake)"),
    )
    return runner


# ── the attribution carrier (Step 0: without it, nothing below can be attributed) ───────────


def test_every_llm_call_is_attributed_to_a_declaring_step(class_table):
    """No LLM call may be anonymous: `op` (the prompt name) cannot separate `base` from the
    `round_a`/`round_b` batch finder, since all three use `code-review-base`."""
    runner = _run_gate()
    assert runner.calls, "the gate made no LLM calls — the harness is not exercising the steps"
    assert None not in runner.by_step(), "an LLM call was made with no owning step id"


def test_batch_and_prompt_steps_are_both_attributed(class_table):
    """The four LLM-bearing declarations must each appear, so a later assertion about one of
    them cannot pass vacuously by that step never having run."""
    steps = set(runner_steps := _run_gate().by_step())
    assert {"base", "round_a", "round_b", "verify", "coach"} <= steps, runner_steps


# ── the defect itself: declared classes must reach the runner ───────────────────────────────


def test_base_step_runs_on_its_declared_frontier_class(class_table):
    assert _run_gate().by_step()["base"] == {_FRONTIER}


def test_verify_step_runs_on_its_declared_standard_class(class_table):
    """172e landed `model: standard` here; the live run showed OPUS (i.e. cfg.model)."""
    assert _run_gate().by_step()["verify"] == {_STANDARD}


def test_coach_step_runs_on_its_declared_standard_class(class_table):
    assert _run_gate().by_step()["coach"] == {_STANDARD}


def test_batch_rounds_run_on_their_declared_ladder_entry_class(class_table):
    by_step = _run_gate().by_step()
    assert by_step["round_a"] == {_FRONTIER}
    assert by_step["round_b"] == {_FRONTIER}


def test_no_llm_call_falls_through_to_the_config_default(class_table):
    """The summary assertion, and the one that names the failure mode: every LLM step in this
    gate declares a class, so `cfg.model` reaching the runner means a declaration was dropped."""
    offenders = {s for s, models in _run_gate().by_step().items() if _CFG_MODEL in models}
    assert not offenders, f"steps that fell through to cfg.model: {sorted(offenders)}"
