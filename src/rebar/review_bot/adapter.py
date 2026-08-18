"""The formal b744 verdict→label seam (epic d251 / S4b; reimplemented by b744 / WS6).

This module holds the ONE function the rest of the receiver depends on for a
code-review decision:

    code_review_decision(diff_text, repo_root, ref) -> {decision, message, findings, coverage_gap}

**WS6 (this revision):** the contract is now implemented over the FOUR-PASS
``gate_dispatch.produce_code_review_verdict`` (the typed ``PASS``/``BLOCK``/``INDETERMINATE``
verdict) — a drop-in swap of the earlier single-pass ``review_code`` implementation, with NO
caller change (the voter still reads ``decision`` + ``message``). The four-pass gate's own
deterministic Pass-3 blocker decides PASS vs BLOCK (via ``criteria_routing.json`` thresholds), so
the adapter no longer applies a severity heuristic — ``ReceiverConfig.blocking_severities`` is now
vestigial for this path.

FORCE-ENABLE. The code-review gate is OFF by default (``verify.enable_code_review``), but voter
activation is itself the authoritative gate (a project is only reviewed once its receiver is
deployed + configured), so the adapter passes ``enabled=True`` — else every change would get the
inert disabled verdict. See ADR 0015.

DECISION RULE (fail-closed). PASS only for a genuine ``verdict == PASS`` with full coverage. A
real BLOCK (blocking findings), an INDETERMINATE (LLM outage), a fail-closed security-scanner
abstain, an inert-disabled verdict, or ANY exception → BLOCK. A BLOCK caused by a coverage gap
(infra) is marked ``coverage_gap=True`` and its message carries a DISTINCT tag from a real
finding, so an operator can tell an infra veto from a code veto.

SOURCE. The receiver has ALREADY cloned the change ref into ``repo_root`` (see
``gerrit_client.clone_change_ref`` / ``voter``); we review that working tree by passing
``repo_root`` (the security detectors scan the changed files there) + the fetched ``diff_text``.

TREE↔VOTE BINDING. ``ref`` names what was fetched; ``revision`` is what the vote attaches to.
They are two independently supplied fields, and the reviewed tree is merely whatever ended up in
``repo_root``, so the adapter asserts the two agree before reviewing anything and raises
``ReviewedTreeMismatch`` — the receiver then casts NO vote — when they provably do not.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rebar.review_bot.finding_publish import render_findings_block

if TYPE_CHECKING:
    from rebar.llm.auth import LLMRuntime

logger = logging.getLogger("rebar.review_bot.adapter")

__all__ = [
    "RETRYABLE_GAP_REASONS",
    "ReviewedTreeMismatch",
    "append_retries_exhausted_note",
    "code_review_decision",
]

#: The review-message first-line tag suffixes, keyed by reason. The message begins with
#: ``[LLM-Review: <suffix>]`` so an infra-failure ``-1`` (a coverage-gap sub-reason) is
#: unmistakable from a real-finding ``-1``. Documented vocabulary — asserted by a test.
_TAG_SUFFIXES: dict[str, str] = {
    "PASS": "PASS",
    "finding": "BLOCK — finding",
    "gate-disabled": "BLOCK — coverage-gap (gate-disabled)",
    "llm-unavailable": "BLOCK — coverage-gap (llm-unavailable)",
    "scanner": "BLOCK — coverage-gap (scanner)",
    "review-error": "BLOCK — coverage-gap (review-error)",
    "indeterminate": "BLOCK — coverage-gap (indeterminate)",
}

#: Coverage-gap sub-reasons that are RETRYABLE (ticket 0347): the review never ran to a usable
#: conclusion (a broken/missing gate, an LLM outage, a scanner that could not run, a disabled
#: gate), so the voter defers vote-less — the vote-LESS change stays visible to the backfill
#: reconciler, which re-drives it — instead of casting a fail-closed ``-1`` that would suppress
#: that recovery. NOT retryable: ``finding`` (a real code veto) and ``indeterminate`` (the
#: review RAN TO COMPLETION and concluded coverage could not be established — a result, not an
#: interruption). The merge-path ``_merge_coverage_gap_decision`` (voter.py) deliberately
#: carries no ``gap_reason`` and keeps its immediate fail-closed vote.
RETRYABLE_GAP_REASONS = frozenset({"review-error", "llm-unavailable", "scanner", "gate-disabled"})

#: Map the four-pass kernel severity ({critical,major,minor,none}) to the finding vocabulary the
#: receiver logs ({critical,high,medium,info}) — mirrors the WS4 shim.
_KERNEL_TO_COMMON_SEVERITY = {
    "critical": "critical",
    "major": "high",
    "minor": "medium",
    "none": "info",
}


def _message_tag(
    reason: str, *, label: str = "LLM-Review", merge_commits: int | None = None
) -> str:
    """The first-line tag, e.g. ``[LLM-Review: PASS]``. For a merge change (``merge_commits``
    set) the merge-change variant is appended INSIDE the tag —
    ``[LLM-Review: PASS (merge-change, 3 integrated commits)]`` — reusing the strict
    ``_TAG_SUFFIXES[reason]`` lookup so the non-merge tag vocabulary is unchanged."""
    suffix = _TAG_SUFFIXES[reason]
    if merge_commits is not None:
        suffix += f" (merge-change, {merge_commits} integrated commit(s))"
    return f"[{label}: {suffix}]"


def _coverage_gap_reason(coverage: dict[str, Any]) -> str | None:
    """The coverage-gap sub-reason for a verdict's ``coverage`` block, or None if coverage was
    fully established. Order: inert **disabled** gate (``enabled is False``), then an **LLM outage**
    (``llm_unavailable``), then a **fail-closed security scanner abstain**. A scanner MATCH
    (``reason == 'detector-finding'``) is a real finding, NOT a coverage gap."""
    if coverage.get("enabled") is False:
        return "gate-disabled"
    if coverage.get("llm_unavailable"):
        return "llm-unavailable"
    for note in coverage.get("security_detectors") or []:
        if note.get("reason") == "fail-closed-abstain":
            return "scanner"
    return None


def _translate_findings(verdict: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize the verdict's blocking + advisory findings to the receiver's logged shape
    (``{severity, dimension, detail, location}``).

    ``location`` is carried through (bug lacquer-grotesque-urson) so the voter can anchor a
    finding to a real file/line as an inline Gerrit comment; it was previously dropped here,
    which is why no anchor ever reached the Gerrit layer. The key is additive — consumers
    reading ``{severity, dimension, detail}`` are unaffected."""
    out: list[dict[str, Any]] = []
    for f in (verdict.get("blocking") or []) + (verdict.get("advisory") or []):
        criteria = f.get("criteria") or []
        out.append(
            {
                "severity": _KERNEL_TO_COMMON_SEVERITY.get(
                    str(f.get("severity", "")).lower(), "info"
                ),
                "dimension": criteria[0] if criteria else "general",
                "detail": str(f.get("finding", "")).strip(),
                "location": f.get("location") or "",
            }
        )
    return out


