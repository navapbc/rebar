"""Held-out oracle for the banked completion-recovery rework (epic 10ae / story 2948).

Offline only — no live LLM. Stubs/fakes drive the orchestration; the arithmetic and bank
semantics are pinned directly. Covers: criterion-ID minting, pool arithmetic as a function
of criteria count c, batch caps by slot, the 2× iteration conversion and batch-shrink
re-planning, bank idempotency/stamps/caps/truncated flag, the record tool contract, coverage
merge by ID (order-insensitive, retry-on-missing), the zero-progress breaker, the
deterministic no-LLM fallback, and the ABSENCE of any per-criterion fan-out.
"""

from __future__ import annotations

import inspect
from dataclasses import replace

import pytest

from rebar.llm.config import LLMConfig
from rebar.llm.errors import CompletionRecoveryError, LLMBudgetExhaustedError, LLMError
from rebar.llm.workflow import completion_banking as cb
from rebar.llm.workflow import completion_criteria as cc
from rebar.llm.workflow import completion_recovery as cr
from rebar.llm.workflow.executor import StepContext

pytestmark = pytest.mark.unit


def _verify_cfg():
    from rebar._config_schema import VerifyConfig

    return VerifyConfig()


# ── criterion identity ──────────────────────────────────────────────────────────────
def test_mint_criterion_id_format_and_normalization() -> None:
    import hashlib
    import re

    text = "  Ship   the\tFIX  "
    norm = re.sub(r"\s+", " ", text).strip().casefold()
    expect_h = hashlib.sha256(norm.encode()).hexdigest()[:8]
    assert cr._bank.mint_criterion_id(7, text) == f"c07-{expect_h}"
    # whitespace-collapse + casefold ⇒ identical hash suffix, different index prefix.
    a = cb.mint_criterion_id(0, "Ship the fix")
    b = cb.mint_criterion_id(3, "  ship   the   fix ")
    assert a[4:] == b[4:]
    assert a.startswith("c00-") and b.startswith("c03-")


# ── pool arithmetic as a function of c ──────────────────────────────────────────────
def test_pool_arithmetic_pinned_and_clamp_max() -> None:
    vc = _verify_cfg()
    # Recalibrated (ticket 8d74): c=8 → floor 24×8+16 = 208, N 104, global 156,
    # exhausted-primary (spent==N) successor 52.
    pinned = cb.plan_recovery_pool(8, 104, vc)
    assert pinned == {"floor": 208, "N": 104, "global_pool": 156, "successor_pool": 52}
    # clamp-max: c=60 → floor 960 (24×60+16 = 1456 clamped), N 480, global 720, successor 240.
    clamp = cb.plan_recovery_pool(60, 480, vc)
    assert clamp == {"floor": 960, "N": 480, "global_pool": 720, "successor_pool": 240}
    # a healthy (unspent) primary leaves the whole global pool.
    assert cb.plan_recovery_pool(8, 0, vc)["successor_pool"] == 156
    # childful row: direct_children flows through the shared formula (16 steps/child).
    childful = cb.plan_recovery_pool(8, 0, vc, direct_children=4)
    assert childful == {"floor": 272, "N": 136, "global_pool": 204, "successor_pool": 204}
    assert childful["floor"] > cb.plan_recovery_pool(8, 0, vc)["floor"]


def test_pool_scales_linearly_and_clamps() -> None:
    vc = _verify_cfg()
    # 24×c + 16 between the floor_min (160) and the 960 ceiling.
    assert cb.plan_recovery_pool(20, 0, vc)["floor"] == 496
    assert cb.plan_recovery_pool(2, 0, vc)["floor"] == 160  # clamped up to floor_min
    assert cb.plan_recovery_pool(100, 0, vc)["floor"] == 960  # clamped down to max


# ── batch caps by resolved slot ─────────────────────────────────────────────────────
def test_batch_cap_by_slot() -> None:
    from rebar.llm.model_classes import ClassSlot

    slots = {
        "frontier": ClassSlot(model="frontier-x"),
        "standard": ClassSlot(model="standard-x"),
        "trivial": ClassSlot(model="trivial-x"),
    }
    assert cb.successor_batch_cap("frontier-x", slots) == 12
    assert cb.successor_batch_cap("standard-x", slots) == 8
    assert cb.successor_batch_cap("trivial-x", slots) == 8
    # unrecognized → the conservative standard cap.
    assert cb.successor_batch_cap("who-knows", slots) == 8


