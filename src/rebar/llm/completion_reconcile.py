"""Verdict reconciliation for the completion verifier: normalization + remediation.

The agent emits the verdict; this module owns the deterministic guardrails every
``completion_verdict`` passes through. :func:`reconcile_verdict` normalizes the verdict IN
PLACE and enforces the FAIL⇔findings invariant (including the "no verdict obtainable" fault
marker of bug 2a6f and the insufficiency-only remediation swap), and
:func:`deterministic_child_failure` builds the no-LLM FAIL verdict from deterministic blocking
findings (unclosed children, the epic caused_by floor, oversize descriptions) in the same
shape as an agentic verdict so callers treat both uniformly.

Consumed through the stable :mod:`rebar.llm.completion` seam by the workflow gate ops
(``rebar.llm.workflow.gate_ops``) and by ``rebar.llm.workflow.completion_verdict_assembly``'s
callers; the deterministic child findings it consumes come from
:mod:`rebar.llm.completion_child_gate`.
"""

from __future__ import annotations

from rebar.llm import findings

_REVIEWER_ID = "completion-verifier"
_OUTPUT_SCHEMA = "completion_verdict"

# Generic remediation guidance carried on EVERY FAIL verdict (attached in reconcile_verdict, the
# one chokepoint both the agentic and deterministic child-closure verdicts pass through). It
# points callers at the evidence path defined by the criterion kind: repository proof for the
# codebase-verifiable default, or a concrete ticket attestation for an exactly tagged
# non-codebase criterion. Kept deliberately generic and focused on completing or documenting
# the work rather than bypassing the gate.
COMPLETION_REMEDIATION_GUIDANCE = (
    "How to resolve the unmet criteria: use the evidence path that matches each one. For "
    "codebase-verifiable work, complete any unfinished work and make its proof discoverable "
    "in the repository. For evidence that inherently lives outside the repository, mark the "
    "criterion with the exact `[non-codebase]` tag and add a comment to this ticket that "
    "documents the concrete artifacts that meet it (commands and their output, links, or the "
    "reasoning that ties the evidence to the criterion). The completion verifier reads this "
    "ticket's comments, so properly tagged evidence you record there is taken into account on "
    "the next verification. An untagged external criterion cannot be satisfied by a ticket "
    "comment alone. Then re-verify. Note that a finding reporting a ticket record as absent "
    "means it was NOT VISIBLE IN THE TICKET SNAPSHOT THE VERIFIER READ — that snapshot is "
    "pinned when the run starts, so a record written after the pin, or not yet committed to "
    "the store, reads as missing even though it exists; re-verify after the write lands "
    "rather than re-recording evidence you already wrote."
)

# Remediation carried instead of the generic guidance when a FAIL is INSUFFICIENT EVIDENCE
# only — every unmet criterion carries the framework-set `evidence_sufficient: false` marker
# (the bounded evidence search was exhausted), so nothing was positively refuted. The honest
# next move is surfacing evidence, not "completing unfinished work".
INSUFFICIENT_EVIDENCE_REMEDIATION = (
    "How to resolve the insufficient-evidence criteria: the bounded evidence search was "
    "exhausted before evidence demonstrating these criteria was found — this is search "
    "exhaustion, not refutation; nothing was positively refuted. Make the evidence cheaply "
    "discoverable: add an UNTAGGED comment to this ticket citing the exact test function "
    "names, file paths, and merge SHAs that prove each criterion, then re-verify — the "
    "completion verifier reads this ticket's comments, so recorded evidence is taken into "
    "account on the next verification. The `[non-codebase]` tag is reserved for evidence "
    "that inherently lives outside the repository: a criterion a test file, a file path, "
    "or a merge SHA can prove IS in the codebase, so it is not non-codebase."
)
# Bounded completion verification wants a DECISIVE model, not a maximally-thorough one: the
# framework default (opus) over-explores — it rabbit-holes on confirming code is "wired",
# blowing the step budget even on a 2-criterion ticket (it tripped recursion_limit=300 / 385s
# in testing) — whereas sonnet converges in ~12s. So default the verifier to sonnet (matching
# the DSO completion-verifier's `model: sonnet`). An operator who EXPLICITLY sets a


NO_VERDICT_CRITERION = "(no verdict obtainable)"


