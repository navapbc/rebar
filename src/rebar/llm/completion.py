"""Completion-verification operation: verify a ticket's completion requirements are met.

``verify_completion(ticket_id)`` runs a tool-using LLM agent (the ``completion-verifier``
reviewer) that checks every completion requirement on a ticket — acceptance/success/close
criteria, definitions of done, and (for bugs) that the bug is resolved — is demonstrably
satisfied by the implementation, and returns a **``completion_verdict``** (``{verdict, findings,
…}``). The agent is read-only: line-numbered repo file tools plus a read-only rebar
``show_ticket`` tool; it never writes, transitions, signs, or closes.

Like the review ops, this owns the **deterministic** parts (assembling the ticket context from
rebar's own reads, resolving the reviewer prompt, picking the runner) and delegates the agent
run to a :class:`~rebar.llm.runner.Runner`. The structured-output **contract** is selected by
``output_schema="completion_verdict"`` (the pluggable-contract seam). The agent emits the
verdict; the operation then deterministically normalizes/reconciles it (the verdict is the
agent's, with a guardrail — see :func:`reconcile_verdict`) and resolves citations against the repo.

Optionality: stdlib-only at import; the agent stack is lazy-imported by the runner. The
pydantic_ai runner provides ``show_ticket`` natively (pai_tools.rebar_tools), so the verifier
needs no injected ticket tool.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from rebar.llm import findings
from rebar.llm.config import VERIFIER_DEFAULT_MODEL, LLMConfig
from rebar.llm.runner import Runner

logger = logging.getLogger(__name__)

# Public seam: these three deterministic helpers are the completion gate's stable API,
# consumed by the workflow gate ops (rebar.llm.workflow.gate_ops). They are exported (not
# leading-underscore privates) so a MANDATORY gate does not depend on another module's
# underscore-privates — a rename here is a visible contract change, not a silent break.
__all__ = [
    "COMPLETION_REMEDIATION_GUIDANCE",
    "child_closure_findings",
    "deterministic_child_failure",
    "reconcile_verdict",
    "verify_completion",
]

_REVIEWER_ID = "completion-verifier"
_OUTPUT_SCHEMA = "completion_verdict"

# Generic remediation guidance carried on EVERY FAIL verdict (attached in reconcile_verdict, the
# one chokepoint both the agentic and the deterministic child-closure verdicts pass through). Its
# job is to point the caller at the intended channel for a requirement that is *already met but
# not discoverable from the code alone*: record the supporting evidence as a comment on the
# ticket. The completion verifier can read a ticket's comments (its read-only show_ticket tool),
# so evidence documented there is taken into account on the next verification. Kept deliberately
# generic — it describes the evidence channel for any unmet criterion, not any single situation —
# and it names only that channel, so the caller's attention lands on documenting evidence (or
# finishing genuinely incomplete work) rather than on any way of bypassing the gate.
COMPLETION_REMEDIATION_GUIDANCE = (
    "How to resolve the unmet criteria: for each one, decide whether the work is genuinely "
    "incomplete or simply undocumented. If a requirement is already satisfied but the proof "
    "is not discoverable from the code alone, add a comment to this ticket that documents the "
    "evidence — cite the concrete artifacts that meet the requirement (file paths and line "
    "ranges, commands and their output, links, or the reasoning that ties the work to the "
    "criterion). The completion verifier reads this ticket's comments, so evidence you record "
    "there is taken into account on the next verification. For any criterion whose work is "
    "genuinely unfinished, complete it, then re-verify."
)
# Bounded completion verification wants a DECISIVE model, not a maximally-thorough one: the
# framework default (opus) over-explores — it rabbit-holes on confirming code is "wired",
# blowing the step budget even on a 2-criterion ticket (it tripped recursion_limit=300 / 385s
# in testing) — whereas sonnet converges in ~12s. So default the verifier to sonnet (matching
# the DSO completion-verifier's `model: sonnet`). An operator who EXPLICITLY sets
# REBAR_LLM_MODEL to a non-default still wins (below). The literal lives in config.py
# (VERIFIER_DEFAULT_MODEL) as the single source shared with the plan-review verifier.
_VERIFIER_DEFAULT_MODEL = VERIFIER_DEFAULT_MODEL
# Completion verification is inherently more tool-heavy than a single-dimension review: it
# must check potentially many criteria, each against several files. The framework review
# default (REBAR_LLM_MAX_STEPS=50 ≈ 25 tool calls) is far too low and trips the recursion cap
# mid-verification (→ a false fail-closed block at the gate). Use a generous verification FLOOR;
# an operator who explicitly sets a HIGHER REBAR_LLM_MAX_STEPS still wins. Very large tickets
# (e.g. a whole framework epic) may still need it raised further, or --force-close. (Doubled
# from 120→240 after a substantive story tripped the cap, then 240→480 after an 11-child
# framework epic tripped 240 at the close gate. Per-run step usage is now logged by the
# runner — `llm call [completion-verifier] … steps=N/limit` — so the next resize can be sized
# from observed headroom rather than guesswork. The verifier also short-circuits tickets with
# nothing to verify, so this floor is the ceiling for genuinely multi-criteria work.)
_VERIFY_MIN_STEPS = 480


def child_closure_findings(ticket_id: str, repo_root) -> tuple[list[dict], list[dict]]:
    """Deterministic child-closure / certification gate — the "epic-level verdict trust" rule.

    Returns ``(blocking, uncertified)`` for a parent's **direct** children (childless tickets yield
    ``([], [])`` — a natural no-op for most tasks/bugs). Checked deterministically (a graph +
    signature invariant, not an LLM judgment): we DO NOT recurse into grandchildren (each child
    owns its own subtree), and we DO NOT re-verify a child's own completion criteria — a child's
    **certified signature IS** the trusted attestation that its criteria were validated at close.

    * **blocking** — a direct child that is NOT closed. The parent is INCOMPLETE (delegated work
      unfinished): the close gate fails fast WITHOUT an LLM call and closure is BLOCKED.
    * **uncertified** — a direct child that is closed but WITHOUT a certified/valid closure (a
      force-closed / reopened / drift-stale child). Its work is done, but its subtree is
      unattested: the parent may CLOSE (subject to its OWN criteria) but cannot be CERTIFIED —
      certification propagates, so an unattested descendant WITHHOLDS the parent's signature.

    **Read-error path (fail-safe on certification).** If enumerating the children itself fails
    (a transient store read error), we can no longer prove the subtree is attested, so we
    WITHHOLD certification rather than forge it: we return ``([], [<marker>])`` — an EMPTY
    ``blocking`` (the parent may still close on its OWN criteria; a read glitch shouldn't block
    a legitimate close) but a NON-EMPTY ``uncertified`` (so ``certifiable`` is ``False`` and the
    parent closes UNSIGNED). Returning ``([], [])`` here (the old behaviour) would have LAUNDERED
    certification — a read failure would have signed the parent as if it were childless. This
    mirrors ``plan_review.attest._attested_delivered``, which fails closed on the same error."""
    import rebar  # verify_signature (not a rebar._reads read) is sourced from the facade
    from rebar import _reads

    try:
        children = _reads.list_tickets(parent=ticket_id, repo_root=repo_root)
    except Exception as exc:  # noqa: BLE001 — WITHHOLD certification on a read error; logged below
        logger.warning(
            "child-closure enumeration failed for %s; withholding certification "
            "(the parent may still close on its own criteria, but UNSIGNED) rather than "
            "forging it from an unread (assumed-empty) child set",
            ticket_id,
            exc_info=True,
        )
        return [], [
            {
                "criterion": f"direct children of {ticket_id} could not be certified",
                "severity": "high",
                "dimension": "completion",
                "detail": (
                    f"could not enumerate the direct children of {ticket_id} to verify their "
                    f"certified closure ({exc}); WITHHOLDING certification — the parent may still "
                    "close on its OWN criteria but is NOT signed, rather than forging "
                    "certification from an unread (assumed-empty) child set. Re-close once the "
                    "store read succeeds to certify."
                ),
                "citations": [
                    {
                        "kind": "source",
                        "description": f"list_tickets(parent={ticket_id}) read error: {exc}",
                    }
                ],
            }
        ]
    blocking: list[dict] = []
    uncertified: list[dict] = []
    for c in children:
        cid = c.get("ticket_id")
        if cid is None:
            continue
        title = (c.get("title") or "")[:50]
        status = c.get("status")
        if status != "closed":
            blocking.append(
                {
                    "criterion": f"direct child {cid} is closed",
                    "severity": "high",
                    "dimension": "completion",
                    "detail": f"child {cid} ('{title}') is '{status}', not closed.",
                    "citations": [
                        {"kind": "source", "description": f"ticket {cid} status={status}"}
                    ],
                }
            )
            continue
        # Verify the child's COMPLETION-VERIFIER attestation specifically (epic
        # dark-acme-lumen) — not the most-recent signature of any kind — then run
        # compute_validity so a reopened/materially-edited closure no longer counts as a
        # validated closure (validity-on-read; records are never mutated).
        try:
            sig = rebar.verify_signature(cid, kind="completion-verifier", repo_root=repo_root)
            if sig.get("verdict") == "certified":
                from rebar.llm.plan_review.attest import compute_validity

                v = compute_validity(sig, c, "completion-verifier", repo_root=repo_root)
                valid, detail = v.get("valid", False), v.get("reason", "")
            else:
                # Carry the REASON, not just the verdict: the verdict alone ("unsigned")
                # tells a reader nothing about what to do next (bug 94a3).
                valid, detail = False, f"signature: {sig.get('verdict')} — {sig.get('reason', '')}"
        except Exception as exc:  # noqa: BLE001 — never let a signature read crash the verification: recorded in-band
            valid, detail = False, f"error: {exc}"
        if not valid:
            uncertified.append(
                {
                    "criterion": f"direct child {cid} has a certified closure",
                    "severity": "high",
                    "dimension": "completion",
                    "detail": (
                        f"child {cid} ('{title}') is closed but its completion closure is not "
                        f"certified/valid ({detail}) — its subtree is unattested, so the parent "
                        "closes WITHOUT certification. Re-close the child through the gate to "
                        "certify (and re-close the parent) if a signed closure is required."
                    ),
                    "citations": [
                        {"kind": "source", "description": f"completion-verifier({cid}): {detail}"}
                    ],
                }
            )
    return blocking, uncertified


# Enforced screen ceiling for the epic-close bug screen (ticket 4b54): at most this many
# candidates are LLM-evaluated per close — linked-to-subtree candidates first, then by created
# timestamp descending — and the remainder is recorded as an unevaluated-overflow count in the
# sidecar tally (visible to the operator, never silently dropped). Backtested fan-out over 56
# epic closes: median 3, p90 11, max 24, so 32 clears every observed close.
EPIC_BUG_SCREEN_CEILING = 32


def _epic_subtree_states(ticket_id: str, repo_root) -> list[dict]:
    """Compiled states of the epic + every descendant (parent links, any depth), BFS."""
    from rebar import _reads

    root = _reads.show_ticket(ticket_id, repo_root=repo_root)
    out: list[dict] = [root]
    seen = {root.get("ticket_id", ticket_id)}
    frontier = [root.get("ticket_id", ticket_id)]
    while frontier:
        next_frontier: list[str] = []
        for pid in frontier:
            for child in _reads.list_tickets(parent=pid, repo_root=repo_root):
                cid = child.get("ticket_id")
                if cid and cid not in seen:
                    seen.add(cid)
                    out.append(child)
                    next_frontier.append(cid)
        frontier = next_frontier
    return out


def _open_bugs(repo_root) -> list[dict]:
    """Every open or in_progress bug in the store (the only tickets the screen may see)."""
    from rebar import _reads

    bugs: list[dict] = []
    for status in ("open", "in_progress"):
        bugs.extend(_reads.list_tickets(status=status, ticket_type="bug", repo_root=repo_root))
    return bugs


def _first_in_progress_ns(ticket_id: str, repo_root) -> int | None:
    """ns-epoch timestamp of the ticket's FIRST ``open -> in_progress`` STATUS event.

    Read WITH retired tombstones (the ``event_metrics._event_files`` idiom,
    ``include_retired=True``) — snapshot compaction folds old events, so the compiled state
    alone cannot supply this anchor. ``None`` when no such event exists (never claimed)."""
    import json as _json
    import os as _os

    from rebar import config as _config
    from rebar.metrics.event_metrics import _event_files

    ticket_dir = _os.path.join(str(_config.tracker_dir(repo_root)), ticket_id)
    if not _os.path.isdir(ticket_dir):
        return None
    for path in _event_files(ticket_dir, "STATUS", include_retired=True):
        try:
            with open(path, encoding="utf-8") as handle:
                event = _json.load(handle)
        except (OSError, ValueError):
            continue
        data = event.get("data") or {}
        if data.get("current_status") == "open" and data.get("status") == "in_progress":
            ts = event.get("timestamp")
            if isinstance(ts, int):
                return ts
    return None


def epic_bug_floor_findings(ticket_id: str, repo_root) -> list[dict]:
    """The deterministic ``caused_by`` floor of the epic-close bug screen (ticket 4b54).

    Any OPEN/IN_PROGRESS bug carrying a ``caused_by`` edge into the epic's subtree (the epic
    or any descendant) deterministically blocks the epic's close, exactly like an unclosed
    direct child — semantically unambiguous (the bug RECORDS this work broke it), 0 false
    positives over the 56-close backtest. ``caused_by`` is DIRECTIONAL and lives on the BUG's
    own compiled ``deps[]`` with no reciprocal on the epic side, so the floor enumerates the
    open bugs and scans EACH BUG's deps — the epic's own deps cannot supply these edges.
    Returns blocking findings shaped exactly like :func:`child_closure_findings`'s."""
    subtree = {
        s.get("ticket_id") for s in _epic_subtree_states(ticket_id, repo_root) if s.get("ticket_id")
    }
    found: list[dict] = []
    for bug in _open_bugs(repo_root):
        bid = bug.get("ticket_id")
        if bid is None or bid in subtree:
            continue  # in-hierarchy bugs belong to the direct-children gate
        for dep in bug.get("deps") or []:
            if dep.get("relation") != "caused_by":
                continue
            target = dep.get("target_id")
            if target not in subtree:
                continue
            title = (bug.get("title") or "")[:50]
            status = bug.get("status")
            found.append(
                {
                    "criterion": f"no open caused_by bug against the subtree of {ticket_id}",
                    "severity": "high",
                    "dimension": "completion",
                    "detail": (
                        f"bug {bid} ('{title}') is '{status}' and records caused_by -> "
                        f"{target} inside this epic's subtree — the epic's own work broke it. "
                        "Before closing the epic: fix (close) the bug, re-parent it under the "
                        "epic as delegated work, or dispute the caused_by link if it is wrong."
                    ),
                    "citations": [
                        {
                            "kind": "source",
                            "description": f"ticket {bid} caused_by {target}; status={status}",
                        }
                    ],
                }
            )
            break  # one finding per bug, however many subtree edges it carries
    return found


