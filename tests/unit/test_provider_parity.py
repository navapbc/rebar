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

import pydantic
import pytest

from rebar.llm import parity
from rebar.llm import structured as _structured
from rebar.llm.config import LLMConfig
from rebar.llm.evals import provider_parity as pp

pytestmark = pytest.mark.unit


class _DirectiveProbe(pydantic.BaseModel):
    x: int


# The fixed lead-in of the prompted-path schema directive (`structured.schema_directive`),
# single-sourced from the real function so this test tracks the production text. A prompted arm
# appends "\n\n" + this directive to the final user turn; a native-output arm carries the schema
# out-of-band and omits it entirely. The directive's only newline is the one after this lead-in
# (the schema JSON is separator-compact), so a split on "\n" isolates it exactly.
_SCHEMA_DIRECTIVE_LEADIN = _structured.schema_directive(_DirectiveProbe).split("\n", 1)[0]

# The schema JSON now TRAILS into "\n\n" + SENTINEL_DIRECTIVE, so the schema is no longer the
# last thing in the turn. The lead-in and this sentinel tail are FIXED (model-independent); only
# the compact schema line between them varies by model. Both are single-sourced from production.
_SENTINEL_TAIL = "\n\n" + _structured.SENTINEL_DIRECTIVE


def _drop_prompted_directive(messages: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], bool]:
    """Strip the trailing prompted-path schema directive from the message stream.

    Returns ``(messages_without_directive, had_directive)``. A native-output arm carries no
    directive (unchanged, ``False``); a prompted arm appended ``"\\n\\n" + schema_directive(...)``
    to its FINAL user turn -- the one documented, measured payload difference the standard slot
    tolerates (see ``test_the_native_output_capability_difference_is_measured_and_id_scoped``).

    Only the final user turn's TRAILING block is stripped, and only when it is the full directive:
    the fixed lead-in, exactly one compact JSON schema line, then the fixed ``_SENTINEL_TAIL`` --
    so a directive on the wrong turn, a duplicated directive, or unexpected trailing content is
    NOT silently stripped away."""
    out = list(messages)
    for i in range(len(out) - 1, -1, -1):
        role, content = out[i]
        if role != "user":
            continue
        marker = "\n\n" + _SCHEMA_DIRECTIVE_LEADIN + "\n"
        idx = content.find(marker)
        if idx == -1 or not content.endswith(_SENTINEL_TAIL):
            return out, False
        schema_text = content[idx + len(marker) : -len(_SENTINEL_TAIL)]
        # the schema is exactly one compact JSON line, bracketed by lead-in and sentinel tail.
        if "\n" in schema_text:
            return out, False
        json.loads(schema_text)  # the appended schema must be well-formed JSON
        out[i] = (role, content[:idx])
        return out, True
    return out, False


def _prompted_directive_schema(messages: list[tuple[str, str]]) -> dict | None:
    """Parse the JSON schema out of a prompted arm's appended directive, or None if absent."""
    for _role, content in messages:
        marker = "\n\n" + _SCHEMA_DIRECTIVE_LEADIN + "\n"
        idx = content.find(marker)
        if idx != -1:
            schema_line = content[idx + len(marker) :].split("\n", 1)[0]
            return json.loads(schema_line)
    return None


def _bedrock_native_schema(payload: dict) -> dict | None:
    """Parse the out-of-band JSON schema a Bedrock native-output arm carries in `outputConfig`,
    or None if the request carries no native structured-output config."""
    spec = (
        ((payload.get("outputConfig") or {}).get("textFormat") or {}).get("structure") or {}
    ).get("jsonSchema")
    if not spec:
        return None
    schema = spec.get("schema")
    return json.loads(schema) if isinstance(schema, str) else schema


# Presentation/strictness envelope keys that the native serializer normalizes but the prompted
# directive (a raw `model_json_schema()`) leaves as-is: `title` (native strips), the root
# `description` (native lifts it into `jsonSchema.description`), and `additionalProperties: False`
# (native adds it). Stripping these compares the schemas' MEANING -- properties, `$defs`, `$ref`s,
# `required`, types, `anyOf`, defaults -- so an omitted or corrupted native schema still fails.
_SCHEMA_ENVELOPE_KEYS = frozenset({"title", "description", "additionalProperties"})


