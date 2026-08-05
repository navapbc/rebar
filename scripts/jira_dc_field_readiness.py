"""ONE shared definition of "the Epic custom fields are registered on this instance".

Bug 9790-cafa-dffa-462e created this module; bug 941b-f049-5f29-4410 corrected what
it is FOR. Both the live-suite fixture (``tests/external/live_jira_dc/conftest.py``)
and the deterministic probe (``scripts/jira_dc_epic_link_clear_probe.py``) need the
SAME answer to the same question — "are the Jira Software (GreenHopper) custom fields
``Epic Link`` and ``Epic Name`` registered yet?" — and until this module existed they
answered it two different ways, which is the drift it exists to prevent.

WHEN THE FIELDS APPEAR — THE MEASUREMENT THAT DEFINES THIS MODULE'S CONTRACT.
GitHub Actions experiment run **30981084637**, against the harness's own pinned image,
recorded verbatim:

    [before]            HTTP 200 total=27 customfield_count=0   EpicLink=False EpicName=False
    [after-180s-quiet]  HTTP 200 total=27 customfield_count=0   EpicLink=False EpicName=False
    create project -> HTTP 201 {'id': 10000, 'key': 'RBJEXPT'}
    [after-create+0s]   HTTP 200 total=55 customfield_count=13  EpicLink=True  EpicName=True
    VERDICT: before_create_ready=False after_180s_quiet_ready=False
             after_create_ready=True elapsed_after_create=0.0512

**GreenHopper provisions its custom fields when the first Jira Software PROJECT is
created — not when the plugin starts.** 180 extra seconds of quiet time moved nothing;
the project create moved everything in 0.05 seconds. So an inventory of 27 system
fields and zero ``customfield_*`` entries is the NORMAL, healthy state of a fresh
instance that has no project yet — it is not a plugin mid-start, and no amount of
waiting changes it.

WHY THAT MATTERS: WAITING BEFORE A PROJECT EXISTS IS A DEADLOCK, NOT A SLOW PATH.
Change 9790-cafa-dffa-462e made the live suite's SESSION readiness wait for these
fields before it had created anything, i.e. it waited for a thing only the blocked
action could produce. Observed in production CI:

  * run **30975323866** (600s allowance): 1 xfailed, **62 ERRORS** — every cell died at
    fixture setup.
  * run **30978613228** (1800s allowance): expired again after 181 polls with a
    BYTE-IDENTICAL 27-field, zero-``customfield_*`` inventory. The container log shows
    ``Startup is complete. Jira is ready to serve.`` and ``Plugins upgrades completed
    successfully``, then ~29 minutes of total silence before the failure. Tripling the
    allowance changed nothing, because time was never the missing ingredient.
  * run **30964805133**, immediately BEFORE that gate landed: **62 passed** — no gate, so
    the suite created its projects, the fields were provisioned, and the existing
    post-creation check passed.
  * probe runs **30944211742** and **30930839323** both died at
    ``PROBE SETUP FAILED: Epic Link=None Epic Name=None`` — the identical ordering bug on
    the probe side.

SO THE ONLY CORRECT PLACE TO CALL :func:`await_required_fields` IS **AFTER** A JIRA
SOFTWARE PROJECT HAS BEEN CREATED. It is still a bounded WAIT rather than a one-shot
read: provisioning was measured at 0.0512s, which a single read immediately after the
201 can lose a race to. Callers must not resurrect a pre-create call site.

WHY IT IS STDLIB-ONLY AND IMPORTS NOTHING FROM rebar. The probe workflow
(``.github/workflows/jira-dc-epic-link-probe.yml``) runs
``python scripts/jira_dc_epic_link_clear_probe.py`` with NO virtualenv and NO installed
dependencies — not even pytest. Anything imported here must therefore be in the
standard library, and the HTTP call itself stays with the caller (both callers already
own a raw ``urllib`` helper that records evidence in its own format); this module takes
an injected ``request`` callable instead.

WHAT A FAILURE MEANS, NOW THAT THE PRECONDITION IS EXPLICIT. Because every call site is
post-create, the two candidate causes are separated by the observed INVENTORY, not by
elapsed time:

  * **no** ``customfield_*`` **entries at all** — this ran before any Jira Software
    project existed on the instance, i.e. the call site is in the wrong place. That is
    the 941b regression, and it is a code fault, not an environment one.
  * **other** ``customfield_*`` **entries present but not these** — provisioning DID
    happen and this image genuinely no longer ships the Epic fields. A real degrade;
    re-run the capability map.

Every failure message this module produces therefore dumps the inventory verbatim: it
is the single observation that tells those two apart.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

#: The REST endpoint that answers the field question. Named once so both callers
#: poll the same path.
FIELD_PATH = "/rest/api/2/field"

#: Instance field NAMES the Epic machinery needs. Data Center requires
#: ``Epic Name`` to create an Epic at all, and ``Epic Link`` is how a child is
#: attached to one. Both are instance-wide GreenHopper custom fields, so they are
#: read from ``/rest/api/2/field`` rather than from any project.
REQUIRED_FIELDS: tuple[str, ...] = ("Epic Link", "Epic Name")

#: Poll cadence while waiting. Deliberately much finer than the 10s this used to
#: use: the quantity being waited on is now a sub-second provisioning step (see
#: :data:`PROVISIONING_TO_FIELDS_VISIBLE_S`), not a multi-minute plugin start, so a
#: coarse cadence would spend seconds sleeping past an answer that already arrived.
FIELD_POLL_INTERVAL_S: float = 1.0

#: THE MEASUREMENT THIS WAIT IS SIZED FROM. Seconds between the project-create
#: ``201`` and ``Epic Link``/``Epic Name`` being visible in ``/rest/api/2/field``, as
#: recorded by GitHub Actions experiment run **30981084637** for bug
#: 941b-f049-5f29-4410
#: (``elapsed_after_create=0.0512``). Named as a constant, and cited, specifically so
#: the allowance below can never again be an un-attributable number — the previous
#: 600.0 was traceable to no measurement at all, which is why raising it to 1800.0 in
#: run 30978613228 felt like a reasonable response to a failure it could not fix.
PROVISIONING_TO_FIELDS_VISIBLE_S: float = 0.0512

#: How long to keep polling for the fields after a Jira Software project exists,
#: before declaring them absent. 120s is ~2400x the measured
#: :data:`PROVISIONING_TO_FIELDS_VISIBLE_S` — enormous headroom for a slow or loaded
#: runner, while still turning a genuinely fieldless image into a loud failure in two
#: minutes rather than ten. It is NOT a cold-boot allowance: the caller has already
#: waited out Jira's boot (``serverInfo``) and already holds a created project, so the
#: only thing outstanding is a step measured in tens of milliseconds.
FIELD_READY_BUDGET_S: float = 120.0

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
    #: message quotes, so a reader can tell a misplaced call site from a genuine
    #: degrade without re-running.
    inventory: str
    attempts: int
    ids: dict[str, str | None] = field(default_factory=dict)
    #: Wall-clock seconds the wait actually took. Carried because the wait recorded
    #: NOTHING on success (bug 941b-f049-5f29-4410): every green run was silent, so no
    #: run ever produced a number anyone could size this wait from, and only expiries
    #: spoke — and an expiry says "too small" while saying nothing about how small.
    #: With this, a green harness log states the real cost of the wait every time.
    elapsed_s: float = 0.0


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
    "we could not see the fields" is NOT-READY, never a vacuous pass. Collapsing
    those two would let an instance that answers nothing at all report a healthy
    Epic capability, which is the exact class of defect this module exists to stop.
    """
    entries = _field_dicts(status, body)
    if entries is None:
        return list(names)
    present = {str(entry.get("name")) for entry in entries}
    return [name for name in names if name not in present]


