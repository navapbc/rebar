"""Test the shared four-pass review kernel from ``vivid-gang-day``.

The suite derives Pass 3 decisions from validity, impact, priority, and configured
thresholds. Contrasting thresholds verify parameterized policy. Plan review uses the
shared kernel rather than maintaining separate decision logic.
"""

from __future__ import annotations

import importlib

import pytest

from rebar.llm import review_kernel
from rebar.llm.plan_review import coach_moves
from rebar.llm.review_kernel import decide as kdecide

kcoach = importlib.import_module("rebar.llm.review_kernel.coach")

pytestmark = pytest.mark.unit


def _verif(binary=None, attrs=None) -> dict:
    base_b = {q: "yes" for q in review_kernel.GRADED_BINARY}
    base_b["cited_reference_accurate"] = "na"
    base_a = {
        "prod_impact": "high",
        "debt_impact": "high",
        "blast_radius": "system",
        "likelihood": "high",
        "reversibility": "hard",
    }
    return {
        "binary": {**base_b, **(binary or {})},
        "severity_attributes": {**base_a, **(attrs or {})},
    }


# ── ground-truth math (by construction) ───────────────────────────────────────
def test_validity_is_the_graded_fraction() -> None:
    assert review_kernel.validity({q: "yes" for q in review_kernel.GRADED_BINARY}) == 1.0
    assert review_kernel.validity({q: "no" for q in review_kernel.GRADED_BINARY}) == 0.0
    assert review_kernel.validity({q: "insufficient" for q in review_kernel.GRADED_BINARY}) == 0.5
    # 'na' answers are excluded from the graded denominator; empty ⇒ 0.0.
    assert review_kernel.validity({}) == 0.0


def test_impact_is_mean_of_ordinal_attributes() -> None:
    # all-max ⇒ 1.0; all-floor ⇒ the ordinal floors averaged (none/local/low/easy).
    assert review_kernel.impact(_verif()["severity_attributes"]) == 1.0
    floor = {
        "prod_impact": "none",
        "debt_impact": "none",
        "blast_radius": "local",
        "likelihood": "low",
        "reversibility": "easy",
    }
    assert review_kernel.impact(floor) == round((0.0 + 0.33 + 0.33 + 0.33) / 4.0, 4)
    # prod/debt take the MAX of the two, not the sum.
    assert review_kernel.impact(
        {"prod_impact": "high", "debt_impact": "none"}
    ) == review_kernel.impact({"prod_impact": "none", "debt_impact": "high"})


def test_severity_label_buckets() -> None:
    assert review_kernel.severity_label(0.8) == "critical"
    assert review_kernel.severity_label(0.6) == "major"
    assert review_kernel.severity_label(0.3) == "minor"
    assert review_kernel.severity_label(0.1) == "none"


def test_pass3_decide_never_stamps_a_severity_key() -> None:
    """Pass 3 omits the retired ``severity`` field from every outcome."""
    assert "severity" not in review_kernel.pass3_decide(None)
    assert "severity" not in review_kernel.pass3_decide(_verif(), blocking_enabled=True)
    assert "severity" not in review_kernel.pass3_decide(_verif(), blocking_enabled=False)
    # low-validity drop
    n_no = len(review_kernel.GRADED_BINARY) // 2 + 1
    low = _verif(binary={q: "no" for q in list(review_kernel.GRADED_BINARY)[:n_no]})
    d_low = review_kernel.pass3_decide(low)
    assert d_low["decision"] == "dropped"
    assert "severity" not in d_low
    # cited-reference veto
    vetoed = _verif(binary={"cited_reference_accurate": "no"})
    d_vetoed = review_kernel.pass3_decide(vetoed)
    assert d_vetoed["decision"] == "dropped"
    assert "severity" not in d_vetoed