def _schema_shape(schema: object) -> object:
    """The JSON schema stripped of the presentation/strictness envelope keys."""
    if isinstance(schema, dict):
        return {k: _schema_shape(v) for k, v in schema.items() if k not in _SCHEMA_ENVELOPE_KEYS}
    if isinstance(schema, list):
        return [_schema_shape(x) for x in schema]
    return schema


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


def test_a_polarity_free_spec_is_excluded_from_the_corpus() -> None:
    """A spec whose scorer has no block/advisory polarity must not enter the corpus.

    RED before the fix: `code-review-verify` resolved to the `code-review` arm and was eligible,
    so its gold-BLOCK case `V-real-defect` was scored `advisory` on BOTH arms — a guaranteed recall
    miss that looked like agreement (bug ed82-08f3-a693-425f)."""
    eligible = pp.eligible_cases()
    selected = pp.select_corpus(eligible)
    assert {i.solver_id for i in eligible} & pp.POLARITY_FREE_SOLVERS == set()
    assert {i.spec for i in eligible} & pp.POLARITY_FREE_SOLVERS == set()
    assert {i.spec for i in selected} & pp.POLARITY_FREE_SOLVERS == set()
    for solver_id in pp.POLARITY_FREE_SOLVERS:
        assert pp.solver_arm(solver_id) is None


def test_the_polarity_free_exclusion_is_not_folded_into_the_agentic_one() -> None:
    """The two exclusions must stay separately named — they exclude for unrelated reasons.

    AGENTIC_SOLVERS excludes ops `gate_config` cannot reach (both arms would read the same ambient
    model). POLARITY_FREE_SOLVERS excludes a scorer with no decision to compare. Collapsing them
    would lose one reason, and a future reader deleting the "redundant" set would silently
    re-admit mis-scored cases."""
    assert pp.POLARITY_FREE_SOLVERS
    assert pp.POLARITY_FREE_SOLVERS.isdisjoint(pp.AGENTIC_SOLVERS)
    # Each polarity-free solver is excluded ON ITS OWN, not incidentally by the agentic set.
    for solver_id in pp.POLARITY_FREE_SOLVERS:
        assert solver_id not in pp.AGENTIC_SOLVERS
        assert pp.solver_arm(solver_id) is None


def test_a_verifications_only_output_carries_no_decision_polarity() -> None:
    """Why exclusion is the fix and "presence -> block" is NOT.

    `code-review-verify` is scored on the PRESENCE of a `verifications` list, "independently of any
    FAIL/BLOCK polarity", and its dataset requires a non-empty list for the CLEAN diff as much as
    the real-defect one. So the same output shape is correct for a gold-block case and a
    gold-advisory case: mapping presence to `block` would fix the recall miss and simultaneously
    score the clean case as a block, manufacturing a false accept."""
    verify_shaped = {"verifications": [{"id": "F1", "binary": {"path_reachable": "yes"}}]}
    # It has neither key the two-bucket mapping reads, so it cannot yield a meaningful decision.
    assert "verdict" not in verify_shaped
    assert not verify_shaped.get("findings")
    assert pp.verdict_decision(verify_shaped) == "advisory"
    # ...which is why the arm is excluded rather than adapted: `advisory` is wrong for a gold-block
    # case, and `block` would be wrong for the gold-advisory one.


def test_the_gold_floor_still_holds_after_the_polarity_free_exclusion() -> None:
    """Excluding a spec must not drop the corpus below the gold floor.

    Pinned so a future exclusion cannot silently shrink the corpus under `MIN_GOLD_ITEMS`, which
    would make the parity verdict rest on too few gold items."""
    selected = pp.select_corpus(pp.eligible_cases())
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


# ── 7. PAYLOAD + TOOL PARITY: the outbound request, captured per provider ───────────
#
# Epic 061c's scope is that the same PAYLOAD reaches the model and the same TOOLS are offered,
# whatever the provider. Comparing two models' JUDGEMENTS is out of scope: LLMs are
# non-deterministic, so a verdict difference between two different models is expected rather
# than a defect. Parity is therefore proven STRUCTURALLY, from the request that actually leaves
# the process — not by reading code.
#
# Neither arm reaches the network, so this costs nothing. The Anthropic arm's real builder runs
# and wraps an `httpx.MockTransport` (the technique `test_llm_provider_factory` already uses), so
# the genuine SDK assembles the body; the Bedrock arm's real boto3 client is constructed and only
# its `converse` is intercepted. Both abort at the provider boundary.