def describe_inventory(status: int, body: object) -> str:
    """One-line, self-diagnosing dump of what the field inventory actually held.

    Names the HTTP status ACTUALLY received, the field count, and the sorted field
    NAMES observed (capped) — verbatim, because "27 fields, zero customfield_*" versus
    "55 fields, 13 customfield_*" (run 30981084637, before and after the project
    create — bug 941b-f049-5f29-4410) is the single observation that distinguishes a
    call site that ran before any Jira Software project existed from an image that
    genuinely dropped the Epic
    fields. The status is included on the unusable branch too, so a failure caused by
    a 401/503 names its own cause instead of reading as "the fields are missing".
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
    """Poll ``request(FIELD_PATH)`` until every name is registered, or the wait expires.

    **CALL THIS ONLY AFTER A JIRA SOFTWARE PROJECT EXISTS.** GreenHopper provisions
    ``Epic Link``/``Epic Name`` on the first Software project create, so calling this
    beforehand waits for something only the blocked action can produce — bug
    941b-f049-5f29-4410, measured on run 30981084637 and observed as a total-suite
    deadlock in runs 30975323866 and 30978613228 (see the module docstring). It is nevertheless a bounded LOOP rather
    than a single read, because provisioning is fast (0.0512s) but not instantaneous
    and a one-shot read immediately after the ``201`` can lose that race.

    ``budget``/``interval`` default to the MODULE-LEVEL constants read at CALL time
    rather than bound as default-argument values, so a caller (or a test) that rebinds
    ``FIELD_READY_BUDGET_S`` / ``FIELD_POLL_INTERVAL_S`` on this module actually takes
    effect.

    ``sleep``/``monotonic`` are injected so a test can drive the loop without burning
    wall-clock. At least one attempt is always made, even with a zero or negative
    allowance — the answer must come from an observation, never from arithmetic.
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
        # Recorded on BOTH outcomes: on success it is the number future sizing decisions
        # are made from, on failure it says how long the instance was actually given.
        elapsed_s=max(0.0, monotonic() - started),
    )