def test_decision_labels_by_construction() -> None:
    # no verification ⇒ indeterminate
    assert review_kernel.pass3_decide(None)["decision"] == "indeterminate"
    # all-yes, max severity, blocking opted in + over threshold ⇒ block
    assert review_kernel.pass3_decide(_verif(), blocking_enabled=True)["decision"] == "block"
    # same finding, blocking NOT opted in ⇒ advisory (the v1 default posture)
    assert review_kernel.pass3_decide(_verif(), blocking_enabled=False)["decision"] == "advisory"
    # A strict no majority keeps validity below 0.5 as the graded set grows.
    n_no = len(review_kernel.GRADED_BINARY) // 2 + 1
    low = _verif(binary={q: "no" for q in list(review_kernel.GRADED_BINARY)[:n_no]})
    assert review_kernel.pass3_decide(low)["decision"] == "dropped"
    # the cited-reference veto drops even a high-validity finding
    vetoed = _verif(binary={"cited_reference_accurate": "no"})
    assert review_kernel.pass3_decide(vetoed)["decision"] == "dropped"


def test_absence_claim_veto_drops_refuted_absence() -> None:
    """A refuted absence claim is dropped even at maximum validity and impact."""
    refuted = _verif(binary={"claims_absence": "yes", "absence_confirmed_in_context": "no"})
    d = review_kernel.pass3_decide(refuted, blocking_enabled=True)
    assert d["decision"] == "dropped"
    assert d["reason"] == "veto:absence-refuted"


def test_on_target_veto_only_fires_in_execution_review() -> None:
    """Only execution review drops a goal that the current state already satisfies."""
    on_target = _verif(binary={"current_state_satisfies_plan_goal": "yes"})
    # Planning phase (default): the veto is inert — full-validity finding survives.
    assert review_kernel.pass3_decide(on_target, blocking_enabled=True)["decision"] != "dropped"
    # Execution phase: the veto fires even at full validity + impact.
    d = review_kernel.pass3_decide(on_target, blocking_enabled=True, execution_review=True)
    assert d["decision"] == "dropped"
    assert d["reason"] == "veto:plan-goal-satisfied"
    # Only a DEFINITE "yes" vetoes: "no"/"insufficient"/absent never drop, even in execution.
    for ans in ("no", "insufficient", "na"):
        keep = _verif(binary={"current_state_satisfies_plan_goal": ans})
        assert (
            review_kernel.pass3_decide(keep, blocking_enabled=True, execution_review=True)[
                "decision"
            ]
            != "dropped"
        )


# ── the threshold is a PARAMETER: two consumers, one kernel, independent partitions ──
def test_parameterized_threshold_two_consumers_one_kernel() -> None:
    """One kernel keeps a mid-priority finding advisory or blocking by consumer threshold."""
    mid = _verif(
        attrs={
            "prod_impact": "low",
            "debt_impact": "low",
            "blast_radius": "module",
            "likelihood": "medium",
            "reversibility": "moderate",
        }
    )
    priority = review_kernel.pass3_decide(mid, blocking_enabled=True, block_threshold=0.0)[
        "priority"
    ]
    assert 0.0 < priority < 0.95
    strict = review_kernel.pass3_decide(mid, block_threshold=0.95, blocking_enabled=True)
    lenient = review_kernel.pass3_decide(mid, block_threshold=priority, blocking_enabled=True)
    assert strict["decision"] == "advisory"
    assert lenient["decision"] == "block"


def test_pass3_over_findings_uses_the_threshold_resolver() -> None:
    """``pass3_over_findings`` resolves the per-finding posture via the consumer-supplied
    callable, keyed on each finding's criteria — proving the lookup is parameterized, not
    hardcoded."""
    findings = [{"finding": "a", "criteria": ["STRICT"]}, {"finding": "b", "criteria": ["LENIENT"]}]
    verifs = {0: _verif(), 1: _verif()}  # both max priority (1.0)

    def threshold_for(criteria):
        # STRICT never blocks (threshold above max); LENIENT blocks (opted in, low threshold).
        if "LENIENT" in criteria:
            return 0.5, True
        return 1.5, True

    decided = review_kernel.pass3_over_findings(findings, verifs, threshold_for=threshold_for)
    assert [d["decision"] for d in decided] == ["advisory", "block"]
    # each decided finding carries its verification + the LLM tier marker
    assert all(d["tier"] == "LLM" and d["verification"] is not None for d in decided)


# ── no second copy: the plan-review re-exports ARE the kernel objects (AC #3) ──
def test_plan_review_reexports_are_the_kernel_objects() -> None:

    assert kdecide.pass3_decide is kdecide.pass3_decide
    assert kdecide.validity is kdecide.validity
    assert kdecide.impact is kdecide.impact
    assert kdecide.severity_label is kdecide.severity_label
    assert kdecide.GRADED_BINARY is kdecide.GRADED_BINARY
    assert kdecide.DEFAULT_BLOCK_THRESHOLD == kdecide.DEFAULT_BLOCK_THRESHOLD