def _summarize(reason: str, verdict: dict[str, Any]) -> str:
    coverage = verdict.get("coverage") or {}
    advisory = verdict.get("advisory") or []
    if reason == "PASS":
        if not advisory:
            return "rebar code review passed."
        # The finding TEXT, not just its count (bug lacquer-grotesque-urson): a count alone left
        # advisories unreadable on the change, so nobody could judge which criteria to promote
        # to blocking.
        return "rebar code review passed. " + render_findings_block(advisory, kind="advisory")
    if reason == "finding":
        blocking = verdict.get("blocking") or []
        block = render_findings_block(blocking, kind="blocking") or (
            "rebar code review found 0 blocking issue(s):"
        )
        # Advisories ride along on the BLOCK path too — they were dropped here as well.
        tail = render_findings_block(advisory, kind="advisory")
        return f"{block}\n\n{tail}" if tail else block
    # coverage-gap sub-reasons — name the gap; it is infra, not "bad code".
    if reason == "scanner":
        gaps = "; ".join(
            f"{n.get('criterion')} ({', '.join(n.get('abstain_reasons') or [])})"
            for n in coverage.get("security_detectors") or []
            if n.get("reason") == "fail-closed-abstain"
        )
        detail = f"a security scanner could not run: {gaps}"
    else:
        llm_err = coverage.get("llm_error", "outage")
        detail = {
            "gate-disabled": "the code-review gate is disabled — cannot certify",
            "llm-unavailable": f"the review LLM was unavailable ({llm_err})",
            "indeterminate": "the review returned INDETERMINATE with no blocking findings "
            "(could not establish coverage — not a code finding)",
        }.get(reason, "the code review could not run")
    return (
        f"rebar code review coverage gap — {detail}. Fail-closed veto (infrastructure, not your "
        "code); re-run once the gate/scanner is healthy."
    )


def _block(
    reason: str, verdict: dict[str, Any], *, merge_commits: int | None = None
) -> dict[str, Any]:
    tag = _message_tag(reason, merge_commits=merge_commits)
    return {
        "decision": "BLOCK",
        "message": f"{tag}\n{_summarize(reason, verdict)}",
        "findings": _translate_findings(verdict),
        "coverage_gap": reason != "finding",
        # Machine-readable sub-reason (ticket 0347): drives the voter's defer-vs-vote split.
        # None for a real finding — only coverage gaps carry a gap_reason.
        "gap_reason": reason if reason != "finding" else None,
        # The FULL verdict is threaded up (story limestone-unethical-zebrafinch) so the voter can
        # emit a durable code_review artifact; {} on a fail-closed review-error (no artifact then).
        "verdict": verdict,
    }


