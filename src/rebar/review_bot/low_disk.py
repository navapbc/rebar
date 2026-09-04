"""Pre-clone disk-admission helpers for the Gerrit review bot.

The voter writes large temporary clones on the process temp volume before the review
adapter can produce a normal decision. These helpers keep that pre-clone guard small in
``voter.py`` while preserving the existing ADR 0069 decision shape: low disk is a
retryable coverage-gap subreason while attempts remain, and a terminal no-vote deferral
when its own budget is exhausted.

TWO CONDITIONS, ONE GAP REASON (bug ``1ef8-c849-5801-4eee``). The module now admits on two
host disk conditions, not one:

* the free-space FLOOR — the volume is there but too full (:func:`review_clone_has_room`);
* the volume is DECLARED BUT NOT MOUNTED (:func:`scratch_unavailable_detail`), ADR 0112
  decision 3. A bare mount point is an ordinary, writable, EMPTY directory, so it raises
  nothing and usually PASSES the free-space floor — which is exactly how the review-bot kept
  cloning onto the root filesystem while ``gate_admission()`` correctly refused the gates.

Both keep the SAME ``low-disk`` gap reason, and that reuse is load-bearing rather than lazy.
ADR 0069 carves out exactly one reason from the fail-closed ``-1`` escalation: on an
exhausted budget ``low-disk`` logs ``REVIEW_LOW_DISK_DEFERRED_EXHAUSTED`` and leaves the
labels UNCHANGED, "because a full root volume is an operator/host condition and must not
become a false code veto". An unmounted disk is the same category error, so a NEW reason
would have to be hand-added to that carve-out branch in ``voter._handle_retryable_gap`` and
could regress into a false ``LLM-Review -1`` against an innocent change. Reusing the reason
makes "never a ``-1``" structural instead of remembered. The sub-condition is carried in the
coverage block and the operator-facing message, where it informs rather than routes.
"""

from __future__ import annotations

import tempfile
from typing import Any

GAP_REASON = "low-disk"
TAG_SUFFIX = "BLOCK — coverage-gap (low-disk)"


def tag_line() -> str:
    """Return the exact first-line tag parsed by retrigger.classify_tag."""
    return f"[LLM-Review: {TAG_SUFFIX}]"


def coverage(
    *,
    path: str = "",
    free_bytes: int | None = None,
    min_bytes: int | None = None,
) -> dict[str, Any]:
    """Machine-readable coverage block consumed by adapter._coverage_gap_reason.

    The optional details are intentionally advisory: tests and routing key only on the
    stable ``low_disk`` boolean and ``gap_reason`` string, while operators get free/floor
    values when the caller had them.
    """
    out: dict[str, Any] = {"low_disk": True}
    if path:
        out["low_disk_path"] = path
    if free_bytes is not None:
        out["low_disk_free_bytes"] = free_bytes
    if min_bytes is not None:
        out["low_disk_min_bytes"] = min_bytes
    return out


def verdict(coverage_block: dict[str, Any] | None = None) -> dict[str, Any]:
    """Small BLOCK verdict carrying only the low-disk coverage signal."""
    return {
        "verdict": "BLOCK",
        "blocking": [],
        "advisory": [],
        "coverage": coverage_block or coverage(),
    }


def scratch_unavailable_detail() -> str | None:
    """Delegate to the ONE owner of the two-marker scratch predicate, or ``None``.

    Deliberately a one-line forward to :func:`rebar.llm.gate_admission.scratch_unavailable_detail`
    rather than a second implementation: the gates and the review-bot clone must never be able
    to disagree about whether the dedicated volume is mounted, and ``observability.sh`` reads
    the same proof marker so monitoring cannot diverge from enforcement either. Imported
    lazily to keep ``rebar.llm`` off the review-bot's import path until admission actually
    runs.
    """
    from rebar.llm.gate_admission import scratch_unavailable_detail as _detail

    return _detail()


def scratch_coverage(detail: str) -> dict[str, Any]:
    """Coverage block for an unmounted scratch volume.

    Carries the SAME ``low_disk`` boolean the free-space floor sets — that boolean is what
    ``adapter._coverage_gap_reason`` routes on, so routing stays identical — plus the
    sub-condition, which is advisory detail for an operator reading the log.
    """
    return {**coverage(), "scratch_unavailable": True, "scratch_detail": detail}


def scratch_unavailable_decision(detail: str) -> dict[str, Any]:
    """Adapter-shaped decision for a pre-clone refusal on an unmounted scratch volume."""
    block = scratch_coverage(detail)
    return {
        "decision": "BLOCK",
        "message": (
            f"{tag_line()}\n"
            "rebar code review deferred: this host declares a dedicated gate-scratch "
            "volume that is not mounted, so cloning was not started — running anyway "
            f"would put the clone back on the root filesystem ({detail}). This is a HOST "
            "condition, not a review result. Re-run once the volume is mounted."
        ),
        "findings": [],
        "coverage_gap": True,
        "gap_reason": GAP_REASON,
        "verdict": verdict(block),
    }


def pre_clone_refusal(cfg: object) -> dict[str, Any] | None:
    """The pre-clone admission decision, or ``None`` to proceed with the clone.

    ONE seam for both host disk conditions, so ``voter._decision_for_review_target`` keeps a
    single guard. Order is not arbitrary: an unmounted mount point is an empty directory that
    normally SATISFIES the free-space floor, so checking the floor first would admit exactly
    the case this refuses.
    """
    detail = scratch_unavailable_detail()
    if detail is not None:
        return scratch_unavailable_decision(detail)
    if not review_clone_has_room(cfg):
        return decision()
    return None


def is_low_disk_decision(candidate: dict[str, Any]) -> bool:
    return candidate.get("gap_reason") == GAP_REASON


def review_clone_has_room(_cfg: object) -> bool:
    """Return whether the review clone target volume has the configured free-space floor.

    The config argument is accepted so tests can monkeypatch the voter alias with the same
    call signature and so a future ReceiverConfig-carried knob can thread through without
    changing the voter call site.
    """
    from rebar._snapshot.janitor import has_min_free_space

    return has_min_free_space(tempfile.gettempdir())


def decision() -> dict[str, Any]:
    """Synthetic adapter-shaped decision for a pre-clone low-disk refusal."""
    return {
        "decision": "BLOCK",
        "message": (
            f"{tag_line()}\n"
            "rebar code review deferred: the review host is below the hard "
            "free-space admission floor, so cloning was not started. Re-run after "
            "disk remediation."
        ),
        "findings": [],
        "coverage_gap": True,
        "gap_reason": GAP_REASON,
        "verdict": verdict(),
    }


def exhausted_result(
    *, change_id: str, revision: str, attempts: int, max_attempts: int
) -> dict[str, Any]:
    return {
        "status": "deferred-exhausted",
        "change_id": change_id,
        "revision": revision,
        "gap_reason": GAP_REASON,
        "attempt": attempts,
        "max_attempts": max_attempts,
    }


def exhausted_message(attempts: int, max_attempts: int) -> str:
    return (
        f"{tag_line()}\n"
        f"Automatic low-disk retry holdback exhausted ({attempts}/{max_attempts} "
        "attempt(s)); leaving LLM-Review labels unchanged. The change remains "
        "unsubmittable until a new patchset or rerun retries after disk remediation."
    )


def exhausted_status(result: dict[str, Any]) -> bool:
    """True for the terminal no-vote result returned after low-disk retries exhaust."""
    return result.get("status") == "deferred-exhausted" and result.get("gap_reason") == GAP_REASON