# ── WS2: Pass-2 finding-verifier + the `verification` contract ─────────────────
from rebar.llm.review_kernel import verify as kverify  # noqa: E402


def _fnd(text: str, *, criteria=("E1",), evidence=(), impact_text="") -> dict:
    return {
        "finding": text,
        "criteria": list(criteria),
        "evidence": list(evidence),
        "impact": impact_text,
    }


def _binary_fields(model: type) -> set[str]:
    return set(
        model.model_fields["verifications"]
        .annotation.__args__[0]
        .model_fields["binary"]
        .annotation.model_fields
    )


def _severity_fields(model: type) -> set[str]:
    return set(
        model.model_fields["verifications"]
        .annotation.__args__[0]
        .model_fields["severity_attributes"]
        .annotation.model_fields
    )


def test_verification_contract_shares_the_binary_vocabulary() -> None:
    """Plan review extends the shared contract without changing code review's vocabulary."""
    from rebar.llm.plan_review import passes

    # plan-review dispatches the EXTENDED model, not the kernel alias
    assert passes._pass2_model is kverify.plan_review_verification_model

    # The shared vocabulary adds three conditional vetoes outside validity grading.
    expected_binary = {
        *review_kernel.GRADED_BINARY,
        "cited_reference_accurate",
        "claims_absence",
        "absence_confirmed_in_context",
    }
    base = kverify.verification_model()
    plan = kverify.plan_review_verification_model()
    code = kverify.code_review_verification_model()
    # Base and code-review remain byte-identical. Plan-review adds the prerequisite-only
    # attribution veto outside validity arithmetic; it must not leak into the shared/code
    # contracts or the graded vocabulary.
    assert _binary_fields(base) == expected_binary
    assert _binary_fields(code) == expected_binary
    assert _binary_fields(plan) == expected_binary | {
        "prerequisite_attribution_valid",
        "current_state_satisfies_plan_goal",
    }
    assert "prerequisite_attribution_valid" not in review_kernel.GRADED_BINARY
    assert "current_state_satisfies_plan_goal" not in review_kernel.GRADED_BINARY
    # The on-target veto binary MUST default to "na" (abstain) so an omitted/tool-less
    # verification never spuriously fires the execution-phase drop.
    _binary_model = (
        plan.model_fields["verifications"].annotation.__args__[0].model_fields["binary"].annotation
    )
    assert _binary_model.model_fields["current_state_satisfies_plan_goal"].default == "na"
    # the plan model is a strict SUPERSET on severity_attributes: the base five + 7 axes + detection
    base_sev = _severity_fields(base)
    plan_sev = _severity_fields(plan)
    assert base_sev == {"prod_impact", "debt_impact", "blast_radius", "likelihood", "reversibility"}
    assert base_sev < plan_sev
    assert plan_sev - base_sev == {
        "ac_unverifiable",
        "dod_uncertifiable",
        "undecomposed",
        "divergent_implementation",
        "internal_conflict",
        "vague_directive",
        "irreversible_without_rationale",
        "silent_vs_self_revealing",
    }


def test_listing_preserves_global_index() -> None:
    listing = kverify.verify_instructions([(3, _fnd("f3")), (4, _fnd("f4"))])
    assert "indices 3–4" in listing
    assert "### finding index 3" in listing and "### finding index 4" in listing
    # an empty batch states the empty-array contract (the single aggregate call with no
    # findings must not solicit verifications for indices that cannot exist)
    assert kverify.verify_instructions([]) == kverify.EMPTY_BATCH_INSTRUCTIONS
    assert "empty verifications array" in kverify.verify_instructions([])


