"""Structural + calibration-math tests for the project.portability eval corpus
(epic jira-reb-1003, task indigoid-unmystic-serpent).

These are the DETERMINISTIC, offline half of the calibration child: they validate the
committed `.rebar/evals/plan-review-project-portability.eval.yaml` corpus shape and the
`calibrate_criterion` metric math via an injected solve — never a billable model call.
The live semantic threshold (recall/false-accept/kappa) is the ticket's separate
[operator-attested] acceptance criterion, run via `rebar criteria eval` and recorded on
the ticket.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from rebar.llm.evals import eval as _eval

REPO_ROOT = str(Path(__file__).resolve().parents[2])
PID = "plan-review-project-portability"
_EVAL_FILE = Path(REPO_ROOT) / ".rebar" / "evals" / f"{PID}.eval.yaml"

_FIRE_IDS = {"PORT-F1", "PORT-F2", "PORT-F3", "PORT-F4"}
_PASS_IDS = {"PORT-N1", "PORT-N2", "PORT-N3", "PORT-N4"}
_NOTE_LABELS = ("Plan citation:", "Alternate shape:", "Causal mechanism:", "Observable breakage:")


def _spec() -> dict:
    return _eval.load_eval_spec(PID, repo_root=REPO_ROOT)


# ── the user override at .rebar/evals resolves for project.portability ────────────
def test_override_resolution():
    p = _eval.eval_spec_path(PID, repo_root=REPO_ROOT)
    assert p == _EVAL_FILE
    assert p.is_file()
    assert _spec()["prompt"] == PID  # loaded from the user override, validates clean


# ── the exact top-level contract ─────────────────────────────────────────────────
def test_spec_contract():
    spec = _spec()
    assert spec["prompt"] == "plan-review-project-portability"
    assert spec["model"] == "anthropic:claude-sonnet-4-6"
    assert spec["epochs"] == 3
    assert spec["gate"] == "at_least(2)"
    assert spec["coverage_threshold"] == 1.0
    det = [s for s in spec["scorers"] if s.get("type") == "deterministic"]
    assert len(det) == 1
    assert det[0]["name"] == "emits_valid_findings"
    assert _eval.validate_eval_spec(spec) == []  # no validation errors


# ── the balanced 8-case dataset ──────────────────────────────────────────────────
def test_balanced_corpus():
    spec = _spec()
    ds = {c["id"]: c["expect"] for c in spec["dataset"]}
    assert len(spec["dataset"]) == 8
    assert {cid for cid, e in ds.items() if e == "finding"} == _FIRE_IDS
    assert {cid for cid, e in ds.items() if e == "pass"} == _PASS_IDS


# ── every must-fire note carries the four counterexample labels (raw YAML) ───────
def test_finding_contract():
    raw = yaml.safe_load(_EVAL_FILE.read_text(encoding="utf-8"))
    fires = [c for c in raw["dataset"] if c["id"] in _FIRE_IDS]
    assert len(fires) == 4
    for c in fires:
        note = c["note"]
        assert isinstance(note, str) and note.strip()
        for i, label in enumerate(_NOTE_LABELS):
            assert label in note, f"{c['id']} note missing label {label!r}"
            start = note.index(label) + len(label)
            end = note.index(_NOTE_LABELS[i + 1]) if i + 1 < len(_NOTE_LABELS) else len(note)
            assert note[start:end].strip(), f"{c['id']} label {label!r} has no text after it"


# ── the must-not-fire cases cover the four benign boundaries ─────────────────────
def test_negative_boundaries():
    spec = _spec()
    by_id = {c["id"]: c for c in spec["dataset"]}
    for nid in _PASS_IDS:
        assert by_id[nid]["expect"] == "pass"

    def _text(cid: str) -> str:
        c = by_id[cid]
        return f"{c.get('note', '')} {c.get('input', '')}".lower()

    assert "silen" in _text("PORT-N1")  # silence
    n2 = _text("PORT-N2")
    assert "project config" in n2 or "plan_review_moves" in n2  # project configuration
    assert "cli" in _text("PORT-N3")  # explicit surface scoping
    n4 = _text("PORT-N4")
    # portable alternative: explicit path + standard-library operations
    assert "explicit" in n4
    assert "standard-library" in n4 or "standard library" in n4


# ── the gold_set: 8 balanced, well-formed entries ────────────────────────────────
def test_gold_set():
    gold = _spec()["gold_set"]
    assert len(gold) == 8
    for g in gold:
        assert isinstance(g.get("input"), str) and g["input"].strip()
        assert g.get("label") in ("finding", "pass")
    labels = [g["label"] for g in gold]
    assert labels.count("finding") == 4
    assert labels.count("pass") == 4


# ── calibrate_criterion metric math under an injected perfect solve ──────────────
def test_calibration_metrics():
    def _perfect_solve(pid, case):  # noqa: ANN001
        fires = case.get("expect") in ("finding", "fail")
        return {"findings": [{"criteria": ["project.portability"]}] if fires else []}

    r = _eval.calibrate_criterion("project.portability", repo_root=REPO_ROOT, solve=_perfect_solve)
    assert (r["n_fire"], r["n_nofire"]) == (4, 4)
    assert r["recall"] == 1.0
    assert r["false_accept"] == 0.0
    assert r["agreement"] == 1.0
    assert r["kappa"] == pytest.approx(1.0)
