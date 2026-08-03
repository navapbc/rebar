"""Offline coverage for the provider-parity harness (`rebar.llm.evals.provider_parity`).

No model, no credentials, no network: every arm is driven by an injected solver.

The harness compares a v1 (direct Anthropic) arm against a v2 (Bedrock) arm over a pooled,
gold-labelled slice of the standing eval corpus and scores the pair with `parity.parity_report`.
These tests prove it MEASURES rather than asserts — it passes on agreement, fails on a real
decision flip, fails on an under-covered gold set — and that a transient runtime error can never
be laundered into a decision flip, which is the one failure mode that would produce a wrong
non-inferiority verdict. The committed recorded run is re-scored here against the same bar.
"""

from __future__ import annotations

import json
import pathlib
import subprocess

import pytest

from rebar.llm import parity
from rebar.llm.config import LLMConfig
from rebar.llm.evals import provider_parity as pp

pytestmark = pytest.mark.unit


def _cfg() -> LLMConfig:
    return LLMConfig(runner="fake")


def _fixture_corpus(n_block: int = 12, n_pass: int = 12) -> list[pp.CorpusItem]:
    """A synthetic gold corpus large enough to clear `parity.MIN_GOLD_ITEMS`."""
    items: list[pp.CorpusItem] = []
    for i in range(n_block):
        items.append(
            pp.CorpusItem(
                spec="fixture", case_id=f"B{i:02d}", solver_id="T2", label="block", case={}
            )
        )
    for i in range(n_pass):
        items.append(
            pp.CorpusItem(
                spec="fixture", case_id=f"P{i:02d}", solver_id="T2", label="advisory", case={}
            )
        )
    return items


def _solver(*, flip: set[str] = frozenset(), raise_on: dict[str, Exception] | None = None):
    """A solver that answers each item with its own gold label, except where told to flip or
    raise. `flip` names case ids whose decision is inverted; `raise_on` maps a case id to the
    exception raised for EVERY epoch of that item."""
    raise_on = raise_on or {}

    def solve(item: pp.CorpusItem) -> dict:
        if item.case_id in raise_on:
            raise raise_on[item.case_id]
        decision = item.label
        if item.case_id in flip:
            decision = "advisory" if decision == "block" else "block"
        # `_verdict_decision` reads a FAIL verdict / any finding as `block`.
        return {"verdict": "FAIL" if decision == "block" else "PASS", "findings": []}

    return solve


# ── 1. the harness MEASURES: agreement passes, a real flip fails ────────────────────
def test_agreeing_arms_clear_the_parity_bar() -> None:
    items = _fixture_corpus()
    block = pp.run_slot(
        "standard",
        items,
        v1_config=_cfg(),
        v2_config=_cfg(),
        v1_solve=_solver(),
        v2_solve=_solver(),
        epochs=3,
    )
    assert block["measured"] is True
    assert block["passed"] is True, block["gating_failures"]
    assert block["gating_failures"] == []
    assert block["metrics"]["decision_flips"] == 0
    assert block["metrics"]["n_gold"] == len(items)
    assert block["invalid"] is False


def test_a_flipped_gold_decision_fails_the_bar_and_is_named() -> None:
    items = _fixture_corpus()
    block = pp.run_slot(
        "standard",
        items,
        v1_config=_cfg(),
        v2_config=_cfg(),
        v1_solve=_solver(),
        v2_solve=_solver(flip={"B00"}),
        epochs=3,
    )
    assert block["passed"] is False
    assert block["metrics"]["decision_flips"] == 1
    assert any("decision-level flip(s) on the gold set" in f for f in block["gating_failures"])


def test_a_corpus_below_the_gold_floor_fails_the_coverage_guard() -> None:
    items = _fixture_corpus(n_block=2, n_pass=2)
    block = pp.run_slot(
        "standard",
        items,
        v1_config=_cfg(),
        v2_config=_cfg(),
        v1_solve=_solver(),
        v2_solve=_solver(),
        epochs=1,
    )
    assert block["passed"] is False
    assert any("gold set too small" in f for f in block["gating_failures"])


