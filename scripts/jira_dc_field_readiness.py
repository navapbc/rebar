"""ONE shared definition of "the Jira DC harness is ready" — the Epic custom fields exist.

Bug 9790-cafa-dffa-462e. Both the live-suite fixture
(``tests/external/live_jira_dc/conftest.py``) and the deterministic probe
(``scripts/jira_dc_epic_link_clear_probe.py``) need the SAME answer to the same
question — "are the Jira Software (GreenHopper) custom fields registered yet?" —
and until this module existed they answered it two different ways, which is how
the drift below happened.

THE MEASUREMENT (GitHub Actions probe run 30944211742, recorded verbatim):

  * ``/rest/api/2/serverInfo`` first returned 200 at **8m32s** after boot.
  * **ONE SECOND later**, ``/rest/api/2/field`` returned **27 fields, every one a
    SYSTEM field** — not a single ``customfield_*``, therefore no ``Epic Link``
    and no ``Epic Name``.

So a ``serverInfo`` 200 does NOT imply the Epic machinery is usable: Jira answers
REST well before the GreenHopper plugin finishes registering its custom fields.
The live pytest harness never tripped over this only because it does slower work
after readiness (dependency install, then a ~41-minute suite) before its fixture
asserts the same fields — an INCIDENTAL margin that no one designed and nothing
maintains. That is a latent flake, not a guarantee.

Gerrit change 1387 fixed the probe path alone, which left two definitions of
readiness in the tree: the probe polled the field inventory, the harness polled
``serverInfo``. This module collapses them back into one, so a future change to
the readiness rule cannot silently apply to only half the callers.

WHY IT IS STDLIB-ONLY AND IMPORTS NOTHING FROM rebar. The probe workflow
(``.github/workflows/jira-dc-epic-link-probe.yml``) runs
``python scripts/jira_dc_epic_link_clear_probe.py`` with NO virtualenv and NO
installed dependencies — not even pytest. Anything imported here must therefore
be in the standard library, and the HTTP call itself stays with the caller (both
callers already own a raw ``urllib`` helper that records evidence in its own
format); this module takes an injected ``request`` callable instead.

WHY A "SYSTEM-ONLY INVENTORY" IS NOT-READY, NEVER "DEGRADED IMAGE". The two
outcomes look identical in a single read, and the first probe run misdiagnosed
exactly this. The distinction is TIME: a field that is absent at 8m33s and
present at 12m is a readiness/timing fact; a field still absent after the whole
budget is a genuine image degrade. So every failure message produced here names
BOTH candidate causes and dumps the observed inventory as the evidence that tells
them apart.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

#: The REST endpoint that answers the readiness question. Named once so both
#: callers poll the same path.
FIELD_PATH = "/rest/api/2/field"

#: Instance field NAMES the Epic machinery needs. Data Center requires
#: ``Epic Name`` to create an Epic at all, and ``Epic Link`` is how a child is
#: attached to one. Both are instance-wide GreenHopper custom fields, so they are
#: read from ``/rest/api/2/field`` rather than from any project.
REQUIRED_FIELDS: tuple[str, ...] = ("Epic Link", "Epic Name")

#: Poll cadence while waiting. Coarse on purpose: plugin registration takes
#: minutes, so a tight loop only inflates the evidence log with identical
#: inventories.
FIELD_POLL_INTERVAL_S: float = 10.0

#: How long to wait for the fields before declaring not-ready. Bounded so a
#: genuinely fieldless image fails loudly instead of hanging the job.
FIELD_READY_BUDGET_S: float = 600.0

#: Cap on how many observed field names a diagnostic dump prints. The inventory
#: grows without bound on a real instance; ~40 names is enough to see whether any
#: ``customfield_*`` has appeared at all, which is the actual signal.
_INVENTORY_NAME_CAP = 40


@dataclass(frozen=True)
class FieldReadiness:
    """The outcome of one bounded wait for the required fields."""

    ready: bool
    missing: list[str]
    #: ``describe_inventory`` of the LAST observation — the evidence a failure
    #: message quotes, so a reader can tell timing from degrade without re-running.
    inventory: str
    attempts: int
    ids: dict[str, str | None] = field(default_factory=dict)


def _field_dicts(status: int, body: object) -> list[dict] | None:
    """The inventory as a list of dicts, or ``None`` when the read is unusable."""
    if status != 200 or not isinstance(body, list):
        return None
    return [entry for entry in body if isinstance(entry, dict)]


def missing_required_fields(
    status: int, body: object, names: Sequence[str] = REQUIRED_FIELDS
) -> list[str]:
    """Which of ``names`` are absent from a ``/rest/api/2/field`` response.

    An unusable read (non-200, or a body that is not a list) returns ALL names:
    "we could not see the fields" is NOT-READY, never "the image dropped them".
    Collapsing those two into a pass is what let the original 8m32s race through.
    """
    entries = _field_dicts(status, body)
    if entries is None:
        return list(names)
    present = {str(entry.get("name")) for entry in entries}
    return [name for name in names if name not in present]


def describe_inventory(status: int, body: object) -> str:
    """One-line, self-diagnosing dump of what the field inventory actually held.

    Names the HTTP status, the field count, and the sorted field NAMES observed
    (capped) — verbatim, because "27 fields, all of them system fields" is the
    single observation that distinguishes a not-yet-registered plugin from an
    image that genuinely does not ship Epic fields.
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
    """Poll ``request(FIELD_PATH)`` until every name is registered, or the budget expires.

    Waits for the CAPABILITY, not for a proxy of it (see the module docstring:
    ``serverInfo`` 200 preceded a system-only field inventory by one second).

    ``budget``/``interval`` default to the MODULE-LEVEL constants read at CALL
    time rather than bound as default-argument values, so a caller (or a test)
    that rebinds ``FIELD_READY_BUDGET_S`` / ``FIELD_POLL_INTERVAL_S`` on this
    module actually takes effect.

    ``sleep``/``monotonic`` are injected so a test can drive the loop without
    burning wall-clock. At least one attempt is always made, even with a zero or
    negative budget — the answer must come from an observation, never from
    arithmetic.
    """
    effective_budget = FIELD_READY_BUDGET_S if budget is None else budget
    effective_interval = FIELD_POLL_INTERVAL_S if interval is None else interval
    deadline = monotonic() + effective_budget

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
    )