def epic_bug_candidates(ticket_id: str, repo_root) -> tuple[list[dict], int]:
    """The deterministic candidate filter of the epic-close bug screen (ticket 4b54).

    Candidates = OPEN/IN_PROGRESS bugs OUTSIDE the epic's subtree that are (a) created after
    the epic's FIRST ``open -> in_progress`` transition (fallback: the epic's ``created_at``
    when it was never claimed — a wider window is the safe direction, over-inclusion only
    feeds the cheap screen), OR (b) linked by ANY relation, EITHER direction, to the epic or
    any descendant. Commit-relation matching was evaluated and REJECTED (0 unique catches,
    2 FPs on the backtest). Returns ``(candidates, unevaluated_overflow)`` — at most
    :data:`EPIC_BUG_SCREEN_CEILING` candidates, linked-to-subtree first, then created
    descending; the remainder is counted, never silently dropped."""
    states = _epic_subtree_states(ticket_id, repo_root)
    subtree = {s.get("ticket_id") for s in states if s.get("ticket_id")}
    incoming: set[str] = set()  # ids that SUBTREE members link out to (epic-side edges)
    for s in states:
        for dep in s.get("deps") or []:
            target = dep.get("target_id")
            if target:
                incoming.add(target)
    anchor = _first_in_progress_ns(ticket_id, repo_root)
    if anchor is None:
        root = states[0] if states else {}
        anchor = root.get("created_at") or 0
    qualifying: list[tuple[bool, int, dict]] = []
    for bug in _open_bugs(repo_root):
        bid = bug.get("ticket_id")
        if bid is None or bid in subtree:
            continue
        linked = bid in incoming or any(
            dep.get("target_id") in subtree for dep in bug.get("deps") or []
        )
        created = bug.get("created_at") or 0
        if linked or created > anchor:
            qualifying.append((linked, created, bug))
    qualifying.sort(key=lambda q: (0 if q[0] else 1, -q[1]))
    kept = [bug for _linked, _created, bug in qualifying[:EPIC_BUG_SCREEN_CEILING]]
    return kept, max(0, len(qualifying) - len(kept))