# ── 2. errors are classified, never laundered into a decision flip ──────────────────
def test_an_erroring_arm_records_an_errored_item_and_the_comparison_completes() -> None:
    items = _fixture_corpus()
    block = pp.run_slot(
        "standard",
        items,
        v1_config=_cfg(),
        v2_config=_cfg(),
        v1_solve=_solver(),
        v2_solve=_solver(raise_on={"B00": RuntimeError("connection reset by peer")}),
        epochs=3,
    )
    # The run COMPLETED (a block was produced) rather than aborting on the raise.
    assert block["measured"] is True
    assert block["error_counts"]["v2"]["generic"] == 3  # one per epoch of the one item
    assert block["excluded_errored_pairs"] == 1


def test_a_transient_error_is_not_counted_as_a_decision_flip() -> None:
    """The load-bearing guard. `parity.parity_report` selects gold pairs on the gold `label`
    alone and then compares decisions with no `errored` guard, so an arm that merely failed to
    run scores as a decision flip and fails the slot as a Bedrock quality regression. The harness
    must exclude errored PAIRS from the records it scores."""
    items = _fixture_corpus()
    block = pp.run_slot(
        "standard",
        items,
        v1_config=_cfg(),
        v2_config=_cfg(),
        v1_solve=_solver(),
        v2_solve=_solver(raise_on={"B00": RuntimeError("throttled: too many requests")}),
        epochs=3,
    )
    assert block["metrics"]["decision_flips"] == 0
    assert not any("decision-level flip" in f for f in block["gating_failures"])
    # The dropped pair is accounted for, not silently swallowed.
    assert block["excluded_errored_pairs"] == 1
    assert block["metrics"]["n_gold"] == len(items) - 1


def test_a_temperature_rejection_marks_the_slot_invalid_not_a_flip() -> None:
    items = _fixture_corpus()
    exc = RuntimeError(
        "ValidationException: The model returned the following errors: "
        "`temperature` is deprecated for this model."
    )
    block = pp.run_slot(
        "standard",
        items,
        v1_config=_cfg(),
        v2_config=_cfg(),
        v1_solve=_solver(),
        v2_solve=_solver(raise_on={"B00": exc}),
        epochs=3,
    )
    assert block["error_counts"]["v2"]["sampling_parameter"] == 3
    assert block["error_counts"]["v2"]["auth_validation"] == 0  # precedence: temperature wins
    assert block["invalid"] is True
    assert any("sampling_parameter" in r for r in block["invalid_reasons"])
    assert block["metrics"]["decision_flips"] == 0


def test_error_classification_precedence_is_ordered() -> None:
    assert pp.classify_error("`temperature` is deprecated") == pp.SAMPLING_PARAMETER
    # A temperature rejection IS a ValidationException; the sampling bucket must win.
    assert pp.classify_error("ValidationException: `temperature` is deprecated") == (
        pp.SAMPLING_PARAMETER
    )
    assert pp.classify_error("AccessDeniedException: not authorized") == pp.AUTH_VALIDATION
    assert pp.classify_error("ValidationException: on-demand throughput") == pp.AUTH_VALIDATION
    assert pp.classify_error("connection reset by peer") == pp.GENERIC


def test_each_arms_config_reaches_every_worker_under_concurrency() -> None:
    """The split-brain guard. Items run in parallel, and a bare thread does NOT inherit the
    `gate_config` ContextVar — a worker would then resolve the AMBIENT model and the "Bedrock"
    arm would silently measure something else. Every worker must see its OWN arm's model."""
    from rebar.llm.config import resolve_gate_config

    seen: dict[str, set[str]] = {"v1": set(), "v2": set()}

    def spy(arm: str):
        def solve(item: pp.CorpusItem) -> dict:
            seen[arm].add(str(resolve_gate_config().model))
            return {"verdict": "FAIL" if item.label == "block" else "PASS"}

        return solve

    items = _fixture_corpus()
    v1_cfg = LLMConfig(runner="fake", model="claude-sonnet-4-6")
    v2_cfg = LLMConfig(runner="fake", model="bedrock:us.anthropic.claude-sonnet-4-6")
    block = pp.run_slot(
        "standard",
        items,
        v1_config=v1_cfg,
        v2_config=v2_cfg,
        v1_solve=spy("v1"),
        v2_solve=spy("v2"),
        epochs=1,
        concurrency=6,
    )
    assert seen["v1"] == {"claude-sonnet-4-6"}
    assert seen["v2"] == {"bedrock:us.anthropic.claude-sonnet-4-6"}
    assert block["passed"] is True