class ReviewedTreeMismatch(RuntimeError):
    """The working tree the reviewer was handed is provably NOT the revision the vote would
    attach to. Raised by :func:`code_review_decision` INSTEAD of returning a decision: there is
    no honest vote to cast about a tree that is not the change, so the receiver refuses to vote
    and leaves the change unsubmittable (fail-closed)."""


#: Wall-clock bound (seconds) on the single read-only ``rev-parse`` used to identify the tree.
_REV_PARSE_TIMEOUT = 30

#: A git object name we are willing to COMPARE: 7+ hex digits (git's own minimum useful
#: abbreviation). Shorter or non-hex input is not a commit name we can compare without inviting
#: a false verdict, so it is treated as unverifiable rather than as a mismatch.
_SHA_RE = re.compile(r"\A[0-9a-f]{7,64}\Z")


def _resolve_reviewed_head(repo_root: str | Path) -> str | None:
    """The commit actually checked out at ``repo_root``, or ``None`` when the directory carries
    no resolvable git identity (not a repo, no HEAD, git unavailable).

    ``HEAD`` — deliberately NOT ``FETCH_HEAD``. ``gerrit_client.clone_change_ref`` fetches the
    change ref, checks it out, and THEN fetches the tickets branch from the mirror; that second
    fetch REWRITES ``.git/FETCH_HEAD``, so reading FETCH_HEAD after the clone yields the tickets
    commit and would mismatch on every single review. The detached HEAD left by the checkout is
    the reviewed tree, and nothing later in the clone moves it."""
    try:
        # raw-git-ok: read-only `rev-parse` against a disposable review clone, not the tracker
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=_REV_PARSE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _assert_reviewed_tree(repo_root: str | Path, ref: str, revision: str) -> None:
    """Bind the tree that is about to be REVIEWED to the revision the vote will ATTACH to.

    ``ref`` (the patch set's ``refs/changes/...``) and ``revision`` arrive as two independently
    supplied webhook fields, and the reviewed tree is simply whatever ended up in ``repo_root``.
    A stale or reused clone directory, a partial fetch, or a future refactor of the checkout
    logic all produce a tree that is not the voted revision, with no signal at all — this is the
    signal (ticket ``da31-f9d1``).

    Raises :class:`ReviewedTreeMismatch` ONLY on a PROVEN disagreement: two comparable commit
    names that differ. A false mismatch would veto every review, so the check abstains (returns,
    logging a warning) whenever the binding cannot be established — no revision supplied (the
    seam is also called outside Gerrit), no git identity at ``repo_root``, or either name too
    short/not hex to compare. A clone that never happened already fails closed upstream, where
    ``clone_change_ref`` raises."""
    if not revision:
        return
    resolved = _resolve_reviewed_head(repo_root)
    if resolved is None:
        logger.warning(
            "adapter: cannot identify the reviewed tree at %s — binding not checked", repo_root
        )
        return
    left, right = resolved.strip().lower(), revision.strip().lower()
    if not _SHA_RE.match(left) or not _SHA_RE.match(right):
        logger.warning(
            "adapter: uncomparable commit names (%r vs %r) — binding not checked", left, right
        )
        return
    # Prefix-tolerant so an abbreviated name on either side still matches its full form; the
    # 7-hex floor above is what keeps that from matching unrelated commits.
    if left.startswith(right) or right.startswith(left):
        return
    raise ReviewedTreeMismatch(
        f"the reviewed tree is not the voted revision: the clone of {ref} at {repo_root} is "
        f"checked out at {left}, but the vote would attach to {right}. Refusing to vote — "
        "reviewing one commit and certifying another cannot be made safe. Check that the clone "
        "directory was fresh and that the fetch of that ref completed, then re-trigger."
    )