# ── 2× conversion + batch planning + shrink re-planning ─────────────────────────────
def test_iteration_limit_is_twice_the_request_budget() -> None:
    assert cb.iteration_limit_for(16) == 32
    assert cb.iteration_limit_for(1) == 2


def test_plan_recovery_batches_healthy_pool() -> None:
    batches = cb.plan_recovery_batches(22, 8, 48)
    # ceil(22/8)=3 runs, floor(48/3)=16 requests each ⇒ iteration_limit 32 each.
    assert [b.batch_size for b in batches] == [8, 8, 6]
    assert all(b.budget_requests == 16 and b.iteration_limit == 32 for b in batches)


def test_plan_recovery_batches_shrinks_then_launches() -> None:
    # remainder 9, cap 8, pool 20: an 8-batch needs B≥16 but only gets 10, so it shrinks to 5.
    batches = cb.plan_recovery_batches(9, 8, 20)
    assert [b.batch_size for b in batches] == [5, 4]
    assert all(b.budget_requests == 10 for b in batches)


def test_plan_recovery_batches_no_launch_finalizes_empty() -> None:
    # A 1-criterion batch needs B≥2; pool 1 cannot launch → empty plan (finalize from bank).
    assert cb.plan_recovery_batches(1, 8, 1) == []
    assert cb.plan_recovery_batches(1, 8, 2) == [cb.PlannedBatch(1, 2, 4)]


def test_allocate_batch_matches_plan_first_step() -> None:
    assert cb.allocate_batch(22, 8, 48) == (8, 16)
    assert cb.allocate_batch(9, 8, 20) == (5, 10)
    assert cb.allocate_batch(1, 8, 1) == (0, 0)


# ── bank store: idempotency / stamps / caps / truncated flag ─────────────────────────
def _bank(tmp_path, ticket="T-1", material=None, tree=None):
    stamps = cb.BankStamps(ticket_id=ticket, material_fingerprint=material, tree_sha=tree)
    return cb.CriterionBank(tmp_path / "bank", stamps)


def test_bank_upsert_is_idempotent_and_stamped(tmp_path) -> None:
    bank = _bank(tmp_path, material="mat-abc", tree="tree-def")
    bank.upsert("c00-aaaa", True, "first")
    bank.upsert("c00-aaaa", False, "second")  # overwrite
    entry = bank.get("c00-aaaa")
    assert entry["met"] is False and entry["evidence"] == "second"
    assert entry["ticket_id"] == "T-1"
    assert entry["material_fingerprint"] == "mat-abc"
    assert entry["tree_sha"] == "tree-def"
    assert entry["schema_version"] == cb.BANK_SCHEMA_VERSION
    assert set(bank.banked_ids()) == {"c00-aaaa"}


def test_bank_caps_evidence_and_flags_truncation(tmp_path) -> None:
    bank = _bank(tmp_path)
    bank.upsert("c01-bbbb", True, "x" * 5000)
    entry = bank.get("c01-bbbb")
    assert len(entry["evidence"]) == cb.EVIDENCE_CAP_CHARS == 3000
    assert entry["truncated"] is True
    bank.upsert("c02-cccc", True, "short")
    assert bank.get("c02-cccc")["truncated"] is False


def test_stamp_mismatch_fails_loud_naming_the_stamp(tmp_path) -> None:
    bank = _bank(tmp_path, ticket="T-1", material="mat-1", tree="tree-1")
    # material drift.
    with pytest.raises(CompletionRecoveryError, match="material_fingerprint"):
        bank.preflight(cb.BankStamps("T-1", "mat-2", "tree-1"))
    # tree drift.
    with pytest.raises(CompletionRecoveryError, match="tree_sha"):
        bank.preflight(cb.BankStamps("T-1", "mat-1", "tree-2"))
    # ticket drift.
    with pytest.raises(CompletionRecoveryError, match="ticket_id"):
        bank.preflight(cb.BankStamps("T-9", "mat-1", "tree-1"))
    # a None (not-comparable) stamp is never a mismatch.
    bank.preflight(cb.BankStamps("T-1", None, None))