def test_chunks_split_over_budget_preserving_global_indices() -> None:
    """Over-budget findings split into >1 chunk; the GLOBAL index is preserved so the
    per-chunk outputs re-merge by index. A tiny window forces splitting."""
    findings = [_fnd(f"finding number {i}") for i in range(6)]
    # window small enough that ~2 findings fit per chunk (system reserve + per-finding output).
    window = kverify.VERIFY_SYSTEM_RESERVE_TOKENS + 4 * kverify.PER_FINDING_VERIFY_TOKENS
    chunks, omitted = kverify.verify_request_chunks(
        findings, window_tokens=int(window / 0.8), est_tokens=lambda s: len(s) // 4, headroom=0.8
    )
    assert len(chunks) > 1, "a tiny window must split the request"
    assert omitted == []
    flat = [gi for chunk in chunks for gi, _ in chunk]
    assert flat == list(range(6)), "global indices preserved + complete across chunks"


def test_chunks_omit_a_finding_too_big_to_verify_alone() -> None:
    huge = _fnd("x" * 100_000)
    small = _fnd("ok")
    # budget fits the small finding's instructions (~hundreds of chars) but not the huge one.
    window = kverify.VERIFY_SYSTEM_RESERVE_TOKENS + kverify.PER_FINDING_VERIFY_TOKENS + 2_000
    chunks, omitted = kverify.verify_request_chunks(
        [huge, small],
        window_tokens=window,
        est_tokens=len,
        headroom=1.0,
    )
    assert omitted == [0], "the too-big finding is omitted by its GLOBAL index"
    assert [gi for chunk in chunks for gi, _ in chunk] == [1]


def test_merge_by_index_keys_on_global_index() -> None:
    out_a = [{"index": 0, "severity_attributes": {"prod_impact": "high"}, "binary": {}}]
    out_b = [{"index": 5, "severity_attributes": {}, "binary": {"is_verifiable": "yes"}}]
    merged = kverify.merge_verifications_by_index([out_a, out_b])
    assert set(merged) == {0, 5}
    assert merged[0]["severity_attributes"]["prod_impact"] == "high"
    # a verification with no usable integer index is dropped (→ no verification → INDETERMINATE)
    assert kverify.merge_verifications_by_index([[{"index": None, "binary": {}}]]) == {}


def test_verify_findings_chunks_runs_and_merges() -> None:
    findings = [_fnd(f"f{i}") for i in range(3)]

    def run_chunk(instructions: str, context: str) -> list[dict]:
        # echo a verification per finding index named in the instructions
        import re

        return [
            {"index": int(m), "severity_attributes": {}, "binary": {"is_verifiable": "yes"}}
            for m in re.findall(r"### finding index (\d+)", instructions)
        ]

    result = kverify.verify_findings(
        findings,
        context="the plan text",
        run_chunk=run_chunk,
        window_tokens=1_000_000,
        est_tokens=lambda s: len(s) // 4,
    )
    assert set(result["verifications"]) == {0, 1, 2}
    assert result["omitted"] == []


def test_verify_findings_degrades_to_indeterminate_on_unparseable_turn() -> None:
    """An unparseable chunk yields no verification and degrades to indeterminate."""
    findings = [_fnd("f0"), _fnd("f1")]

    def run_chunk(instructions: str, context: str) -> list[dict]:
        raise ValueError("model returned garbage that json-repair could not fix")

    result = kverify.verify_findings(
        findings,
        context="ctx",
        run_chunk=run_chunk,
        window_tokens=1_000_000,
        est_tokens=lambda s: len(s) // 4,
    )
    assert result["verifications"] == {}, "a degraded chunk yields no verifications (no crash)"
    # downstream: no verification ⇒ pass3_decide(None) ⇒ INDETERMINATE
    assert review_kernel.pass3_decide(result["verifications"].get(0))["decision"] == "indeterminate"
    # a GENERIC (non-contract) failure is an honest degrade — NOT a contract violation, so the
    # contract-violation report stays empty (distinct from the StructuredOutputError path below).
    assert not result["contract_violations"]


