"""Check scratch availability and free space before cloning a review.
Both an unmounted declared scratch volume and a low free-space floor use
the low-disk coverage reason. Retry while budget remains, then defer
without changing the Gerrit vote."""

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
    """Ask the shared gate-admission predicate whether dedicated scratch is
    mounted. Import it only when admission runs."""
    from rebar.llm.gate_admission import scratch_unavailable_detail as _detail

    return _detail()


def scratch_coverage(detail: str) -> dict[str, Any]:
    """Return a low-disk coverage block with the unmounted-volume detail.
    The shared reason keeps voter routing unchanged."""
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
    """Check the declared mount before the free-space floor.
    An unmounted path may appear writable and pass the floor. Return a
    refusal for either disk condition, or None to permit the clone."""
    detail = scratch_unavailable_detail()
    if detail is not None:
        return scratch_unavailable_decision(detail)
    if not review_clone_has_room(cfg):
        return decision()
    return None


def is_low_disk_decision(candidate: dict[str, Any]) -> bool:
    return candidate.get("gap_reason") == GAP_REASON


def review_clone_has_room(_cfg: object) -> bool:
    """Check the clone target's configured free-space floor.
    Accept config for the voter's patchable call signature."""
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