def test_bank_read_failloud_on_bad_schema(tmp_path) -> None:
    bank = _bank(tmp_path)
    bank.upsert("c00-aaaa", True, "e")
    # Corrupt the entry's schema version.
    path = next((tmp_path / "bank").glob("*.json"))
    path.write_text('{"schema_version": 999}', encoding="utf-8")
    with pytest.raises(CompletionRecoveryError, match="schema"):
        bank.all()


# ── record_criterion_verdict tool contract ──────────────────────────────────────────
def test_record_tool_signature_and_idempotency(tmp_path) -> None:
    bank = _bank(tmp_path)
    tool = bank.make_record_tool()
    params = list(inspect.signature(tool).parameters)
    assert params == ["criterion_id", "met", "evidence"]
    assert tool.__name__ == "record_criterion_verdict"
    tool("c00-aaaa", True, "e1")
    tool("c00-aaaa", False, "e2")  # idempotent overwrite of the provisional entry
    assert bank.get("c00-aaaa")["met"] is False
    # the tool enforces the cap.
    tool("c01-bbbb", True, "y" * 4000)
    assert bank.get("c01-bbbb")["truncated"] is True


def test_record_tool_docstring_is_the_bounded_commit_transition(tmp_path) -> None:
    doc = inspect.getdoc(_bank(tmp_path).make_record_tool()) or ""
    lowered = doc.casefold()
    assert "confident" not in lowered
    assert "current criterion id" in lowered
    assert "single commit action" in lowered
    assert "next response after the third repository evidence call" in lowered
    # met=false is a POSITIVE REFUTATION only; a not-found records nothing — the bounded
    # fallback (framework) owns insufficiency, so the model is never told to bank it.
    assert "record `met=false`" in lowered and "refut" in lowered
    assert "record nothing" in lowered
    assert "confirmation selects the next id" in lowered


# ── coverage merge by ID, order-insensitive, retry-on-missing ────────────────────────
def test_validate_coverage_order_insensitive_success() -> None:
    criteria = ["A crit", "B crit", "C crit"]
    ids = cb.criterion_id_map(criteria)
    # Returned OUT of order, keyed by criterion_id — still full coverage.
    result = {
        "verdict": "FAIL",
        "criteria": [
            {"criterion_id": ids["C crit"], "met": True},
            {"criterion_id": ids["A crit"], "met": True},
            {"criterion_id": ids["B crit"], "met": False},
        ],
    }
    cc._validate_coverage(result, criteria, ids)  # no raise


def test_validate_coverage_retries_on_missing() -> None:
    criteria = ["A crit", "B crit"]
    ids = cb.criterion_id_map(criteria)
    result = {"verdict": "FAIL", "criteria": [{"criterion_id": ids["A crit"], "met": True}]}
    with pytest.raises(CompletionRecoveryError, match="incomplete criterion coverage"):
        cc._validate_coverage(result, criteria, ids)


def test_merge_finalizer_backfills_remainder_from_bank(tmp_path) -> None:
    criteria = ["A crit", "B crit", "C crit"]
    ids = cb.criterion_id_map(criteria)
    bank = _bank(tmp_path)
    bank.upsert(ids["A crit"], True, "banked A")
    bank.upsert(ids["B crit"], True, "banked B")
    # The finalizer scored only C (by verbatim text); A/B are backfilled from the bank.
    finalizer_result = {"verdict": "PASS", "criteria": [{"criterion": "C crit", "met": True}]}
    merged = cb.merge_finalizer_with_bank(finalizer_result, criteria, bank.all(), id_by_text=ids)
    cc._validate_coverage(merged, criteria, ids)
    by_text = {r["criterion"]: r["met"] for r in merged["criteria"]}
    assert by_text == {"A crit": True, "B crit": True, "C crit": True}
    assert merged["verdict"] == "PASS"


def test_downgrade_authority_landed_by_finalizer(tmp_path) -> None:
    criteria = ["A crit"]
    ids = cb.criterion_id_map(criteria)
    bank = _bank(tmp_path)
    bank.upsert(ids["A crit"], True, "banked met=true")
    # The LLM finalizer contradicts the banked PASS (cross-criterion downgrade).
    finalizer_result = {"verdict": "FAIL", "criteria": [{"criterion": "A crit", "met": False}]}
    merged = cb.merge_finalizer_with_bank(finalizer_result, criteria, bank.all(), id_by_text=ids)
    assert merged["criteria"][0]["met"] is False
    assert merged["verdict"] == "FAIL"


