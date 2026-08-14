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
``completion_banking``; this module owns orchestration only. The normal workflow still owns
child precheck and deterministic reconciliation, so recovery cannot bypass either.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, NoReturn

from rebar.llm.completion_tool_policy import make_completion_record_tool
from rebar.llm.config import LLMConfig
from rebar.llm.errors import (
    CompletionRecoveryError,
    LLMBudgetExhaustedError,
    LLMError,
    LLMRunnerError,
    RunawayToolLoopError,
    UnretryableOutputError,
)
from rebar.llm.prompting import prompts
from rebar.llm.runner import Runner, RunRequest, get_runner

from . import completion_banking as _bank
from . import completion_verdict_cache as _cache
from . import executor as _ex
from .completion_banking import _banked_evidence_payload
from .runs import RunnerAgentStep

_CHECKBOX = re.compile(r"(?m)^\s*-\s*\[[ xX]\]\s*(?P<text>\S.*)$")
_AC_HEADING = re.compile(r"^\s*##\s+acceptance criteria\b", re.IGNORECASE)
_H2_HEADING = re.compile(r"^\s*##(?:\s+|$)")
_MAX_CRITERIA = 32
_MAX_CRITERION_CHARS = 4_000
_MAX_TOTAL_CRITERIA_CHARS = 32_000
# PHYSICAL context ceiling. Each verifier run (primary or successor) must fit ONE model
# window. Derived from the resolved verifier model's OWN window (model_classes.own_window_tokens)
# at a deliberately conservative 2 chars/token (English prose averages ~4), leaving the other
# half of the window for the system prompt, criteria, tool traffic, and output. The old ECONOMIC
# product bound (len(context) × len(criteria)) is RETIRED: story 2948 deleted the per-criterion
# fan-out that re-sent the context once per criterion, so spend no longer scales with the
# criterion count — a successor batches ≤ batch_cap criteria over ONE full-context run.
_CONTEXT_CHARS_PER_TOKEN = 2
_MAX_FINALIZER_INPUT_CHARS = 132_000
_FINALIZER_OUTPUT_TOKENS = 8_000

logger = logging.getLogger(__name__)


def explicit_completion_criteria(ticket: dict[str, Any]) -> list[str]:
    """Return stable, explicit checklist requirements from a ticket.

    Recovery deliberately does not ask an already-exhausted agent to rediscover
    scope. Explicit Markdown checkboxes under canonical ``## Acceptance
    Criteria`` sections are the mechanically enumerable completion surface.
    Bugs also get an independent resolution criterion; other ticket types
    without such checkboxes fail closed.
    """

    description = str(ticket.get("description") or "")
    completion_lines: list[str] = []
    in_completion_section = False
    for line in description.splitlines():
        if _AC_HEADING.match(line):
            in_completion_section = True
            continue
        if _H2_HEADING.match(line):
            in_completion_section = False
            continue
        if in_completion_section:
            completion_lines.append(line)

    criteria: list[str] = []
    seen: set[str] = set()
    for match in _CHECKBOX.finditer("\n".join(completion_lines)):
        text = match.group("text").strip()
        if text and text not in seen:
            seen.add(text)
            criteria.append(text)
    title = str(ticket.get("title") or "ticket").strip()
    ticket_type = ticket.get("ticket_type") or ticket.get("type")
    if str(ticket_type or "").strip().lower() == "bug":
        bug_core = (
            f"Bug '{title}' is actually resolved: the reported defect no longer "
            "reproduces and expected behavior holds."
        )
        if bug_core not in seen:
            criteria.append(bug_core)
    if not criteria:
        raise CompletionRecoveryError(
            "cannot enumerate completion recovery criteria for a non-bug ticket "
            "without explicit acceptance-criteria checkboxes"
        )
    return criteria


def physical_context_ceiling(model: str | None) -> int:
    """The max recovery context in chars: the resolved model's own window * 2 chars/token.

    Story a9dd note: the completion gate now pre-loads the ticket's declared file_impact
    contents + referencing-commit diffs into a ``<prefetched_file_contents>`` section that is
    part of the assembled ``context`` string — so those prefetch bytes are ALREADY counted by
    ``_validate_recovery_inputs``'s ceiling check below. ``gate_ops`` pre-trims that section to
    THIS ceiling via ``completion_prefetch.fit_within_ceiling`` before assembling the context,
    so an oversize prefetch is trimmed at the gate rather than tripping this validator here."""
    from rebar.llm.model_classes import own_window_tokens

    return own_window_tokens(model) * _CONTEXT_CHARS_PER_TOKEN