# ── verifier→decide CONTRACT enforcement (epic drag-gripe-brake) ─────────────────────────────
def test_strict_verification_model_rejects_divergent_shape_tolerant_drops() -> None:
    """The strict model rejects divergent keys that the tolerant model drops or defaults."""
    from rebar.llm.errors import StructuredOutputError
    from rebar.llm.structured import parse_structured

    tolerant = kverify.verification_model()  # the LIVE registered contract (strict=False)
    strict = kverify.verification_model(strict=True)  # test-pinned, flip-ready

    wrong_wrapper = '{"findings": [{"index": 0, "binary": {}}]}'  # `findings` not `verifications`
    wrong_item_key = (  # `attributes` not `severity_attributes`
        '{"verifications": [{"index": 0, "attributes": {"prod_impact": "high"}}]}'
    )

    # Tolerant parsing drops a wrong wrapper and defaults a wrong item payload.
    assert parse_structured(wrong_wrapper, tolerant).verifications == []
    item = parse_structured(wrong_item_key, tolerant).verifications
    assert len(item) == 1 and item[0].severity_attributes.prod_impact == "none"  # payload lost

    # STRICT path: the same divergences are REJECTED loudly instead of silently mishandled.
    with pytest.raises(StructuredOutputError):
        parse_structured(wrong_wrapper, strict)
    with pytest.raises(StructuredOutputError):
        parse_structured(wrong_item_key, strict)


def test_reshape_classifies_contract_violations_structurally() -> None:
    """Reshaping reports duplicate, unexpected, and malformed indices without changing its map."""
    raw = [
        {"index": 0, "binary": {"is_verifiable": "yes"}},
        {"index": 0, "binary": {}},  # duplicate
        {"index": 9, "binary": {}},  # out of range (valid is {0, 1})
        {"no_index": True},  # malformed (no usable integer index)
    ]
    reshape = kverify.reshape_verifications(raw, valid_indices=range(2))
    assert reshape.has_violations
    assert reshape.summary() == {"malformed": 1, "duplicates": [0], "unexpected": [9]}
    # the MAP is unchanged from the tolerant behavior: index 0 present (last-wins), 9 excluded.
    assert set(reshape.verifications) == {0}


def test_reshape_clean_run_reports_no_violations() -> None:
    """A conforming verifier output yields the map and an EMPTY (falsy) violation report — so a
    clean run surfaces nothing (the verdict-coverage count stays absent → byte-identical)."""
    reshape = kverify.reshape_verifications(
        [{"index": 0, "binary": {}}, {"index": 1, "binary": {}}], valid_indices=range(2)
    )
    assert not reshape.has_violations
    assert reshape.summary() == {}
    assert set(reshape.verifications) == {0, 1}


def test_verify_findings_surfaces_structured_contract_failure_loudly(caplog) -> None:
    """A structured output failure is logged as a shape failure before fallback."""
    import logging

    from rebar.llm.errors import StructuredOutputError

    findings = [_fnd("f0"), _fnd("f1")]

    def run_chunk(instructions: str, context: str) -> list[dict]:
        raise StructuredOutputError("verifier emitted a shape that failed validation + retry")

    with caplog.at_level(logging.ERROR, logger="rebar.llm.review_kernel.verify"):
        result = kverify.verify_findings(
            findings,
            context="ctx",
            run_chunk=run_chunk,
            window_tokens=1_000_000,
            est_tokens=lambda s: len(s) // 4,
        )
    assert result["verifications"] == {}, "outcome unchanged: a contract break still → no verifs"
    assert result["contract_violations"].get("shape_failures") == [0, 1]
    assert any(record.levelno == logging.ERROR for record in caplog.records), "must log LOUDLY"
    # distinct, but still INDETERMINATE downstream (no crash, verdict-safe)
    assert review_kernel.pass3_decide(result["verifications"].get(0))["decision"] == "indeterminate"


# ── semantically-empty verifier outcomes (story columned-azure-flea) ─────────────────────────
def test_verify_findings_clean_chunk_counts_as_clean_outcome() -> None:
    """A nonempty conforming chunk increments only the clean outcome count."""
    findings = [_fnd("f0"), _fnd("f1")]

    def run_chunk(instructions: str, context: str) -> list[dict]:
        return [
            {"index": 0, "severity_attributes": {}, "binary": {}},
            {"index": 1, "severity_attributes": {}, "binary": {}},
        ]

    result = kverify.verify_findings(
        findings,
        context="ctx",
        run_chunk=run_chunk,
        window_tokens=1_000_000,
        est_tokens=lambda s: len(s) // 4,
    )
    assert not result["contract_violations"]
    assert result["outcome_counts"] == {
        "clean": 1,
        "recovered": 0,
        "empty_outcomes": 0,
        "unrecoverable": 0,
    }


