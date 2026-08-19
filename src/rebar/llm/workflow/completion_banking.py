"""Run-scoped incremental verdict bank for completion recovery (epic 10ae / story 2948).

The shipped recovery design fanned out one isolated evidence run PER criterion after a
primary-run failure. This module replaces that with BANKED incremental verification: the
primary (and any successor) verifier run carries a ``record_criterion_verdict`` tool that
banks each criterion's verdict the moment its evidence is sufficient, so a run that
verified K of N criteria before exhausting keeps that work. On a typed primary failure the
recovery orchestrator (``completion_recovery``) resumes with BATCHED successor runs over
only the unverified remainder, then finalizes a full-coverage verdict from the bank.

This module owns the mechanical pieces with no LLM in them so they are unit-pinnable:

* ``mint_criterion_id`` — the deterministic criterion identity.
* ``plan_recovery_pool`` / ``successor_batch_cap`` / ``plan_recovery_batches`` — the budget
  arithmetic (denominated in MODEL REQUESTS) and batch planning.
* ``CriterionBank`` — the fail-loud, schema-versioned, idempotent, stamped store plus the
  ``record_criterion_verdict`` tool it hands to a verifier run.
* ``assemble_deterministic_verdict`` — the no-LLM finalizer fallback.

Privacy/fail-loud contracts (inherited from ``completion_recovery``): the bank never stores
prompts or tool arguments; a capped evidence entry declares its own truncation; and any
store read/write error aborts recovery with a typed ``CompletionRecoveryError`` rather than
silently losing a verdict.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rebar.llm.completion import verify_step_floor
from rebar.llm.errors import CompletionRecoveryError

# Verdict assembly lives in its own module (extracted along the rendering-seam call-graph
# cluster); re-exported here so existing callers and the public `__all__` are unchanged.
from rebar.llm.workflow.completion_verdict_assembly import (
    assemble_deterministic_verdict,
    merge_finalizer_with_bank,
    ticket_id_of,
)

if TYPE_CHECKING:
    from rebar._config_schema import VerifyConfig

# The banking-time per-entry evidence cap (chars). Sized net of finalizer prompt overhead
# and criteria text (see the recovery module's _MAX_FINALIZER_INPUT_CHARS budget) so the
# whole bank fits one finalizer window: a single event is bounded by this cap × _MAX_CRITERIA.
EVIDENCE_CAP_CHARS = 3_000

# Schema version stamped on every bank entry so a format change is a loud read failure, not
# a silent misread.
BANK_SCHEMA_VERSION = 1

# Batch caps keyed on the resolved verifier class SLOT (model_classes vocabulary). Research
# ground: FARA-style whole-rubric 12-15 is frontier-only; mid-tier caps at 6-8. An
# unrecognized model falls back to the standard cap.
_BATCH_CAP_BY_SLOT: dict[str, int] = {"frontier": 12, "standard": 8, "trivial": 8}
_DEFAULT_BATCH_CAP = 8

_SUCCESSOR_BANKING_ADDENDUM = """

## Resuming after exhaustion (incremental banking)

You are RESUMING a completion verification that a previous run could not finish within its
budget. Verify the REMAINDER criteria listed in the user message (identified by id), and treat
the already-verified criteria as read-only context.

Run this authoritative state machine while any remainder id remains unbanked. Loop invariant:
every response in this loop contains exactly one tool call.

1. **SELECT** — process the remainder ids in their listed order: choose the first unbanked id as
   the exactly one current unbanked criterion and set its evidence-call count to 0. Evidence
   priority: use applicable prefetched evidence first, followed by other applicable evidence
   already present in the ticket context.
2. **EVIDENCE** — when more evidence is needed and the count is 0, 1, or 2, the response calls
   exactly one repository evidence tool (``read_file``, ``list_directory``, or ``search_files``)
   for the current id. Its result increments the current id's evidence-call count. Reconsider
   the current id after each result; evidence gathered now may be reused for later ids. This
   state permits at most three additional repository evidence-tool calls for the current id.
3. **COMMIT** — when the evidence demonstrates a verdict, commit immediately. Commit boundary:
   at count 3, the next response is commit. That response calls only
   ``record_criterion_verdict(criterion_id, met, evidence)``: bank ``met=true`` when the evidence
   demonstrates the criterion, or bank ``met=false`` with the bounded searches when it does not.