#: Envelope differences the two provider APIs REQUIRE. Enumerated rather than waved at: the
#: comparison normalizes exactly these away and nothing else, so a NEW divergence shows up as a
#: differing field instead of being silently absorbed.
ENVELOPE_DIFFERENCES = (
    "model id: Anthropic `model` takes the bare id; Bedrock `modelId` takes the "
    "inference-profile id (the `us.` form) -- a plain on-demand id is refused.",
    "max tokens: Anthropic top-level `max_tokens`; Bedrock `inferenceConfig.maxTokens`.",
    "sampling: Anthropic top-level `temperature`; Bedrock `inferenceConfig.temperature`.",
    "system prompt: Anthropic a list of `{type: text, text}` blocks; Bedrock a list of `{text}` "
    "blocks. Same concatenated text.",
    "prompt cache marker: Anthropic a `cache_control: {type: ephemeral, ttl}` ATTRIBUTE on the "
    "block it terminates; Bedrock a standalone `{cachePoint: {type: default}}` LIST ENTRY, "
    "carrying no TTL.",
    "tools: Anthropic `tools: [{name, description, input_schema}]`; Bedrock "
    "`toolConfig.tools: [{toolSpec: {name, description, inputSchema: {json}}}]` plus its own "
    "`{cachePoint}` entry.",
    "tool choice: Anthropic sends `tool_choice: {type: auto}` explicitly; Converse omits it and "
    "defaults to auto.",
    "streaming: Anthropic a `stream` field; Bedrock a different METHOD (`converse` vs "
    "`converse_stream`).",
)


def _normalize(payload: dict, *, provider: str) -> dict:
    """Reduce a captured request to the provider-neutral content under test. Everything
    normalized away is an entry in ENVELOPE_DIFFERENCES."""
    if provider == "bedrock":
        inference = payload.get("inferenceConfig") or {}
        tools = {
            t["toolSpec"]["name"]: (
                t["toolSpec"].get("description"),
                json.dumps(t["toolSpec"]["inputSchema"]["json"], sort_keys=True),
            )
            for t in (payload.get("toolConfig") or {}).get("tools") or []
            if "toolSpec" in t
        }
        model_id, max_tokens = payload["modelId"], inference.get("maxTokens")
        temperature = inference.get("temperature")
    else:
        tools = {
            t["name"]: (t.get("description"), json.dumps(t.get("input_schema"), sort_keys=True))
            for t in payload.get("tools") or []
            if "name" in t
        }
        model_id, max_tokens = payload["model"], payload.get("max_tokens")
        temperature = payload.get("temperature")
    return {
        "model_id": model_id,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system_text": "".join(s.get("text", "") for s in payload.get("system") or []),
        "messages": [
            (m["role"], "".join(c.get("text", "") for c in m["content"]))
            for m in payload.get("messages") or []
        ],
        "tools": tools,
    }


def _capture(model: str, monkeypatch, *, temperature=None) -> dict:
    """Capture the outbound request for ONE logical agentic call, aborting at the boundary."""
    import contextlib

    import boto3
    import httpx
    import pydantic_ai.models

    from rebar.llm import gate_source
    from rebar.llm.runner import PydanticAIRunner, RunRequest

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-capture-dummy")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "capture-dummy")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "capture-dummy")
    monkeypatch.setattr(pydantic_ai.models, "ALLOW_MODEL_REQUESTS", True)

    captured: dict = {}
    bedrock = model.startswith("bedrock:")

    class _Abort(Exception):
        pass

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode())
        captured["endpoint"] = str(request.url)
        raise _Abort()

    real_client = boto3.Session.client

    def _patched_client(self, *a, **kw):
        client = real_client(self, *a, **kw)
        if a and a[0] == "bedrock-runtime":

            def _converse(**params):
                captured["payload"] = params
                captured["endpoint"] = str(client.meta.endpoint_url)
                raise _Abort()

            client.converse = _converse
        return client

    if bedrock:
        monkeypatch.setattr(boto3.Session, "client", _patched_client)
    else:
        monkeypatch.setattr(
            httpx, "AsyncHTTPTransport", lambda *a, **kw: httpx.MockTransport(_handler)
        )

    cfg = LLMConfig(
        model=model,
        repo_path=".",
        runner="pydantic_ai",
        temperature=temperature,
        bedrock_region_name="us-east-1",
    )
    handle = gate_source.resolve_gate_handle(None, "local", None)
    with gate_source.gate_read_root(handle):
        gcfg = gate_source.apply_handle(cfg, handle)
        request = RunRequest(
            system_prompt="You are a code reviewer. Report findings as structured JSON.",
            instructions="Review the diff in the repository and report any defect you find.",
            config=gcfg,
            reviewers=["code-quality"],
            mode="findings",
            output_schema="review_result",
            execution_mode="agentic",
        )
        with contextlib.suppress(Exception):
            PydanticAIRunner(gcfg).run(request)
    assert "payload" in captured, f"no outbound request captured for {model!r}"
    captured["provider"] = "bedrock" if bedrock else "anthropic"
    return captured