def _validate_recovery_inputs(criteria: list[str], context: str, model: str | None) -> None:
    """Reject hostile/unbounded recovery work before the first recovery call."""

    if len(criteria) > _MAX_CRITERIA:
        raise CompletionRecoveryError(
            "completion recovery criterion-count bound exceeded",
            diagnostic={
                "criteria_total": len(criteria),
                "criteria_limit": _MAX_CRITERIA,
                "criteria_completed": 0,
            },
        )
    oversized = next(
        (
            (index, len(criterion))
            for index, criterion in enumerate(criteria)
            if len(criterion) > _MAX_CRITERION_CHARS
        ),
        None,
    )
    if oversized is not None:
        index, chars = oversized
        raise CompletionRecoveryError(
            "completion recovery per-criterion character bound exceeded",
            diagnostic={
                "criterion_index": index,
                "criterion_chars": chars,
                "criterion_char_limit": _MAX_CRITERION_CHARS,
                "criteria_completed": 0,
            },
        )
    total_chars = sum(len(criterion) for criterion in criteria)
    if total_chars > _MAX_TOTAL_CRITERIA_CHARS:
        raise CompletionRecoveryError(
            "completion recovery total criterion character bound exceeded",
            diagnostic={
                "criteria_chars": total_chars,
                "criteria_char_limit": _MAX_TOTAL_CRITERIA_CHARS,
                "criteria_completed": 0,
            },
        )
    context_ceiling = physical_context_ceiling(model)
    if len(context) > context_ceiling:
        raise CompletionRecoveryError(
            "completion recovery context bound exceeded",
            diagnostic={
                "context_chars": len(context),
                "context_char_limit": context_ceiling,
                "criteria_completed": 0,
            },
        )


def _normalized_finish_reason(exc: BaseException) -> str:
    """Return allowlisted typed finish metadata, preferring runner diagnostics."""

    outcome = getattr(exc, "outcome", None)
    sources = (
        getattr(exc, "diagnostic", None),
        getattr(outcome, "diagnostic", None),
        outcome,
        getattr(exc, "usage", None),
    )
    for source in sources:
        if isinstance(source, dict):
            value = source.get("finish_reason")
            if isinstance(value, str) and value.strip():
                return re.sub(r"[\s-]+", "_", value.strip().lower())
    value = getattr(exc, "finish_reason", None)
    if isinstance(value, str):
        return re.sub(r"[\s-]+", "_", value.strip().lower())
    return ""


def _is_token_exhaustion(exc: BaseException) -> bool:
    """Classify typed output exhaustion without mistaking generic context text."""

    finish_reason = _normalized_finish_reason(exc)
    if finish_reason in {
        "length",
        "max_tokens",
        "max_output_tokens",
        "context_length_exceeded",
        "context_window_exceeded",
        "context_window_overflow",
        "maximum_context_length_exceeded",
    }:
        return True

    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "finish_reason=length",
            "finish_reason: length",
            "max_tokens",
            "max tokens",
            "token cap",
            "context length exceeded",
            "context_length_exceeded",
            "context window exceeded",
            "context-window exceeded",
            "context window overflow",
            "context-window overflow",
            "maximum context length exceeded",
        )
    )


def _bounded_diagnostic(
    exc: BaseException,
    *,
    stage: str,
    total: int,
    completed: int,
) -> dict[str, Any]:
    """Extract only safe, bounded failure metadata from runner exceptions."""

    diagnostic: dict[str, Any] = {
        "stage": stage,
        "exception_type": type(exc).__name__,
        "recovery_attempted": True,
        "criteria_total": total,
        "criteria_completed": completed,
        "trace_id": None,
        "requests": None,
        "tool_calls": None,
        "input_tokens": None,
        "output_tokens": None,
    }
    allowed = {
        "trace_id",
        "finish_reason",
        "requests",
        "request_count",
        "request_limit",
        "tool_calls",
        "tool_calls_limit",
        "tool_calls_distinct",
        "max_consecutive_repeat",
        "top_repeated_tool_calls",
        "distinct_ratio_window",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "criteria_limit",
        "criterion_index",
        "criterion_chars",
        "criterion_char_limit",
        "criteria_chars",
        "criteria_char_limit",
        "context_chars",
        "context_char_limit",
        "evidence_chars",
        "evidence_char_limit",
        "total_evidence_chars",
        "total_evidence_char_limit",
        "finalizer_input_chars",
        "finalizer_input_char_limit",
        "criteria_unmet",
        "criteria_returned",
        "criteria_exhausted",
        "criteria_completed",
        "coverage_exact",
    }

    def merge(source: object, *, overwrite: bool = False) -> None:
        if not isinstance(source, dict):
            return
        for key, value in source.items():
            if key not in allowed:
                continue
            if key == "top_repeated_tool_calls":
                # The one sanctioned non-scalar: a bounded list of
                # {"signature", "count"} dicts (hashed signatures, no prompt or
                # argument text). Copy it so the diagnostic never aliases the
                # exception's own structure.
                if not isinstance(value, list):
                    continue
                if overwrite or diagnostic.get(key) is None:
                    diagnostic[key] = [
                        dict(item) if isinstance(item, dict) else item for item in value
                    ]
                continue
            if not isinstance(value, (str, int, float, bool)) and value is not None:
                continue
            if overwrite or diagnostic.get(key) is None:
                diagnostic[key] = value

    inherited = getattr(exc, "diagnostic", None)
    merge(inherited, overwrite=True)
    outcome = getattr(exc, "outcome", None)
    outcome_diag = getattr(outcome, "diagnostic", None)
    merge(outcome_diag)
    merge(outcome)
    merge(getattr(exc, "usage", None))
    for key in allowed:
        value = getattr(exc, key, None)
        if isinstance(value, (str, int, float, bool)) and diagnostic.get(key) is None:
            diagnostic[key] = value
    return diagnostic