def reconcile_verdict(result: dict) -> None:
    """Normalize the verdict and enforce the FAIL⇔findings invariant IN PLACE.

    The agent emits the verdict; this is a deterministic guardrail, NOT a re-judge:
    * normalize ``verdict`` — upper-case; exactly ``PASS`` is PASS, anything else FAIL
      (fail-safe: a garbled verdict never silently passes);
    * ``FAIL`` with no findings → synthesize one placeholder finding (the contract is
      FAIL ⇒ ≥1 finding; this is the sloppy-model case the shape-only schema lets reach here);
    * ``PASS`` with findings → flip to ``FAIL`` (the prompt defines findings as failures-only,
      so a listed failure must block — keyed on the EXISTENCE of a failure finding, not on
      severity, so it stays consistent with "the agent emits the verdict").
    """
    raw = str(result.get("verdict", "")).strip().upper()
    verdict = "PASS" if raw == "PASS" else "FAIL"
    items = result.get("findings") or []
    if verdict == "PASS" and items:
        verdict = "FAIL"
    if verdict == "FAIL" and not items:
        items = [
            {
                "criterion": "(unspecified)",
                "severity": "high",
                "dimension": "completion",
                "detail": "verifier returned FAIL without itemizing the failing criterion.",
            }
        ]
    result["verdict"] = verdict
    result["findings"] = items
    # Coach the caller toward the evidence channel on ANY failure: a criterion that is already
    # met but not visible in the code can be satisfied by DOCUMENTING the evidence as a comment
    # on the ticket (the verifier reads ticket comments). Set here — the single chokepoint both
    # the agentic verdict and the deterministic child-closure verdict pass through — so every FAIL
    # carries it uniformly. A PASS has nothing to remediate, so it never carries the field (and a
    # verdict flipped PASS->... stays consistent: only FAIL gets guidance).
    if verdict == "FAIL":
        result["remediation"] = COMPLETION_REMEDIATION_GUIDANCE
    else:
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