def _is_no_verdict_fault(result: dict, items: list) -> bool:
    """Whether ``result`` is an ALREADY-reconciled "no verdict obtainable" fault (bug 2a6f) —
    i.e. it carries the framework marker AND its findings are exactly the fault finding this
    module synthesizes. Keying on the framework-owned criterion label (not on the marker
    alone) is what stops a model from minting the retryable disposition for itself by
    emitting ``verdict_obtainable`` in its own structured output."""
    return (
        result.get("verdict_obtainable") is False
        and len(items) == 1
        and isinstance(items[0], dict)
        and items[0].get("criterion") == NO_VERDICT_CRITERION
    )


def _findings_from_criteria(criteria) -> list[dict]:
    """Rebuild failure findings from the positive per-criterion manifest (bug 2a6f).

    A verdict may arrive non-PASS with an empty ``findings`` but a populated ``criteria``
    manifest carrying ``met: false`` entries — the failures ARE known, they just were not
    mirrored into the failures-only list. Recovering them names real criteria instead of
    reporting a fault, so this is tried BEFORE the no-verdict-obtainable path. Anything
    malformed yields no findings, which falls through to that path."""
    if not isinstance(criteria, list):
        return []
    out: list[dict] = []
    for record in criteria:
        if not isinstance(record, dict) or record.get("met") is not False:
            continue
        name = str(record.get("criterion") or "").strip()
        if not name:
            continue
        out.append(
            {
                "criterion": name,
                "severity": "high",
                "dimension": "completion",
                "detail": (
                    "recorded as NOT met in the verifier's per-criterion evaluation "
                    "(recovered from the criteria manifest, which the verdict did not mirror "
                    "into its findings)."
                ),
            }
        )
    return out


def _insufficiency_only(result: dict) -> bool:
    """True when a FAIL's unmet criteria are ALL insufficiency records.

    Reads the per-criterion ``evidence_sufficient: false`` markers (framework-set by the
    banking/assembly seams — a model cannot mint them there): at least one unmet record must
    carry the marker and none may be a genuine refutation (met=false without it)."""
    records = [
        r for r in (result.get("criteria") or []) if isinstance(r, dict) and r.get("met") is False
    ]
    return bool(records) and all(r.get("evidence_sufficient") is False for r in records)