def not_ready_message(result: FieldReadiness, *, base_url: str, budget: float) -> str:
    """The SHARED failure prose for a wait that never saw the required fields.

    Deliberately refuses to pick a cause. ``Epic Link``/``Epic Name`` are Jira
    Software (GreenHopper) custom fields, and their absence has exactly two
    explanations that a single observation cannot separate — so both are named,
    the observed inventory is quoted as evidence, and the reader is told which
    follow-up discriminates them.
    """
    missing = ", ".join(result.missing) or "(none)"
    return (
        f"Jira DC at {base_url}: required Epic field(s) {missing} were still absent after "
        f"{result.attempts} poll(s) over {budget:.0f}s. These are Jira Software "
        f"(GreenHopper) custom fields, and there are exactly two explanations: either they "
        f"never registered within that budget (a READINESS/TIMING failure — Jira answers "
        f"/rest/api/2/serverInfo minutes before the plugin finishes registering its custom "
        f"fields; measured at 8m32s vs a 27-field system-only inventory one second later), "
        f"or this image does not offer them at all (a GENUINE DEGRADE). Tell them apart by "
        f"the inventory below: if it contains no customfield_* entries whatsoever the plugin "
        f"is still starting and the budget (JIRA_DC_FIELD_READY_TIMEOUT) is too short; if it "
        f"contains other custom fields but not these, the image genuinely changed and the "
        f"capability map must be re-run. Last observation: {result.inventory}"
    )