4. **ADVANCE** — advance only after ``record_criterion_verdict`` confirms the write. Its
   confirmation selects the next id and resets the evidence-call count to 0. A later discovery
   may revise a provisional verdict with one overwrite call, then resume the current id and
   count. After every remainder id has a confirmed bank write, emit the structured verdict for
   the remainder (keyed per criterion).
"""


def successor_system_prompt(repo_root: str | None) -> str:
    """The successor verifier's system prompt: the byte-stable cacheable prefix of the
    completion-verifier prompt (everything before the volatile ticket tail) plus the
    incremental-banking addendum. Splitting at the ``<!--volatile-->`` marker keeps the
    static prefix byte-identical across the primary and every successor so the automatic
    prompt cache hits (story 2948 cache note)."""
    from rebar.llm.prompting import prompts

    prompt = prompts.get_prompt("completion-verifier", repo_root=repo_root)
    static = prompt.text.split("<!--volatile-->", 1)[0].rstrip()
    return static + _SUCCESSOR_BANKING_ADDENDUM


def load_verify_cfg(repo_root: str | None) -> Any:
    """Load ``VerifyConfig`` fail-safe (unreadable config → packaged defaults)."""
    from rebar import config as _config
    from rebar._config_schema import VerifyConfig

    try:
        return _config.compose_config(repo_root).verify
    except Exception:  # noqa: BLE001 — config unreadable → packaged defaults
        return VerifyConfig()


def allocate_batch(remainder: int, batch_cap: int, pool_remaining: int) -> tuple[int, int]:
    """The live per-run allocation: ``floor(pool/runs_remaining)`` with the batch-aware
    no-launch shrink. Returns (batch_size, budget_requests); a (0, 0) result means no batch
    can launch. Mirrors :func:`plan_recovery_batches`'s first step for one live run."""
    cap = max(1, batch_cap)
    batch_size = min(cap, remainder)
    runs_remaining = math.ceil(remainder / batch_size) if batch_size else 0
    budget = pool_remaining // runs_remaining if runs_remaining else 0
    while batch_size >= 1 and budget < 2 * batch_size:
        batch_size = budget // 2
        if batch_size < 1:
            break
        runs_remaining = math.ceil(remainder / batch_size)
        budget = pool_remaining // runs_remaining if runs_remaining else 0
    if batch_size < 1 or budget < 2:
        return 0, 0
    return batch_size, budget


def successor_instructions(
    ticket_id: str,
    ticket_context: str,
    batch: list[str],
    id_by_text: dict[str, str],
    banked: dict[str, dict[str, Any]],
) -> str:
    """Assemble a successor's user message: the remainder criteria listed by ID (the only
    ones to verify), banked verdicts as read-only one-line summaries, then the FULL ticket
    context (no elision). The static system prefix is unchanged across runs so the automatic
    prompt cache hits."""
    banked_lines = [
        f"- {cid}: met={bool(entry.get('met'))} (already verified — do NOT re-verify)"
        for cid, entry in sorted(banked.items())
    ]
    remainder_lines = [f"- {id_by_text[text]}: {text}" for text in batch]
    parts = [f"Ticket: {ticket_id}", ""]
    if banked_lines:
        parts.append("Already verified (read-only):")
        parts.extend(banked_lines)
        parts.append("")
    parts.append("Remainder to verify (record each with record_criterion_verdict, by id):")
    parts.extend(remainder_lines)
    parts.append("")
    parts.append("Ticket context:")
    parts.append(ticket_context)
    return "\n".join(parts)


def mint_criterion_id(index: int, text: str) -> str:
    """The deterministic criterion identity: ``c<two-digit index>-<sha256(norm)[:8]>``.

    Normalization collapses runs of whitespace to a single space, strips, and casefolds, so
    the primary run and every successor mint IDENTICAL ids from the same verbatim criterion
    text (``explicit_completion_criteria`` is a deterministic, document-order parse). The id
    keys the bank, the record tool, the successor remainder listing, and coverage validation;
    the emitted ``completion_verdict`` still keys by the verbatim string (schema unchanged).
    """
    norm = re.sub(r"\s+", " ", text).strip().casefold()
    h = hashlib.sha256(norm.encode()).hexdigest()[:8]
    return f"c{index:02d}-{h}"


