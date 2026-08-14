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
# one chokepoint both the agentic and deterministic child-closure verdicts pass through). It
# points callers at the evidence path defined by the criterion kind: repository proof for the
# codebase-verifiable default, or a concrete ticket attestation for an exactly tagged
# operator-attested criterion. Kept deliberately generic and focused on completing or documenting
# the work rather than bypassing the gate.
COMPLETION_REMEDIATION_GUIDANCE = (
    "How to resolve the unmet criteria: use the evidence path that matches each one. For "
    "codebase-verifiable work, complete any unfinished work and make its proof discoverable "
    "in the repository. For evidence that inherently lives outside the repository, mark the "
    "criterion with the exact `[operator-attested]` tag and add a comment to this ticket that "
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
    "account on the next verification. Do NOT tag code-verifiable criteria as "
    "`[operator-attested]`: that tag is reserved for evidence that inherently lives outside "
    "the repository."
)
# Bounded completion verification wants a DECISIVE model, not a maximally-thorough one: the
# framework default (opus) over-explores — it rabbit-holes on confirming code is "wired",
# blowing the step budget even on a 2-criterion ticket (it tripped recursion_limit=300 / 385s
# in testing) — whereas sonnet converges in ~12s. So default the verifier to sonnet (matching
# the DSO completion-verifier's `model: sonnet`). An operator who EXPLICITLY sets a
# non-default `[tool.rebar.llm].model` still wins (below). The literal lives in config.py
# (VERIFIER_DEFAULT_MODEL) as the single source shared with the plan-review verifier.
_VERIFIER_DEFAULT_MODEL = VERIFIER_DEFAULT_MODEL
# Completion verification is inherently more tool-heavy than a single-dimension review: it
# must check potentially many criteria, each against several files. The framework review
# default (REBAR_LLM_MAX_STEPS=50 ≈ 25 tool calls) is far too low and trips the recursion cap
# mid-verification (→ a false fail-closed block at the gate). Historically a FLAT floor of 480
# handled this, but a flat floor MANUFACTURED exhaustion for TYPICAL tickets (epic 10ae/story
# 2948, lever 1): measured, an 8-criteria verify converges at ~32 requests yet spends the whole
# 480-step (240-request) budget on ~77% read_file re-read waste until the runaway guard trips —
# the exhaustion the recovery path then has to bank around. Lever 1 replaces the flat floor with
# a CRITERIA-SCALED one: verify_step_floor(c) = clamp(steps_per_criterion × c, step_floor_min,
# 480). It is AUTHORITATIVE over the framework default (it may LOWER a small ticket below the 250
# default — that is the point), but an operator who explicitly sets a step budget still wins
# (min-only against an explicit budget; the criteria-scaled value only ever RAISES an explicit
# budget that is below it). 480 is retained ONLY as the clamp MAX so no ticket exceeds today's
# ceiling. Per-run step usage is logged by the runner — `llm call [completion-verifier] …
# steps=N/limit` — so a resize can be sized from observed headroom. The verifier also
# short-circuits tickets with nothing to verify.
_VERIFY_STEP_FLOOR_MAX = 480


def verify_step_floor(criteria_count: int, verify_cfg) -> int:
    """The criteria-scaled PRIMARY completion-verifier step floor (epic 10ae/story 2948, lever 1).

    ``clamp(steps_per_criterion × c, step_floor_min, 480)`` where ``c`` is the ticket's explicit
    criteria count. Config-tunable via ``verify.completion_verify_steps_per_criterion`` (default
    8) and ``verify.completion_verify_step_floor_min`` (default 48). ``c`` is floored at 1 so a
    degenerate zero-criteria surface still receives at least ``step_floor_min``.
    """
    per = verify_cfg.completion_verify_steps_per_criterion
    lo = verify_cfg.completion_verify_step_floor_min
    scaled = per * max(int(criteria_count), 1)
    return max(lo, min(scaled, _VERIFY_STEP_FLOOR_MAX))


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
    except Exception as exc:
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


def _uncertified_child_ids(uncertified: list[dict]) -> list[str]:
    """The child ids carried in ``uncertified`` findings from :func:`child_closure_findings`.

    Each per-child finding's ``criterion`` is ``f"direct child {cid} has a certified closure"``;
    the read-error marker (``"direct children of … could not be certified"``) carries no id and is
    skipped — but that path also makes :func:`build_child_closure_evidence` return ``""`` (its own
    enumeration fails identically), so no id is ever needed from it."""
    ids: list[str] = []
    for f in uncertified:
        parts = str(f.get("criterion", "")).split()
        if len(parts) >= 3 and parts[0] == "direct" and parts[1] == "child":
            ids.append(parts[2])
    return ids