# ── deterministic no-LLM fallback ────────────────────────────────────────────────────
def test_assemble_deterministic_verdict_full_coverage_and_provenance(tmp_path) -> None:
    criteria = ["A crit", "B crit", "C crit"]
    ids = cb.criterion_id_map(criteria)
    bank = _bank(tmp_path)
    bank.upsert(ids["A crit"], True, "ok")
    bank.upsert(ids["B crit"], False, "no")
    verdict = cb.assemble_deterministic_verdict("T-1", criteria, bank.all(), id_by_text=ids)
    assert verdict["finalizer"] == "deterministic_fallback"
    assert verdict["certifiable"] is False
    assert verdict["downgrade_authority"] == "skipped"
    assert verdict["verdict"] == "FAIL"
    by_text = {r["criterion"]: r["met"] for r in verdict["criteria"]}
    # banked flags as-is; the unbanked C is a met=false unverified placeholder.
    assert by_text == {"A crit": True, "B crit": False, "C crit": False}
    unbanked = next(r for r in verdict["criteria"] if r["criterion"] == "C crit")
    assert unbanked.get("unverified") and unbanked.get("exhausted")


# ── orchestration harness ────────────────────────────────────────────────────────────
def _ticket(n):
    lines = ["## Acceptance Criteria"]
    lines += [f"- [ ] Criterion number {i} is satisfied" for i in range(n)]
    return {
        "ticket_id": "T-1",
        "title": "t",
        "ticket_type": "task",
        "description": "\n".join(lines),
    }


class _StubRunner:
    """A scripted runner: successor runs invoke ``on_successor``; finalizer runs invoke
    ``on_finalizer``. Records how many successor (verifier) runs launched."""

    name = "stub"

    def __init__(self, on_successor=None, on_finalizer=None):
        self.on_successor = on_successor
        self.on_finalizer = on_finalizer
        self.successor_runs = 0
        self.finalizer_runs = 0

    def preflight(self):
        pass

    def run(self, req):
        if req.execution_mode == "single_turn":
            self.finalizer_runs += 1
            if self.on_finalizer is None:
                raise LLMError("no finalizer scripted")
            return self.on_finalizer(req)
        self.successor_runs += 1
        record = req.extra_tools[0] if req.extra_tools else None
        return self.on_successor(req, record, self.successor_runs)


def _step(runner, model="unmapped-model"):
    cfg = replace(LLMConfig.from_env(), model=model)
    return cr.CompletionAgentStep(runner=runner, repo_root=None, config=cfg)


def _ctx(n, tmp_path, monkeypatch):
    monkeypatch.setattr("rebar._reads.show_ticket", lambda tid, repo_root=None: _ticket(n))
    return StepContext(
        run_id="run-xyz",
        step_id="s1",
        kind="agent",
        step={},
        inputs={"ticket_id": "T-1", "context": "CTX"},
        workflow={},
        target_ticket="T-1",
        repo_root=str(tmp_path),
    )


def _primary_exc(requests=0):
    exc = LLMBudgetExhaustedError("primary exhausted")
    exc.diagnostic = {"requests": requests}
    return exc


def _fresh_bank(tmp_path):
    stamps = cb.BankStamps("T-1", None, None)
    return cb.CriterionBank(tmp_path / "run-xyz" / "bank", stamps)


# ── fan-out ABSENCE: one BATCHED run per batch, never one per criterion ───────────────
def test_no_per_criterion_fan_out(tmp_path, monkeypatch) -> None:
    ctx = _ctx(10, tmp_path, monkeypatch)
    ids = cb.criterion_id_map(cr.explicit_completion_criteria(_ticket(10)))
    id_list = list(ids.values())

    def on_successor(req, record, run_no):
        # Bank every criterion listed in this batch (id appears in the instructions).
        banked = [cid for cid in id_list if cid in req.instructions]
        for cid in banked:
            record(cid, True, "ev")
        return {"verdict": "PASS", "criteria": [], "_usage": {"requests": 0}}

    def on_finalizer(req):
        crit = cr.explicit_completion_criteria(_ticket(10))
        return {
            "verdict": "PASS",
            "criteria": [{"criterion": c, "met": True} for c in crit],
        }

    runner = _StubRunner(on_successor=on_successor, on_finalizer=on_finalizer)
    step = _step(runner)
    bank = _fresh_bank(tmp_path)
    result = step._recover(ctx, _primary_exc(), bank)
    # 10 criteria, standard cap 8 ⇒ ceil(10/8)=2 batched runs — NOT 10 per-criterion runs.
    assert runner.successor_runs == 2
    assert result.outputs["verdict"] == "PASS"