def criterion_id_map(criteria: list[str]) -> dict[str, str]:
    """Map each criterion's verbatim text to its minted id, in document order."""
    return {text: mint_criterion_id(index, text) for index, text in enumerate(criteria)}


_MANIFEST_TEXT_CAP = 200


def primary_criteria_manifest(
    expected: list[str],
    id_by_text: dict[str, str],
    seeded_ids: frozenset[str] | None = None,
) -> str:
    """The PRIMARY run's criterion-id manifest (story 2948 dogfood fix).

    The primary carries the ``record_criterion_verdict`` tool but — unlike a successor, which
    is handed its remainder listed by id — the base completion-verifier context lists the
    acceptance criteria as prose with NO ids, so the model has no valid ``criterion_id`` to
    bank and banks nothing. This appends a volatile DATA-ONLY id manifest (one line per
    criterion, in document order, each truncated) to the primary's untrusted ticket context;
    the authoritative banking instructions remain in the system prompt. Returns "" when
    there are no criteria (nothing to bank). ``seeded_ids`` — criteria already credited from
    the cross-run PASS-verdict cache (ticket 8d74) — are OMITTED: the primary has no work to
    bank for them."""
    listed = [t for t in expected if not (seeded_ids and id_by_text[t] in seeded_ids)]
    if not listed:
        return ""
    lines = ["", "## Criterion IDs"]
    for text in listed:
        one_line = re.sub(r"\s+", " ", text).strip()
        if len(one_line) > _MANIFEST_TEXT_CAP:
            one_line = one_line[:_MANIFEST_TEXT_CAP] + "…"
        lines.append(f"- {id_by_text[text]}: {one_line}")
    return "\n".join(lines)


def plan_recovery_pool(
    criteria_count: int,
    primary_requests_spent: int,
    verify_cfg: VerifyConfig,
    direct_children: int = 0,
) -> dict:
    """The successor budget pool, denominated in MODEL REQUESTS (story 2948).

    ``N`` is the primary's per-run request budget as ``build_usage_limits`` computes it —
    ``ceil(eff_max_iter / 2)`` — with ``eff_max_iter`` floored at the evidence-surface-scaled
    ``verify_step_floor(c, direct_children=k)`` (lever 1, recalibrated by ticket 8d74;
    ``direct_children`` is a pass-through so both consumers share ONE formula). The global
    recovery pool is ``completion_recovery_pool_multiplier × N`` (default 1.5); the successor
    pool is that minus what the primary already spent (from the typed failure's usage
    diagnostic), so a fully-exhausted primary (spend == N) leaves ``0.5 × N``.

    Pinned as a function of ``(c, k)`` by the oracle: childless c=8 → floor 208 (24×8+16),
    N 104, global 156, exhausted-primary successor 52; childful c=8, k=4 → floor 272
    (24×8+16×4+16), N 136, global 204; clamp-max c=60 → floor 960, N 480, global 720,
    exhausted-primary successor 240.
    """
    floor = verify_step_floor(criteria_count, verify_cfg, direct_children=direct_children)
    n = math.ceil(floor / 2)
    global_pool = round(verify_cfg.completion_recovery_pool_multiplier * n)
    successor_pool = max(0, global_pool - int(primary_requests_spent))
    return {"floor": floor, "N": n, "global_pool": global_pool, "successor_pool": successor_pool}


def resolve_verifier_slot(model: str | None, slots: Any | None = None) -> str:
    """Resolve a verifier model string to its class slot (trivial/standard/frontier).

    Matches the resolved model against the configured class-slot models; an unrecognized
    model falls back to ``standard`` (whose cap is the conservative default). Config read is
    fail-safe — an unreadable config yields ``standard``.
    """
    try:
        from rebar.llm.model_classes import CLASS_NAMES, load_class_slots, resolve_class

        slots = slots if slots is not None else load_class_slots()
        for name in CLASS_NAMES:
            try:
                resolved = resolve_class(name, slots)
            except Exception:  # noqa: BLE001 — a misconfigured slot is skipped, not fatal
                continue
            slot_model = getattr(slots.get(name), "model", None)
            if model and (resolved == model or (slot_model and slot_model in model)):
                return name
    except Exception:  # noqa: BLE001 — config unreadable → conservative standard cap
        return "standard"
    return "standard"