def _compare(model_v1: str, model_v2: str, monkeypatch, *, temperature=None) -> tuple[dict, dict]:
    a = _capture(model_v1, monkeypatch, temperature=temperature)
    b = _capture(model_v2, monkeypatch, temperature=temperature)
    return (
        _normalize(a["payload"], provider=a["provider"]),
        _normalize(b["payload"], provider=b["provider"]),
    )


@pytest.mark.parametrize("slot", sorted(pp.CLASS_SLOTS))
def test_the_outbound_payload_is_equivalent_across_providers(slot, monkeypatch) -> None:
    """PAYLOAD PARITY. For the same logical call, the request reaching the provider carries the
    same system prompt, the same messages and the same sampling settings under both providers.
    Only the model id differs -- that is the variable under test."""
    v1_model, v2_model = pp.CLASS_SLOTS[slot]
    a, b = _compare(v1_model, v2_model, monkeypatch)
    assert a["system_text"] == b["system_text"] and a["system_text"]
    if slot == "standard":
        # 18ae: `us.anthropic.claude-sonnet-4-6` (v2, Bedrock) is measured-native, so it carries
        # the schema out-of-band and appends NO schema_directive; its direct-Anthropic twin
        # (v1) DELIBERATELY stays prompted (capabilities._REBAR_OVERRIDES) and appends it. That
        # one id-scoped, measured difference is asserted in full by
        # test_the_native_output_capability_difference_is_measured_and_id_scoped; strip it here
        # so this test still proves EVERYTHING ELSE in the stream is identical.
        a_msgs, a_dir = _drop_prompted_directive(a["messages"])
        b_msgs, b_dir = _drop_prompted_directive(b["messages"])
        assert (a_dir, b_dir) == (True, False)
        assert a_msgs == b_msgs and a_msgs
        skip = {"model_id", "messages"}
    else:
        assert a["messages"] == b["messages"] and a["messages"]
        skip = {"model_id"}
    assert a["max_tokens"] == b["max_tokens"]
    assert a["temperature"] == b["temperature"]
    differing = [f for f in a if f not in skip and a[f] != b[f]]
    assert differing == [], f"{slot}: payload diverges beyond the model id: {differing}"
    assert a["model_id"] != b["model_id"]
    assert b["model_id"].startswith("us.anthropic.")


@pytest.mark.parametrize("slot", sorted(pp.CLASS_SLOTS))
def test_the_tool_set_offered_to_the_model_is_identical_across_providers(slot, monkeypatch) -> None:
    """TOOL PARITY. Same tool names, same descriptions, same input schemas -- the model is
    offered exactly the same capabilities whichever provider carries the call."""
    v1_model, v2_model = pp.CLASS_SLOTS[slot]
    a, b = _compare(v1_model, v2_model, monkeypatch)
    assert a["tools"], "the captured call offered no tools; it cannot prove tool parity"
    assert sorted(a["tools"]) == sorted(b["tools"])
    for name, spec in a["tools"].items():
        assert spec == b["tools"][name], f"{slot}: tool {name} differs across providers"


