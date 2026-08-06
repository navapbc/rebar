"""The Gerrit bugfix-size attestation criterion (ticket ad0d B2).

An oversized bug-fix change (>150 non-test diff lines) landing through Gerrit must carry a
VALID plan-review attestation on the bug named by its ``rebar-ticket:`` trailer. A large fix
whose bug was never plan-reviewed — or whose attestation cannot be verified — is exactly the
"drive-by rewrite labeled as a fix" failure mode this project's bug-trend analysis surfaced,
so the review bot BLOCKS it with a teaching finding. Historical backtest: 13 of 113 bug-fix
commits exceeded the floor, and every one of the 13 was a substantive change that warranted a
reviewed plan; ``scripts/backtest_bugfix_size.py`` re-derives that corpus from git history
against this module's shared constant + classifier.

Design constraints:

* **Gerrit-only** — ``finalize_code_review_verdict`` invokes this gate only when the request
  carries a ``change_id``; a local ``rebar review-code`` preview never blocks on it.
* **Fail-open on infrastructure** — a store read failure or an unknown future verdict yields
  an ADVISORY finding (never a block). Only an affirmative "the attestation is
  missing/stale-material" classification blocks.
* **Code drift is ACCEPTED** — ``stale-code`` / ``stale-head`` mean the plan WAS reviewed and
  the tree moved on afterwards (routine on a rebase-if-necessary trunk); punishing them would
  make the gate flaky-by-design.
* **The FACT of a plan review, not its SOURCE** (current policy, adopted under bug 846b) —
  the gate asks only whether an attested plan review was completed for the bug, and
  deliberately NOT which environment or identity certified it. It does not consult
  ``.rebar/trusted_environments.yaml``. Gating on the signer made the criterion
  unsatisfiable in practice: a plan review run in an ordinary developer environment signs
  with that developer's own environment id as the DSSE principal, and no contributor can pin
  their own environment (that file is CODEOWNERS-protected), so a genuinely PASSING, genuinely
  signed review was rejected purely on its provenance. What this does NOT grant: it does not
  widen who may cast the Gerrit ``LLM-Review``/``Verified`` votes, so a change still cannot
  self-approve — the plan review is an input to those gates, not a substitute for them.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

BUGFIX_SIZE_THRESHOLD_NON_TEST_LINES = 150
"""The size floor, in non-test changed lines. Shared with ``scripts/backtest_bugfix_size.py``."""

CRITERION_ID = "bugfix-size-attestation"

_PLAN_REVIEW_KIND = "plan-review"

# ── verdict vocabulary ────────────────────────────────────────────────────────────────────
# compute_validity's plan-review-reachable literals ('not-closed' is the completion-verifier
# arm's literal and deliberately NOT part of this vocabulary), plus this gate's own `error`.
# The verify-layer enum (mismatch / key_not_valid_at_era / invalid / unavailable / ...) is
# deliberately ABSENT: since 846b the gate no longer verifies WHO signed, so those literals are
# unreachable here and carrying them would be dead vocabulary contradicting the stated policy.
_COMPUTE_VALIDITY_VERDICTS = frozenset(
    {
        "certified",
        "unsigned",
        "wrong-kind",
        "malformed-pin",
        "malformed-phase",
        "stale-code",
        "stale-head",
        "stale-material",
        "stale-reopened",
        "stale-pin-drift",
        "stale-pin-missing",
        "unverifiable-material",
        "incompatible-phase",
    }
)
KNOWN_VERDICTS = _COMPUTE_VALIDITY_VERDICTS | frozenset({"error"})

ACCEPTED_VERDICTS = frozenset({"certified", "stale-code", "stale-head"})
INFRA_VERDICTS = frozenset({"error"})
FLAG_VERDICTS = KNOWN_VERDICTS - ACCEPTED_VERDICTS - INFRA_VERDICTS


def bucket_for_verdict(verdict: str) -> str:
    """``accepted`` / ``flag`` / ``infra``. Unknown future literals fail OPEN (infra)."""
    if verdict in ACCEPTED_VERDICTS:
        return "accepted"
    if verdict in FLAG_VERDICTS:
        return "flag"
    return "infra"


# ── diff accounting ───────────────────────────────────────────────────────────────────────


def is_test_path(path: str) -> bool:
    """True iff ``path`` is test material: under ``tests/`` or a ``conftest.py`` anywhere.

    Same rule as ``plan_review.orchestrator.bug_blast_radius_escalates`` (B1) — the two ends
    of this criterion must agree on what "test-only" means."""
    p = str(path).strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    if not p:
        return False
    return p.startswith("tests/") or os.path.basename(p) == "conftest.py"


def count_non_test_diff_lines(diff_text: str) -> int:
    """Added+removed lines in ``diff_text`` attributed to non-test files.

    Unified-diff walk with hunk-state tracking: ``---``/``+++`` are file headers only OUTSIDE
    a hunk (a deleted content line like ``-- foo`` renders ``--- foo`` INSIDE one). The ``+++``
    (new-location) side classifies a file; a deletion (``+++ /dev/null``) keeps the ``---``
    side's classification."""
    count = 0
    counting = False
    in_hunk = False
    for line in (diff_text or "").splitlines():
        if line.startswith("diff --git "):
            in_hunk = False
            counting = False
            continue
        if line.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk and (line.startswith("--- ") or line.startswith("+++ ")):
            target = line[4:].strip()
            if target == "/dev/null":
                continue  # keep the other side's classification
            if target[:2] in ("a/", "b/"):
                target = target[2:]
            counting = not is_test_path(target)
            continue
        if in_hunk and counting and line[:1] in ("+", "-"):
            count += 1
    return count