def ready_message(result: FieldReadiness, *, base_url: str) -> str:
    """The SHARED success prose — the mirror image of :func:`not_ready_message`.

    Exists because the wait used to say nothing when it worked (bug
    941b-f049-5f29-4410). A silent success meant every green run threw away the one
    measurement that could size this wait, leaving only expiries to reason from — and
    an expiry can only ever argue "make it bigger", which is precisely the reasoning
    that took run 30978613228's allowance to 1800s without fixing anything. Emitting
    elapsed time, poll count and the resolved field ids on the happy path means the
    next person to touch :data:`FIELD_READY_BUDGET_S` has real numbers from real runs.

    Both callers emit it: the pytest fixture ``print``s it (the harness job runs pytest
    with ``-rA`` specifically so captured stdout survives into the log), the probe logs
    it through its own logger.
    """
    return (
        f"Jira DC at {base_url}: Epic field(s) {list(result.ids)} registered after "
        f"{result.attempts} poll(s) in {result.elapsed_s:.3f}s; resolved ids {result.ids}. "
        f"Provisioning happens on the first Jira Software project create and was measured at "
        f"{PROVISIONING_TO_FIELDS_VISIBLE_S:.4f}s on run 30981084637 — an elapsed time far "
        f"above that is worth investigating even though this run passed."
    )


def not_ready_message(result: FieldReadiness, *, base_url: str, budget: float | None = None) -> str:
    """The SHARED failure prose for a wait that never saw the required fields.

    Rewritten by bug 941b-f049-5f29-4410. The previous wording told the reader that an
    inventory with no ``customfield_*`` entries meant "the plugin is still starting and
    the allowance is too short", which is FALSE — run 30981084637 held such an instance
    quiet for 180 extra seconds with zero change, then watched the fields appear 0.05s
    after a project was created. Worse, that wording was self-confirming: it named the
    one remedy that could never work, so it misdiagnosed the change that introduced the
    deadlock AND this bug's own opening analysis, buying a wasted 50-minute run before
    anyone questioned it. It therefore no longer offers "wait longer" as an option at
    all.

    It still refuses to pick a cause where the evidence genuinely underdetermines one:
    the observed inventory is quoted verbatim, and the reader is told which half of it
    to look at to discriminate the two real explanations.
    """
    missing = ", ".join(result.missing) or "(none)"
    # ``budget`` is reported as an allowance in seconds when the caller passes one, and is
    # OPTIONAL because the elapsed time now travels on the result itself — a caller that
    # simply wants the prose no longer has to re-derive the number it waited.
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