def test_the_temperature_capability_difference_is_symmetric_and_explicit(monkeypatch) -> None:
    """The one capability difference that legitimately alters the payload, made explicit.

    `us.anthropic.claude-opus-4-8` refuses `temperature` (MEASURED: Converse returns
    `ValidationException: ... \\`temperature\\` is deprecated for this model`), and
    `capabilities._MODEL_ID_CAPABILITY_OVERRIDES` carries `supports_temperature: False` for it.
    The withdrawal must be driven by the MODEL's capability, not by the provider: it applies to
    opus on BOTH providers and to neither sonnet arm."""
    sonnet_v1, sonnet_v2 = pp.CLASS_SLOTS["standard"]
    opus_v1, opus_v2 = pp.CLASS_SLOTS["frontier"]
    s1, s2 = _compare(sonnet_v1, sonnet_v2, monkeypatch, temperature=0.0)
    o1, o2 = _compare(opus_v1, opus_v2, monkeypatch, temperature=0.0)
    # Requested temperature reaches sonnet on both providers...
    assert s1["temperature"] == 0.0 and s2["temperature"] == 0.0
    # ...and is withdrawn from opus on both providers. Symmetric => model-driven, not provider.
    assert o1["temperature"] is None and o2["temperature"] is None
    # Withdrawing it changes nothing else about the payload.
    assert [f for f in o1 if f != "model_id" and o1[f] != o2[f]] == []


def test_the_native_output_capability_difference_is_measured_and_id_scoped(monkeypatch) -> None:
    """The SECOND capability difference that legitimately alters the payload, made explicit.

    18ae enabled `native_structured_output` for `us.anthropic.claude-sonnet-4-6` (the Bedrock
    inference-profile id) in `capabilities._MODEL_ID_CAPABILITY_OVERRIDES`, from a MEASURED
    Converse PASS (E1). A native-output arm carries the JSON schema out-of-band (NativeOutput),
    so `structured.schema_directive` is NOT appended to the user turn; a prompted arm appends it.

    Unlike the temperature withdrawal, this difference is asymmetric BY DESIGN, and legitimately
    so: only the exact Bedrock id is MEASURED-native, and its direct-Anthropic twin
    (`claude-sonnet-4-6`) DELIBERATELY stays PromptedOutput (`capabilities._REBAR_OVERRIDES`,
    'Do not reintroduce it'). Opus (frontier) was enabled on NEITHER arm, so it stays symmetric.
    The divergence is confined to the appended directive -- strip it and the streams are equal."""
    sonnet_v1, sonnet_v2 = pp.CLASS_SLOTS["standard"]
    opus_v1, opus_v2 = pp.CLASS_SLOTS["frontier"]
    raw_s1 = _capture(sonnet_v1, monkeypatch)["payload"]
    raw_s2 = _capture(sonnet_v2, monkeypatch)["payload"]
    s1 = _normalize(raw_s1, provider="anthropic")
    s2 = _normalize(raw_s2, provider="bedrock")
    o1, o2 = _compare(opus_v1, opus_v2, monkeypatch)
    s1_msgs, s1_dir = _drop_prompted_directive(s1["messages"])
    s2_msgs, s2_dir = _drop_prompted_directive(s2["messages"])
    _, o1_dir = _drop_prompted_directive(o1["messages"])
    _, o2_dir = _drop_prompted_directive(o2["messages"])
    # sonnet: direct-Anthropic prompted (directive present), Bedrock native (directive absent).
    assert (s1_dir, s2_dir) == (True, False)
    # opus: prompted on BOTH arms -- native was not enabled for it, so no asymmetry.
    assert (o1_dir, o2_dir) == (True, True)
    # The divergence is confined to the directive: strip it and sonnet's streams are identical.
    assert s1_msgs == s2_msgs and s1_msgs
    # The Bedrock native arm did not merely DROP the schema: it carries it out-of-band in
    # `outputConfig`, semantically EQUAL to the schema the direct-Anthropic arm states in prose.
    # (Without this a native arm that omitted or corrupted its schema would slip through.)
    directive_schema = _prompted_directive_schema(s1["messages"])
    native_schema = _bedrock_native_schema(raw_s2)
    assert directive_schema and native_schema
    assert _schema_shape(native_schema) == _schema_shape(directive_schema)
    # ...and the prompted arm carries NO out-of-band native config (it is PromptedOutput).
    assert _bedrock_native_schema(raw_s1) is None


def test_the_enumerated_envelope_differences_are_the_only_ones_normalized() -> None:
    """The envelope list is documentation with teeth: it must name every field the comparison
    normalizes away, so a future divergence cannot hide behind a vague 'modulo envelope'."""
    joined = " ".join(ENVELOPE_DIFFERENCES).lower()
    for token in (
        "modelid",
        "maxtokens",
        "temperature",
        "system",
        "cachepoint",
        "toolconfig",
        "tool_choice",
        "converse_stream",
    ):
        assert token in joined, f"envelope difference list does not name {token}"