def successor_batch_cap(model: str | None, slots: Any | None = None) -> int:
    """The successor batch cap for a verifier model: frontier→12, standard/trivial→8,
    unrecognized→8 (the standard cap)."""
    return _BATCH_CAP_BY_SLOT.get(resolve_verifier_slot(model, slots), _DEFAULT_BATCH_CAP)


def iteration_limit_for(budget_requests: int) -> int:
    """Convert a per-run REQUEST budget B into a ``RunRequest.iteration_limit``.

    ``build_usage_limits`` derives ``req_limit = ceil(eff_max_iter / 2)``, so a successor
    granted B requests must set ``iteration_limit = 2 × B`` — passing B raw would silently
    halve every successor budget.
    """
    return 2 * int(budget_requests)


@dataclass(frozen=True)
class PlannedBatch:
    """One planned successor run: how many remainder criteria it covers, its request budget,
    and the derived iteration limit (2×budget)."""

    batch_size: int
    budget_requests: int
    iteration_limit: int


def plan_recovery_batches(
    remainder: int, batch_cap: int, successor_pool: int
) -> list[PlannedBatch]:
    """Statically plan the successor runs over ``remainder`` unverified criteria.

    ``runs = ceil(remainder / batch_cap)``, sequential, each ONE batched agentic run (never
    per-criterion). Per-run allocation is ``floor(pool_remaining / runs_remaining)`` so an
    early run cannot drain later ones; later runs inherit unspent budget. The batch-aware
    no-launch guard requires ``B ≥ 2 × batch_size`` (a k-criterion batch needs ≥2k tool
    calls); below it the batch shrinks to ``floor(B/2)`` and the allocation is re-planned
    (batch size strictly decreases, so it terminates). When even a 1-criterion batch cannot
    launch (``B < 2``) planning stops and the caller finalizes from the bank.

    This assumes each run consumes exactly its allocation — the deterministic plan the oracle
    pins. The live orchestrator re-plans after each run using ACTUAL successor spend.
    """
    batches: list[PlannedBatch] = []
    pool = int(successor_pool)
    remaining = int(remainder)
    cap = max(1, int(batch_cap))
    while remaining > 0:
        batch_size = min(cap, remaining)
        runs_remaining = math.ceil(remaining / batch_size)
        budget = pool // runs_remaining if runs_remaining else 0
        # Batch-aware no-launch guard: shrink until the launch floor B ≥ 2×batch_size holds
        # (or the batch collapses below 1). Strictly decreasing batch_size guarantees exit.
        while batch_size >= 1 and budget < 2 * batch_size:
            batch_size = budget // 2
            if batch_size < 1:
                break
            runs_remaining = math.ceil(remaining / batch_size)
            budget = pool // runs_remaining if runs_remaining else 0
        if batch_size < 1 or budget < 2:
            # Even a 1-criterion batch cannot launch: stop and finalize with what is banked.
            break
        batches.append(PlannedBatch(batch_size, budget, iteration_limit_for(budget)))
        pool -= budget
        remaining -= batch_size
    return batches


@dataclass(frozen=True)
class BankStamps:
    """The provenance stamps every bank entry binds: the ticket id, the ticket-material
    fingerprint (the same seam plan-review/completion signing use), and the resolved
    verification tree sha (for a ``--ref`` close, the resolved ref's tree). A successor
    preflight re-resolves all three and fails loud on any mismatch, so a bank can never be
    reused across a material or tree change."""

    ticket_id: str
    material_fingerprint: str | None
    tree_sha: str | None


