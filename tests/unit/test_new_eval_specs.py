"""The reviewer eval specs added by WS-EVAL-EXISTING (epic 6f2d), plus the plan-review
specs that followed. Offline discipline only — these assert spec SHAPE
(strict validity, registered scorers, balanced dataset, non-empty gold_set); the live
recall/false-fire SCORING runs in the eval CI ([agents] extra + credentials)."""

from __future__ import annotations

import pytest

from rebar.llm.evals import eval as ev
from rebar.llm.evals import eval_scorers as sc

NEW_SPECS = (
    "completion-verifier",
    "spec-alignment",
    "plan-review-T10",
    "plan-review-T15",
)


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


def test_every_packaged_spec_filename_matches_its_prompt_exactly() -> None:
    """Callers open ``<prompt>.eval.yaml`` by the spec's ``prompt`` id, so a filename that
    differs from it, even only in letter case, cannot be loaded on a case-sensitive
    filesystem (bug militant-vixenish-gorilla). Compare the strings directly so the check
    fails on every host, not only on Linux."""
    from pathlib import Path

    import yaml

    spec_dir = Path(ev.__file__).resolve().parent.parent / "eval_specs"
    paths = sorted(spec_dir.glob("*.eval.yaml"))
    assert paths, f"no packaged eval specs under {spec_dir}"
    mismatched = {}
    for path in paths:
        stem = path.name[: -len(".eval.yaml")]
        prompt = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("prompt")
        if prompt != stem:
            mismatched[path.name] = prompt
    assert mismatched == {}
