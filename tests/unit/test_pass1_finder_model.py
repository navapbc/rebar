"""The plan-review Pass-1 FINDER runs on the frontier class, and Pass-2 does not (ticket 77ed).

Pass-1 generates the findings that become blocking, so it must run on `frontier`; Pass-2 verify and
Pass-4 coach run on `standard` via `_verifier_cfg`, and that downgrade is deliberate. The failure
mode these guard against is the two collapsing onto one model.

WHAT IS ASSERTED is the model that REACHES THE RUNNER, captured at
`ProductionBatchRunner` -> `run_pass1`. Asserting the YAML text would prove nothing: the declaration
and the model actually used can disagree, and have. `test_model_class_ladder.py` covers the
resolution machinery but feeds it a SYNTHETIC ladder, so it cannot see a wrong rung in the shipped
document — these read the ladder from the real gate file. See ticket 77ed for the history.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
import yaml

from rebar.llm.config import LLMConfig
from rebar.llm.workflow.runners import BatchRunRequest, BatchRunResult

pytestmark = pytest.mark.unit

_GATE = pathlib.Path("src/rebar/llm/workflow/gates/plan-review.yaml")


def _yaml_pass1_ladder() -> list[str]:
    """The Pass-1 `model_ladder` as the SHIPPED gate document declares it.

    Walked recursively rather than scanning top-level steps: the finders batch lives INSIDE a
    branch (`steps[1].branch.then[1].batch`), so a flat scan silently finds nothing — which is
    exactly how the first draft of this test passed vacuously.
    """
    found: list[list[str]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            ladder = node.get("model_ladder")
            if ladder:
                found.append(list(ladder))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(yaml.safe_load(_GATE.read_text()))
    assert len(found) == 1, f"expected exactly one model_ladder in plan-review.yaml, got {found!r}"
    return found[0]


class _FakePlanContext:
    plan_text = "plan"
    repo_root = None
    centrality = 0.0
    ticket_id = "abcd-0000-0000-0001"
    ticket_type = "task"


def _retarget_frontier_only(monkeypatch) -> str:
    """Point the `frontier` class at a distinctive target and return its resolved id.

    Only `frontier` is retargeted, so the frontier id differs from the default any unresolved
    `cfg.model` would carry — that difference is what makes "used the frontier class" observable.
    The nine per-class env overrides are cleared so an ambient one on the developer's machine
    cannot retarget a class behind the table.
    """
    from rebar.llm import config as llm_config
    from rebar.llm.model_classes import FRONTIER_CLASS, load_class_slots, resolve_class

    for cls in ("TRIVIAL", "STANDARD", "FRONTIER"):
        for field in ("MODEL", "PROVIDER", "ENDPOINT"):
            monkeypatch.delenv(f"REBAR_LLM_{cls}_{field}", raising=False)
    monkeypatch.setattr(
        llm_config,
        "_read_llm_file_table",
        lambda repo_root=None: {
            "model_classes": {
                "frontier": {"model": "us.anthropic.claude-opus-4-8", "provider": "bedrock"}
            }
        },
    )
    return resolve_class(FRONTIER_CLASS, load_class_slots(None))


def _run_batch(monkeypatch, ladder: list[str], with_inputs: dict[str, Any] | None = None) -> str:
    """The `cfg.model` handed to `run_pass1` — the model the finder's calls actually run on."""
    from rebar.llm.plan_review import production_batch_runner as pbr
    from rebar.llm.runner import FakeRunner

    captured: dict[str, str] = {}

    def _fake_run_pass1(
        ctx, cfg, runner, single, agent, coverage, cap_override=None, tf_provider=None
    ):
        captured["model"] = cfg.model
        return []

    monkeypatch.setattr(pbr, "run_pass1", _fake_run_pass1)
    monkeypatch.setattr(pbr, "assemble_context", lambda target, repo_root=None: _FakePlanContext())
    monkeypatch.setattr(pbr, "_resolve_criteria", lambda criteria: ([], [], []))
    monkeypatch.setattr(pbr, "_project_criteria", lambda ctx, seen, probe=None: ([], []))

    req: dict[str, Any] = {
        "finder": "plan-review-finder",
        "criteria": ({"prompt": "plan-review-A1"},),
        "usd_budget": None,
        "model_ladder": tuple(ladder),
        "workflow": {},
        "target_ticket": "abcd-0000-0000-0001",
        "repo_root": None,
        "run_id": "run-1",
        "step_id": "find",
        "with_inputs": dict(with_inputs or {}),
    }
    result = pbr.ProductionBatchRunner(runner=FakeRunner()).run(BatchRunRequest(**req))
    assert isinstance(result, BatchRunResult)
    return captured["model"]


def _entry_model(monkeypatch, ladder: list[str]) -> str:
    return _run_batch(monkeypatch, ladder)


# ── the finder runs on the frontier class, read from the SHIPPED document ─────────────────────


