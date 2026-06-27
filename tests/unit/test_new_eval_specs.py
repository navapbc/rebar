"""The 3 reviewer eval specs added by WS-EVAL-EXISTING (epic 6f2d): completion-verifier,
ticket-quality, spec-alignment. Offline discipline only — these assert spec SHAPE
(strict validity, registered scorers, balanced dataset, non-empty gold_set); the live
recall/false-fire SCORING runs in the eval CI ([eval] extra + credentials)."""

from __future__ import annotations

import pytest

from rebar.llm import eval as ev
from rebar.llm import eval_scorers as sc

NEW_SPECS = ("completion-verifier", "ticket-quality", "spec-alignment")


@pytest.mark.parametrize("prompt_id", NEW_SPECS)
def test_new_spec_loads_and_is_strict_clean(prompt_id: str) -> None:
    spec = ev.load_eval_spec(prompt_id)  # lenient load (raises on invalid)
    assert spec["prompt"] == prompt_id
    assert ev.validate_eval_spec(spec, strict=True) == [], prompt_id


@pytest.mark.parametrize("prompt_id", NEW_SPECS)
def test_new_spec_scorers_are_registered_and_disciplined(prompt_id: str) -> None:
    spec = ev.load_eval_spec(prompt_id)
    known = sc.known_scorer_names()
    det = [s for s in spec["scorers"] if s.get("type") == "deterministic"]
    judges = [s for s in spec["scorers"] if s.get("type") == "llm-judge"]
    assert det, f"{prompt_id} has no deterministic gating scorer"
    for s in det:
        assert s["name"] in known, f"{prompt_id}: unregistered scorer {s['name']!r}"
    # every judge reports (never gates) and is cross-family pinned
    for j in judges:
        assert j.get("gates") is False, f"{prompt_id}: judge must not gate"
        assert ev.validate_scorer(j, generator_model=spec.get("model")) == []


@pytest.mark.parametrize("prompt_id", NEW_SPECS)
def test_new_spec_dataset_balanced_and_gold_present(prompt_id: str) -> None:
    spec = ev.load_eval_spec(prompt_id)
    assert spec.get("gold_set"), f"{prompt_id}: empty gold_set"
    expects = {c.get("expect") for c in spec["dataset"]}
    # balanced across whichever axis the spec uses
    if expects & (sc.FIRE_EXPECTS | sc.NOFIRE_EXPECTS):
        assert expects & sc.FIRE_EXPECTS and expects & sc.NOFIRE_EXPECTS, prompt_id