def test_verify_findings_flags_semantically_empty_outcome_as_contract_violation() -> None:
    """An empty successful chunk becomes an empty-outcome violation before fallback."""
    import logging

    findings = [_fnd("f0"), _fnd("f1")]

    def run_chunk(instructions: str, context: str) -> list[dict]:
        return []  # tolerant parse "succeeded" but produced nothing usable

    with caplog_for_verify() as caplog:
        result = kverify.verify_findings(
            findings,
            context="ctx",
            run_chunk=run_chunk,
            window_tokens=1_000_000,
            est_tokens=lambda s: len(s) // 4,
        )
    assert result["verifications"] == {}
    assert result["contract_violations"].get("empty_outcomes") == [0, 1]
    assert result["outcome_counts"]["empty_outcomes"] == 1
    assert any(r.levelno == logging.ERROR for r in caplog.records), "must log LOUDLY"
    assert review_kernel.pass3_decide(result["verifications"].get(0))["decision"] == "indeterminate"


def test_verify_findings_recovered_outcome_counted_distinctly_from_clean() -> None:
    """A nonempty response that needs reshaping counts as recovered rather than clean."""
    findings = [_fnd("f0"), _fnd("f1")]

    def run_chunk(instructions: str, context: str) -> list[dict]:
        return [
            {"index": 0, "severity_attributes": {}, "binary": {}},
            {"index": 0, "severity_attributes": {}, "binary": {}},  # duplicate — needs reshaping
        ]

    result = kverify.verify_findings(
        findings,
        context="ctx",
        run_chunk=run_chunk,
        window_tokens=1_000_000,
        est_tokens=lambda s: len(s) // 4,
    )
    assert result["outcome_counts"]["recovered"] == 1
    assert result["outcome_counts"]["clean"] == 0
    assert result["contract_violations"].get("duplicates") == [0]


def test_verify_findings_mixed_chunks_tally_each_outcome_independently() -> None:
    """Mixed chunks retain independent clean, empty, and shape-failure telemetry."""
    from rebar.llm.errors import StructuredOutputError

    findings = [_fnd(f"f{i}") for i in range(6)]

    def run_chunk(instructions: str, context: str) -> list[dict]:
        if "### finding index 2" in instructions:
            return []  # empty outcome
        if "### finding index 4" in instructions:
            raise StructuredOutputError("divergent shape")
        idx = int(instructions.split("### finding index ")[1].split("\n")[0])
        return [{"index": idx, "severity_attributes": {}, "binary": {}}]

    # A tiny window forces exactly ONE finding per chunk, so each outcome is isolated.
    window = kverify.VERIFY_SYSTEM_RESERVE_TOKENS + kverify.PER_FINDING_VERIFY_TOKENS + 100
    result = kverify.verify_findings(
        findings,
        context="ctx",
        run_chunk=run_chunk,
        window_tokens=window,
        est_tokens=lambda s: 10,
        headroom=1.0,
    )
    counts = result["outcome_counts"]
    assert counts["clean"] >= 1
    assert counts["empty_outcomes"] == 1
    assert counts["unrecoverable"] == 1
    assert result["contract_violations"]["empty_outcomes"] == [2]
    assert result["contract_violations"]["shape_failures"] == [4]


def caplog_for_verify():
    """Small context-manager shim so the empty-outcome test can assert on ERROR logs without
    depending on pytest's `caplog` fixture injection order relative to the `with` block."""
    import contextlib
    import logging

    @contextlib.contextmanager
    def _cm():
        logger = logging.getLogger("rebar.llm.review_kernel.verify")
        handler = logging.Handler()
        records: list[logging.LogRecord] = []
        handler.emit = records.append  # type: ignore[method-assign]
        logger.addHandler(handler)
        prior_level = logger.level
        logger.setLevel(logging.ERROR)
        try:

            class _Caplog:
                pass

            c = _Caplog()
            c.records = records
            yield c
        finally:
            logger.removeHandler(handler)
            logger.setLevel(prior_level)

    return _cm()


def test_resolve_verifier_model_non_frontier_default() -> None:
    # the default model downgrades to the non-frontier verifier default
    assert kverify.resolve_verifier_model("D", default_model="D", verifier_default="V") == "V"
    # an explicit operator choice wins
    assert kverify.resolve_verifier_model("explicit", default_model="D", verifier_default="V") == (
        "explicit"
    )