def reconcile_verdict(result: dict) -> None:
    """Normalize the verdict and enforce the FAIL⇔findings invariant IN PLACE.

    The agent emits the verdict; this is a deterministic guardrail, NOT a re-judge:
    * normalize ``verdict`` — upper-case; exactly ``PASS`` is PASS, anything else FAIL
      (fail-safe: a garbled verdict never silently passes);
    * ``FAIL`` with no findings → recover the failing criteria from the positive ``criteria``
      manifest when it names any (the contract is FAIL ⇒ ≥1 finding), else record that NO
      verdict was obtainable — see below;
    * ``PASS`` with findings → flip to ``FAIL`` (the prompt defines findings as failures-only,
      so a listed failure must block — keyed on the EXISTENCE of a failure finding, not on
      severity, so it stays consistent with "the agent emits the verdict").

    **"No verdict obtainable" (bug 2a6f).** A FAIL that names no criterion is not evidence the
    work is incomplete — it is the verifier failing to produce a usable answer (a truncated or
    garbled structured turn; ``verdict`` absent entirely also lands here, since anything that is
    not exactly ``PASS`` normalizes to FAIL). Reporting that as an unmet criterion invented a
    requirement the ticket never had and left the caller with no remediation path. It is now
    marked with ``verdict_obtainable=False`` so callers can distinguish a verifier FAULT from a
    judgement. The marker is framework-set and rides ALONGSIDE the ``{PASS, FAIL}`` vocabulary
    rather than adding a third token, so the normalizing fail-safe above, the schema, and every
    existing consumer's blocking behaviour are unchanged: the verdict stays ``FAIL`` and still
    blocks. The decision keys on FINDINGS, not on ``criteria`` — the workflow path populates
    ``result["criteria"]`` before delegating here, so a genuine fault can arrive carrying a
    criteria manifest.
    """
    raw = str(result.get("verdict", "")).strip().upper()
    verdict = "PASS" if raw == "PASS" else "FAIL"
    items = result.get("findings") or []
    if verdict == "PASS" and items:
        verdict = "FAIL"
    if verdict == "FAIL" and not items:
        items = _findings_from_criteria(result.get("criteria"))
        if items:
            # Real, named criteria recovered from the positive manifest — a judgement, not a
            # fault, and a far better diagnostic than the placeholder this used to emit.
            result.pop("verdict_obtainable", None)
        else:
            items = [
                {
                    "criterion": NO_VERDICT_CRITERION,
                    "severity": "high",
                    "dimension": "completion",
                    "detail": (
                        "the completion verifier did not produce a usable verdict: it returned "
                        "a non-PASS result naming no criterion. This is a VERIFIER FAULT, not "
                        "evidence that a criterion is unmet — no criterion was evaluated "
                        "against. Re-run the verification; if it recurs, capture the run's logs."
                    ),
                }
            ]
            result["verdict_obtainable"] = False
    elif not _is_no_verdict_fault(result, items):
        # Clear a stale/undeserved marker — but NOT when this verdict is an
        # already-reconciled fault. `reconcile_verdict` runs a second time on the sidecar
        # path (over an in-place-mutated copy), where `findings` now holds the fault finding
        # this function itself synthesized; popping there would strip the marker from the
        # durable record and the fault would look like a genuine unmet criterion forever
        # after. Recognised by the framework-owned criterion label, so a model cannot mint
        # the marker by supplying it in its own output.
        result.pop("verdict_obtainable", None)
    result["verdict"] = verdict
    result["findings"] = items
    # Coach the caller toward the evidence channel on ANY failure: a criterion that is already
    # met but not visible in the code can be satisfied by DOCUMENTING the evidence as a comment
    # on the ticket (the verifier reads ticket comments). Set here — the single chokepoint both
    # the agentic verdict and the deterministic child-closure verdict pass through — so every FAIL
    # carries it uniformly. A PASS has nothing to remediate, so it never carries the field (and a
    # verdict flipped PASS->... stays consistent: only FAIL gets guidance).
    # The top-level `evidence_sufficient` marker is DERIVED here, never trusted from model
    # output: set iff the FAIL has no genuinely-unmet criterion (met=false WITHOUT the
    # per-criterion marker) and at least one marker-carrying record — pure insufficiency.
    # Such a FAIL carries the insufficient-evidence remediation instead of the generic one.
    if verdict == "FAIL":
        if _insufficiency_only(result):
            result["evidence_sufficient"] = False
            result["remediation"] = INSUFFICIENT_EVIDENCE_REMEDIATION
        else:
            result.pop("evidence_sufficient", None)
            result["remediation"] = COMPLETION_REMEDIATION_GUIDANCE
    else:
        result.pop("evidence_sufficient", None)
        result.pop("remediation", None)


def deterministic_child_failure(
    ticket_id: str, child_findings: list[dict], cfg, *, summary: str | None = None
) -> dict:
    """Build a FAIL ``completion_verdict`` from the deterministic BLOCKING child findings
    (direct children that are not closed) WITHOUT invoking the LLM evaluator.

    Used by the child-closure gate: a parent with an UNCLOSED direct child is incomplete by a
    graph invariant, so there is nothing for the LLM to judge — we return the deterministic
    failure directly (no billable call). (An uncertified-but-closed child does NOT come here — it
    withholds certification, not closure; the LLM still runs on the parent's own criteria.) Shaped
    like a normal verdict (target/reviewers/runner) so callers treat it uniformly;
    ``runner='deterministic'`` records that no model ran. ``summary`` overrides the default
    unclosed-children text — the epic-close caused_by floor (ticket 4b54) reuses this verdict
    shape for its own deterministic block and supplies its own summary."""
    result = {
        "verdict": "FAIL",
        "findings": [
            findings.normalize_finding(f, reviewer_id=_REVIEWER_ID) for f in child_findings
        ],
        "summary": summary
        or (
            f"{len(child_findings)} direct child ticket(s) are not closed — the parent cannot be "
            "complete until they are."
        ),
        "target": {"kind": "ticket", "ticket_ids": [ticket_id]},
        "reviewers": [_REVIEWER_ID],
        "runner": "deterministic",
        "model": None,
        "trace_id": None,
    }
    findings.resolve_citations(result, cfg.repo_path)
    reconcile_verdict(result)  # FAIL⇔findings invariant (already satisfied; defensive)
    return findings.validate_structured(result, _OUTPUT_SCHEMA)