def code_review_decision(
    diff_text: str,
    repo_root: str | Path,
    ref: str,
    *,
    revision: str = "",
    merge_commits: int | None = None,
    commit_message: str = "",
    change_id: str = "",
    runtime: LLMRuntime | None = None,
) -> dict[str, Any]:
    """Review ``diff_text`` (at the cloned ``repo_root``) via the four-pass gate and return
    ``{decision, message, findings, coverage_gap}``. PASS only for a genuine full-coverage PASS;
    a real BLOCK, an INDETERMINATE, a fail-closed scanner abstain, an inert-disabled verdict, or
    ANY exception → BLOCK (fail-closed). Signature + return shape are stable (the voter is
    unchanged); the four-pass gate owns the threshold.

    ``change_id`` (the Gerrit change) selects the ``change:<id>`` novelty keyspace for the
    region-gated floor (epic super-path-bag), so cross-patchset finding-memory is keyed on the
    CHANGE — spanning its revisions — the Gerrit analogue of the local ``session:<id>`` key.

    ``ref`` + ``revision`` bind the tree to the vote: before any review runs, the commit checked
    out at ``repo_root`` is asserted to be ``revision``. A proven disagreement raises
    :class:`ReviewedTreeMismatch` (the caller must then cast NO vote) rather than returning a
    decision — see :func:`_assert_reviewed_tree`."""
    _assert_reviewed_tree(repo_root, ref, revision)
    try:
        # Lazily imported: the [agents] extra (heavy) must not load merely because the receiver
        # package was imported — only when a review actually runs.
        from rebar.llm.config import LLMConfig
        from rebar.llm.workflow.gate_dispatch import (
            CodeReviewRequest,
            produce_code_review_verdict,
        )
    except Exception as exc:  # noqa: BLE001 — a missing/broken extra is a fail-closed BLOCK
        logger.warning("adapter: gate import failed: %s", exc)
        return _block("review-error", {}, merge_commits=merge_commits)

    # A composed startup runtime (RP-04 S5) is forwarded provider-native: build the runner WITH
    # it and inject it on the request, instead of leaving the gate to resolve an ambient runner.
    # ``runtime is None`` keeps the ambient path unchanged (runner stays None).
    runner = None
    if runtime is not None:
        try:
            from rebar.llm.runner import get_runner

            runner = get_runner(LLMConfig.from_env(repo_root=repo_root), runtime=runtime)
        except Exception as exc:  # noqa: BLE001 — a runner build failure is a fail-closed BLOCK
            logger.warning("adapter: runner build failed: %s", exc)
            return _block("review-error", {}, merge_commits=merge_commits)

    try:
        verdict = produce_code_review_verdict(
            CodeReviewRequest(
                LLMConfig.from_env(repo_root=repo_root),
                diff_text=diff_text,
                commit_message=commit_message,  # drives the scope-intent overlay (default "")
                change_id=change_id,  # selects the change:<id> novelty keyspace (finding-memory)
                repo_root=repo_root,
                enabled=True,  # voter activation is the authoritative gate (ADR 0015)
                runner=runner,  # forwarded composed runtime (None → ambient, unchanged)
            )
        )
    except Exception as exc:  # noqa: BLE001 — ANY review failure is fail-closed
        logger.warning("adapter: produce_code_review_verdict raised: %s", exc)
        return _block("review-error", {}, merge_commits=merge_commits)

    if not isinstance(verdict, dict) or "verdict" not in verdict:
        return _block("review-error", {}, merge_commits=merge_commits)

    gap = _coverage_gap_reason(verdict.get("coverage") or {})
    if verdict.get("verdict") == "PASS" and gap is None:
        tag = _message_tag("PASS", merge_commits=merge_commits)
        return {
            "decision": "PASS",
            "message": f"{tag}\n{_summarize('PASS', verdict)}",
            "findings": _translate_findings(verdict),
            "coverage_gap": False,
            "gap_reason": None,
            "verdict": verdict,  # threaded up for the code_review artifact
        }
    if gap is not None:
        return _block(gap, verdict, merge_commits=merge_commits)
    if verdict.get("blocking"):
        return _block("finding", verdict, merge_commits=merge_commits)
    # Non-PASS (e.g. INDETERMINATE) with NO blocking findings and no detected coverage gap: the
    # review could not establish coverage — a coverage gap, NOT a code finding. Mapping this to
    # "finding" rendered the misleading "[LLM-Review: BLOCK — finding] ... 0 blocking issue(s):"
    # false -1 on a clean change (bug spy-luge-wool, observed on change 223).
    return _block("indeterminate", verdict, merge_commits=merge_commits)


def append_retries_exhausted_note(decision: dict[str, Any], attempts: int) -> dict[str, Any]:
    """A copy of a coverage-gap ``decision`` with the retries-exhausted note appended to its
    message BODY (the first-line ``_TAG_SUFFIXES`` tag is untouched, so the documented tag
    vocabulary is unchanged). Used by the voter when a retryable gap's attempt budget is spent
    and the fail-closed ``-1`` is finally cast (ticket 0347)."""
    out = dict(decision)
    out["message"] = (
        f"{decision.get('message', '')}\n\n"
        f"Automatic retries exhausted ({attempts} attempt(s)) — casting the fail-closed -1. "
        "A contributor re-trigger or a new patchset restarts the retry budget."
    )
    return out