def build_child_closure_evidence(ticket_id: str, repo_root, uncertified: list[dict]) -> str:
    """A compact, deterministic child-closure evidence block for the completion verifier (6ec8).

    :func:`child_closure_findings` already proves, deterministically, whether every DIRECT child of
    ``ticket_id`` is closed AND carries a certified completion-verifier signature. That proof is
    used for the gate short-circuit but never reaches the verifier's prompt, so an epic AC like
    "every child story is closed" is judged with ZERO evidence. This surfaces the SAME result as a
    few lines of evidence (counts + ids of any closed-but-uncertified children), so such a criterion
    resolves WITHOUT a tool call.

    Reuses the same child enumeration as :func:`child_closure_findings` so the counts stay
    consistent, and derives the uncertified ids from the passed-in ``uncertified`` (never
    re-derived). Returns
    ``""`` when the ticket has no direct children (a childless ticket gets no block — no noise) or
    when enumeration fails. Callers inject the string into the fenced (untrusted) context; the
    caveat governing how the verifier TREATS it lives in the trusted verifier prompt.

    Note: on the caller's LLM path ``blocking`` is necessarily empty (a non-empty ``blocking``
    short-circuited earlier), so only closure+certification is reported here."""
    from rebar import _reads

    try:
        children = _reads.list_tickets(parent=ticket_id, repo_root=repo_root)
    except Exception:  # noqa: BLE001 — mirror child_closure_findings: an unreadable child set yields no block
        return ""
    total = len(children)
    if total == 0:
        return ""
    unc_ids = _uncertified_child_ids(uncertified)
    lines = [
        "## Deterministic child-closure evidence (computed by the close gate, not the LLM)",
        f"This ticket has {total} direct child ticket(s).",
    ]
    if not unc_ids:
        lines.append(
            f"All {total} direct child ticket(s) are CLOSED and carry a certified "
            "completion-verifier signature (deterministically proven)."
        )
    else:
        lines.append(
            f"{total - len(unc_ids)} of {total} direct child ticket(s) are closed AND certified; "
            f"the following {len(unc_ids)} are closed but NOT certified: {', '.join(unc_ids)}."
        )
    lines.append(
        "NOTE: whether each child's change is 'Verified +1' on Gerrit is NOT observable from "
        "repository tools; only closure and certification are proven above."
    )
    return "\n".join(lines)


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


def _verifier_model_for_completion(repo_root: str | None = None) -> str:
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

    ``repo_root`` is the root the class table is read from — the caller threads ``cfg.repo_path``
    so the verifier's model comes from the SAME root the config resolved against instead of from
    ambient cwd discovery (bug 2876). Left ``None`` it falls back to the active gate root, then
    ambient discovery, exactly as every other class read does.
    """
    from rebar.llm.model_classes import STANDARD_CLASS, resolve_model_string

    return resolve_model_string(STANDARD_CLASS, repo_root)


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
    cfg = replace(cfg, model=_verifier_model_for_completion(cfg.repo_path))
    # Model-max output budget for the PRIMARY verifier call (bug 30a2): applied AFTER the model
    # swap so the raise matches the model that actually runs; only ever raises, so an explicit
    # higher operator REBAR_LLM_MAX_TOKENS still wins.
    from rebar.llm.review_kernel import max_output_cfg

    cfg = max_output_cfg(cfg)
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

    # Criteria-scaled PRIMARY step budget (epic 10ae/story 2948, lever 1). Compute the scaled
    # floor from the ticket's explicit criteria count, then apply it: it is AUTHORITATIVE over the
    # framework default (== DEFAULT_MAX_ITERATIONS means no explicit operator step budget, so the
    # scaled floor becomes the budget even when that LOWERS it — the whole point of lever 1), but
    # min-only against an EXPLICIT operator budget (a different value the operator set is only ever
    # raised up to the floor, never lowered). Config read is fail-safe: an unreadable config falls
    # back to the packaged VerifyConfig defaults so the floor still applies.
    from rebar import config as _config
    from rebar._config_schema import VerifyConfig
    from rebar.llm.config import DEFAULT_MAX_ITERATIONS
    from rebar.llm.workflow.completion_recovery import (
        CompletionRecoveryError,
        explicit_completion_criteria,
    )

    try:
        verify_cfg = _config.load_config(repo_root).verify
    except Exception:  # noqa: BLE001 — config unreadable → packaged defaults, floor still applies
        verify_cfg = VerifyConfig()
    # Lever-1 floor scales with the ticket's explicit checkbox count. A ticket with no
    # enumerable checkboxes (a non-bug without an Acceptance Criteria block) makes
    # `explicit_completion_criteria` fail closed — but that is the VERDICT PRODUCTION path's
    # concern (the agents-extra guard / child-closure precheck in produce_completion_verdict own
    # it), NOT this pre-flight budget sizing. Enumeration must not raise HERE, ahead of the
    # agents guard, or a lean (no-extras) install degrades to CompletionRecoveryError instead of
    # the typed missing-extra LLMError (regression caught by the degradation-path gate). Fall
    # back to a zero criteria count (base floor) and let the downstream path decide.
    try:
        criteria_count = len(explicit_completion_criteria(root))
    except CompletionRecoveryError:
        criteria_count = 0
    step_floor = verify_step_floor(criteria_count, verify_cfg)
    if cfg.max_iterations == DEFAULT_MAX_ITERATIONS or cfg.max_iterations < step_floor:
        cfg = replace(cfg, max_iterations=step_floor)

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
