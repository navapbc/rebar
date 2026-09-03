"""Held-out oracle for the plan-review fixture-spec emitter (ticket 092c).

These cases are withheld from the implementer and validated by the orchestrator. They pin the
behavior that separates a real emitter from one that only transcribes the happy path:

- balance refusal — a criterion with candidates on only ONE side of the fire/no-fire axis
  emits no file and is counted ``skipped-unbalanced`` (AC3);
- gold refusal — a criterion with no candidate material (only a zero-candidate row) emits no
  file and is counted ``skipped-unbalanced`` (AC4);
- the per-side cap — at most 4 cases per side, taken in the manifest's recorded rank order
  (AC5);
- output lifecycle — a spec written for a criterion absent from the next manifest does not
  survive the next run (AC6);
- idempotence — two runs over the same manifest emit byte-identical trees (AC7);
- every emitted spec passes the REAL strict validator, and the emitter runs to completion under
  the repository's default network guard (DoD / AC on the network guard).

Reuses the ``candidate`` row helper and ``balanced_rows`` from the visible happy-path oracle
so both suites speak the selector's real manifest shape.
"""

from __future__ import annotations

import filecmp
from pathlib import Path
from typing import Any

import yaml
from rebar.llm.evals.fixture_emit import emit_specs
from test_fixture_emit import balanced_rows, candidate

from rebar.llm.criteria.ids import criterion_prompt_id
from rebar.llm.evals.eval import validate_eval_spec
from rebar.llm.evals.fixture_selection import write_manifest


def _spec_path(out: Path, criterion: str) -> Path:
    return out / f"{criterion_prompt_id(criterion)}.eval.yaml"


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_one_sided_criterion_is_skipped_unbalanced(tmp_path: Path) -> None:
    """A criterion with fire candidates but no no_fire candidate cannot balance — it emits
    no file and is named in ``skipped_unbalanced`` (AC3)."""
    rows = [
        candidate("project.alpha", "fire", 0, norm_id="n0", tier="blocking"),
        candidate("project.alpha", "fire", 1, norm_id="n1"),
    ]
    manifest = tmp_path / "m.jsonl"
    write_manifest(rows, manifest)
    out = tmp_path / "out"

    report = emit_specs(manifest, out)

    assert not _spec_path(out, "project.alpha").exists()
    assert "project.alpha" in report.skipped_unbalanced
    assert "project.alpha" not in report.emitted


def test_no_candidate_material_is_skipped_unbalanced(tmp_path: Path) -> None:
    """A criterion present only as a zero-candidate row supplies no dataset or gold material —
    it emits no file and is counted ``skipped-unbalanced`` (AC4)."""
    rows = [
        {"kind": "zero_candidate", "criterion": "project.beta", "reason": "no-admitted-candidate"},
    ]
    manifest = tmp_path / "m.jsonl"
    write_manifest(rows, manifest)
    out = tmp_path / "out"

    report = emit_specs(manifest, out)

    assert not _spec_path(out, "project.beta").exists()
    assert "project.beta" in report.skipped_unbalanced
    assert "project.beta" not in report.emitted


def test_per_side_cap_keeps_first_four_in_rank_order(tmp_path: Path) -> None:
    """Given 6 ranked fire candidates (and a balancing no_fire side), the emitted dataset holds
    exactly the first 4 fire cases in rank order — the per-side cap and rank order (AC5)."""
    rows = [candidate("project.alpha", "fire", r, norm_id=f"n{r}") for r in range(6)]
    rows += [
        candidate("project.alpha", "no_fire", 0, review_event_uuid="s0"),
        candidate("project.alpha", "no_fire", 1, review_event_uuid="s1"),
    ]
    manifest = tmp_path / "m.jsonl"
    write_manifest(rows, manifest)
    out = tmp_path / "out"

    emit_specs(manifest, out)

    pid = criterion_prompt_id("project.alpha")
    spec = _load(_spec_path(out, "project.alpha"))
    fire_ids = [case["id"] for case in spec["dataset"] if case["expect"] == "finding"]
    assert fire_ids == [f"{pid}-fire-{r}" for r in range(4)]
    assert validate_eval_spec(spec, strict=True) == []