# ── ticket resolution + attestation classification ─────────────────────────────────────────


def ticket_for_commit_message(commit_message: str, repo_root: Any = None) -> str | None:
    """The ticket id named by the message's ``rebar-ticket:`` trailer (or leading ``<id>:``
    subject), resolved against the tracker — the voter/CI trailer convention. ``None`` when no
    ref resolves."""
    from rebar import config as _config
    from rebar._commands.verify_commit import extract_ticket_refs
    from rebar._engine_support.resolver import resolve_ticket_id

    tracker = str(_config.tracker_dir(repo_root))
    for ref in extract_ticket_refs(commit_message or "") or []:
        resolved = resolve_ticket_id(ref, tracker)
        if resolved:
            return str(resolved)
    return None


def _load_ticket_state(ticket_id: str, repo_root: Any = None) -> dict[str, Any]:
    from rebar import _reads

    return _reads.show_ticket(ticket_id, repo_root=repo_root)


def classify_plan_review_attestation(
    ticket_id: str, repo_root: Any = None, state: dict[str, Any] | None = None
) -> dict[str, str]:
    """Classify ``ticket_id``'s plan-review attestation: was an attested review COMPLETED?

    The chain: read the attestation record → decode its op-cert envelope (its presence is the
    evidence that a review ran and was signed) → compute lifecycle/freshness validity
    (``compute_validity``), which is about the PLAN changing under the review, not about who
    signed it. Per the module docstring, the signing principal is deliberately NOT consulted.
    Returns ``{"verdict", "reason"}`` with the verdict drawn from ``KNOWN_VERDICTS``; any
    exception is caught and reported as ``error`` (infra)."""
    try:
        from rebar.attest import opcert as _opcert
        from rebar.llm.plan_review.attest import PlanValidityProfile, compute_validity

        if state is None:
            state = _load_ticket_state(ticket_id, repo_root=repo_root)
        record = (state.get("attestations") or {}).get(_PLAN_REVIEW_KIND)
        if not isinstance(record, dict):
            return {"verdict": "unsigned", "reason": "no plan-review attestation record"}
        decoded = _opcert.opcert_from_record(record)
        if decoded is None:
            return {
                "verdict": "unsigned",
                "reason": "attestation record carries no decodable op-cert envelope",
            }
        _envelope, bound = decoded
        # SUBJECT BINDING survives the removal of provenance checking, because it asks WHAT the
        # attestation is about, not WHO signed it: a cert minted for another ticket or another
        # kind is not evidence that THIS bug's plan was reviewed, however impeccable its signer.
        # (Cross-ticket replay is also caught downstream by the material-fingerprint comparison;
        # this states the invariant directly instead of relying on that coincidence.)
        if str(bound.get("ticket_id") or "") != str(ticket_id):
            return {
                "verdict": "wrong-kind",
                "reason": f"attestation is bound to ticket {bound.get('ticket_id')!r}, "
                f"not {ticket_id!r}",
            }
        if str(bound.get("kind") or "") != _PLAN_REVIEW_KIND:
            return {
                "verdict": "wrong-kind",
                "reason": f"attestation is a {bound.get('kind')!r} cert, not a plan review",
            }
        # Lifecycle/freshness validity on the SIGNED payload's fields — the only remaining
        # question, and the one that catches a plan that MOVED after it was reviewed.
        shaped: dict[str, Any] = {
            "opcert": True,
            "verified": True,
            "signed_manifest": bound.get("manifest"),
            "manifest": record.get("manifest") or [],
            "material_fingerprint": bound.get("material_fingerprint"),
            "merged_log_commit": bound.get("merged_log_commit"),
            "signed_at": record.get("signed_at"),
        }
        validity = compute_validity(
            shaped,
            state,
            _PLAN_REVIEW_KIND,
            repo_root=repo_root,
            profile=PlanValidityProfile.DEFAULT,
        )
        return {
            "verdict": str(validity.get("verdict") or "error"),
            "reason": str(validity.get("reason") or ""),
        }
    except Exception as exc:
        logger.warning("bugfix-size attestation classification failed", exc_info=True)
        return {"verdict": "error", "reason": f"classification failed: {exc}"}


