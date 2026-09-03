"""Emit deterministic eval specs from labeled fixture-candidate manifests."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from rebar.llm.criteria.ids import criterion_prompt_id
from rebar.llm.evals.eval import validate_eval_spec

_MODEL = "anthropic:claude-sonnet-4-6"
_SCORER = {
    "type": "deterministic",
    "name": "emits_valid_findings",
    "description": "Output validates as a plan-review finding set.",
}
_CORPUS = "rebar"
_SIDE_LIMIT = 4


@dataclass(frozen=True)
class FixtureEmitReport:
    """Summary of emitted and refused fixture specs."""

    emitted: list[str]
    skipped_unbalanced: list[str]


def emit_specs(manifest_path: str | Path, out_dir: str | Path) -> FixtureEmitReport:
    """Replace ``out_dir`` with per-criterion eval specs derived from ``manifest_path``.

    The JSONL manifest is the only input. Balanced criteria produce one strict-valid
    ``<prompt_id>.eval.yaml`` spec; criteria missing either fire or no-fire candidates
    are skipped and reported as unbalanced.
    """
    criteria, candidates = _read_manifest(Path(manifest_path))
    specs: dict[str, dict[str, Any]] = {}
    emitted: list[str] = []
    skipped: list[str] = []

    for criterion in sorted(criteria):
        rows = candidates.get(criterion, [])
        fire = _selected(rows, "fire")
        no_fire = _selected(rows, "no_fire")
        if not fire or not no_fire:
            skipped.append(criterion)
            continue
        specs[criterion] = _build_spec(criterion, fire + no_fire)
        emitted.append(criterion)

    _replace_output_tree(Path(out_dir), specs)
    return FixtureEmitReport(emitted=sorted(emitted), skipped_unbalanced=sorted(skipped))


def _read_manifest(path: Path) -> tuple[set[str], dict[str, list[dict[str, Any]]]]:
    criteria: set[str] = set()
    candidates: dict[str, list[dict[str, Any]]] = {}
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                continue
            criterion = row.get("criterion")
            if not isinstance(criterion, str):
                continue
            criteria.add(criterion)
            if row.get("kind") == "candidate":
                candidates.setdefault(criterion, []).append(row)
    return criteria, candidates


def _selected(rows: list[dict[str, Any]], direction: str) -> list[dict[str, Any]]:
    side = [row for row in rows if row.get("direction") == direction]
    return sorted(side, key=lambda row: int(row["rank"]))[:_SIDE_LIMIT]


def _build_spec(criterion: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    prompt_id = criterion_prompt_id(criterion)
    dataset = [_dataset_case(prompt_id, row) for row in rows]
    spec: dict[str, Any] = {
        "prompt": prompt_id,
        "model": _MODEL,
        "epochs": 3,
        "gate": "at_least(2)",
        "coverage_threshold": 1.0,
        "scorers": [_SCORER],
        "dataset": dataset,
        "gold_set": [{"input": case["input"], "label": case["expect"]} for case in dataset],
    }
    errors = validate_eval_spec(spec, strict=True)
    if errors:
        raise ValueError(f"emitted eval spec for {criterion!r} failed validation: {errors}")
    return spec


def _dataset_case(prompt_id: str, row: dict[str, Any]) -> dict[str, Any]:
    direction = str(row["direction"])
    rank = int(row["rank"])
    expect = "finding" if direction == "fire" else "pass"
    return {
        "id": f"{prompt_id}-{direction}-{rank}",
        "corpus": _CORPUS,
        "criterion": row["criterion"],
        "expect": expect,
        "input": _input_descriptor(row),
    }


def _input_descriptor(row: dict[str, Any]) -> str:
    signals = ",".join(str(signal) for signal in sorted(row.get("signals") or []))
    direction = str(row["direction"])
    stable_id_key = "norm_id" if direction == "fire" else "review_event_uuid"
    stable_id = row.get(stable_id_key)
    return (
        f"criterion={row['criterion']} "
        f"direction={direction} "
        f"tier={row['tier']} "
        f"signals={signals} "
        f"{stable_id_key}={stable_id} "
        f"rank={int(row['rank'])}"
    )


def _replace_output_tree(out_dir: Path, specs: dict[str, dict[str, Any]]) -> None:
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"{out_dir.name}.tmp-", dir=out_dir.parent))
    try:
        for criterion, spec in specs.items():
            prompt_id = criterion_prompt_id(criterion)
            spec_path = tmp_dir / f"{prompt_id}.eval.yaml"
            with spec_path.open("w", encoding="utf-8") as stream:
                yaml.safe_dump(spec, stream, sort_keys=True, default_flow_style=False)
        _remove_path(out_dir)
        os.replace(tmp_dir, out_dir)
    except Exception:
        _remove_path(tmp_dir)
        raise


def _remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()