# ── zero-progress breaker ────────────────────────────────────────────────────────────
def test_zero_progress_breaker_stops_and_finalizes(tmp_path, monkeypatch) -> None:
    ctx = _ctx(4, tmp_path, monkeypatch)
    crit = cr.explicit_completion_criteria(_ticket(4))
    ids = cb.criterion_id_map(crit)

    def on_successor(req, record, run_no):
        # Bank NOTHING new — triggers the zero-progress breaker after one run.
        return {"verdict": "FAIL", "criteria": [], "_usage": {"requests": 1}}

    def on_finalizer(req):
        return {
            "verdict": "FAIL",
            "criteria": [{"criterion": c, "met": (c == crit[0])} for c in crit],
        }

    runner = _StubRunner(on_successor=on_successor, on_finalizer=on_finalizer)
    step = _step(runner)
    bank = _fresh_bank(tmp_path)
    # Pre-bank one criterion (as the PRIMARY would have) so the bank is non-empty.
    bank.upsert(ids[crit[0]], True, "primary banked")
    result = step._recover(ctx, _primary_exc(), bank)
    assert runner.successor_runs == 1  # stopped after the first no-progress successor
    assert result.outputs["verdict"] == "FAIL"  # full coverage from the bank


def test_zero_banked_total_stays_verdict_less_error(tmp_path, monkeypatch) -> None:
    ctx = _ctx(4, tmp_path, monkeypatch)

    def on_successor(req, record, run_no):
        return {"verdict": "FAIL", "criteria": [], "_usage": {"requests": 1}}

    runner = _StubRunner(on_successor=on_successor)
    step = _step(runner)
    bank = _fresh_bank(tmp_path)
    # Nothing banked by primary or successor ⇒ the ONE verdict-less state: a typed error.
    with pytest.raises(CompletionRecoveryError, match="banked no verdicts"):
        step._recover(ctx, _primary_exc(), bank)
    assert step.failure_diagnostic is not None


# ── deterministic fallback through the orchestrator ──────────────────────────────────
def test_deterministic_fallback_when_finalizer_fails_twice(tmp_path, monkeypatch) -> None:
    ctx = _ctx(2, tmp_path, monkeypatch)
    crit = cr.explicit_completion_criteria(_ticket(2))
    ids = cb.criterion_id_map(crit)

    def on_successor(req, record, run_no):
        for cid in [c for c in ids.values() if c in req.instructions]:
            record(cid, True, "ev")
        return {"verdict": "PASS", "criteria": [], "_usage": {"requests": 0}}

    runner = _StubRunner(on_successor=on_successor, on_finalizer=None)  # finalizer always raises
    step = _step(runner)
    bank = _fresh_bank(tmp_path)
    result = step._recover(ctx, _primary_exc(), bank)
    # finalizer retried once, failed twice ⇒ deterministic no-LLM verdict.
    assert runner.finalizer_runs == 2
    assert result.outputs["finalizer"] == "deterministic_fallback"
    assert result.outputs["certifiable"] is False
    assert result.outputs["downgrade_authority"] == "skipped"
    # both criteria were banked met=true by the successor ⇒ full-coverage PASS.
    assert result.outputs["verdict"] == "PASS"


# ── K-of-N survival with successor coverage ──────────────────────────────────────────
def test_k_of_n_survival_full_coverage(tmp_path, monkeypatch) -> None:
    ctx = _ctx(3, tmp_path, monkeypatch)
    crit = cr.explicit_completion_criteria(_ticket(3))
    ids = cb.criterion_id_map(crit)

    def on_successor(req, record, run_no):
        for cid in [c for c in ids.values() if c in req.instructions]:
            record(cid, True, "successor ev")
        return {"verdict": "PASS", "criteria": [], "_usage": {"requests": 0}}

    def on_finalizer(req):
        return {"verdict": "PASS", "criteria": [{"criterion": c, "met": True} for c in crit]}

    runner = _StubRunner(on_successor=on_successor, on_finalizer=on_finalizer)
    step = _step(runner)
    bank = _fresh_bank(tmp_path)
    bank.upsert(ids[crit[0]], True, "primary banked K")  # K=1 banked by primary
    result = step._recover(ctx, _primary_exc(), bank)
    # remainder (2) covered by ONE batched successor (cap 8) ⇒ 1 run, full coverage.
    assert runner.successor_runs == 1
    assert {r["criterion"] for r in result.outputs["criteria"]} == set(crit)
    assert result.outputs["verdict"] == "PASS"


