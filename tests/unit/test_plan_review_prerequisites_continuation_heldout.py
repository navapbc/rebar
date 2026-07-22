from __future__ import annotations

import json
from pathlib import Path

from rebar.llm import parity
from rebar.llm.config import LLMConfig
from rebar.llm.plan_review import fidelity_spot_eval as fse


def test_prerequisite_baseline_has_atomic_comparable_metadata() -> None:
    path = Path("src/rebar/llm/eval_specs/plan-review-prerequisite-packing.baseline.json")
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert isinstance(payload["recall"], float)
    assert isinstance(payload["false_accept"], float)
    assert isinstance(payload["coverage_completeness"], float)
    assert isinstance(payload["prerequisite_attribution_error_rate"], float)
    assert isinstance(payload["model"], str) and payload["model"]
    assert isinstance(payload["corpus_digest"], str) and payload["corpus_digest"]
    assert isinstance(payload["recorded_at"], str) and payload["recorded_at"]


def test_prerequisite_packing_cli_rejects_comparative_regression(
    monkeypatch, tmp_path, capsys
) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "corpus": "plan-review-prerequisite-packing-v1",
                "recall": 1.0,
                "false_accept": 0.0,
                "coverage_completeness": 1.0,
                "prerequisite_attribution_error_rate": 0.0,
                "model": LLMConfig.from_env().model,
                "corpus_digest": fse.prerequisite_corpus_digest(),
                "recorded_at": "2026-07-22T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    def fake_eval(**kwargs):
        assert kwargs["baseline_recall"] == 1.0
        assert kwargs["baseline_false_accept"] == 0.0
        return parity.ParityReport(
            passed=False,
            gating_failures=["prerequisite recall 0.950 below baseline recall 1.000"],
            metrics={
                "recall": {"v1": 1.0, "v2": 0.95},
                "false_accept": {"v1": 0.0, "v2": 0.0},
                "coverage_completeness": 1.0,
                "prerequisite_attribution_error_rate": 0.0,
            },
        )

    monkeypatch.setattr(fse, "prerequisite_packing_spot_eval", fake_eval)

    rc = fse.main(["--prerequisite-packing", "--baseline", str(baseline)])

    assert rc == 1
    output = json.loads(capsys.readouterr().out)
    assert any("baseline recall" in failure for failure in output["gating_failures"])


def test_prerequisite_gold_corpus_has_required_scenarios() -> None:
    corpus = fse._prerequisite_corpus()
    kinds = [case["name"] for case in corpus]

    assert len(corpus) >= 8
    assert sum(kind.startswith("all-consistent") for kind in kinds) >= 2
    assert sum(kind.startswith("single-conflict") for kind in kinds) >= 2
    assert sum(kind.startswith("tail-conflict") for kind in kinds) >= 2
    assert "attribution-confusion" in kinds
    assert "multi-bin" in kinds
    assert all(len(case["blocks"]) >= 3 for case in corpus)
    assert all(case["subject_plan"] for case in corpus)


def test_prerequisite_spot_eval_runs_singleton_and_packed_focused_finder(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_finder(runner, cfg, *, subject_plan, blocks, ticket_id=""):
        calls.append([str(block["canonical_id"]) for block in blocks])
        records = []
        findings = []
        for block in blocks:
            prerequisite_id = str(block["canonical_id"])
            if "CONFLICT" in str(block["rendered_text"]):
                finding = {
                    "finding": "The subject conflicts with this prerequisite.",
                    "criteria": ["prerequisite-consistency"],
                    "prerequisite_id": prerequisite_id,
                }
                records.append(
                    {
                        "prerequisite_id": prerequisite_id,
                        "disposition": "finding",
                        "findings": [finding],
                    }
                )
                findings.append(finding)
            else:
                records.append(
                    {
                        "prerequisite_id": prerequisite_id,
                        "disposition": "consistent",
                        "findings": [],
                    }
                )
        return records, findings

    monkeypatch.setattr(
        "rebar.llm.plan_review.prerequisites.run_focused_finder",
        fake_finder,
    )

    report = fse.prerequisite_packing_spot_eval(
        config=LLMConfig(runner="fake"),
        runner=object(),
    )

    assert report.passed, report.gating_failures
    assert any(len(call) == 1 for call in calls)
    assert any(len(call) >= 3 for call in calls)