def test_the_shipped_ladder_puts_the_finder_on_the_frontier_class(monkeypatch):
    """THE REGRESSION GUARD. Drives the real gate document's ladder through the seam that decides
    the finder's model, and asserts it lands on the frontier class."""
    from rebar.llm.model_classes import FRONTIER_CLASS, load_class_slots, resolve_class

    expected = resolve_class(FRONTIER_CLASS, load_class_slots(None))
    assert _entry_model(monkeypatch, _yaml_pass1_ladder()) == expected


def test_the_finder_does_not_run_on_the_cheapest_class(monkeypatch):
    """Stated separately from the assertion above so a failure says WHICH way it went wrong: the
    regression this ticket fixes pointed the finding generator at the cheapest rung."""
    from rebar.llm.model_classes import TRIVIAL_CLASS, load_class_slots, resolve_class

    trivial = resolve_class(TRIVIAL_CLASS, load_class_slots(None))
    assert _entry_model(monkeypatch, _yaml_pass1_ladder()) != trivial


# ── the prerequisite finder sizes its bins against the frontier window ────────────────────────


def test_the_prerequisite_finder_packs_against_the_frontier_model(monkeypatch):
    """The prerequisite arm of Pass-1 BIN-PACKS, and `pack_prerequisite_bins` sizes the bins from
    whatever model it is handed. If that model is not the frontier one the finder over-chunks —
    paying for extra calls and splitting prerequisites that would have fit in one window — while
    every other assertion in this file still passes, because the packing model is a second,
    independent path off the resolved config.

    The observable is the model that REACHES `pack_prerequisite_bins`, captured with a spy. It is
    NOT the resulting window number: `MODEL_LADDER` declares 1_000_000 for both the sonnet and
    the opus rung, so the window alone cannot tell the frontier class from the one below it
    (ticket 1157). `largest_window_tokens` is deliberately left unpatched for the same reason —
    a synthetic window would make the wiring unobservable, which is what left this gap open.

    The class table retargets `frontier` ONLY, so "resolved the frontier class" and "fell through
    to the default `cfg.model`" are different strings. With no table configured they are the same
    string and the assertion would hold no matter which path ran.
    """
    from rebar.llm.plan_review import sizing

    frontier = _retarget_frontier_only(monkeypatch)
    assert frontier != LLMConfig().model, "the discriminator collapsed; the test proves nothing"

    captured: dict[str, Any] = {}

    def _spy(blocks, **kwargs):
        captured["model"] = kwargs.get("model")
        return [], []

    monkeypatch.setattr(sizing, "pack_prerequisite_bins", _spy)
    _run_batch(
        monkeypatch,
        _yaml_pass1_ladder(),
        with_inputs={
            "subject_plan": "plan",
            "prerequisites": [{"canonical_id": "abcd-0000-0000-0002", "rendered_text": "prereq"}],
        },
    )

    assert captured["model"] == frontier, (
        f"the prerequisite finder packed against {captured.get('model')!r}; it must size its bins "
        f"against the frontier class ({frontier!r}) that Pass-1 runs on (ticket 77ed)"
    )


# ── Pass-1 and Pass-2 must not collapse onto one model ───────────────────────────────────────


def test_pass1_and_the_pass2_verifier_resolve_to_different_models(monkeypatch):
    """The failure mode is these two collapsing onto one model. Pass-2/Pass-4 should be the
    decisive non-frontier model (`_verifier_cfg`); the Pass-1 finder must not be."""
    from rebar.llm.plan_review import _verifier_cfg

    pass1 = _entry_model(monkeypatch, _yaml_pass1_ladder())
    pass2 = _verifier_cfg(LLMConfig()).model
    assert pass1 != pass2, (
        f"Pass-1 and the Pass-2 verifier both resolved to {pass1!r}; the finder must not inherit "
        "the verifier downgrade (ticket 77ed)"
    )


def test_the_pass2_verifier_is_still_downgraded(monkeypatch):
    """The control: this ticket must NOT fix Pass-1 by removing the Pass-2 downgrade. The verifier
    running on the decisive non-frontier model is correct and deliberate.

    Compared against the resolved STANDARD class rather than the bare `VERIFIER_DEFAULT_MODEL`
    constant, because `_verifier_cfg` routes through `resolve_model_string(STANDARD_CLASS)` and so
    returns a provider-qualified string.
    """
    from rebar.llm.model_classes import STANDARD_CLASS, load_class_slots, resolve_class
    from rebar.llm.plan_review import _verifier_cfg

    assert _verifier_cfg(LLMConfig()).model == resolve_class(STANDARD_CLASS, load_class_slots(None))


# ── the declaration itself, as a readable companion to the behavioural tests ──────────────────


def test_the_ladder_declares_classes_and_enters_at_frontier():
    """Cheap and legible, but NOT sufficient on its own: the declaration and the model actually
    used can disagree, so only the behavioural tests above have teeth."""
    from rebar.llm.model_classes import CLASS_NAMES, FRONTIER_CLASS

    ladder = _yaml_pass1_ladder()
    assert ladder[0] == FRONTIER_CLASS, f"Pass-1 must enter at the frontier class, got {ladder!r}"
    assert all(rung in CLASS_NAMES for rung in ladder), (
        f"every rung must name a model CLASS so it follows the configured provider: {ladder!r}"
    )
