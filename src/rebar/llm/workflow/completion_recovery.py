"""Banked incremental recovery for completion-verifier exhaustion (epic 10ae / story 2948).

The ordinary completion workflow keeps its historical one-call fast path. The PRIMARY run
now carries a ``record_criterion_verdict`` tool (wired through ``RunnerAgentStep``'s
extra-tools seam), so it banks each criterion's verdict incrementally as it works. Only a
typed, non-retryable exhaustion (budget / runaway-loop / token-cap) enters recovery.

Recovery no longer fans out one isolated evidence run PER criterion (that design exploded
token/wall-clock cost and discarded the primary's verified work). Instead it resumes with
BATCHED successor runs over only the UNVERIFIED remainder, denominated in a MODEL-REQUEST
budget pool, then finalizes a FULL-COVERAGE verdict from the bank — with a deterministic
no-LLM fallback so a run with any banked progress can never die verdict-less. The mechanical
pieces (bank store, id minting, pool/batch arithmetic, deterministic finalizer) live in
``completion_banking``, the criteria contract (enumeration, admission bounds, coverage) in
``completion_criteria``, and the failure taxonomy (exhaustion classification, bounded
diagnostics, workflow-failure finalization) in ``completion_failures``; this module owns
orchestration only. The normal workflow still owns
child precheck and deterministic reconciliation, so recovery cannot bypass either.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from rebar.llm import failure as _failure
from rebar.llm.completion_tool_policy import make_completion_record_tool
from rebar.llm.config import LLMConfig
from rebar.llm.errors import (
    CompletionRecoveryError,
    LLMBudgetExhaustedError,
    LLMError,
    LLMRunnerError,
    LLMUnavailableError,
    RunawayToolLoopError,
    UnretryableOutputError,
)
from rebar.llm.prompting import prompts
from rebar.llm.runner import Runner, RunRequest, get_runner
from rebar.llm.structured_run import SingleTurnBounds

from . import completion_banking as _bank
from . import completion_verdict_cache as _cache
from . import executor as _ex
from .completion_banking import _banked_evidence_payload
from .completion_criteria import (
    _validate_coverage,
    _validate_recovery_inputs,
    explicit_completion_criteria,
    physical_context_ceiling,
)
from .completion_failures import (
    _bounded_diagnostic,
    _is_token_exhaustion,
    raise_completion_workflow_failure,
)
from .runs import RunnerAgentStep

# Finalizer bounds stay with the orchestrator: they size ITS finalizer call, not the criteria
# contract (completion_criteria owns the per-run admission bounds).
_MAX_FINALIZER_INPUT_CHARS = 132_000
_FINALIZER_OUTPUT_TOKENS = 8_000

logger = logging.getLogger(__name__)


class CompletionAgentStep(_ex.AgentStepRunner):
    """Keep the one-call fast path; on typed exhaustion, resume with banked successors.

    The primary run carries the ``record_criterion_verdict`` tool (via ``RunnerAgentStep``'s
    extra-tools seam), so it banks verdicts incrementally. On PRIMARY SUCCESS the final
    structured output is authoritative and the bank is discarded unread. Only a typed failure
    consults the bank and drives batched successor recovery + finalization.
    """

    def __init__(
        self,
        *,
        runner: Runner | None,
        repo_root: str | None,
        config: LLMConfig,
        verify_ref: str | None = None,
    ) -> None:
        self._runner_override = runner
        self._repo_root = repo_root
        self._config = config
        self._ref = verify_ref  # the gate handle's pinned verification sha (None → HEAD)
        self.failure_diagnostic: dict[str, Any] | None = None

    def run(self, ctx: _ex.StepContext) -> _ex.StepResult:
        ticket_id = str(ctx.inputs.get("ticket_id") or ctx.target_ticket or "")
        stamps = _bank.resolve_bank_stamps(ticket_id, ctx.repo_root)
        bank = _bank.CriterionBank.for_run(ctx.run_id, stamps, repo_root=ctx.repo_root)
        primary_manifest, criterion_ids = self._primary_manifest_contract(ctx, ticket_id, bank)
        record_tool = make_completion_record_tool(bank, criterion_ids)
        primary = RunnerAgentStep(
            runner=self._runner_override,
            repo_root=self._repo_root,
            config=self._config,
            extra_tools=[record_tool],
            extra_context=primary_manifest,
        )
        try:
            result = primary.run(ctx)
        except LLMBudgetExhaustedError as primary_exc:
            return self._recover(ctx, primary_exc, bank)
        except RunawayToolLoopError as primary_exc:
            # The loop breaker (bug c827) aborted a repeating primary run mid-flight — the
            # same recovery contract as a budget stop: resume the unverified remainder.
            return self._recover(ctx, primary_exc, bank)
        except UnretryableOutputError as primary_exc:
            if not _is_token_exhaustion(primary_exc):
                raise
            return self._recover(ctx, primary_exc, bank)
        except LLMUnavailableError as primary_exc:
            # A retryable provider outage mid-run cannot RESUME here (successor/finalizer LLM
            # calls would only re-fail under the same outage). Its disposition must ride
            # forward so the close gate maps a retryable outage → exit 11 instead of a
            # fail-closed exit 1. Mirror the plan-review degrade precedent: read the
            # disposition off the caught error and stamp it onto failure_diagnostic. Only
            # stamp when a real disposition is present.
            outcome = _failure.outcome_of(primary_exc)
            if outcome is not None:
                self.failure_diagnostic = {
                    **(self.failure_diagnostic or {}),
                    **_failure.resolution_fields(outcome),
                }
            # Bank-only degrade (ticket b39a): if the primary already banked an
            # operator-actionable BLOCK (a genuine refutation on an evaluated criterion),
            # finalize a deterministic BLOCK from the bank with NO further LLM calls, so the
            # operator gets the available feedback rather than a verdict-less "could not run".
            # An empty bank — or one holding only PASSes / insufficiency placeholders — has
            # nothing actionable, so it re-raises and keeps the retryable exit-11 posture.
            degraded = self._degrade_from_actionable_bank(ctx, ticket_id, bank)
            if degraded is not None:
                return degraded
            raise
        # Primary SUCCESS: the structured output is authoritative; the bank is discarded unread.
        bank.discard()
        # Stamp the provenance keys the reconcile step wires unconditionally (referencing a
        # missing step output raises): a successful primary is the authoritative, certifiable
        # verdict. The banking/deterministic paths set these themselves.
        if isinstance(result.outputs, dict):
            result.outputs.setdefault("finalizer", "primary")
            result.outputs.setdefault("certifiable", True)
        return result

    # ── successor recovery ────────────────────────────────────────────────────────────
    def _degrade_from_actionable_bank(
        self, ctx: _ex.StepContext, ticket_id: str, bank: _bank.CriterionBank
    ) -> _ex.StepResult | None:
        """Finalize a deterministic BLOCK from the bank on a provider outage, or None.

        Returns a ``StepResult`` carrying a full-coverage deterministic verdict (assembled with
        NO further LLM calls) ONLY when the bank already holds an operator-actionable BLOCK — a
        genuine banked refutation on an evaluated criterion. That verdict is ``verdict=FAIL``,
        ``finalizer=deterministic_fallback``, ``certifiable=False``: it flows to the close
        gate's non-PASS branch, which does NOT close the ticket and surfaces the banked
        findings (an operator decision). Returns None when there is nothing actionable to
        surface (empty bank, or only PASSes / insufficiency placeholders) — the caller then
        re-raises the outage, preserving the retryable exit-11 posture.

        The whole decision is guarded: any fault (a bank read error, an unreadable ticket)
        logs at WARN and returns None so the caller re-raises the outage — the degrade is an
        enhancement, never a new failure mode on the outage path.
        """
        try:
            entries = bank.all()
            if not _bank.bank_has_actionable_block(entries):
                return None
            expected, id_by_text = self._expected_criteria(ctx, ticket_id)
            if not expected:
                return None
            verdict = _bank.assemble_deterministic_verdict(
                ticket_id,
                expected,
                entries,
                id_by_text=id_by_text,
                runner=getattr(self._runner_override, "name", None) or "deterministic_fallback",
                model=self._config.model,
            )
            logger.warning(
                "completion gate degraded to a deterministic BLOCK from the bank under a "
                "provider outage: %d of %d criteria banked, verdict=%s certifiable=%s "
                "(operator decision required — not auto-closed)",
                len(entries),
                len(expected),
                verdict.get("verdict"),
                verdict.get("certifiable"),
            )
            bank.discard()
            return _ex.StepResult(outputs=verdict)
        except Exception:  # best-effort: any fault falls through to the caller's re-raise (exit-11)
            logger.warning(
                "completion gate degrade attempt was suppressed by an internal fault; "
                "falling through to the retryable outage path (exit 11)",
                exc_info=True,
            )
            return None

    def _expected_criteria(
        self, ctx: _ex.StepContext, ticket_id: str
    ) -> tuple[list[str], dict[str, str]]:
        """The ticket's expected completion criteria and their id map (a repo read, no LLM).

        Empty ``([], {})`` when the ticket has no explicit criteria or the read fails — the
        caller then declines to degrade."""
        from rebar import _reads

        ticket = _reads.show_ticket(ticket_id, repo_root=ctx.repo_root)
        expected = explicit_completion_criteria(ticket)
        if not expected:
            return [], {}
        return expected, _bank.criterion_id_map(expected)

    def _primary_manifest(self, ctx: _ex.StepContext, ticket_id: str) -> str:
        """The primary run's criterion-id manifest, or "" (fail-open).

        The primary carries the `record_criterion_verdict` tool but the base context lists the
        criteria without ids; this appends an id manifest so the model can bank as it goes
        (story 2948 dogfood fix). Any read/parse failure returns "" so the primary runs
        exactly as before — the manifest is an enhancement, never a new failure mode on the
        healthy path."""
        return self._primary_manifest_contract(ctx, ticket_id)[0]

    def _primary_manifest_contract(
        self, ctx: _ex.StepContext, ticket_id: str, bank: _bank.CriterionBank | None = None
    ) -> tuple[str, tuple[str, ...]]:
        """Return the primary's data-only manifest and its ordered criterion ids.

        With a ``bank``, still-valid cross-run cached PASS verdicts are seeded into it first
        (stamped ``seeded: true``, ticket 8d74); seeded ids are omitted from the manifest and
        an "already credited — do not re-verify" directive is appended for them."""
        try:
            from rebar import _reads

            ticket = _reads.show_ticket(ticket_id, repo_root=ctx.repo_root)
            expected = explicit_completion_criteria(ticket)
            if not expected:
                return "", ()
            id_by_text = _bank.criterion_id_map(expected)
            seeded: frozenset[str] = frozenset()
            if bank is not None:
                seeded = _cache.seed_bank_from_cache(
                    bank, ticket_id, ticket, expected, id_by_text, ctx.repo_root, ref=self._ref
                )
            manifest = _bank.primary_criteria_manifest(expected, id_by_text, seeded_ids=seeded)
            manifest += _cache.seeded_context_block(
                [text for text in expected if id_by_text[text] in seeded], id_by_text
            )
            return manifest, tuple(id_by_text[text] for text in expected)
        except Exception as exc:  # noqa: BLE001 -- non-ticket manifest faults remain best-effort
            from rebar._engine_support.reads import reraise_pinned_read_failure

            reraise_pinned_read_failure(exc)
            return "", ()

    def _recover(
        self,
        ctx: _ex.StepContext,
        primary_exc: LLMRunnerError,
        bank: _bank.CriterionBank,
    ) -> _ex.StepResult:
        from rebar import _reads
        from rebar._errors import RebarError

        ticket_id = str(ctx.inputs.get("ticket_id") or ctx.target_ticket or "")
        expected: list[str] = []
        recovery_started = False
        try:
            # The prelude reads are INSIDE the diagnostic-capturing try (bug 215f): a
            # RebarError here must land in the stage="preflight" arm, preserving the primary
            # run's diagnostic and emitting the sidecar — not escape raw and discard both.
            ticket = _reads.show_ticket(ticket_id, repo_root=ctx.repo_root)
            runner = get_runner(self._config, override=self._runner_override)
            ticket_context = str(
                ctx.inputs.get("context") or ctx.inputs.get("ticket_context") or ""
            )
            expected = explicit_completion_criteria(ticket)
            _validate_recovery_inputs(expected, ticket_context, self._config.model)
            recovery_started = True
            id_by_text = _bank.criterion_id_map(expected)
            # Observability: how many criteria the PRIMARY banked before it exhausted (the
            # bank holds only its verdicts at this handoff, before any successor runs). A run
            # that banked >0 here proves incremental banking preserved primary progress.
            logger.info(
                "completion recovery: primary banked %d of %d criteria before exhaustion",
                len(bank.banked_ids()),
                len(expected),
            )

            self._run_successors(
                runner, ticket_id, ticket_context, expected, id_by_text, bank, primary_exc
            )

            entries = bank.all()
            if not entries:
                # The ONE remaining verdict-less state — ZERO banked verdicts total. An
                # all-placeholder FAIL would conflate not-done with not-verified on a signed
                # gate, so this stays a typed error + gate_error sidecar (deliberate).
                raise CompletionRecoveryError(
                    "completion recovery banked no verdicts before exhausting its budget; "
                    "zero progress signals a pathology needing diagnosis. Inspect the "
                    "gate_error_v1 diagnostic and retry after addressing the reported stage.",
                    diagnostic={"criteria_total": len(expected), "criteria_completed": 0},
                )
            verdict = self._finalize(runner, ticket_id, expected, bank, id_by_text)
            bank.discard()
            return _ex.StepResult(outputs=verdict)
        except (LLMError, RebarError) as recovery_exc:
            stage = "preflight" if not recovery_started else "successor"
            diagnostic = _bounded_diagnostic(
                recovery_exc, stage=stage, total=len(expected), completed=len(bank.banked_ids())
            )
            primary_diag = _bounded_diagnostic(
                primary_exc, stage="aggregate", total=len(expected), completed=0
            )
            for key, value in primary_diag.items():
                diagnostic[f"aggregate_{key}"] = value
            self.failure_diagnostic = diagnostic
            if isinstance(recovery_exc, CompletionRecoveryError):
                message = str(recovery_exc)
            elif not recovery_started:
                message = (
                    f"completion recovery preflight failed before any successor run: "
                    f"{recovery_exc}. The primary failure's diagnostic is preserved in "
                    "the gate_error_v1 record."
                )
            else:
                message = (
                    "completion verifier exhausted its budget and banked successor recovery "
                    f"also failed at the {stage} stage; inspect the gate_error_v1 diagnostic "
                    "and retry after addressing the reported stage."
                )
            raise CompletionRecoveryError(message, diagnostic=diagnostic) from recovery_exc

    def _run_successors(
        self,
        runner: Any,
        ticket_id: str,
        ticket_context: str,
        expected: list[str],
        id_by_text: dict[str, str],
        bank: _bank.CriterionBank,
        primary_exc: LLMRunnerError,
    ) -> None:
        """Batched successor runs over the UNVERIFIED remainder, budgeted in model requests.

        Each iteration re-plans from the LIVE remainder and the pool remaining after actual
        spend (later runs inherit unspent budget). The batch-aware no-launch guard shrinks a
        batch that cannot meet its ``B ≥ 2 × batch_size`` launch floor; when even a
        1-criterion batch cannot launch, or a run banks nothing new (zero-progress breaker),
        the loop stops and the caller finalizes from the bank.
        """
        verify_cfg = _bank.load_verify_cfg(self._repo_root)
        primary_spent = int(getattr(primary_exc, "diagnostic", {}).get("requests") or 0)
        pool = _bank.plan_recovery_pool(
            len(expected),
            primary_spent,
            verify_cfg,
            direct_children=_cache.direct_child_count(ticket_id, self._repo_root),
        )
        pool_remaining = pool["successor_pool"]
        batch_cap = _bank.successor_batch_cap(self._config.model)
        system_prompt = _bank.successor_system_prompt(self._repo_root)
        while True:
            remainder = [text for text in expected if id_by_text[text] not in bank.banked_ids()]
            if not remainder:
                return
            batch_size, budget = _bank.allocate_batch(len(remainder), batch_cap, pool_remaining)
            if batch_size < 1 or budget < 2:
                return  # cannot launch even a 1-criterion batch → finalize from the bank
            # Re-resolve the provenance stamps and fail loud on drift before resuming.
            bank.preflight(_bank.resolve_bank_stamps(ticket_id, self._repo_root))
            batch = remainder[:batch_size]
            before = bank.banked_ids()
            result, spent = self._run_one_successor(
                runner, ticket_id, ticket_context, system_prompt, batch, id_by_text, budget, bank
            )
            if isinstance(result, dict):
                _bank.harvest_structured_into_bank(bank, result, id_by_text)
            pool_remaining = max(0, pool_remaining - min(spent, budget))
            if not (bank.banked_ids() - before):
                return  # zero-progress breaker: bank did not grow → finalize from the bank

    def _run_one_successor(
        self,
        runner: Any,
        ticket_id: str,
        ticket_context: str,
        system_prompt: str,
        batch: list[str],
        id_by_text: dict[str, str],
        budget: int,
        bank: _bank.CriterionBank,
    ) -> tuple[dict[str, Any] | None, int]:
        """Run ONE batched successor; return (structured result or None, requests spent).

        A typed exhaustion (budget/runaway/token-cap) is CAUGHT — the run keeps whatever it
        banked via the tool; only still-unbanked criteria count as no-progress. A non-token
        UnretryableOutputError propagates (a genuine unretryable defect)."""
        request = RunRequest(
            system_prompt=system_prompt,
            instructions=_bank.successor_instructions(
                ticket_id, ticket_context, batch, id_by_text, bank.all()
            ),
            config=self._config,
            reviewers=["completion-verifier"],
            target={"kind": "ticket", "ticket_ids": [ticket_id]},
            mode="structured",
            output_schema="completion_verdict",
            execution_mode="agentic",
            iteration_limit=_bank.iteration_limit_for(budget),
            extra_tools=[
                make_completion_record_tool(bank, tuple(id_by_text[text] for text in batch))
            ],
        )
        try:
            result = runner.run(request)
        except (LLMBudgetExhaustedError, RunawayToolLoopError) as exc:
            return None, int(getattr(exc, "diagnostic", {}).get("requests") or budget)
        except UnretryableOutputError as exc:
            if not _is_token_exhaustion(exc):
                raise
            return None, int(getattr(exc, "diagnostic", {}).get("requests") or budget)
        usage = result.get("_usage") if isinstance(result, dict) else None
        spent = int((usage or {}).get("requests") or 0)
        return (result if isinstance(result, dict) else None), spent

    # ── finalization ──────────────────────────────────────────────────────────────────
    def _finalize(
        self,
        runner: Any,
        ticket_id: str,
        expected: list[str],
        bank: _bank.CriterionBank,
        id_by_text: dict[str, str],
    ) -> dict[str, Any]:
        """Finalize a FULL-COVERAGE verdict from the bank.

        The tool-free LLM finalizer runs, retried once on any failure (incl. a coverage-
        validation miss). If it fails twice, the verdict is assembled DETERMINISTICALLY from
        the bank with no model call — stamped ``finalizer=deterministic_fallback`` and
        ``certifiable=False`` so the signing path withholds a certified signature. Either way
        the result is full coverage: banked criteria scored from banked evidence, unbanked
        criteria as met=false unverified placeholders."""
        entries = bank.all()
        for _attempt in range(2):
            try:
                result = self._run_finalizer(runner, ticket_id, expected, entries)
                merged = _bank.merge_finalizer_with_bank(
                    result, expected, entries, id_by_text=id_by_text
                )
                _validate_coverage(merged, expected, id_by_text)
                _cache.persist_pass_verdicts(
                    ticket_id, merged, entries, self._repo_root, ref=self._ref
                )
                return merged
            except (LLMError, ValueError):
                continue
        return _bank.assemble_deterministic_verdict(
            ticket_id,
            expected,
            entries,
            id_by_text=id_by_text,
            runner=getattr(self._runner_override, "name", None) or "deterministic_fallback",
            model=self._config.model,
        )

    def _run_finalizer(
        self,
        runner: Any,
        ticket_id: str,
        expected: list[str],
        entries: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        finalizer = prompts.get_prompt("completion-verifier-finalizer", repo_root=self._repo_root)
        banked_evidence = _banked_evidence_payload(entries)
        finalizer_instructions = json.dumps(
            {
                "ticket_id": ticket_id,
                "expected_criteria": expected,
                "criterion_ids": _bank.criterion_id_map(expected),
                "banked_evidence": banked_evidence,
            },
            ensure_ascii=False,
        )
        finalizer_input_chars = len(finalizer.text) + len(finalizer_instructions)
        if finalizer_input_chars > _MAX_FINALIZER_INPUT_CHARS:
            raise CompletionRecoveryError(
                "completion recovery finalizer input bound exceeded",
                diagnostic={
                    "finalizer_input_chars": finalizer_input_chars,
                    "finalizer_input_char_limit": _MAX_FINALIZER_INPUT_CHARS,
                    "criteria_completed": len(entries),
                },
            )
        return runner.run(
            RunRequest.for_structured(
                system_prompt=finalizer.text,
                instructions=finalizer_instructions,
                config=self._config,
                reviewers=[finalizer.id],
                target={"kind": "ticket", "ticket_ids": [ticket_id]},
                output_schema="completion_verdict",
                bounds=SingleTurnBounds(
                    output_tokens=_FINALIZER_OUTPUT_TOKENS,
                    timeout_s=None,
                    structured_retries=None,
                    transport_attempts=None,
                ),
            )
        )


__all__ = [
    "CompletionAgentStep",
    "explicit_completion_criteria",
    "physical_context_ceiling",
    "raise_completion_workflow_failure",
]