# ── primary criterion-id manifest (dogfood fix: the primary must be told the ids) ─────
def test_primary_criteria_manifest_lists_every_id_and_truncates() -> None:
    crit = ["Criterion one exists", "  Criterion  two\tworks  ", "z" * 400]
    ids = cb.criterion_id_map(crit)
    manifest = cb.primary_criteria_manifest(crit, ids)
    for cid in ids.values():
        assert cid in manifest
    assert "record_criterion_verdict" not in manifest
    assert "bank" not in manifest.casefold()
    assert manifest.splitlines()[1] == "## Criterion IDs"
    assert all(line.startswith("- c") for line in manifest.splitlines()[2:])
    assert "…" in manifest  # the 400-char criterion is truncated
    assert cb.primary_criteria_manifest([], {}) == ""  # no criteria → no manifest


def test_system_prompts_define_the_finite_per_criterion_bank_loop() -> None:
    from pathlib import Path

    primary = Path("src/rebar/llm/reviewers/completion_verifier.md").read_text()
    successor = cb.successor_system_prompt(None)
    for prompt in (primary, successor):
        compact = " ".join(prompt.split())
        assert "exactly one current unbanked criterion" in compact
        assert "use applicable prefetched evidence first" in compact
        assert "at most three additional repository evidence-tool calls" in compact
        assert compact.index("use applicable prefetched evidence first") < compact.index(
            "at most three additional repository evidence-tool calls"
        )
        assert "bank `met=false`" in compact
        assert "only after `record_criterion_verdict` confirms the write" in compact
        assert "every response in this loop contains exactly one tool call" in compact
        assert "increments the current id's evidence-call count" in compact
        assert "at count 3, the next response is commit" in compact
        assert "selects the next id and resets the evidence-call count to 0" in compact
    successor_addendum = " ".join(
        successor.split("## Resuming after exhaustion (incremental banking)", 1)[1].split()
    )
    for requirement in (
        "remainder ids in their listed order",
        "use applicable prefetched evidence first",
        "at most three additional repository evidence-tool calls",
        "every response in this loop contains exactly one tool call",
        "at count 3, the next response is commit",
        "selects the next id and resets the evidence-call count to 0",
    ):
        assert requirement in successor_addendum


def test_primary_manifest_reads_ticket_and_fails_open(tmp_path, monkeypatch) -> None:
    ctx = _ctx(3, tmp_path, monkeypatch)
    step = _step(_StubRunner())
    manifest = step._primary_manifest(ctx, "T-1")
    ids = cb.criterion_id_map(cr.explicit_completion_criteria(_ticket(3)))
    assert manifest and all(cid in manifest for cid in ids.values())

    def _boom(*a, **k):
        raise RuntimeError("read failed")

    monkeypatch.setattr("rebar._reads.show_ticket", _boom)
    assert step._primary_manifest(ctx, "T-1") == ""  # fail-open: primary runs unchanged


def test_primary_run_passes_manifest_as_extra_context(tmp_path, monkeypatch) -> None:
    ctx = _ctx(3, tmp_path, monkeypatch)
    captured: dict = {}

    class _FakePrimary:
        def __init__(self, **kw):
            captured.update(kw)

        def run(self, _ctx):
            raise _primary_exc()

    monkeypatch.setattr(cr, "RunnerAgentStep", _FakePrimary)
    monkeypatch.setattr(
        cr.CompletionAgentStep, "_recover", lambda self, c, e, b: cr._ex.StepResult(outputs={})
    )
    step = _step(_StubRunner())
    step.run(ctx)
    extra = captured.get("extra_context") or ""
    assert "record_criterion_verdict" not in extra
    assert "bank" not in extra.casefold()
    ids = cb.criterion_id_map(cr.explicit_completion_criteria(_ticket(3)))
    assert all(cid in extra for cid in ids.values())
    (record_tool,) = captured.get("extra_tools")
    policy = record_tool._rebar_completion_evidence_policy
    assert policy.criterion_ids == tuple(ids.values())
    assert policy.max_evidence_responses == 3
    assert policy.evidence_tool_names == {"read_file", "list_directory", "search_files"}


