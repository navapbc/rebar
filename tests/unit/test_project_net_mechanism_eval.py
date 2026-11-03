"""Structural tests for the project.net-mechanism eval corpus
(epic 77e3-28e2-31ec-47ec, task 39bc-072e-9ebf-461d).

These are the DETERMINISTIC, offline half of the calibration child: they validate the
committed `.rebar/evals/plan-review-project-net-mechanism.eval.yaml` corpus shape and
never make a billable model call. The live semantic threshold is the ticket's separate
[operator-attested] acceptance criterion, run via
`rebar criteria eval project.net-mechanism --runs 3` and recorded on the ticket.
"""

from __future__ import annotations

import ast
from pathlib import Path

import yaml

from rebar.llm.evals import eval as _eval

REPO_ROOT = str(Path(__file__).resolve().parents[2])
PID = "plan-review-project-net-mechanism"
_EVAL_FILE = Path(REPO_ROOT) / ".rebar" / "evals" / f"{PID}.eval.yaml"

_FIRE_IDS = {"NETMECH-F1", "NETMECH-F2", "NETMECH-F3", "NETMECH-F4"}
_PASS_IDS = {"NETMECH-N1", "NETMECH-N2", "NETMECH-N3", "NETMECH-N4"}
_REQUIRED_CASE_FIELDS = ("id", "corpus", "criterion", "expect", "note", "input")

# The marker line F1 is reconstructed from; cited in F1's note as its provenance.
_F1_PROVENANCE = "src/rebar/llm/config_readers.py:152"


def _spec() -> dict:
    return _eval.load_eval_spec(PID, repo_root=REPO_ROOT)


# ── the spec parses as YAML and resolves as the user override ────────────────────
def test_spec_parses_as_yaml():
    raw = yaml.safe_load(_EVAL_FILE.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)


def test_override_resolution():
    p = _eval.eval_spec_path(PID, repo_root=REPO_ROOT)
    assert p == _EVAL_FILE
    assert p.is_file()


# ── the exact top-level contract, one assertion per declared key ─────────────────
def test_spec_declares_the_criterion_prompt():
    assert _spec()["prompt"] == "plan-review-project-net-mechanism"


def test_spec_declares_epochs():
    assert _spec()["epochs"] == 3


def test_spec_declares_gate():
    assert _spec()["gate"] == "at_least(2)"


def test_spec_declares_coverage_threshold():
    assert _spec()["coverage_threshold"] == 1.0


def test_spec_declares_the_deterministic_scorer():
    det = [s for s in _spec()["scorers"] if s.get("type") == "deterministic"]
    assert len(det) == 1
    assert det[0]["name"] == "emits_valid_findings"


def test_spec_passes_strict_validation():
    assert _eval.validate_eval_spec(_spec()) == []


# ── the balanced 8-case dataset ──────────────────────────────────────────────────
def test_dataset_has_four_must_fire_cases():
    ds = _spec()["dataset"]
    assert {c["id"] for c in ds if c["expect"] == "finding"} == _FIRE_IDS


def test_dataset_has_four_must_not_fire_cases():
    ds = _spec()["dataset"]
    assert {c["id"] for c in ds if c["expect"] == "pass"} == _PASS_IDS


def test_every_case_carries_all_six_required_fields():
    for case in _spec()["dataset"]:
        for field in _REQUIRED_CASE_FIELDS:
            value = case.get(field)
            assert isinstance(value, str) and value.strip(), (
                f"{case.get('id')!r} is missing a nonempty {field!r}"
            )


def test_every_case_id_is_unique():
    ids = [c["id"] for c in _spec()["dataset"]]
    assert len(ids) == len(set(ids))


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


# ── the red proof keeps its provenance citation ──────────────────────────────────
def test_f1_note_cites_its_provenance():
    by_id = {c["id"]: c for c in _spec()["dataset"]}
    assert _F1_PROVENANCE in by_id["NETMECH-F1"]["note"]


# ── this module runs no model: it imports no runner ──────────────────────────────
def test_module_imports_no_runner():
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
    offenders = [name for name in imported if "runner" in name or name.startswith("pydantic_ai")]
    assert offenders == []