def _verifier_model_for_completion() -> str:
    """The completion verifier's model: the STANDARD model class (ticket 172e).

    This file carried its OWN copy of plan-review's equality test
    (``if cfg.model == DEFAULT_MODEL: replace(model=_VERIFIER_DEFAULT_MODEL)``), so the same defect
    lived on a second path: ANY provider-qualified or Bedrock model id read as an explicit operator
    choice and left the completion verifier on the frontier model. Resolving the class keeps the two
    gates in step.

    With nothing configured, ``standard`` resolves to the same model ``_VERIFIER_DEFAULT_MODEL``
    names -- but the returned string is now PROVIDER-QUALIFIED, so this is not byte-identical to the
    old rule. See :func:`rebar.llm.plan_review._verifier_cfg` for why qualifying is the deliberate
    and desirable direction.

    A separate function rather than an inline call so the resolution is unit-testable without
    standing up a whole ``verify_completion`` run.
    """
    from rebar.llm.model_classes import STANDARD_CLASS, resolve_model_string

    return resolve_model_string(STANDARD_CLASS)


def verify_completion(
    ticket_id: str,
    *,
    graph: bool | None = None,
    ref: str | None = None,
    source: str | None = None,
    fetch: bool = True,
    repo_root=None,
    config: LLMConfig | None = None,
    runner: Runner | None = None,
) -> dict:
    """Verify a ticket's completion requirements and return a ``completion_verdict`` dict.

    Args:
        ticket_id: the ticket to verify (id, short id, or alias).
        graph: include the ticket's descendants in the context. Default: ``True`` for an
            epic (its acceptance criteria are met across children), else ``False``.
        repo_root: rebar repo root (defaults to the resolved root).
        config: an :class:`LLMConfig` (defaults to :meth:`LLMConfig.from_env`).
        runner: an explicit runner (test seam; defaults to the config-selected runner).

    Returns a validated ``completion_verdict`` dict ``{verdict: "PASS"|"FAIL", findings[],
    summary?, target, reviewers, runner, model, trace_id}``. On FAIL, ``findings`` is
    non-empty; each finding carries the failing ``criterion``, an explanation (``detail``),
    and ``citations`` resolved against the real repo. Raises :class:`LLMError` subclasses on
    missing deps/credentials or a failed/empty structured run.
    """
    from rebar.llm import gate_source

    handle = gate_source.resolve_gate_handle(ref, source, repo_root, fetch=fetch)
    with gate_source.gate_read_root(handle):
        return gate_source.annotate_result(
            _verify_completion_inner(
                ticket_id,
                graph=graph,
                repo_root=repo_root,
                config=gate_source.apply_handle(
                    config or LLMConfig.from_env(repo_root=repo_root), handle
                ),
                runner=runner,
            ),
            handle,
        )


