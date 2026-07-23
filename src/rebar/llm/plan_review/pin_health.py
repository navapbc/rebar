"""Derived validity health for related-ticket material pinned by a plan review."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from enum import Enum
from typing import Literal, TypedDict, cast

from .relation_snapshot import PlanMaterialPin, is_canonical_ticket_id

logger = logging.getLogger(__name__)


class PlanValidityProfile(Enum):
    DEFAULT = "default"
    CLOSE = "close"
    DRIFT_REFRESH = "drift_refresh"


PinStatus = Literal[
    "current",
    "current-no-relationships",
    "stale-pin-drift",
    "stale-pin-missing",
    "malformed-pin",
    "legacy-unpinned",
]
TargetPinStatus = Literal["current", "stale-pin-drift", "stale-pin-missing", "malformed-pin"]


class TargetPinDetail(TypedDict):
    canonical_id: str
    role: Literal["child", "prerequisite"]
    pinned_fingerprint: str
    current_fingerprint: str | None
    pin_status: TargetPinStatus


class DerivedPlanMaterialPinHealth(TypedDict):
    pin_status: PinStatus
    enforced: bool
    targets: list[TargetPinDetail]


class DerivedPlanReviewHealth(DerivedPlanMaterialPinHealth):
    """The read-time plan-review relationship and phase health contract.

    This is deliberately derived from the authenticated attestation on every read;
    it is never persisted into a ticket event.  Detailed surfaces can therefore
    render one payload while the lifecycle gates retain their existing decision
    semantics.
    """

    phase_status: Literal["compatible", "incompatible", "malformed"]
    signed_phase: Literal["planning", "execution"] | None
    required_phase: Literal["planning", "execution"] | None
    effective_execution_floor: float | None
    advisory: bool
    enforcement_status: Literal["enabled", "disabled"]
    related_material_status: Literal["pinned", "no-related-material", "legacy-unpinned"]


def review_phase_status(current_phase: object, signed_phase: object, floor: object) -> str:
    """Apply the fixed planning/execution compatibility table to parsed metadata."""
    if current_phase not in ("planning", "execution") or signed_phase not in (
        "planning",
        "execution",
    ):
        return "malformed"
    if signed_phase == "planning":
        return "compatible"
    if not isinstance(floor, (int, float)) or isinstance(floor, bool):
        return "malformed"
    if current_phase == "planning" or float(floor) < 0.80:
        return "incompatible"
    return "compatible"


def read_enforcement(repo_root=None) -> bool:
    """Read only the optional pin key; malformed config deliberately fails open."""
    from rebar import config

    try:
        return bool(config.load_config(repo_root).verify.enforce_plan_material_pins)
    except config.ConfigError:
        record = {"event": "plan_material_pin_config_unreadable"}
        logger.warning(
            "plan material pin config unreadable: %s", record, extra=record, exc_info=True
        )
        return False


def _emit_unreadable(pin: PlanMaterialPin, failure_kind: str) -> None:
    record = {
        "event": "plan_material_pin_target_unreadable",
        "canonical_id": pin.canonical_id,
        "role": pin.role,
        "failure_kind": failure_kind,
    }
    logger.warning("plan material pin target unreadable: %s", record, extra=record)


def _warn_unreadable(pin: PlanMaterialPin, repo_root) -> None:
    """Classify a failed narrow fingerprint read without turning absence into noise."""
    from rebar import _reads

    failure_kind = "reducer"
    try:
        state = _reads.show_ticket(pin.canonical_id, repo_root=repo_root)
        if state.get("status") == "deleted":
            return
        if state.get("ticket_id") != pin.canonical_id:
            failure_kind = "resolution"
    except OSError:
        failure_kind = "io"
    except Exception as exc:  # noqa: BLE001 - resolution absence is an expected stale target
        message = str(exc).lower()
        if any(word in message for word in ("not found", "unknown", "ambiguous", "no ticket")):
            return
        failure_kind = "resolution"
    _emit_unreadable(pin, failure_kind)


def derive_health(
    pin_records: Sequence[PlanMaterialPin] | None,
    *,
    repo_root,
    enforced: bool,
    fingerprint: Callable[..., str | None],
) -> DerivedPlanMaterialPinHealth:
    """Compare typed pins with current narrow material and aggregate fixed-severity health."""
    if not pin_records:
        return {"pin_status": "legacy-unpinned", "enforced": enforced, "targets": []}

    targets: list[TargetPinDetail] = []
    seen: set[tuple[str, str]] = set()
    for pin in pin_records:
        key = (pin.role, pin.canonical_id)
        malformed = (
            pin.role not in ("child", "prerequisite")
            or not is_canonical_ticket_id(pin.canonical_id)
            or key in seen
            or len(pin.material_fingerprint) != 16
            or any(c not in "0123456789abcdef" for c in pin.material_fingerprint)
        )
        seen.add(key)
        current = None
        fingerprint_failed = False
        if not malformed:
            try:
                current = fingerprint(pin.canonical_id, repo_root=repo_root)
            except OSError:
                fingerprint_failed = True
                _emit_unreadable(pin, "io")
            except Exception:  # noqa: BLE001 - invalid reduced material is advisory health
                fingerprint_failed = True
                _emit_unreadable(pin, "reducer")
        if malformed:
            status: TargetPinStatus = "malformed-pin"
        elif current is None:
            status = "stale-pin-missing"
            if not fingerprint_failed:
                _warn_unreadable(pin, repo_root)
        elif current != pin.material_fingerprint:
            status = "stale-pin-drift"
        else:
            status = "current"
        targets.append(
            {
                "canonical_id": pin.canonical_id,
                "role": cast(Literal["child", "prerequisite"], pin.role),
                "pinned_fingerprint": pin.material_fingerprint,
                "current_fingerprint": current,
                "pin_status": status,
            }
        )
    severity = {"current": 0, "stale-pin-drift": 1, "stale-pin-missing": 2, "malformed-pin": 3}
    aggregate = max((target["pin_status"] for target in targets), key=severity.__getitem__)
    return {"pin_status": aggregate, "enforced": enforced, "targets": targets}