# ── WS3: Pass-4 coach mechanism + the pluggable move-registry schema ───────────
# NB: import the SUBMODULE via importlib — the package re-exports a `coach` FUNCTION that
# shadows the `coach` submodule attribute on the package.
import importlib  # noqa: E402

kcoach = importlib.import_module("rebar.llm.review_kernel.coach")

_REG = {
    "1": {"name": "spike", "template": "Spike {subject} first."},
    "perf": {"name": "profile", "template": "Profile {subject}.", "applies_when": ["T5a"]},
}


def _pick_first_move(move_id="1", subject="the X design"):
    def pick(instructions, applicable):
        return [{"move_id": move_id, "subject": subject, "finding_refs": ["f0"]}]

    return pick


def test_coach_gates_on_surviving_makes_no_pick_call() -> None:
    """0 surviving advisories ⇒ no LLM pick call at all, empty coaching."""
    calls = []

    def pick(instructions, applicable):
        calls.append(1)
        return []

    assert kcoach.coach([], _REG, pick=pick) == []
    assert calls == [], "the pick (LLM) must NOT be called when there are 0 surviving findings"


def test_coach_applicability_filter_excludes_unmatched_move() -> None:
    """A move with `applies_when` is OFFERED only when its trigger is active; an always-applicable
    move is always offered. The LLM picks among ONLY the applicable subset."""
    surviving = [{"id": "f0", "finding": "perf regression on hot path"}]
    seen_registries = []

    def pick(instructions, applicable):
        seen_registries.append(set(applicable))
        return []

    # no active triggers → only the always-applicable move is offered
    kcoach.coach(surviving, _REG, pick=pick, active_triggers=[])
    assert seen_registries[-1] == {"1"}
    # T5a active → the perf move becomes applicable too
    kcoach.coach(surviving, _REG, pick=pick, active_triggers=["T5a"])
    assert seen_registries[-1] == {"1", "perf"}


def test_coach_drops_a_pick_outside_the_applicable_set() -> None:
    """The LLM cannot select a move outside the applicable subset: a pick of the non-applicable
    `perf` move (no T5a trigger) is dropped at render."""
    surviving = [{"id": "f0", "finding": "x"}]
    notes = kcoach.coach(
        surviving,
        _REG,
        pick=_pick_first_move(move_id="perf", subject="the hot path"),
        active_triggers=[],
    )
    assert notes == [], "a pick outside the applicable set yields no coaching"
    # but when T5a is active, the same pick renders
    notes2 = kcoach.coach(
        surviving,
        _REG,
        pick=_pick_first_move(move_id="perf", subject="the hot path"),
        active_triggers=["T5a"],
    )
    assert len(notes2) == 1 and notes2[0]["move_id"] == "perf"
    assert notes2[0]["coaching"] == "Profile the hot path."


def test_coach_renders_deterministically_from_template() -> None:
    surviving = [{"id": "f0", "finding": "x"}]
    notes = kcoach.coach(surviving, _REG, pick=_pick_first_move(subject="the retry policy"))
    assert notes == [
        {
            "move_id": "1",
            "move_name": "spike",
            "subject": "the retry policy",
            "finding_refs": ["f0"],
            "coaching": "Spike the retry policy first.",
            # story 8086: coach() tags each note with its finding's decision bucket
            "decision": "advisory",
        }
    ]


def test_subject_validator_rejects_imperative_code_and_overlong() -> None:
    assert kcoach.validate_subject("the retry/timeout policy") == "the retry/timeout policy"
    assert kcoach.validate_subject("Add a retry policy") is None  # leading imperative
    assert kcoach.validate_subject("call foo()") is None  # code tokens
    assert kcoach.validate_subject("a " * 20) is None  # too long


def test_validate_move_registry_strict_raises_lenient_drops() -> None:
    bad = {"x": {"name": "no placeholder", "template": "missing the subject slot"}}
    with pytest.raises(ValueError):
        kcoach.validate_move_registry(bad, strict=True)
    # best-effort (project files): the malformed move is DROPPED, not raised
    assert kcoach.validate_move_registry(bad, strict=False) == {}
    # a valid move with applies_when is normalized + kept
    good = {"m": {"name": "n", "template": "do {subject}", "applies_when": ["T5a"]}}
    assert kcoach.validate_move_registry(good)["m"]["applies_when"] == ["T5a"]