def resolve_bank_stamps(ticket_id: str, repo_root: str | None) -> BankStamps:
    """Resolve the three provenance stamps for a recovery run (best-effort, fail-safe).

    The material fingerprint and tree sha are resolved through the same seams the signing
    path uses. Either may be ``None`` when it cannot be computed (an unattested/local run, a
    non-git snapshot); a ``None`` stamp is treated as not-comparable, exactly like the
    signing path's material check — the mismatch guard only fires when BOTH sides are real
    and differ.
    """
    material: str | None = None
    try:
        from rebar.llm.plan_review.attest import current_material_fingerprint

        material = current_material_fingerprint(ticket_id, repo_root=repo_root)
    except Exception:  # noqa: BLE001 — fingerprint is best-effort provenance
        material = None
    return BankStamps(
        ticket_id=str(ticket_id),
        material_fingerprint=material,
        tree_sha=_resolve_tree_sha(repo_root),
    )


def _resolve_tree_sha(repo_root: str | None) -> str | None:
    """The committed HEAD tree sha of ``repo_root`` (best-effort; ``None`` off a git tree)."""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"],
            cwd=repo_root or None,
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:  # noqa: BLE001 — no git / no tree → not-comparable stamp
        return None
    sha = out.stdout.strip()
    return sha or None


def _stamp_mismatch(stored: BankStamps, fresh: BankStamps) -> str | None:
    """The first mismatched stamp NAME (ticket/material/tree), or None. A ``None`` on either
    side of material/tree is not-comparable (never a mismatch); ticket id must always match."""
    if stored.ticket_id != fresh.ticket_id:
        return "ticket_id"
    if (
        stored.material_fingerprint is not None
        and fresh.material_fingerprint is not None
        and stored.material_fingerprint != fresh.material_fingerprint
    ):
        return "material_fingerprint"
    if (
        stored.tree_sha is not None
        and fresh.tree_sha is not None
        and stored.tree_sha != fresh.tree_sha
    ):
        return "tree_sha"
    return None


