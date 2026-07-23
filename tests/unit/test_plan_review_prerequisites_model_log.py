"""Regression: the plan_review_prerequisite_indeterminate log must report the model that
ACTUALLY produced the record, not the configured model.

Bug (client report §4): run_bin escalates up a model ladder on a context-limit error and
re-runs on the higher model, but emit_indeterminate was called with ``model=cfg.model`` — so an
escalated call logged the configured (lower) model while the payload ran on the escalated one.
``attempt_count`` was likewise hardcoded to 1.
"""

from __future__ import annotations

import dataclasses
import logging

from rebar.llm.config import LLMConfig
from rebar.llm.plan_review import prerequisites, sizing


class _ContextLimit(Exception):
    """A stand-in for a provider context-window overflow."""


class _EscalatingRunner:
    """Raise a context-limit error on the low model; succeed-as-indeterminate on the high one."""

    def __init__(self) -> None:
        self.models_seen: list[str] = []

    def run(self, req):  # noqa: ANN001 - RunRequest
        model = req.config.model
        self.models_seen.append(model)
        if model == "low":
            raise _ContextLimit("context window exceeded")
        # High model runs but returns an empty payload -> normalizes to indeterminate.
        return {"records": []}


def test_indeterminate_log_reports_escalated_model_not_cfg_model(monkeypatch, caplog) -> None:
    block = sizing.PrerequisiteBlock("aaaa-bbbb-cccc-dddd", "prereq text")

    # One bin, one block -> the single-block path escalates up the ladder (no split).
    monkeypatch.setattr(sizing, "pack_prerequisite_bins", lambda blocks, **kw: ([[block]], []))
    monkeypatch.setattr(sizing, "models_at_or_above", lambda model: ["low", "high"])
    monkeypatch.setattr(
        sizing, "is_context_limit_error", lambda exc: isinstance(exc, _ContextLimit)
    )

    # Avoid real prompt-library resolution.
    from rebar.llm.prompting import prompts

    monkeypatch.setattr(prompts, "get_prompt", lambda *a, **k: "prompt")
    monkeypatch.setattr(prompts, "resolve_prompt", lambda *a, **k: ("system", None))

    cfg = dataclasses.replace(LLMConfig(runner="fake"), model="low")
    runner = _EscalatingRunner()

    with caplog.at_level(logging.WARNING, logger="rebar.llm.plan_review.prerequisites"):
        records, _findings = prerequisites.run_focused_finder(
            runner=runner,
            cfg=cfg,
            subject_plan="subject",
            blocks=[{"canonical_id": block.canonical_id, "rendered_text": block.rendered_text}],
            ticket_id="tkt0-tkt0-tkt0-tkt0",
        )

    # The escalation actually happened.
    assert runner.models_seen == ["low", "high"]
    assert records and records[0]["disposition"] == "indeterminate"

    warnings = [r for r in caplog.records if r.msg == "plan_review_prerequisite_indeterminate %s"]
    assert len(warnings) == 1, f"expected one indeterminate warning, got {len(warnings)}"
    rec = warnings[0]
    # The producing model was the ESCALATED one; reporting cfg.model ("low") is the bug.
    assert rec.model == "high", f"log reported {rec.model!r}, expected the escalated 'high'"
    # attempt_count reflects the real number of model attempts (low, then high), not a hardcoded 1.
    assert rec.attempt_count == 2, f"attempt_count was {rec.attempt_count!r}, expected 2"