def test_plan_review_coach_reexports_are_the_kernel_objects() -> None:

    assert kcoach.render_coach_notes is kcoach.render_coach_notes
    assert kcoach.validate_subject is kcoach.validate_subject
    assert kcoach.coach_listing is kcoach.coach_listing
    assert kcoach.applicable_moves is kcoach.applicable_moves
    # plan-review's MOVE_REGISTRY moves are always-applicable EXCEPT the scoped
    # foundation/enhancement move (epic cite-stone-sea / WS8), offered only for the
    # sizing/complexity/risk criteria in its applies_when.
    for move in coach_moves.MOVE_REGISTRY.values():
        if move.get("applies_when"):
            # scoped: off with no trigger, on for its own triggers
            assert not kcoach.move_applies(move, active_triggers=[])
            assert kcoach.move_applies(move, active_triggers=move["applies_when"])
        else:
            assert kcoach.move_applies(move, active_triggers=[])  # always-applicable


# ── shared run-scoped telemetry (ticket c2c5): contract-violation sink + decide_outcome_counts ──
def test_contract_violation_sink_is_a_no_op_outside_a_collection_scope() -> None:
    # No active sink: recording never raises, and draining returns empty (never leaks/crashes a
    # `*_decide` op that is unit-tested in isolation, outside a gate run).
    review_kernel.record_contract_violation({"duplicates": [0]})
    assert review_kernel.drain_contract_violations() == []


def test_contract_violation_sink_collects_and_drains_within_scope() -> None:
    with review_kernel.collect_contract_violations():
        review_kernel.record_contract_violation({"duplicates": [0]})
        review_kernel.record_contract_violation({})  # falsy summary — not recorded
        review_kernel.record_contract_violation({"unexpected": [5]})
        assert review_kernel.drain_contract_violations() == [
            {"duplicates": [0]},
            {"unexpected": [5]},
        ]
        # drained — a second drain in the same scope is empty
        assert review_kernel.drain_contract_violations() == []
    # the sink is dropped on exit — outside the scope, draining is empty again
    assert review_kernel.drain_contract_violations() == []


def test_decide_outcome_counts_no_findings_is_all_zero() -> None:
    reshape = kverify.reshape_verifications([], valid_indices=range(0))
    assert review_kernel.decide_outcome_counts([], [], reshape) == {
        "clean": 0,
        "recovered": 0,
        "empty_outcomes": 0,
        "unrecoverable": 0,
    }


def test_decide_outcome_counts_empty_verifications_despite_findings_is_empty_outcomes() -> None:
    findings = [{"finding": "f0"}]
    reshape = kverify.reshape_verifications([], valid_indices=range(1))
    counts = review_kernel.decide_outcome_counts([], findings, reshape)
    assert counts == {"clean": 0, "recovered": 0, "empty_outcomes": 1, "unrecoverable": 0}


def test_decide_outcome_counts_clean_reshape_is_clean() -> None:
    findings = [{"finding": "f0"}]
    raw = [{"index": 0, "severity_attributes": {}, "binary": {}}]
    reshape = kverify.reshape_verifications(raw, valid_indices=range(1))
    assert not reshape.has_violations
    counts = review_kernel.decide_outcome_counts(raw, findings, reshape)
    assert counts == {"clean": 1, "recovered": 0, "empty_outcomes": 0, "unrecoverable": 0}


def test_decide_outcome_counts_violated_reshape_is_recovered_never_unrecoverable() -> None:
    findings = [{"finding": "f0"}, {"finding": "f1"}]
    raw = [
        {"index": 0, "severity_attributes": {}, "binary": {}},
        {"index": 0, "severity_attributes": {}, "binary": {}},  # duplicate → has_violations
    ]
    reshape = kverify.reshape_verifications(raw, valid_indices=range(2))
    assert reshape.has_violations
    counts = review_kernel.decide_outcome_counts(raw, findings, reshape)
    # `unrecoverable` is ALWAYS 0 at the decide boundary — a StructuredOutputError is a
    # per-chunk execution signal that does not survive the merge into the flat list.
    assert counts == {"clean": 0, "recovered": 1, "empty_outcomes": 0, "unrecoverable": 0}
