"""Region-state vocabulary + detector for the code-review region-gated novelty floor (story
blameless-grindable-noctule, epic super-path-bag).

The deliberate divergence from plan-review's WHOLE-artifact floor: a code-review finding carries a
source citation, so the novelty floor is gated PER-FINDING on whether the cited code REGION changed
since the prior review. Only an UNCHANGED region can be dropped; a CHANGED or UNKNOWN region always
RAISES (the fail-safe direction — a broken/ambiguous signal can only make the gate stricter).

Detection is CONTENT-ADDRESSED (compare the cited file's current sha256 to the prior review's
``deps`` map) rather than reachability-based, so it survives a rebase / force-push and needs no
reachable commit. File-level in v1; line-level (git diff --no-index against a stored snapshot) is a
deferred follow-on (ADR 0037)."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# The closed region-state vocabulary (referenced by the predicate; not prose).
REGION_UNCHANGED = (
    "unchanged"  # cited file's current content == the prior review's hash → droppable
)
REGION_CHANGED = (
    "changed"  # cited file's content differs → always raise (the change may be relevant)
)
REGION_UNKNOWN = "unknown"  # ambiguous/absent/error → always raise (fail-safe)


def region_for_finding(
    finding: dict[str, Any], prior_deps: dict[str, str] | None, *, repo_root: Any = None
) -> str:
    """Classify a code-review finding's cited region against the prior review's ``deps`` map.

    Returns :data:`REGION_UNCHANGED` iff the finding cites exactly ONE concrete file that is present
    in ``prior_deps`` AND whose CURRENT content-hash equals the prior hash; :data:`REGION_CHANGED`
    iff that single cited file's content differs; :data:`REGION_UNKNOWN` for everything else —
    a path absent from ``prior_deps``, a multi-location / absence-evidence finding (no single
    concrete file), a created/deleted file (``absent`` sentinel on either side), or ANY error.

    Only :data:`REGION_UNCHANGED` enables a drop; UNKNOWN and CHANGED both raise. FAIL-SAFE: wrapped
    in try/except → UNKNOWN (raise) on any error, so a broken detector never drops a finding."""
    from rebar.llm.plan_review import attest

    try:
        loc = str((finding or {}).get("location") or "").strip()
        if not loc:
            return REGION_UNKNOWN  # absence-evidence / no concrete location
        # A single "path" or "path:line[:col]". A multi-location citation (comma/whitespace/newline
        # separated) has no single region to hash → UNKNOWN (moved/renamed/spread findings too).
        if "," in loc or any(ws in loc for ws in (" ", "\t", "\n")):
            return REGION_UNKNOWN
        path = loc.split(":", 1)[0].strip()
        if not path:
            return REGION_UNKNOWN
        prior = (prior_deps or {}).get(path)
        if prior is None:
            return (
                REGION_UNKNOWN  # not in the prior review's reviewed-file set → no basis to compare
            )
        base = attest._hash_basis(repo_root)
        current = attest._hash_file(path, base=base)
        # A create/delete on either side is not a stable "unchanged region" — treat as UNKNOWN
        # (raise) rather than risk dropping across an appearance/disappearance.
        if current == attest._ABSENT_HASH or prior == attest._ABSENT_HASH:
            return REGION_UNKNOWN
        return REGION_UNCHANGED if current == prior else REGION_CHANGED
    except Exception:  # noqa: BLE001 — fail-safe: any error → UNKNOWN (raise), never a spurious drop
        logger.warning("region detection failed; treating region as UNKNOWN (raise)", exc_info=True)
        return REGION_UNKNOWN