def _verify_completion_inner(
    ticket_id: str,
    *,
    graph: bool | None,
    repo_root,
    config: LLMConfig,
    runner: Runner | None,
) -> dict:
    from rebar import _reads

    cfg = config
    cfg = replace(cfg, model=_verifier_model_for_completion())
    # Model-max output budget for the PRIMARY verifier call (bug 30a2): applied AFTER the model
    # swap so the raise matches the model that actually runs; only ever raises, so an explicit
    # higher operator REBAR_LLM_MAX_TOKENS still wins.
    from rebar.llm.review_kernel import max_output_cfg

    cfg = max_output_cfg(cfg)
    # Raise the agent step budget to a verification-appropriate floor (an explicit higher
    # REBAR_LLM_MAX_STEPS still wins) so a multi-criteria verification doesn't trip the
    # recursion cap mid-run.
    if cfg.max_iterations < _VERIFY_MIN_STEPS:
        cfg = replace(cfg, max_iterations=_VERIFY_MIN_STEPS)
    # Pin GREEDY decoding for the verifier (bug e458): an unpinned temperature runs at the
    # provider default (~1.0), whose sampling variance flips borderline judgments — e.g. whether
    # the agent's (fallible, free-form) search located a criterion's test — between runs on
    # IDENTICAL input (proven: ad9f FAIL→PASS same-sha). Mirrors the plan-review Pass-2 verifier's
    # greedy pin; an explicit operator REBAR_LLM_TEMPERATURE (cfg.temperature not None) still wins,
    # exactly like the model / step-floor tuning above. This is a variance MITIGATION, not the root
    # fix — the prompt guidance (search by ticket-id/exact-symbols, not regex/semantic phrases)
    # addresses the mechanism directly.
    if cfg.temperature is None:
        cfg = replace(cfg, temperature=0.0)
    # Resolve the ticket type once (one local read; no network). graph default depends on
    # ticket type (epics verify across children).
    root = _reads.show_ticket(ticket_id, repo_root=repo_root)
    if graph is None:
        graph = root.get("ticket_type") == "epic"

    # Verdict PRODUCTION runs through the v3 engine workflow
    # (gates/completion-verification.yaml) — which owns its OWN deterministic child-closure
    # precheck → agentic verify → reconcile — and returns the reconciled completion_verdict.
    # (The child-closure precheck is the workflow's `completion_precheck` op, which reuses
    # `child_closure_findings` / `deterministic_child_failure` from this module, so there is
    # exactly ONE child-closure implementation and no double check.) The close gate's signing
    # wrapper (_commands.transition) is unchanged, so the signed attestation stays
    # byte-compatible. cfg is already tuned (verifier model + step floor) above.
    from rebar.llm.workflow import gate_dispatch

    return gate_dispatch.produce_completion_verdict(
        ticket_id, graph=graph, repo_root=repo_root, cfg=cfg, runner=runner
    )