def test_concurrency_keeps_records_aligned_with_the_corpus_order() -> None:
    """Records must stay index-aligned with `items` regardless of thread scheduling, or the
    paired comparison would diff unrelated cases."""
    items = _fixture_corpus()
    block = pp.run_slot(
        "standard",
        items,
        v1_config=_cfg(),
        v2_config=_cfg(),
        v1_solve=_solver(),
        v2_solve=_solver(flip={"B03"}),
        epochs=1,
        concurrency=6,
    )
    assert [r["case_id"] for r in block["records"]] == [i.case_id for i in items]
    flipped = [r for r in block["records"] if r["v1"]["decision"] != r["v2"]["decision"]]
    assert [r["case_id"] for r in flipped] == ["B03"]
    assert block["metrics"]["decision_flips"] == 1


# ── 3. the corpus selector ──────────────────────────────────────────────────────────
def test_the_corpus_excludes_every_agentic_solver_and_clears_the_gold_floor() -> None:
    eligible = pp.eligible_cases()
    selected = pp.select_corpus(eligible)
    assert {i.solver_id for i in eligible} & pp.AGENTIC_SOLVERS == set()
    assert {i.spec for i in selected} & pp.AGENTIC_SOLVERS == set()
    # Every selected case must resolve to a real, non-agentic `run_case` arm — an id that
    # resolves to NOTHING would raise at run time and score as an error, not a verdict.
    assert all(pp.solver_arm(i.solver_id) is not None for i in eligible)
    assert len(selected) >= parity.MIN_GOLD_ITEMS
    assert sum(1 for i in selected if i.label == "block") >= 1
    assert sum(1 for i in selected if i.label == "advisory") >= 1


def test_the_corpus_selection_is_deterministic_and_stratified_per_spec() -> None:
    eligible = pp.eligible_cases()
    first = pp.select_corpus(eligible)
    second = pp.select_corpus(pp.eligible_cases())
    assert [(i.spec, i.case_id) for i in first] == [(i.spec, i.case_id) for i in second]
    assert pp.corpus_digest(first) == pp.corpus_digest(second)
    # At most one gold-block and one gold-advisory case per spec.
    for spec in {i.spec for i in first}:
        per_spec = [i for i in first if i.spec == spec]
        assert sum(1 for i in per_spec if i.label == "block") <= 1
        assert sum(1 for i in per_spec if i.label == "advisory") <= 1
    assert pp.corpus_digest(first) != pp.corpus_digest(first[:-1])


def test_the_isf_finder_spec_is_excluded_because_it_resolves_to_no_arm() -> None:
    """`plan-review-isf-finder` carries gold-labelled cases but `run_case` has no arm for it
    (it is neither a criterion id, nor a code-review id, nor one of the agentic three), so it
    would raise rather than return a verdict. Eligibility is arm RESOLUTION, not merely
    "not one of the agentic three"."""
    assert pp.solver_arm("plan-review-isf-finder") is None
    assert pp.solver_arm("plan-review-isf-finder") not in {"criterion", "code-review", "novelty"}
    assert all(i.spec != "plan-review-isf-finder" for i in pp.eligible_cases())


def test_container_cases_without_a_children_payload_are_excluded() -> None:
    """A container criterion runs over a (parent, children, roster) decomposition, so a fixture
    that carries only the parent plan raises instead of returning a verdict. MEASURED in the
    recorded live run: both plan-review-container G3 cases raised on BOTH arms, at every epoch,
    before any model call. Such a case is not corpus."""
    from rebar.llm.plan_review.pass1 import CONTAINER_CRITERIA

    for item in pp.eligible_cases():
        if item.solver_id in CONTAINER_CRITERIA:
            assert item.case.get("children"), f"{item.spec}/{item.case_id} has no children payload"
    # The rule is real, not vacuous: the container spec DOES carry such cases and they are gone.
    assert not pp._runnable("G3", {"input": "parent plan only"})
    assert pp._runnable("G3", {"children": [{"ticket_id": "t"}]})
    assert pp._runnable("T2", {"input": "inline text"})  # non-container arms are unaffected
    assert all(i.spec != "plan-review-container" for i in pp.eligible_cases())


