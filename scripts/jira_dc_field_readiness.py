"""Share post-project readiness checks for Jira Software Epic fields.

GreenHopper registers ``Epic Link`` and ``Epic Name`` after the first Jira Software
project is created, not when the plugin starts. Run 30981084637 for ticket
941b-f049-5f29-4410 observed no custom fields before creation or after 180 seconds of
waiting. It observed both required fields 0.0512 seconds after creation. Call
:func:`await_required_fields` only after the project exists. The bounded poll covers
the short registration race.

The external-test fixture and deterministic probe both use this standard-library-only
module with their own injected HTTP request function. Failure output includes the field
inventory. No ``customfield_*`` entries indicate a misplaced pre-creation call. Other
custom fields without the Epic fields indicate that the image needs capability review.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

#: The shared REST endpoint polled by both callers.
FIELD_PATH = "/rest/api/2/field"

#: GreenHopper field names required to create an Epic and attach a child.
REQUIRED_FIELDS: tuple[str, ...] = ("Epic Link", "Epic Name")

#: Poll cadence for the subsecond registration step measured below.
FIELD_POLL_INTERVAL_S: float = 1.0

#: Seconds from project creation to field visibility in run 30981084637 for ticket
#: 941b-f049-5f29-4410.
PROVISIONING_TO_FIELDS_VISIBLE_S: float = 0.0512

#: Post-project allowance with about 2,400 times the measured registration latency.
FIELD_READY_BUDGET_S: float = 120.0

#: Diagnostic cap that still reveals whether any ``customfield_*`` entry exists.
_INVENTORY_NAME_CAP = 40


@dataclass(frozen=True)
class FieldReadiness:
    """The outcome of one bounded wait for the required fields."""

    ready: bool
    missing: list[str]
    #: Last inventory, used to distinguish call-site ordering from missing capability.
    inventory: str
    attempts: int
    ids: dict[str, str | None] = field(default_factory=dict)
    #: Elapsed seconds recorded on both successful and expired waits.
    elapsed_s: float = 0.0


def _field_dicts(status: int, body: object) -> list[dict] | None:
    """The inventory as a list of dicts, or ``None`` when the read is unusable."""
    if status != 200 or not isinstance(body, list):
        return None
    return [entry for entry in body if isinstance(entry, dict)]


def missing_required_fields(
    status: int, body: object, names: Sequence[str] = REQUIRED_FIELDS
) -> list[str]:
    """Return absent names, treating an unusable response as all names absent."""
    entries = _field_dicts(status, body)
    if entries is None:
        return list(names)
    present = {str(entry.get("name")) for entry in entries}
    return [name for name in names if name not in present]


def describe_inventory(status: int, body: object) -> str:
    """Describe the status, capped names, and custom-field inventory in one line.

    The inventory distinguishes a pre-project call from an image that lacks Epic
    fields. Unusable responses retain their HTTP status and body type.
    """
    entries = _field_dicts(status, body)
    if entries is None:
        return (
            f"GET {FIELD_PATH} -> HTTP {status}, body type {type(body).__name__} "
            f"(unusable: not a field list)"
        )
    names = sorted({str(entry.get("name")) for entry in entries})
    shown = names[:_INVENTORY_NAME_CAP]
    suffix = f" (+{len(names) - len(shown)} more)" if len(names) > len(shown) else ""
    return (
        f"GET {FIELD_PATH} -> HTTP {status}, {len(entries)} field(s); "
        f"names observed: {shown}{suffix}"
    )


def customfield_count(status: int, body: object) -> int | None:
    """Count ``customfield_*`` IDs or return ``None`` for an unusable response.

    Zero means the observed inventory had no custom fields. ``None`` means the
    inventory could not be observed and must remain a distinct state.
    """
    entries = _field_dicts(status, body)
    if entries is None:
        return None
    return sum(1 for entry in entries if str(entry.get("id", "")).startswith("customfield_"))


def field_ids(status: int, body: object, names: Sequence[str]) -> dict[str, str | None]:
    """Map each requested field NAME to its instance field id, or ``None``."""
    entries = _field_dicts(status, body)
    resolved: dict[str, str | None] = dict.fromkeys(names)
    if entries is None:
        return resolved
    by_name = {str(entry.get("name")): str(entry.get("id")) for entry in entries}
    for name in names:
        resolved[name] = by_name.get(name)
    return resolved


def await_required_fields(
    request: Callable[[str], tuple[int, object]],
    *,
    names: Sequence[str] = REQUIRED_FIELDS,
    budget: float | None = None,
    interval: float | None = None,
    sleep: Callable[[float], object] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> FieldReadiness:
    """Poll after project creation until every field appears or the budget expires.

    Module defaults are resolved at call time so callers can rebind them. Injected
    clock functions support deterministic tests. Every invocation observes the endpoint
    at least once, including when the budget is zero or negative.
    """
    effective_budget = FIELD_READY_BUDGET_S if budget is None else budget
    effective_interval = FIELD_POLL_INTERVAL_S if interval is None else interval
    started = monotonic()
    deadline = started + effective_budget

    attempts = 0
    status: int = 0
    body: object = None
    missing = list(names)
    while True:
        attempts += 1
        status, body = request(FIELD_PATH)
        missing = missing_required_fields(status, body, names)
        if not missing:
            break
        if monotonic() >= deadline:
            break
        sleep(effective_interval)

    return FieldReadiness(
        ready=not missing,
        missing=missing,
        inventory=describe_inventory(status, body),
        attempts=attempts,
        ids=field_ids(status, body, names),
        # Preserve elapsed time for both sizing evidence and expiry diagnostics.
        elapsed_s=max(0.0, monotonic() - started),
    )


def ready_message(result: FieldReadiness, *, base_url: str) -> str:
    """Describe a successful wait with elapsed time, polls, and resolved IDs.

    Both callers emit this text so successful runs retain budget-sizing evidence.
    """
    return (
        f"Jira DC at {base_url}: Epic field(s) {list(result.ids)} registered after "
        f"{result.attempts} poll(s) in {result.elapsed_s:.3f}s; resolved ids {result.ids}. "
        f"Provisioning happens on the first Jira Software project create and was measured at "
        f"{PROVISIONING_TO_FIELDS_VISIBLE_S:.4f}s on run 30981084637 — an elapsed time far "
        f"above that is worth investigating even though this run passed."
    )


def not_ready_message(result: FieldReadiness, *, base_url: str, budget: float | None = None) -> str:
    """Describe an expired wait without inferring a cause from elapsed time.

    The verbatim inventory lets the reader distinguish a pre-project call from missing
    image capability. The message never recommends a longer wait.
    """
    missing = ", ".join(result.missing) or "(none)"
    # Include the allowance when supplied. The result already carries elapsed time.
    waited = f"{result.elapsed_s:.1f}s"
    if budget is not None:
        waited = f"{waited} of an allowed {budget:.0f}s"
    return (
        f"Jira DC at {base_url}: required Epic field(s) {missing} were still absent after "
        f"{result.attempts} poll(s) over {waited}. These are Jira Software (GreenHopper) "
        f"custom fields, and the precondition for their existence is specific: GreenHopper "
        f"registers them when the FIRST Jira Software project is created on the instance, not "
        f"when the plugin starts (measured on run 30981084637 — 27 fields and zero "
        f"customfield_* entries both before and after 180s of quiet, then 55 fields including "
        f"both Epic fields 0.0512s after a project create). Waiting longer cannot produce them. "
        f"Read the inventory below to tell the two real causes apart: if it contains NO "
        f"customfield_* entries whatsoever, this check ran before any Jira Software project "
        f"existed and the call site is in the wrong place (bug 941b-f049-5f29-4410); if it "
        f"contains other custom fields but not these, provisioning DID happen and the image "
        f"genuinely changed, so re-run the capability map. Last observation: {result.inventory}"
    )
