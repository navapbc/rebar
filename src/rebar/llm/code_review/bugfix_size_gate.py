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
* **Fail-open on infrastructure** — a store read failure, an unresolvable storage anchor, or
  an unknown future verdict yields an ADVISORY finding (never a block). Only an affirmative
  "the attestation is missing/invalid/stale-material" classification blocks.
* **Code drift is ACCEPTED** — ``stale-code`` / ``stale-head`` mean the plan WAS reviewed and
  the tree moved on afterwards (routine on a rebase-if-necessary trunk); punishing them would
  make the gate flaky-by-design.
* **Cross-host verification** — the review bot is NOT the environment that signed the
  attestation, so this module verifies the op-cert against the PINNED
  ``.rebar/trusted_environments.yaml`` keyring (never the bot's own env key), mirroring
  ``verify_required_environment``'s not-pinned arm for unpinned principals.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BUGFIX_SIZE_THRESHOLD_NON_TEST_LINES = 150
"""The size floor, in non-test changed lines. Shared with ``scripts/backtest_bugfix_size.py``."""

CRITERION_ID = "bugfix-size-attestation"

_PLAN_REVIEW_KIND = "plan-review"

# ── verdict vocabulary ────────────────────────────────────────────────────────────────────
# (a) compute_validity's plan-review-reachable literals ('not-closed' is the completion-
# verifier arm's literal and deliberately NOT part of this vocabulary), (b) the canonical
# verify-layer enum (verify_signature_result.schema.json), (c) this gate's own error verdict.
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
# (b) the cross-host op-cert verification vocabulary — the literals emitted through
# verify_opcert + the registry scheme passthrough. NOT the whole verify_signature_result
# enum: `foreign_key` is the OWN-ENV wrapper's verdict (verify_opcert_record), which B2
# never calls precisely because it reads foreign_key for every legitimate foreign signer;
# `unsigned`/`certified` enter the vocabulary via (a). A foreign_key string can therefore
# never reach this gate — if enum growth ever routes one here, bucket_for_verdict fails
# open (infra/advisory) and the coverage test forces an explicit decision.
_VERIFY_LAYER_VERDICTS = frozenset(
    {
        "mismatch",
        "key_not_valid_at_era",
        "invalid",
        "unavailable",
        "unknown_kind",
        "unknown_scheme",
    }
)
KNOWN_VERDICTS = _COMPUTE_VALIDITY_VERDICTS | _VERIFY_LAYER_VERDICTS | frozenset({"error"})

ACCEPTED_VERDICTS = frozenset({"certified", "stale-code", "stale-head"})
INFRA_VERDICTS = frozenset({"unavailable", "error"})
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


def _plan_review_anchor(ticket_id: str, repo_root: Any = None) -> tuple[str, str] | None:
    """The storage anchor ``(commit, position)`` of the TERMINAL plan-review SIGNATURE event.

    The plan-review analog of ``_commands/verify_opcert.py``'s completion-verifier anchor
    derivation: the last envelope-bearing SIGNATURE event in the ticket's directory whose
    manifest-authoritative kind is ``plan-review`` (filename order == timestamp order — the
    same last-writer-wins rule the reducer applies per kind). ``None`` when no such event
    exists or its introducing tickets-branch commit cannot be resolved (→ infra fail-open,
    NOT a flag: an unresolvable anchor is a store problem, not evidence of a bad cert)."""
    from rebar import config as _config
    from rebar.attest.authorship import resolve_event_commit
    from rebar.reducer._processors import attestation_kind

    ticket_dir = Path(str(_config.tracker_dir(repo_root))) / ticket_id
    if not ticket_dir.is_dir():
        return None
    terminal: str | None = None
    for name in sorted(os.listdir(ticket_dir)):
        if not name.endswith("-SIGNATURE.json"):
            continue
        try:
            event = json.loads((ticket_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        data = event.get("data") or {}
        if not isinstance(data, dict) or not data.get("envelope"):
            continue
        if attestation_kind(data.get("manifest"), data) != _PLAN_REVIEW_KIND:
            continue
        terminal = name
    if terminal is None:
        return None
    position = terminal[: -len("-SIGNATURE.json")]
    commit = resolve_event_commit(position, str(ticket_dir), repo_root=repo_root)
    if not commit:
        return None
    return str(commit), position


def classify_plan_review_attestation(
    ticket_id: str, repo_root: Any = None, state: dict[str, Any] | None = None
) -> dict[str, str]:
    """Classify ``ticket_id``'s plan-review attestation for a foreign (bot-side) verifier.

    The cross-host chain: read the attestation record → decode its op-cert envelope → verify
    the signature against the PRINCIPAL's pinned trusted-environment keyring at the record's
    storage anchor → on a verified signature, compute lifecycle/freshness validity
    (``compute_validity``). Returns ``{"verdict", "reason"}`` with the verdict drawn from
    ``KNOWN_VERDICTS``; any exception is caught and reported as ``error`` (infra)."""
    try:
        from rebar.attest import opcert as _opcert
        from rebar.attest import trusted_env as _trusted_env
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
        envelope, bound = decoded
        principal = envelope.signatures[0].keyid if envelope.signatures else ""
        keyring = _trusted_env.trusted_env_keyring(principal, repo_root)
        if keyring is None:
            # verify_required_environment's not-pinned arm: an unpinned signer is a flag —
            # the bot cannot certify an environment the project never chose to trust.
            return {
                "verdict": "mismatch",
                "reason": f"signing environment {principal or '<unknown>'!r} is not pinned "
                "in trusted_environments.yaml",
            }
        anchor = _plan_review_anchor(ticket_id, repo_root=repo_root)
        if anchor is None:
            return {
                "verdict": "unavailable",
                "reason": "cannot resolve the attestation's storage anchor",
            }
        anchor_commit, anchor_position = anchor
        verdict = _opcert.verify_opcert(
            envelope,
            str(bound.get("ticket_id") or ticket_id),
            str(bound.get("material_fingerprint") or ""),
            str(bound.get("merged_log_commit") or ""),
            keyring,
            kind=_PLAN_REVIEW_KIND,
            principal=principal,
            storage_anchor_commit=anchor_commit,
            storage_anchor_position=anchor_position,
            repo_root=str(repo_root) if repo_root is not None else None,
        )
        if not verdict.verified:
            return {"verdict": str(verdict.verdict), "reason": str(verdict.reason or "")}
        # Signature verified → lifecycle/freshness validity on the SIGNED payload's fields.
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
    except Exception as exc:  # noqa: BLE001 — infra fail-open: never raise into the review
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
        "with non-test file impact to the full review), then re-push once the review passes."
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
    except Exception as exc:  # noqa: BLE001 — infra fail-open: never fail the review
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
