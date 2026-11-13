"""The engine-comparison harness (ADR 0123): outcome precedence, agreement, the confusion
matrix, blocking flips, finding overlap, historical agreement, ``--limit``, ``--dry-run`` and
the store rooting — offline, over a labels file written in the test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rebar.llm.evals import engine_comparison as ec
from rebar.llm.runner import FakeRunner

pytestmark = pytest.mark.unit

_PLAN = (
    "A plan body that clears the deterministic readiness floor so the LLM tier runs.\n\n"
    "## What\nchange a thing in `src/thing.py`.\n\n## Why\nbecause.\n\n"
    "## Acceptance Criteria\n- [ ] the thing is changed\n- [ ] `pytest tests/unit` proves it\n"
)
_FLOOR_PLAN = "## What\nchange a thing in `src/thing.py`.\n\n## Why\nbecause.\n"


def _row(n: int, verdict: str, description: str = _PLAN) -> dict:
    return {
        "verified": True,
        "review_event_ts": n,
        "review_event_uuid": f"u{n}",
        "ticket_id": f"0000-0000-0000-000{n}",
        "ticket_type": "task",
        "description": description,
        "file_impact": [],
        "verdict": verdict,
    }


def _labels(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "labels-test.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def _finding(text: str, severity: str = "major") -> dict:
    return {
        "id": text[:4],
        "title": text,
        "description": text,
        "severity": severity,
        "evidence": "e",
        "suggested_fix": "f",
        "criteria": [],
    }


class _Scripted(FakeRunner):
    """Pipeline contracts answer empty; the agent answers ``agentic[ticket title]`` findings,
    or raises when the title is in ``raise_on``."""

    name = "fake"

    def __init__(self, agentic: dict[str, list[dict]], raise_on: set[str] = frozenset()) -> None:
        super().__init__()
        self.calls = 0
        self.configs: list = []
        self._agentic = agentic
        self._raise_on = raise_on

    def run(self, req) -> dict:  # type: ignore[override]
        from rebar.llm import findings as _f

        self.calls += 1
        self.configs.append(req.config)
        if req.mode == "text":
            return {"text": "", "runner": self.name, "model": None, "trace_id": None}
        if req.output_schema == "plan_review_agentic_findings":
            key = next((k for k in self._agentic if k in req.instructions), None)
            if key in self._raise_on:
                raise RuntimeError("provider exploded")
            payload: dict = {"findings": self._agentic.get(key, [])}
        elif req.output_schema == "plan_review_verification":
            payload = {"verifications": []}
        elif req.output_schema == "plan_review_coach":
            payload = {"notes": []}
        else:
            payload = {"analysis": "", "findings": []}
        payload = _f.validate_structured(dict(payload), req.output_schema)
        return {**payload, "runner": self.name, "model": None, "trace_id": None}


def _run(tmp_path: Path, rows: list[dict], runner: _Scripted, **kw) -> dict:
    labels = _labels(tmp_path, rows)
    kw.setdefault("checkout", tmp_path)
    kw.setdefault("sleep", lambda s: None)
    return ec.run(
        ec.load_rows(labels),
        labels_path=labels,
        out=tmp_path / "out.json",
        make_runner=lambda cfg: runner,
        title_lookup=lambda tid: None,
        **kw,
    )


# ── live-shaped runs over a disposable store ────────────────────────────────────
def test_identical_verdicts_agree_and_a_blocker_flips(tmp_path: Path) -> None:
    rows = [_row(1, "PASS"), _row(2, "PASS"), _row(3, "PASS", _PLAN.replace("thing", "flip"))]
    runner = _Scripted({"flip": [_finding("the plan names the wrong module", "blocker")]})
    results = _run(tmp_path, rows, runner)
    summary = results["summary"]
    assert summary["n_compared"] == 3
    assert summary["agreement"] == pytest.approx(2 / 3)
    assert summary["confusion_matrix"]["PASS"] == {"PASS": 2, "BLOCK": 1}
    assert summary["blocking_flips"] == 1
    assert summary["historical_pipeline_agreement"]["pipeline"] == {"agree": 3, "n": 3}
    assert summary["historical_pipeline_agreement"]["agentic"] == {"agree": 2, "n": 3}
    assert results["valid"] and not results["dry_run"]
    assert results["corpus"] == "labels-test.jsonl"  # a name, never a local path
    assert json.loads((tmp_path / "out.json").read_text())["summary"] == summary


def test_errors_and_floor_rows_are_excluded_not_disagreements(tmp_path: Path) -> None:
    rows = [_row(1, "PASS", _PLAN.replace("thing", "boom")), _row(2, "BLOCK", _FLOOR_PLAN)]
    runner = _Scripted({"boom": []}, raise_on={"boom"})
    summary = _run(tmp_path, rows, runner)["summary"]
    assert summary["n_compared"] == 0 and summary["agreement"] is None
    assert summary["excluded"]["agentic"] == {"error": 1, "floor": 1}
    assert summary["excluded"]["pipeline"] == {"floor": 1}


def test_reviews_are_rooted_at_the_disposable_store(tmp_path: Path) -> None:
    runner = _Scripted({})
    _run(tmp_path, [_row(1, "PASS")], runner)
    assert runner.configs, "no review reached the runner"
    for cfg in runner.configs:
        assert cfg.repo_path and Path(cfg.repo_path) != tmp_path
        assert Path(cfg.repo_path).name.startswith("rebar-eval-")
        assert cfg.tickets_path is None


def test_resume_skips_clean_recorded_rows(tmp_path: Path) -> None:
    rows = [_row(1, "PASS")]
    first = _run(tmp_path, rows, _Scripted({}))
    runner = _Scripted({})
    second = _run(tmp_path, rows, runner, resume=first)
    assert runner.calls == 0
    assert second["items"] == first["items"]


# ── pure functions ──────────────────────────────────────────────────────────────
def test_outcome_precedence() -> None:
    degraded = {
        "verdict": "INDETERMINATE",
        "coverage": {"llm_unavailable": True, "resolution_class": "WAIT_AND_RETRY"},
    }
    assert ec.classify_outcome(degraded) == {
        "outcome": "error",
        "class": "WAIT_AND_RETRY",
        "detail": "",
    }
    assert ec.classify_outcome(None, RuntimeError("x"))["class"] == "NEEDS_INVESTIGATION"
    floor = {"verdict": "BLOCK", "blocking": [{"tier": "DET"}], "coverage": {"llm_ran": False}}
    assert ec.classify_outcome(floor) == {"outcome": "floor"}
    other = {"verdict": "INDETERMINATE", "coverage": {"llm_ran": True}}
    assert ec.classify_outcome(other) == {"outcome": "indeterminate"}
    assert ec.classify_outcome({"verdict": "PASS", "coverage": {"llm_ran": True}}) == {
        "outcome": "PASS"
    }


def test_overlap_worked_example_and_threshold() -> None:
    pipeline = [
        "the plan edits the wrong module for the parser",
        "acceptance criteria lack a test command",
        "missing rollback for the migration step",
    ]
    agentic = [
        "plan edits the wrong module for parser",
        "acceptance criteria lack any test command",
        "documentation section is stale",
    ]
    assert ec.match_findings(pipeline, agentic) == {
        "matched": 2,
        "pipeline_only": 1,
        "agentic_only": 1,
    }
    assert ec.match_findings(["alpha beta gamma delta"], ["alpha omega"])["matched"] == 0


def test_det_findings_are_excluded_from_overlap() -> None:
    verdict = {
        "blocking": [{"finding": "det block", "tier": "DET"}],
        "advisory": [{"finding": "agent advice", "tier": "AGENTIC"}, {"finding": "llm"}],
    }
    assert ec._surfaced(verdict) == ["agent advice", "llm"]


def test_limit_keeps_every_block_row() -> None:
    rows = [_row(1, "PASS"), _row(2, "PASS"), _row(3, "BLOCK")]
    assert [r["verdict"] for r in ec.select_rows(rows, 1)] == ["BLOCK"]
    assert [r["verdict"] for r in ec.select_rows(rows, 2)] == ["PASS", "BLOCK"]
    assert ec.select_rows(rows, 0) == rows


def test_dry_run_prints_corpus_without_a_model_call(tmp_path: Path, capsys) -> None:
    labels = _labels(tmp_path, [_row(1, "PASS"), _row(2, "BLOCK")])
    runner = _Scripted({})
    assert ec.main(["--labels", str(labels), "--dry-run"], make_runner=lambda cfg: runner) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["n_items"] == 2 and len(out["corpus_digest"]) == 64
    assert runner.calls == 0


# ── retry, fixtures, resume after an error, titles, validity ────────────────────
def _retryable_error() -> BaseException:
    from rebar.llm import failure
    from rebar.llm.errors import LLMUnavailableError

    exc = LLMUnavailableError("provider busy")
    exc.outcome = failure.LLMOutcome(  # type: ignore[attr-defined]
        failure.ResolutionClass.WAIT_AND_RETRY, {}, retryable=True
    )
    return exc


class _FlakyAgent(_Scripted):
    """The agent call raises a retryable provider error the first ``fail`` times."""

    def __init__(self, fail: int) -> None:
        super().__init__({})
        self._fail = fail

    def run(self, req) -> dict:  # type: ignore[override]
        if req.output_schema == "plan_review_agentic_findings" and self._fail > 0:
            self._fail -= 1
            self.calls += 1
            raise _retryable_error()
        return super().run(req)


def test_a_retryable_provider_error_is_retried_once(tmp_path: Path) -> None:
    slept: list[float] = []
    item = _run(tmp_path, [_row(1, "PASS")], _FlakyAgent(fail=1), sleep=slept.append)["items"][0]
    assert slept == [ec.RETRY_DELAY_S]
    assert item["agentic"]["outcome"] == "PASS"


def test_a_second_retryable_failure_is_recorded_as_an_error(tmp_path: Path) -> None:
    slept: list[float] = []
    item = _run(tmp_path, [_row(1, "PASS")], _FlakyAgent(fail=2), sleep=slept.append)["items"][0]
    assert slept == [ec.RETRY_DELAY_S]
    assert (item["agentic"]["outcome"], item["agentic"]["class"]) == ("error", "WAIT_AND_RETRY")


def test_a_non_retryable_error_is_not_retried(tmp_path: Path) -> None:
    slept: list[float] = []
    rows = [_row(1, "PASS", _PLAN.replace("thing", "boom"))]
    item = _run(tmp_path, rows, _Scripted({"boom": []}, raise_on={"boom"}), sleep=slept.append)
    assert slept == []
    assert item["items"][0]["agentic"]["class"] == "NEEDS_INVESTIGATION"


def test_each_engine_reviews_in_its_own_store(tmp_path: Path) -> None:
    runner = _Scripted({})
    _run(tmp_path, [_row(1, "PASS")], runner)
    roots = {cfg.repo_path for cfg in runner.configs}
    assert len(roots) == 2, roots  # the agentic engine never sees the pipeline's cache


def test_fixture_files_come_from_the_checkout_head(tmp_path: Path) -> None:
    import subprocess

    checkout = tmp_path / "checkout"
    (checkout / "src").mkdir(parents=True)
    (checkout / "src" / "thing.py").write_text("VALUE = 1\n")
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@e.com"),
        ("git", "config", "user.name", "t"),
        ("git", "add", "-A"),
        ("git", "commit", "-q", "-m", "c"),
    ):
        subprocess.run(args, cwd=checkout, check=True, capture_output=True)
    row = _row(1, "PASS")
    row["file_impact"] = [{"path": "src/thing.py"}, {"path": "src/gone.py"}]
    files, missing = ec._fixture_files(row, checkout)
    assert files == {"src/thing.py": "VALUE = 1\n"}
    assert missing == 1
    item = _run(tmp_path, [row], _Scripted({}), checkout=checkout)["items"][0]
    assert item["missing_fixture_files"] == 1


def test_resume_reviews_errored_rows_again(tmp_path: Path) -> None:
    rows = [_row(1, "PASS", _PLAN.replace("thing", "boom"))]
    first = _run(tmp_path, rows, _Scripted({"boom": []}, raise_on={"boom"}))
    assert first["items"][0]["agentic"]["outcome"] == "error"
    runner = _Scripted({})
    second = _run(tmp_path, rows, runner, resume=first)
    assert runner.calls > 0
    assert second["items"][0]["agentic"]["outcome"] == "PASS"


def test_matching_is_one_to_one_and_the_threshold_is_inclusive() -> None:
    # One pipeline finding cannot match two near-identical agentic ones.
    assert ec.match_findings(["alpha beta"], ["alpha beta", "alpha beta"]) == {
        "matched": 1,
        "pipeline_only": 0,
        "agentic_only": 1,
    }
    # Jaccard exactly 0.5 ({alpha, beta} vs {alpha, beta, gamma, delta}) matches.
    assert ec.match_findings(["alpha beta"], ["alpha beta gamma delta"])["matched"] == 1
    assert ec.match_findings(["alpha beta"], ["alpha gamma delta"])["matched"] == 0


def test_row_title_prefers_the_tracker_then_the_first_prose_line() -> None:
    row = _row(1, "PASS", "## Context\n\nThe parser drops entries.\n")
    assert ec.row_title(row, lambda tid: "Real title") == "Real title"
    assert ec.row_title(row, lambda tid: None) == "The parser drops entries."


def test_a_run_that_compares_nothing_is_invalid() -> None:
    floor = {"outcome": "floor", "findings": []}
    items = [{"pipeline": floor, "agentic": floor, "historical_pipeline_verdict": "PASS"}]
    assert ec.summarize(items)["valid"] is False


def test_summarize_excludes_undecided_history_and_sums_overlap_over_compared_items() -> None:
    def item(p, a, hist, overlap):
        return {
            "pipeline": {"outcome": p},
            "agentic": {"outcome": a},
            "historical_pipeline_verdict": hist,
            "overlap": overlap,
        }

    items = [
        item(
            "PASS", "PASS", "INDETERMINATE", {"matched": 1, "pipeline_only": 2, "agentic_only": 0}
        ),
        item("BLOCK", "PASS", "BLOCK", {"matched": 0, "pipeline_only": 1, "agentic_only": 3}),
        item("error", "PASS", "PASS", {"matched": 9, "pipeline_only": 9, "agentic_only": 9}),
    ]
    summary = ec.summarize(items)
    assert summary["overlap"] == {"matched": 1, "pipeline_only": 3, "agentic_only": 3}
    assert summary["historical_pipeline_agreement"]["pipeline"] == {"agree": 1, "n": 1}
    assert summary["historical_pipeline_agreement"]["agentic"] == {"agree": 1, "n": 2}


def test_cli_exit_code_and_file_backed_resume(tmp_path: Path) -> None:
    labels = _labels(tmp_path, [_row(1, "PASS")])
    out = tmp_path / "results.json"
    first = _Scripted({})
    args = ["--labels", str(labels), "--out", str(out)]
    assert ec.main(args, make_runner=lambda cfg: first, title_lookup=lambda t: None) == 0
    assert json.loads(out.read_text())["valid"] is True
    second = _Scripted({})
    assert (
        ec.main([*args, "--resume"], make_runner=lambda cfg: second, title_lookup=lambda t: None)
        == 0
    )
    assert second.calls == 0
    floor = _labels(tmp_path, [_row(2, "PASS", _FLOOR_PLAN)])
    out2 = tmp_path / "floor.json"
    assert (
        ec.main(
            ["--labels", str(floor), "--out", str(out2)],
            make_runner=lambda c: first,
            title_lookup=lambda t: None,
        )
        == 1
    )


# ── defects of vegetarian-mica-phoebe ───────────────────────────────────────────
def test_overlap_counts_overflow_advisories_beyond_the_cap() -> None:
    verdict = {
        "blocking": [{"finding": "block 0", "tier": "LLM"}],
        "advisory": [
            {"finding": "det advice", "tier": "DET"},
            *({"finding": f"advice {n}"} for n in range(20)),
        ],
        "overflow": [{"finding": f"overflow {n}"} for n in range(5)],
    }
    surfaced = ec._surfaced(verdict)
    assert len(surfaced) == 26
    assert "overflow 4" in surfaced and "det advice" not in surfaced


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("corpus_digest", "0" * 64),
        ("corpus", "labels-other.jsonl"),
        ("match_threshold", 0.9),
        ("schema_version", 1),
    ],
)
def test_resume_refuses_a_saved_run_with_a_different_header(
    tmp_path: Path, capsys, field: str, value: object
) -> None:
    labels = _labels(tmp_path, [_row(1, "PASS")])
    out = tmp_path / "results.json"
    args = ["--labels", str(labels), "--out", str(out)]
    assert ec.main(args, make_runner=lambda cfg: _Scripted({}), title_lookup=lambda t: None) == 0
    saved = json.loads(out.read_text())
    saved[field] = value
    out.write_text(json.dumps(saved))
    capsys.readouterr()
    runner = _Scripted({})
    code = ec.main([*args, "--resume"], make_runner=lambda cfg: runner, title_lookup=lambda t: None)
    assert code == 2
    assert field in capsys.readouterr().err
    assert runner.calls == 0
    assert json.loads(out.read_text())[field] == value  # the saved file is left untouched


def test_each_engine_record_names_its_runner_and_model(tmp_path: Path) -> None:
    rows = [_row(1, "PASS"), _row(2, "PASS", _PLAN.replace("thing", "boom"))]
    items = _run(tmp_path, rows, _Scripted({"boom": []}, raise_on={"boom"}))["items"]
    for engine in ec.ENGINES:
        record = items[0][engine]
        assert isinstance(record["runner"], str) and record["runner"]
        assert isinstance(record["model"], str) and record["model"]
        assert "ran_model" in record
    # A provider error degrades inside review_plan, so the errored record still names its runner.
    degraded = items[1]["agentic"]
    assert degraded["outcome"] == "error" and degraded["runner"]


def test_provenance_copies_the_verdicts_runner_model_and_served_model() -> None:
    verdict = {
        "runner": "pydantic_ai",
        "model": "bedrock:us.anthropic.claude-sonnet-5-5",
        "provider_provenance": {"ran_model": "us.anthropic.claude-opus-5-5"},
    }
    assert ec._provenance(verdict) == {
        "runner": "pydantic_ai",
        "model": "bedrock:us.anthropic.claude-sonnet-5-5",
        "ran_model": "us.anthropic.claude-opus-5-5",
    }
    assert ec._provenance(None) == {"runner": None, "model": None, "ran_model": None}