def test_no_fire_side_capped_at_four(tmp_path: Path) -> None:
    """The cap applies to the no_fire side symmetrically: 6 no_fire candidates yield 4 cases."""
    rows = [candidate("project.alpha", "fire", 0, norm_id="n0", tier="blocking")]
    rows += [candidate("project.alpha", "no_fire", r, review_event_uuid=f"s{r}") for r in range(6)]
    manifest = tmp_path / "m.jsonl"
    write_manifest(rows, manifest)
    out = tmp_path / "out"

    emit_specs(manifest, out)

    spec = _load(_spec_path(out, "project.alpha"))
    pass_ids = [case["id"] for case in spec["dataset"] if case["expect"] == "pass"]
    pid = criterion_prompt_id("project.alpha")
    assert pass_ids == [f"{pid}-no_fire-{r}" for r in range(4)]


def test_stale_spec_removed_when_criterion_absent_next_run(tmp_path: Path) -> None:
    """A spec written for a criterion that is absent from the next manifest does not survive the
    next run — the output tree is fully replaced (AC6)."""
    out = tmp_path / "out"

    first = tmp_path / "first.jsonl"
    write_manifest(balanced_rows("project.alpha"), first)
    emit_specs(first, out)
    assert _spec_path(out, "project.alpha").is_file()

    second = tmp_path / "second.jsonl"
    write_manifest(balanced_rows("project.gamma"), second)
    emit_specs(second, out)

    assert not _spec_path(out, "project.alpha").exists()
    assert _spec_path(out, "project.gamma").is_file()


def test_two_runs_are_byte_identical(tmp_path: Path) -> None:
    """Two consecutive runs over the same manifest emit byte-identical trees (AC7)."""
    manifest = tmp_path / "m.jsonl"
    rows = balanced_rows("project.alpha") + balanced_rows("project.gamma")
    write_manifest(rows, manifest)

    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    emit_specs(manifest, out_a)
    emit_specs(manifest, out_b)

    names_a = sorted(p.name for p in out_a.iterdir())
    names_b = sorted(p.name for p in out_b.iterdir())
    assert names_a == names_b
    _match, mismatch, errors = filecmp.cmpfiles(out_a, out_b, names_a, shallow=False)
    assert mismatch == [] and errors == []


def test_multiple_criteria_each_emit_a_strict_valid_spec(tmp_path: Path) -> None:
    """A manifest with several balanced criteria emits one strict-valid spec per criterion, and
    only balanced criteria appear in ``emitted``."""
    rows = balanced_rows("project.alpha") + balanced_rows("project.gamma")
    rows += [candidate("project.delta", "fire", 0, norm_id="d0")]  # one-sided → skipped
    manifest = tmp_path / "m.jsonl"
    write_manifest(rows, manifest)
    out = tmp_path / "out"

    report = emit_specs(manifest, out)

    assert sorted(report.emitted) == ["project.alpha", "project.gamma"]
    assert "project.delta" in report.skipped_unbalanced
    for criterion in ("project.alpha", "project.gamma"):
        spec = _load(_spec_path(out, criterion))
        assert validate_eval_spec(spec, strict=True) == []


def test_emitter_writes_nothing_under_dot_rebar_evals(tmp_path: Path, monkeypatch: Any) -> None:
    """The emitter writes ONLY into its working directory — never into a real ``.rebar/evals``
    tree (Non-goal: it must not overwrite packaged cases)."""
    manifest = tmp_path / "m.jsonl"
    write_manifest(balanced_rows("project.alpha"), manifest)
    out = tmp_path / "working"
    monkeypatch.chdir(tmp_path)

    emit_specs(manifest, out)

    assert not (tmp_path / ".rebar").exists()
    assert _spec_path(out, "project.alpha").is_file()
