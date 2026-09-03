"""Happy-path oracle for the plan-review fixture-spec emitter (ticket 092c).

Pins the CORE observable contract of ``rebar.llm.evals.fixture_emit.emit_specs``: given a
labeled candidate manifest that is BALANCED for a criterion (at least one ``fire`` and one
``no_fire`` candidate), the emitter writes exactly one ``.eval.yaml`` for that criterion into
its working directory, named for the criterion's prompt id, and that file passes the REAL
``validate_eval_spec(strict=True)`` with zero errors while carrying the standard field set
(``epochs: 3``, ``gate: at_least(2)``, ``coverage_threshold: 1.0``, the deterministic
``emits_valid_findings`` scorer).

The edge cases (balance/gold refusal counted ``skipped-unbalanced``, the per-side rank-order
cap, stale-file removal across runs, byte-identical idempotence) live in the held-out suite
and are validated by the orchestrator — they are NOT in this file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from rebar.llm.evals.fixture_emit import emit_specs

from rebar.llm.criteria.ids import criterion_prompt_id
from rebar.llm.evals.eval import validate_eval_spec
from rebar.llm.evals.fixture_selection import write_manifest


def candidate(
    criterion: str,
    direction: str,
    rank: int,
    *,
    norm_id: str | None = None,
    tier: str = "advisory",
    signals: list[str] | None = None,
    escaped_defect: bool = False,
    abs_margin: float | None = None,
    review_event_uuid: str = "u",
) -> dict[str, Any]:
    """A ``candidate`` manifest row in the selector's emitted shape (ticket 549b)."""
    return {
        "kind": "candidate",
        "criterion": criterion,
        "direction": direction,
        "norm_id": norm_id,
        "tier": tier,
        "rank": rank,
        "signals": sorted(signals or ["reproduction_consensus"]),
        "escaped_defect": escaped_defect,
        "abs_margin": abs_margin,
        "review_event_uuid": review_event_uuid,
    }


def balanced_rows(criterion: str = "project.alpha") -> list[dict[str, Any]]:
    """Two fire + two no_fire candidates for one criterion — the minimal balanced manifest."""
    return [
        candidate(
            criterion,
            "fire",
            0,
            norm_id="n0",
            tier="blocking",
            signals=["author_response", "margin", "reproduction_consensus"],
        ),
        candidate(criterion, "fire", 1, norm_id="n1"),
        candidate(criterion, "no_fire", 0, review_event_uuid="s0"),
        candidate(criterion, "no_fire", 1, review_event_uuid="s1"),
    ]


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_balanced_criterion_emits_one_strict_valid_spec(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.jsonl"
    write_manifest(balanced_rows(), manifest)
    out = tmp_path / "out"

    report = emit_specs(manifest, out)

    spec_path = out / f"{criterion_prompt_id('project.alpha')}.eval.yaml"
    assert spec_path.is_file()
    assert "project.alpha" in report.emitted
    assert report.skipped_unbalanced == []

    spec = _load(spec_path)
    assert validate_eval_spec(spec, strict=True) == []


def test_emitted_spec_carries_the_standard_field_set(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.jsonl"
    write_manifest(balanced_rows(), manifest)
    out = tmp_path / "out"

    emit_specs(manifest, out)

    spec = _load(out / f"{criterion_prompt_id('project.alpha')}.eval.yaml")
    assert spec["prompt"] == criterion_prompt_id("project.alpha")
    assert spec["epochs"] == 3
    assert spec["gate"] == "at_least(2)"
    assert spec["coverage_threshold"] == 1.0
    assert spec["model"]
    names = {s.get("name") for s in spec["scorers"] if isinstance(s, dict)}
    assert "emits_valid_findings" in names


def test_case_ids_are_prompt_direction_rank(tmp_path: Path) -> None:
    """Each dataset case id is ``<prompt_id>-<direction>-<rank>`` — a deterministic function
    of the manifest, so the emitter's output is reproducible and diff-stable."""
    manifest = tmp_path / "manifest.jsonl"
    write_manifest(balanced_rows(), manifest)
    out = tmp_path / "out"

    emit_specs(manifest, out)

    pid = criterion_prompt_id("project.alpha")
    spec = _load(out / f"{pid}.eval.yaml")
    ids = {case["id"] for case in spec["dataset"]}
    assert ids == {f"{pid}-fire-0", f"{pid}-fire-1", f"{pid}-no_fire-0", f"{pid}-no_fire-1"}


def test_dataset_maps_fire_to_finding_and_no_fire_to_pass(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.jsonl"
    write_manifest(balanced_rows(), manifest)
    out = tmp_path / "out"

    emit_specs(manifest, out)

    spec = _load(out / f"{criterion_prompt_id('project.alpha')}.eval.yaml")
    expects = sorted(case["expect"] for case in spec["dataset"])
    assert expects == ["finding", "finding", "pass", "pass"]
    for entry in spec["gold_set"]:
        assert entry["input"]
        assert entry["label"] in {"finding", "pass"}