def _validate_coverage(
    result: dict[str, Any], expected: list[str], id_by_text: dict[str, str]
) -> None:
    """Fail closed unless the verdict covers every expected criterion — by criterion ID,
    ORDER-INSENSITIVE (story 2948).

    The old contract required an ordered full-list equality and so could not accept a
    remainder-scoped or banked-union result; the reworked contract accepts the banked-union-
    successor set as long as the returned criterion IDs, as a SET, cover the expected IDs.
    Retry-on-missing is preserved: a record short of full coverage raises
    ``CompletionRecoveryError`` naming the count so the caller can retry/backfill.
    """
    records = result.get("criteria")
    if not isinstance(records, list):
        raise CompletionRecoveryError(
            "completion recovery finalizer omitted per-criterion coverage",
            diagnostic={"criteria_total": len(expected), "criteria_returned": 0},
        )
    returned_ids: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        cid = record.get("criterion_id")
        if not cid:
            cid = id_by_text.get(str(record.get("criterion") or "").strip())
        if cid:
            returned_ids.add(str(cid))
    expected_ids = {id_by_text[text] for text in expected}
    if not expected_ids.issubset(returned_ids):
        raise CompletionRecoveryError(
            "completion recovery finalizer returned incomplete criterion coverage",
            diagnostic={
                "criteria_total": len(expected),
                "criteria_returned": len(returned_ids),
                "coverage_exact": False,
            },
        )
    if any(not isinstance(record.get("met"), bool) for record in records):
        raise CompletionRecoveryError(
            "completion recovery finalizer returned an untyped criterion decision",
            diagnostic={"criteria_total": len(expected), "coverage_exact": True},
        )
    verdict = str(result.get("verdict") or "").strip().upper()
    if verdict == "PASS" and any(not record["met"] for record in records):
        raise CompletionRecoveryError(
            "completion recovery finalizer returned an unmet criterion with a PASS verdict",
            diagnostic={
                "criteria_total": len(expected),
                "criteria_unmet": sum(not record["met"] for record in records),
                "coverage_exact": True,
            },
        )


def raise_completion_workflow_failure(
    ticket_id: str,
    result: _ex.RunResult,
    failure_diagnostic: dict[str, Any] | None,
    workflow_steps_recorded: int,
    repo_root: str | None,
) -> NoReturn:
    """Finalize a failed completion workflow without widening dispatch policy."""

    diagnostic = dict(failure_diagnostic or {})
    diagnostic.setdefault("workflow_steps_recorded", workflow_steps_recorded)
    diagnostic.setdefault("workflow_status", result.status)
    if failure_diagnostic:
        from rebar.llm import usage_log
        from rebar.llm.gate_error_sidecar import emit_gate_error

        emit_gate_error(
            ticket_id,
            "completion",
            cause=result.error or "completion LLM tier failed",
            evidence_ref="completion-verification/recovery",
            diagnostic=diagnostic,
            repo_root=repo_root,
        )
        message = (
            result.error or "completion verification bounded recovery failed without a verdict"
        )
        # The primary run's repetition summary lands under aggregate_-prefixed
        # keys; format_repetition reads bare names, so project before rendering.
        repetition = {
            key.removeprefix("aggregate_"): value
            for key, value in diagnostic.items()
            if key.startswith("aggregate_")
        }
        if all(
            repetition.get(field) is not None
            for field in (
                "requests",
                "tool_calls",
                "tool_calls_distinct",
                "max_consecutive_repeat",
                "top_repeated_tool_calls",
            )
        ):
            # distinct_ratio_window is None BY DESIGN below REPETITION_WINDOW
            # tool calls; render a placeholder rather than dropping the line.
            if repetition.get("distinct_ratio_window") is None:
                repetition["distinct_ratio_window"] = "n/a(<window)"
            message = f"{message}\n{usage_log.format_repetition(repetition)}"
        raise CompletionRecoveryError(
            message,
            diagnostic=diagnostic,
        )
    raise LLMError(
        "completion verification workflow did not produce a verdict: "
        f"{result.error or 'LLM tier failed'}"
    )


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
        except Exception:  # noqa: BLE001 -- the manifest is a best-effort enhancement; any read/parse failure falls back to the pre-banking primary (never a new failure mode)
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
            RunRequest(
                system_prompt=finalizer.text,
                instructions=finalizer_instructions,
                config=self._config,
                reviewers=[finalizer.id],
                target={"kind": "ticket", "ticket_ids": [ticket_id]},
                mode="structured",
                output_schema="completion_verdict",
                execution_mode="single_turn",
                output_token_limit=_FINALIZER_OUTPUT_TOKENS,
            )
        )


__all__ = [
    "CompletionAgentStep",
    "explicit_completion_criteria",
    "raise_completion_workflow_failure",
]
