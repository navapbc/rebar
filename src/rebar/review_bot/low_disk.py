"""Low-disk admission helpers for the Gerrit review bot.

The voter writes large temporary clones on the process temp volume before the review
adapter can produce a normal decision. These helpers keep that pre-clone guard small in
``voter.py`` while preserving the existing ADR 0069 decision shape: low disk is a
retryable coverage-gap subreason while attempts remain, and a terminal no-vote deferral
when its own budget is exhausted.
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