class CriterionBank:
    """A run-scoped, git-ignored, fail-loud store of provisional criterion verdicts.

    Lives under ``.rebar/workflow_runs/<run_id>/bank/`` (run-unique so concurrent closes
    cannot collide; no cross-run reuse). Each verdict is one JSON file keyed by criterion_id;
    an idempotent upsert overwrites a prior provisional entry. Every entry stamps the schema
    version and the three provenance stamps. Any read/write error aborts recovery with a
    typed :class:`CompletionRecoveryError`.
    """

    def __init__(self, run_dir: str | Path, stamps: BankStamps) -> None:
        self._dir = Path(run_dir)
        self._stamps = stamps
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CompletionRecoveryError(
                "completion recovery could not create the verdict bank"
            ) from exc

    @property
    def stamps(self) -> BankStamps:
        return self._stamps

    @classmethod
    def for_run(
        cls, run_id: str, stamps: BankStamps, *, repo_root: str | None = None
    ) -> CriterionBank:
        """Open the bank for ``run_id`` under ``repo_root``'s ``.rebar/workflow_runs``.

        When ``repo_root`` is not supplied, honour ``REBAR_ROOT`` before falling back to the
        cwd so the run-scoped scratch dir never scribbles into the developer/CI checkout
        (        which the suite's repo-leak guard would flag)."""
        from rebar import config as _config

        base = _config.resolve_run_root(repo_root)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(run_id) or "run")
        return cls(base / ".rebar" / "workflow_runs" / safe / "bank", stamps)

    def _path(self, criterion_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", criterion_id)
        return self._dir / f"{safe}.json"

    def upsert(
        self,
        criterion_id: str,
        met: bool,
        evidence: str,
        *,
        source: str = "tool",
        evidence_sufficient: bool | None = None,
        seeded: bool = False,
    ) -> dict[str, Any]:
        """Idempotently record ``criterion_id``'s provisional verdict, overwriting any prior
        entry. Evidence is capped at :data:`EVIDENCE_CAP_CHARS`; a capped entry carries an
        explicit ``truncated=True`` flag. ``evidence_sufficient=False`` marks the entry as
        the bounded fallback's insufficiency record (an evidence gap, not a refutation); the
        default leaves the key absent, so a bare overwrite CLEARS a prior marker. Fail-loud
        on any write error."""
        text = str(evidence or "")
        truncated = len(text) > EVIDENCE_CAP_CHARS
        if truncated:
            text = text[:EVIDENCE_CAP_CHARS]
        entry = {
            "schema_version": BANK_SCHEMA_VERSION,
            "criterion_id": str(criterion_id),
            "met": bool(met),
            "evidence": text,
            "truncated": truncated,
            "source": source,
            "ticket_id": self._stamps.ticket_id,
            "material_fingerprint": self._stamps.material_fingerprint,
            "tree_sha": self._stamps.tree_sha,
        }
        if evidence_sufficient is False:
            entry["evidence_sufficient"] = False
        if seeded:
            # A cross-run cached PASS (ticket 8d74): the merge path credits it verbatim and
            # the finalizer cannot downgrade it. Absent on every same-run entry.
            entry["seeded"] = True
        try:
            self._path(criterion_id).write_text(
                json.dumps(entry, ensure_ascii=False, sort_keys=True), encoding="utf-8"
            )
        except OSError as exc:
            raise CompletionRecoveryError(
                "completion recovery could not write a banked verdict",
                diagnostic={"criterion_banked": False},
            ) from exc
        return entry

    def record_insufficient(self, criterion_id: str, evidence: str) -> dict[str, Any]:
        """Bank the bounded fallback's INSUFFICIENCY record for ``criterion_id``.

        ``met=false`` plus the framework-set ``evidence_sufficient=False`` sibling marker —
        the finite evidence search was exhausted without demonstrating the criterion, which
        is an evidence gap, not a refutation. Framework-only: the model-facing record tool
        never writes this marker, and a later genuine tool refutation (a bare upsert)
        replaces it."""
        return self.upsert(
            criterion_id, False, evidence, source="fallback", evidence_sufficient=False
        )

    def get(self, criterion_id: str) -> dict[str, Any] | None:
        path = self._path(criterion_id)
        if not path.exists():
            return None
        return self._read(path)

    def _read(self, path: Path) -> dict[str, Any]:
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CompletionRecoveryError(
                "completion recovery could not read a banked verdict"
            ) from exc
        if not isinstance(entry, dict) or entry.get("schema_version") != BANK_SCHEMA_VERSION:
            raise CompletionRecoveryError(
                "completion recovery read an unrecognized banked-verdict schema",
                diagnostic={"bank_schema_expected": BANK_SCHEMA_VERSION},
            )
        return entry

    def all(self) -> dict[str, dict[str, Any]]:
        """Every banked entry keyed by criterion_id (fail-loud on a corrupt entry)."""
        out: dict[str, dict[str, Any]] = {}
        try:
            paths = sorted(self._dir.glob("*.json"))
        except OSError as exc:
            raise CompletionRecoveryError(
                "completion recovery could not enumerate the verdict bank"
            ) from exc
        for path in paths:
            entry = self._read(path)
            out[str(entry.get("criterion_id"))] = entry
        return out

    def banked_ids(self) -> set[str]:
        return set(self.all().keys())

    def preflight(self, fresh: BankStamps) -> None:
        """Re-resolve the provenance stamps and fail loud on any mismatch, naming the
        mismatched stamp. Called before EACH successor run so a bank whose ticket/material/
        tree drifted mid-recovery can never be resumed."""
        mismatch = _stamp_mismatch(self._stamps, fresh)
        if mismatch is not None:
            raise CompletionRecoveryError(
                f"completion recovery bank stamp mismatch: {mismatch} changed since the bank "
                "was stamped; refusing to resume a bank over drifted material",
                diagnostic={"bank_stamp_mismatch": mismatch},
            )

    def discard(self) -> None:
        """Remove the bank dir after finalize (the emitted verdict already embeds the
        evidence). Best-effort — a failed cleanup never fails the close."""
        try:
            shutil.rmtree(self._dir, ignore_errors=True)
        except OSError:
            pass

    def make_record_tool(self):
        """Return the ``record_criterion_verdict`` tool bound to this bank.

        Pydantic-ai reads the returned function's signature and docstring; that model-facing
        contract defines the current-id single COMMIT action and third-evidence-call boundary.
        Writes remain bool-only, evidence-capped, provisional, and idempotently revisable.
        """
        bank = self

        def record_criterion_verdict(criterion_id: str, met: bool, evidence: str) -> str:
            """Use this tool for the current criterion id as the response's single COMMIT action.

            Enter COMMIT when existing evidence demonstrates the verdict. Record `met=false`
            ONLY when evidence positively refutes the criterion; when evidence was simply
            not found, record nothing — at the bounded transition the framework itself
            records the evidence gap in the next response after the third repository evidence call.
            A successful confirmation selects the next id.

            Args:
                criterion_id: the criterion's stable id from the manifest or remainder listing.
                met: whether the criterion is demonstrably met (true) or refuted (false).
                evidence: concrete evidence (file paths + line numbers) for the judgment;
                    capped at 3000 characters.

            Re-recording the same criterion_id overwrites the earlier entry. Banked verdicts
            are provisional; on a successful run the final structured output stays authoritative.
            """
            entry = bank.upsert(criterion_id, met, evidence, source="tool")
            flag = " (evidence truncated to 3000 chars)" if entry["truncated"] else ""
            return f"recorded {criterion_id}: met={bool(met)}{flag}"

        return record_criterion_verdict


def _banked_evidence_payload(entries: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """The finalizer's ``banked_evidence`` input rows, sorted by criterion id.

    A bank entry carrying the framework-set ``evidence_sufficient=False`` marker surfaces it
    to the finalizer (so an evidence GAP is presented as insufficiency, not a refutation);
    bare entries omit the key entirely, keeping their prior shape byte-identical."""
    rows: list[dict[str, Any]] = []
    for cid, entry in sorted(entries.items()):
        row: dict[str, Any] = {
            "criterion_id": cid,
            "met": bool(entry.get("met")),
            "evidence": entry.get("evidence") or "",
            "truncated": bool(entry.get("truncated")),
        }
        if entry.get("evidence_sufficient") is False:
            row["evidence_sufficient"] = False
        rows.append(row)
    return rows


def harvest_structured_into_bank(
    bank: CriterionBank, result: dict[str, Any], id_by_text: dict[str, str]
) -> int:
    """Harvest a successor's structured ``criteria`` output into the bank (idempotent upsert).

    Tool-banked and output-harvested entries converge; re-harvest is a no-op overwrite.
    Returns the count of entries written (used by the zero-progress breaker together with
    tool-banking). A record may key by ``criterion_id`` directly or by verbatim ``criterion``
    text mapped through ``id_by_text``. The ``evidence_sufficient`` marker is framework-owned:
    a model-supplied one is IGNORED, and a markerless ``met=false`` overwrite PRESERVES a
    prior entry's marker (a successor echoing a banked insufficiency must not silently
    upgrade it to a refutation) — only ``met=true`` clears it.
    """
    records = result.get("criteria")
    if not isinstance(records, list):
        return 0
    written = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        cid = record.get("criterion_id")
        if not cid:
            cid = id_by_text.get(str(record.get("criterion") or "").strip())
        if not cid or not isinstance(record.get("met"), bool):
            continue
        evidence = record.get("evidence") or record.get("detail") or ""
        citation = record.get("citation")
        if citation and not evidence:
            evidence = json.dumps(citation, ensure_ascii=False, default=str)
        met = bool(record["met"])
        prior = bank.get(str(cid))
        preserve = not met and prior is not None and prior.get("evidence_sufficient") is False
        bank.upsert(
            str(cid),
            met,
            str(evidence),
            source="harvest",
            evidence_sufficient=False if preserve else None,
        )
        written += 1
    return written


__all__ = [
    "BANK_SCHEMA_VERSION",
    "EVIDENCE_CAP_CHARS",
    "BankStamps",
    "CriterionBank",
    "PlannedBatch",
    "allocate_batch",
    "assemble_deterministic_verdict",
    "criterion_id_map",
    "harvest_structured_into_bank",
    "iteration_limit_for",
    "load_verify_cfg",
    "merge_finalizer_with_bank",
    "mint_criterion_id",
    "plan_recovery_batches",
    "plan_recovery_pool",
    "primary_criteria_manifest",
    "resolve_bank_stamps",
    "resolve_verifier_slot",
    "successor_batch_cap",
    "successor_instructions",
    "successor_system_prompt",
    "ticket_id_of",
]