def test_primary_empty_criteria_keeps_record_tool_unflagged(tmp_path, monkeypatch) -> None:
    ctx = _ctx(0, tmp_path, monkeypatch)
    captured: dict = {}

    class _FakePrimary:
        def __init__(self, **kw):
            captured.update(kw)

        def run(self, _ctx):
            raise _primary_exc()

    monkeypatch.setattr(cr, "RunnerAgentStep", _FakePrimary)
    monkeypatch.setattr(
        cr.CompletionAgentStep, "_recover", lambda self, c, e, b: cr._ex.StepResult(outputs={})
    )
    _step(_StubRunner()).run(ctx)
    (record_tool,) = captured["extra_tools"]
    assert not hasattr(record_tool, "_rebar_completion_evidence_policy")
    assert captured.get("extra_context") == ""


def test_successor_record_tool_carries_batch_manifest_policy(tmp_path) -> None:
    captured: dict = {}

    class _CaptureRunner:
        def run(self, req):
            captured["request"] = req
            return {"verdict": "FAIL", "criteria": [], "_usage": {"requests": 1}}

    criteria = ["criterion A", "criterion B"]
    ids = cb.criterion_id_map(criteria)
    bank = _fresh_bank(tmp_path)
    step = _step(_CaptureRunner())
    step._run_one_successor(
        _CaptureRunner(),
        "T-1",
        "CTX",
        "SYSTEM",
        criteria,
        ids,
        4,
        bank,
    )
    (record_tool,) = captured["request"].extra_tools
    policy = record_tool._rebar_completion_evidence_policy
    assert policy.criterion_ids == tuple(ids[text] for text in criteria)
    policy.fallback_record(ids[criteria[0]], "bounded evidence")
    # The bounded fallback banks the framework's INSUFFICIENCY record (ticket 1d71):
    # met=false plus the evidence_sufficient=false marker, under its own source.
    entry = bank.get(ids[criteria[0]])
    assert entry["source"] == "fallback"
    assert entry["met"] is False and entry["evidence_sufficient"] is False


def test_successor_empty_batch_keeps_record_tool_unflagged(tmp_path) -> None:
    captured: dict = {}

    class _CaptureRunner:
        def run(self, req):
            captured["request"] = req
            return {"verdict": "FAIL", "criteria": [], "_usage": {"requests": 0}}

    runner = _CaptureRunner()
    _step(runner)._run_one_successor(
        runner, "T-1", "CTX", "SYSTEM", [], {}, 2, _fresh_bank(tmp_path)
    )
    (record_tool,) = captured["request"].extra_tools
    assert not hasattr(record_tool, "_rebar_completion_evidence_policy")


def test_runner_agent_step_appends_extra_context_to_ticket_context(monkeypatch) -> None:
    from rebar.llm.prompting import prompts as _prompts
    from rebar.llm.workflow import runs as _runs

    seen: dict = {}

    def _fake_resolve(prompt, variables, **kw):
        seen["ticket_context"] = variables["ticket_context"]
        return ("SYS", "INSTR", None)

    monkeypatch.setattr(_prompts, "resolve_prompt_cached", _fake_resolve)
    monkeypatch.setattr(_prompts, "get_prompt", lambda *a, **k: _DummyPrompt())
    monkeypatch.setattr(_runs, "build_agent_request", lambda *a, **k: object())

    class _CapRunner:
        name = "cap"

        def run(self, req):
            return {"verdict": "PASS"}

    step = _runs.RunnerAgentStep(runner=_CapRunner(), extra_context="\nMANIFEST-XYZ")
    ctx = StepContext(
        run_id="r",
        step_id="s",
        kind="agent",
        step={"prompt": "completion-verifier"},
        inputs={"ticket_id": "T-1", "context": "BASECTX"},
        workflow={},
        target_ticket="T-1",
        repo_root=None,
    )
    step.run(ctx)
    assert seen["ticket_context"] == "BASECTX\nMANIFEST-XYZ"


class _DummyPrompt:
    id = "completion-verifier"
    dimension = "completion"
    text = "body"