# ── 4. the provider-clean tally reads the JSONL usage log ───────────────────────────
def test_usage_model_tally_parses_jsonl_rows_not_key_equals_value_text(tmp_path) -> None:
    log = tmp_path / "usage.jsonl"
    log.write_text(
        "\n".join(
            [
                json.dumps({"model": "bedrock:us.anthropic.claude-sonnet-4-6", "input_tokens": 12}),
                json.dumps({"model": "bedrock:us.anthropic.claude-sonnet-4-6", "input_tokens": 30}),
                json.dumps({"model": "anthropic:claude-sonnet-4-6", "input_tokens": 7}),
                # A row with no token count is not a model CALL (e.g. a failure row).
                json.dumps({"model": "anthropic:claude-opus-4-8", "input_tokens": None}),
                "not json at all",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    tally = pp.usage_model_tally(log)
    assert tally == {
        "bedrock:us.anthropic.claude-sonnet-4-6": 2,
        "anthropic:claude-sonnet-4-6": 1,
    }
    assert pp.non_bedrock_calls(tally) == {"anthropic:claude-sonnet-4-6": 1}


# ── 5. the committed recorded live run, re-scored offline with no model call ────────
def test_recorded_live_results_re_score_against_the_same_parity_bar() -> None:
    if not pp.recorded_results_path().exists():
        pytest.skip(
            "no recorded run is committed at this revision -- the artifact lands later in this "
            "stack, and this test is live from that revision onward"
        )
    results = pp.load_recorded_results()
    reports = pp.recheck_recorded(results)
    assert results["slots"], "the recorded run must carry at least one class slot"
    measured = 0
    for slot, block in results["slots"].items():
        if not block.get("measured"):
            assert block.get("refusal"), f"unmeasured slot {slot} must quote a refusal"
            assert slot not in reports
            continue
        measured += 1
        report = reports[slot]
        # Re-scored from the recorded per-item records, with parity_report's DEFAULT
        # min_gold — the floor is never lowered.
        assert report.passed == block["passed"]
        assert report.gating_failures == block["gating_failures"]
        assert report.metrics["decision_flips"] == block["metrics"]["decision_flips"]
        assert report.metrics["n_gold"] == block["metrics"]["n_gold"]
        assert report.metrics["n_gold"] >= parity.MIN_GOLD_ITEMS
        assert block["epochs"] >= 3
        assert block["v1_model"] and block["v2_model"]
        assert block["v2_model"].startswith("bedrock:")
        assert block["corpus_digest"]
        assert set(block["error_counts"]) == {"v1", "v2"}
    assert measured >= 1, "at least one slot must be measured"


def test_recheck_rejects_a_slot_that_is_neither_measured_nor_refused() -> None:
    bad = {"slots": {"standard": {"measured": False}}}
    with pytest.raises(ValueError, match="neither a measured report nor a refusal"):
        pp.recheck_recorded(bad)
    worse = {"slots": {"standard": {"measured": True, "passed": True}}}
    with pytest.raises(ValueError, match="measured slot"):
        pp.recheck_recorded(worse)


# ── 6. the harness has no CI invocation ─────────────────────────────────────────────
def test_the_harness_is_never_invoked_from_a_workflow() -> None:
    """Operator-run only: a live, billable two-arm run must never be reachable from CI."""
    root = pathlib.Path(__file__).resolve().parents[2]
    assert (root / ".github" / "workflows").is_dir(), root
    proc = subprocess.run(
        ["grep", "-rnqE", r"\bprovider_parity\b", ".github/workflows/"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1, f"provider_parity is referenced from a workflow: {proc.stdout}"