# ── the gate ────────────────────────────────────────────────────────────────────────────────


def _teaching_finding(ticket_id: str, non_test_lines: int, classification: dict[str, str]) -> str:
    return (
        f"{CRITERION_ID}: this change touches {non_test_lines} non-test line(s) — over the "
        f"{BUGFIX_SIZE_THRESHOLD_NON_TEST_LINES}-line floor for a bug fix — but bug "
        f"{ticket_id} has no acceptable plan-review attestation "
        f"(verdict: {classification.get('verdict')}; {classification.get('reason')}). "
        "A fix this large is a design change wearing a bug label: write the fix plan into the "
        f"ticket's description, run `rebar review-plan {ticket_id}` (it auto-escalates a bug "
        "with non-test file impact to the full review and SIGNS an attestation on a PASS; if "
        f"the review passed but no attestation landed, `rebar sign-review {ticket_id}` "
        f"re-persists it cheaply, and `rebar review-plan {ticket_id} --status` confirms it is "
        "current without an LLM call), then re-push."
    )


def apply_bugfix_size_gate(
    verdict: dict[str, Any], *, diff_text: str, commit_message: str, repo_root: Any = None
) -> dict[str, Any]:
    """Apply the bugfix-size attestation criterion to ``verdict`` IN PLACE (Gerrit only —
    the caller gates on ``request.change_id``).

    Predicate: the commit resolves to a ``bug`` ticket AND the diff exceeds
    ``BUGFIX_SIZE_THRESHOLD_NON_TEST_LINES`` non-test lines. Then the bug's plan-review
    attestation classification decides: ACCEPTED → coverage note only; FLAG → kernel-shaped
    blocking finding (mirrors ``detectors.apply_failclosed``); INFRA → advisory finding.
    Never raises."""
    ticket_id: str | None = None
    try:
        non_test = count_non_test_diff_lines(diff_text or "")
        if non_test <= BUGFIX_SIZE_THRESHOLD_NON_TEST_LINES:
            return verdict
        ticket_id = ticket_for_commit_message(commit_message or "", repo_root=repo_root)
        if not ticket_id:
            return verdict
        state = _load_ticket_state(ticket_id, repo_root=repo_root)
        if str(state.get("ticket_type") or "") != "bug":
            return verdict
        classification = classify_plan_review_attestation(
            ticket_id, repo_root=repo_root, state=state
        )
    except Exception as exc:
        logger.warning("bugfix-size gate degraded to advisory", exc_info=True)
        try:
            non_test = count_non_test_diff_lines(diff_text or "")
        except Exception:  # noqa: BLE001 — cannot even size the diff; stay silent
            return verdict
        if non_test <= BUGFIX_SIZE_THRESHOLD_NON_TEST_LINES:
            return verdict
        ticket_id = ticket_id or "<unresolved>"
        classification = {"verdict": "error", "reason": f"gate evaluation failed: {exc}"}

    bucket = bucket_for_verdict(str(classification.get("verdict")))
    verdict.setdefault("coverage", {})["bugfix_size_gate"] = {
        "ticket": ticket_id,
        "verdict": classification.get("verdict"),
        "reason": classification.get("reason"),
        "non_test_lines": non_test,
        "threshold": BUGFIX_SIZE_THRESHOLD_NON_TEST_LINES,
        "bucket": bucket,
    }
    if bucket == "flag":
        verdict.setdefault("blocking", []).append(
            {
                "criteria": [CRITERION_ID],
                "severity": "high",
                "decision": "block",
                "tier": "DET",
                "finding": _teaching_finding(ticket_id, non_test, classification),
                "location": None,
            }
        )
        verdict["verdict"] = "BLOCK"
    elif bucket == "infra":
        verdict.setdefault("advisory", []).append(
            {
                "criteria": [CRITERION_ID],
                "severity": "medium",
                "decision": "advise",
                "tier": "DET",
                "finding": (
                    f"{CRITERION_ID}: could not classify bug {ticket_id}'s plan-review "
                    f"attestation ({classification.get('verdict')}: "
                    f"{classification.get('reason')}); the size floor "
                    f"({non_test} non-test lines > {BUGFIX_SIZE_THRESHOLD_NON_TEST_LINES}) "
                    "was met but the gate fails open on infrastructure trouble."
                ),
                "location": None,
            }
        )
    return verdict
