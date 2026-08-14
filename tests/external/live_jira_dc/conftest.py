"""Fixtures for the live Jira Data Center harness smoke tests (story J5).

Provisions and tears down scratch resources against a REAL Jira 8.17.1 DC
instance (see ../../../tests/external/live_jira_dc/README.md for how to bring
one up). Speaks raw REST v2 with stdlib ``urllib`` only, deliberately mirroring
``test_harness_smoke.py``'s own minimal HTTP helper rather than depending on a
Jira client library this harness exists to validate.

Teardown discipline (ADR 0037 §3, "eventual-consistency discipline"): Jira's
search index lags both creates and deletes by an unbounded interval, so
teardown here NEVER queries search to confirm deletion. Instead it asserts the
DELETE call's own HTTP status, then polls the affected resource's *direct*
REST endpoint (``/rest/api/2/issue/{key}``, ``/rest/api/2/project/{key}``)
until it 404s, under its own bounded timeout distinct from the harness
readiness budget — so a genuinely stuck delete fails loudly instead of hanging
the whole suite.
"""

from __future__ import annotations

import base64
import json
import os
import random
import string
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

# The engine ships at <repo>/src/rebar/_engine and is NOT importable as
# `rebar_reconciler` unless that directory is on sys.path — the unit tier gets it
# from tests/unit/rebar_reconciler/conftest.py, which this tier does not inherit.
# Without it every test in test_transport.py dies at setup with
# `ModuleNotFoundError: No module named 'rebar_reconciler'`. That went unnoticed
# because those tests had never actually executed: they were skipping on a missing
# `[jira-datacenter]` extra, and the smoke tests in this same directory (which
# speak raw REST and import nothing from rebar) kept the job green.
#
# `tests/_engine_path.py` is the single place the layout is encoded — reuse it
# rather than re-deriving parent counts, which silently break when a file moves.
_TESTS_DIR = Path(__file__).resolve().parents[2]
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from _engine_path import engine_dir  # noqa: E402

if str(engine_dir()) not in sys.path:
    sys.path.insert(0, str(engine_dir()))

# ONE shared definition of readiness, shared with the deterministic probe
# (`scripts/jira_dc_epic_link_clear_probe.py`) — bug 9790-cafa-dffa-462e. `scripts/`
# is not a package and is not installed, so it is put on `sys.path` the same way
# `_TESTS_DIR` and `engine_dir()` above are, rather than reached by a relative
# import that only works from one working directory.
_SCRIPTS_DIR = _TESTS_DIR.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import jira_dc_field_readiness  # noqa: E402

# The J11 store-copy + DC-client fixtures live in a sibling module (ticket ccf6 —
# this file was 932 lines, past AGENTS.md's 800 hard cap). They are RE-IMPORTED
# here rather than merely defined there: pytest discovers fixtures as attributes
# of the conftest module, so without these names in this namespace every consumer
# ERRORs at SETUP with "fixture 'dc_transport' not found" — which
# `pytest --collect-only` does not catch, because fixtures resolve at setup.
#
# This directory is not a package, so it goes on sys.path the same way _TESTS_DIR,
# engine_dir() and _SCRIPTS_DIR do above, instead of relying on pytest's
# prepend-import-mode insertion (which the by-path unit-tier load does not get).
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from _dc_fixtures import (  # noqa: E402,F401 — re-exported so pytest collects them
    bound_dc_issue,
    dc_store_copy_repo,
    dc_transport,
)

_BASE = os.environ.get("JIRA_DC_BASE_URL", "http://localhost:2990/jira")
_ADMIN_USER = os.environ.get("JIRA_DC_ADMIN", "admin")
_ADMIN_PASSWORD = os.environ.get("JIRA_DC_ADMIN_PASSWORD", "admin")

# Harness readiness: cold start is dominated by atlas-run's ~917-artifact Maven
# download (see Dockerfile / README), not JVM boot, so the default budget is
# generous and deliberately overridable — an emulated arm64 host or a cold
# Maven cache can each blow past a "few minutes" default.
_DEFAULT_READY_TIMEOUT_S = 20 * 60
_READY_POLL_INTERVAL_S = 5.0

# Teardown's direct-endpoint 404 poll is bounded separately from readiness —
# a stuck delete should fail fast and loudly, not hang for 20 minutes.
_TEARDOWN_POLL_TIMEOUT_S = 60.0
_TEARDOWN_POLL_INTERVAL_S = 2.0

_NOT_READY_MESSAGE = (
    "Jira DC harness at {base} did not become ready within {timeout:.0f}s. "
    "Start it with `make jira-dc-up` (native amd64 runner strongly preferred; "
    "an emulated arm64 host cannot finish booting) and run with "
    "REBAR_RUN_EXTERNAL=1."
)


def _request(
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    token: str | None = None,
    basic_auth: tuple[str, str] | None = None,
    timeout: float = 30,
) -> tuple[int, Any]:
    """Minimal REST v2 (or /rest/pat) call. Returns ``(status, decoded_body_or_None)``.

    Mirrors ``test_harness_smoke.py``'s own ``_request`` helper deliberately —
    this harness speaks raw REST, never a Jira client library, so the fixtures
    exercise exactly what the tests exercise.
    """
    url = f"{_BASE.rstrip('/')}{path}"
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Accept", "application/json")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    else:
        user, password = basic_auth if basic_auth is not None else (_ADMIN_USER, _ADMIN_PASSWORD)
        creds = base64.b64encode(f"{user}:{password}".encode()).decode()
        req.add_header("Authorization", f"Basic {creds}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8") or ""
            return resp.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw) if raw.strip() else None
        except json.JSONDecodeError:
            return exc.code, raw


def _ready_timeout() -> float:
    raw = os.environ.get("JIRA_DC_READY_TIMEOUT")
    if raw is None or not raw.strip():
        return float(_DEFAULT_READY_TIMEOUT_S)
    return float(raw)


def _field_ready_timeout() -> float:
    """How long ``_assert_project_capabilities`` waits for the Epic custom fields.

    Separate from ``JIRA_DC_READY_TIMEOUT`` on purpose: the two waits are for
    different things and fail for different reasons. ``JIRA_DC_READY_TIMEOUT`` covers
    "Jira is not answering REST at all", which on this image is dominated by
    atlas-run's ~917-artifact Maven download and takes minutes. This one covers "the
    just-created Jira Software project has not yet had GreenHopper register its custom
    fields", measured at 0.0512s on run 30981084637, so its default is small
    (``jira_dc_field_readiness.FIELD_READY_BUDGET_S``, 120s) and an operator must be
    able to move one without moving the other.

    Note the change of ADDRESSEE since bug 9790-cafa-dffa-462e: this no longer feeds a
    session-start gate (there is nothing to wait for before a project exists — bug
    941b-f049-5f29-4410), only the post-create capability check.
    """
    raw = os.environ.get("JIRA_DC_FIELD_READY_TIMEOUT")
    if raw is None or not raw.strip():
        return float(jira_dc_field_readiness.FIELD_READY_BUDGET_S)
    return float(raw)


def _field_request(path: str) -> tuple[int, Any]:
    """Adapter handing the shared readiness module this module's own HTTP helper.

    Resolves ``_request`` from the module globals AT CALL TIME rather than
    capturing it at definition time, so a test (or a caller) that monkeypatches
    ``_request`` on this module is actually honoured.
    """
    return _request(path, timeout=15)


def wait_for_jira_dc_ready(timeout: float | None = None) -> None:
    """Wait until Jira answers REST at all, or fail loudly.

    Poll ``/rest/api/2/serverInfo`` until it answers. Default budget 20 minutes
    (overridable via ``JIRA_DC_READY_TIMEOUT``, seconds), polled every ~5s. On expiry
    raises ``RuntimeError`` naming both `make jira-dc-up` and `REBAR_RUN_EXTERNAL=1` —
    never a raw connection traceback.

    **THIS DELIBERATELY DOES NOT READ** ``/rest/api/2/field`` (bug
    941b-f049-5f29-4410). Change 9790-cafa-dffa-462e added a second stage here that
    waited for the GreenHopper custom fields ``Epic Link``/``Epic Name`` before the
    session was allowed to proceed, and that is a DEADLOCK rather than a slow path:
    GreenHopper provisions those fields when the first Jira Software PROJECT is
    created, so a session-start wait for them waits on something only the action it is
    blocking can produce. Experiment run 30981084637 measured it directly — 27 fields
    and zero ``customfield_*`` both before AND after 180 seconds of quiet, then 55
    fields with both Epic fields present 0.0512s after a project create. In production
    that gate cost run 30975323866 sixty-two ERRORS at fixture setup, and run
    30978613228 expired again after 181 identical polls despite a tripled allowance,
    while the run immediately before it landed (30964805133) had passed 62/62. No
    budget answers this question at this point in the session, so the question is not
    asked here at all.

    The capability itself is NOT unguarded: it is asserted in
    :func:`_assert_project_capabilities`, which runs immediately after
    :func:`_create_scratch_project` — the first moment at which the fields can exist.
    """
    budget = _ready_timeout() if timeout is None else timeout
    deadline = time.monotonic() + budget
    last_error: Exception | None = None
    server_info_ready = False
    while time.monotonic() < deadline:
        try:
            status, _ = _request("/rest/api/2/serverInfo", timeout=5)
            if status == 200:
                server_info_ready = True
                break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
        time.sleep(_READY_POLL_INTERVAL_S)

    if not server_info_ready:
        message = _NOT_READY_MESSAGE.format(base=_BASE, timeout=budget)
        if last_error is not None:
            message = f"{message} Last error: {last_error!r}"
        raise RuntimeError(message)


# Every scratch project this harness creates carries this prefix, which is what
# makes leftover state from an interrupted run identifiable at session start.
_PROJECT_KEY_PREFIX = "RBJ"


def _random_project_key() -> str:
    # Jira project keys: 2-10 uppercase letters/digits, must start with a letter.
    suffix = "".join(random.choices(string.ascii_uppercase, k=4))
    return f"{_PROJECT_KEY_PREFIX}{suffix}"


def _poll_until_404(path: str, *, what: str) -> None:
    """Poll a direct REST endpoint until it 404s (index-independent; ADR 0037 §3).

    Bounded by its own timeout, separate from harness readiness, so a genuinely
    stuck delete fails loudly instead of hanging the suite.
    """
    deadline = time.monotonic() + _TEARDOWN_POLL_TIMEOUT_S
    last_status = None
    while time.monotonic() < deadline:
        last_status, _ = _request(path)
        if last_status == 404:
            return
        time.sleep(_TEARDOWN_POLL_INTERVAL_S)
    raise AssertionError(
        f"{what} at {path!r} did not 404 within {_TEARDOWN_POLL_TIMEOUT_S:.0f}s "
        f"of teardown (last status {last_status!r}) — the delete may be stuck"
    )


def _leaked_scratch_projects() -> list[str]:
    """Scratch projects left behind by an earlier, interrupted run.

    Every project this harness creates is keyed ``RBJ`` + 4 letters (see
    ``_random_project_key``), and a completed run deletes its own. So any ``RBJ``
    project still present at session start is residue from a run whose teardown
    did not finish — exactly the stale state that must not silently leak forward.

    Read from ``/rest/api/2/project``, the direct project list, NOT the search
    index (ADR 0037 §3: the index lags both creates and deletes, so it could
    equally hide real residue or invent phantom residue).
    """
    status, body = _request("/rest/api/2/project")
    if status != 200 or not isinstance(body, list):
        # Cannot enumerate: do not invent a failure, but do not claim cleanliness
        # either — the readiness wait below is still authoritative for usability.
        return []
    return sorted(
        str(p.get("key"))
        for p in body
        if isinstance(p, dict) and str(p.get("key", "")).startswith(_PROJECT_KEY_PREFIX)
    )


#: Every PAT this harness mints carries this prefix (see ``jira_dc_pat``), which is
#: what makes a token left by an interrupted run identifiable — and, critically,
#: distinguishable from a HUMAN's unrelated PAT on the same account.
_PAT_NAME_PREFIX = "rebar-j5-harness-"


def _leaked_harness_tokens() -> list[dict[str, Any]]:
    """PATs left behind by an earlier, interrupted run.

    The exact analogue of :func:`_leaked_scratch_projects` for tokens, and for the
    same reason: a run whose teardown did not finish leaves residue that silently
    poisons the NEXT run. For tokens the poisoning is specific and total — Jira DC
    caps a user at 10 PATs, so leftovers consume a budget the next session needs
    and it dies at setup (see ``jira_dc_pat``).

    **Only tokens carrying** ``_PAT_NAME_PREFIX`` **are returned.** The admin
    account may legitimately hold PATs a human created, and deleting one of those
    would make this sweep worse than the bug it fixes.

    Read from ``GET /rest/pat/latest/tokens`` — the direct token list, not the
    search index. On any non-200 or unexpected shape this returns EMPTY: a sweep
    that cannot enumerate is not evidence of cleanliness, and inventing a failure
    here would block a run for a reason unrelated to the code under test. The
    mint in ``jira_dc_pat`` remains the authoritative test of whether headroom
    actually exists.
    """
    status, body = _request("/rest/pat/latest/tokens")
    if status != 200 or not isinstance(body, list):
        return []
    return [
        token
        for token in body
        if isinstance(token, dict)
        and str(token.get("name", "")).startswith(_PAT_NAME_PREFIX)
        and token.get("id") is not None
    ]


def _sweep_leaked_harness_tokens() -> list[str]:
    """Delete leftover harness PATs; return the names actually swept.

    Best-effort by design, and the asymmetry with scratch PROJECTS is deliberate:
    a leaked project makes later assertions lie, so ``_jira_dc_harness_ready``
    REFUSES to run against one. A leaked token does not corrupt any assertion —
    it only consumes budget — so the right response is to reclaim it and carry on.
    A failed DELETE is reported to stderr and skipped rather than raised: if the
    reclaim was genuinely insufficient, the mint that follows fails with Jira's
    own explicit limit error, which is a better diagnostic than anything asserted
    here.
    """
    swept: list[str] = []
    for token in _leaked_harness_tokens():
        name = str(token.get("name", ""))
        status, body = _request(f"/rest/pat/latest/tokens/{token['id']}", method="DELETE")
        if status in (200, 204, 404):
            swept.append(name)
        else:
            print(
                f"[jira-dc-harness] could not reclaim leftover PAT {name!r}: {status} {body!r}",
                file=sys.stderr,
            )
    return swept


@pytest.fixture(scope="session", autouse=True)
def _jira_dc_harness_ready() -> None:
    """Wait for the harness, then REFUSE a harness carrying leaked state.

    ``test_harness_smoke.py``'s own module-level ``_live_jira_ready()`` sentinel
    already gates collection with a quick single check, so by the time this
    session fixture runs the instance has typically already answered. This is
    the defensive, spec-mandated readiness wait (20 min default budget) for the
    case where it answered once at collection time but is still settling, or a
    caller invokes fixtures directly.

    **Then the freshness half.** ``make jira-dc-up`` passes ``--force-recreate``,
    but that only guarantees a fresh container when the harness is started THAT
    way. Nothing stops a run against a container left over from an interrupted
    session, whose teardown never completed — and stale projects/issues from a
    previous run are precisely the state that makes a later run's assertions
    lie. Rather than trying to prove provenance of the container (which
    ``--force-recreate`` cannot be observed after the fact), this asserts the
    property that actually matters: **no residue from a prior run is present.**
    On finding residue it fails loudly with the exact recovery command instead of
    silently testing against dirty state.
    """
    wait_for_jira_dc_ready()

    # Reclaim leftover PATs BEFORE anything mints one. Unlike scratch projects
    # (below), a leaked token is reclaimed rather than refused: it corrupts no
    # assertion, it only consumes the 10-token budget the session is about to
    # need. Doing it here — once, at session start — is what stops a crashed run
    # from poisoning the next one.
    swept = _sweep_leaked_harness_tokens()
    if swept:
        print(
            f"[jira-dc-harness] reclaimed {len(swept)} leftover PAT(s) from an "
            f"interrupted run: {sorted(swept)}",
            file=sys.stderr,
        )

    leaked = _leaked_scratch_projects()
    if leaked:
        raise RuntimeError(
            f"the Jira DC harness is carrying state from an interrupted previous run: "
            f"scratch project(s) {leaked} still exist. A run against dirty state can "
            f"pass or fail for reasons that have nothing to do with the code under "
            f"test, so this refuses to continue. Reset it with:\n"
            f"    make jira-dc-down && make jira-dc-up"
        )


@pytest.fixture
def track_issue() -> Iterator[Callable[[str], None]]:
    """Register a Jira issue key for index-independent teardown (ADR 0037 §3).

    Teardown asserts each DELETE's HTTP status, then polls the issue's direct
    ``/rest/api/2/issue/{key}`` endpoint until 404 — never search.

    **A 404 on the DELETE counts as success**, because the project teardown
    legitimately gets there first. ``jira_dc_project`` DEPENDS on this fixture, so
    pytest finalizes this one LAST — after the project is gone — and deleting a
    Jira project cascades to its issues. The contract this fixture owes is
    "the issue no longer exists", and 404 already satisfies it; demanding 204
    would fail on a correct cascade (observed live: ``deleting issue RBJSQZH-1
    failed: 404 Issue Does Not Exist``).

    This is not a weakened assertion: the ``_poll_until_404`` below still runs
    unconditionally, so absence is positively confirmed either way. What changed
    is only that "already absent" is accepted as a way of being absent.
    """
    keys: list[str] = []

    def _track(key: str) -> None:
        keys.append(key)

    yield _track

    for key in keys:
        status, body = _request(f"/rest/api/2/issue/{key}", method="DELETE")
        assert status in (204, 200, 404), (
            f"deleting issue {key} failed: {status} {body} — expected 204/200 "
            f"(deleted) or 404 (already gone via the project cascade)"
        )
        # Runs for every branch, INCLUDING the 404 one: the postcondition is
        # absence, confirmed against the direct endpoint, never the search index.
        _poll_until_404(f"/rest/api/2/issue/{key}", what=f"issue {key}")


# ---------------------------------------------------------------------------
# The DECLARED provisioning contract (bug 3fe5)
# ---------------------------------------------------------------------------
#
# We build and own this image, so the environment it offers is ours to DECLARE as
# data and assert once — not to rediscover on every run and hope for the best.
# The values below are the ground truth measured by a capability-map run against
# the real image (Jira DC 8.17.1): all three of its software templates yield an
# `Epic` issue type, and the instance exposes named fields `Epic Link` and
# `Epic Name`. Pinning the Scrum template makes every run provision the SAME
# project; `_assert_project_capabilities` below is what turns a future image that
# stops honouring this contract into a loud provisioning failure instead of a
# quietly weaker project.

#: The project template every scratch project is created from — the Scrum software
#: development template. Pinned rather than discovered: see
#: :func:`_create_scratch_project`.
_PROJECT_TEMPLATE = "com.pyxis.greenhopper.jira:gh-scrum-template"

#: Issue type NAMES this suite needs the scratch project to offer. `Epic` is the
#: one that actually broke (3fe5): a degraded template offered only
#: ``['Sub-task', 'Task']`` and the epic-parent cells died 35 minutes into the run.
_REQUIRED_ISSUE_TYPES = ("Task", "Sub-task", "Epic")

#: Instance field NAMES this suite needs. Both are Epic machinery: Data Center
#: requires `Epic Name` to create an Epic at all, and `Epic Link` is how a child
#: is attached to one. They are instance-wide custom fields, so they are asserted
#: against ``/rest/api/2/field`` rather than against the project.
#:
#: This IS the shared tuple, not a copy of it (bug 9790): the probe waits for the
#: same names, and two independent literals is exactly the drift that let the
#: harness and the probe disagree about what "ready" means.
_REQUIRED_FIELDS = jira_dc_field_readiness.REQUIRED_FIELDS


def _create_scratch_project(key: str) -> tuple[int, Any]:
    """Create the scratch project from the ONE pinned template. Returns ``(status, body)``.

    This deliberately makes a single create attempt and has no fallback chain.
    The earlier version tried every template discovered from the instance, then a
    bare ``software`` project, then a project with no template at all, and returned
    the first ``201`` **without asserting anything about what it got**. That is the
    defect bug 3fe5 records: on the wire a silent degrade to a template offering no
    ``Epic`` issue type is indistinguishable from a good provision, so nothing
    noticed at provisioning time and the run failed 35 minutes later, inside one
    cell, as ``SETUP FAILED: project offers no 'Epic' issue type``.

    Discovery was never the right shape for an image WE build: the environment is
    ours to declare. So the template is a constant (``_PROJECT_TEMPLATE``), the
    single create either produces exactly the declared project or fails, and
    :func:`_assert_project_capabilities` verifies the result before any test sees
    it. **Do not reintroduce the fallback** — a chain can only convert a loud,
    immediate, accurate failure into a quiet one 35 minutes downstream.

    On failure the returned body NAMES the refused template, so an image bump that
    retires the key reports what was tried rather than a bare ``400``.
    """
    payload: dict[str, Any] = {
        "key": key,
        "name": f"rebar J5 harness scratch {key}",
        "lead": _ADMIN_USER,
        "description": "Scratch project from tests/external/live_jira_dc — safe to delete.",
        "projectTypeKey": "software",
        "projectTemplateKey": _PROJECT_TEMPLATE,
    }
    status, body = _request("/rest/api/2/project", method="POST", payload=payload)
    if status == 201:
        return status, body
    return status, (
        f"{body} (pinned template {_PROJECT_TEMPLATE!r} was refused; there is no fallback "
        f"by design — if this image no longer offers that template, re-run the capability "
        f"map and update `_PROJECT_TEMPLATE` rather than adding a retry)"
    )


def _assert_project_capabilities(key: str) -> None:
    """Verify the freshly-created project actually honours the declared contract.

    The drift detector for bug 3fe5. Creating from a pinned template is necessary
    but not sufficient: a template can be present and still yield a different issue
    type set after an image bump, and Epic machinery additionally depends on
    instance-wide custom fields the template says nothing about. So the contract is
    checked against the provisioned reality, once, at provisioning time — where the
    failure is attributable — instead of being discovered by whichever test cell
    happens to need ``Epic`` half an hour later.

    Raises ``AssertionError`` naming what was required, what was found, and what to
    change. Note that an UNVERIFIABLE contract raises too: if the project or field
    read fails, this must not pass vacuously, because "we could not check" is
    exactly the state that let the original degrade through.
    """
    status, body = _request(f"/rest/api/2/project/{key}")
    if status != 200 or not isinstance(body, dict):
        raise AssertionError(
            f"PROVISIONING FAILED: could not read back scratch project {key} to verify its "
            f"capabilities (HTTP {status}, body {body!r}). The declared contract "
            f"(issue types {list(_REQUIRED_ISSUE_TYPES)}, fields {list(_REQUIRED_FIELDS)}) is "
            f"therefore UNVERIFIED, and an unverified contract is refused rather than assumed."
        )

    offered = sorted(
        str(issue_type.get("name"))
        for issue_type in (body.get("issueTypes") or [])
        if isinstance(issue_type, dict)
    )
    missing_types = [name for name in _REQUIRED_ISSUE_TYPES if name not in offered]
    if missing_types:
        raise AssertionError(
            f"PROVISIONING FAILED: scratch project {key}, created from the pinned template "
            f"{_PROJECT_TEMPLATE!r}, offers no {missing_types} issue type(s). It offers "
            f"{offered}. This is the 3fe5 degrade: the image no longer yields the declared "
            f"environment. Re-run the capability map against the current image and update "
            f"`_PROJECT_TEMPLATE` / `_REQUIRED_ISSUE_TYPES` — do not add a fallback template."
        )

    # THE ONLY PLACE THE EPIC FIELDS ARE WAITED FOR (bug 941b-f049-5f29-4410), and it is
    # here because here is the first instant at which they CAN exist: GreenHopper registers
    # `Epic Link`/`Epic Name` when the first Jira Software project is created, so this call
    # sits directly downstream of `_create_scratch_project`'s 201. Bug 9790 put an identical
    # wait at session start as well; that one could never be satisfied (run 30981084637:
    # zero customfield_* before the create, both fields 0.0512s after it) and it errored all
    # 62 cells at setup in run 30975323866. It is gone; this is what survives it.
    #
    # A bounded WAIT rather than a single read, for one measured reason: provisioning took
    # 0.0512s on run 30981084637 — fast, but not atomic with the create's HTTP response, so
    # a one-shot read immediately after the 201 can lose that race and report a healthy
    # image as broken. The loop re-reads `/rest/api/2/field` on the poll cadence until the
    # names appear.
    #
    # It gets the FULL allowance (`_field_ready_timeout()`, default 120s), not a capped
    # confirmation window. The old `min(..., _CAPABILITY_FIELD_CONFIRM_BUDGET_S)` cap was
    # justified by "readiness already waited out the full field budget at session start, so
    # this is a grace of one extra poll cycle" — a premise that died with the session-start
    # wait. Nothing has looked for these fields before this line, so nothing entitles this
    # line to a discount.
    #
    # An unusable read is treated as NOT-READY (`missing_required_fields` returns every name
    # for a non-200 / non-list body), preserving the rule that an UNVERIFIED contract is
    # refused rather than assumed — "we could not check" is exactly the state that let the
    # original 3fe5 degrade through.
    field_budget = _field_ready_timeout()
    readiness = jira_dc_field_readiness.await_required_fields(
        _field_request,
        names=_REQUIRED_FIELDS,
        budget=field_budget,
    )
    if not readiness.ready:
        # AssertionError, not RuntimeError: this is a provisioning-contract failure and
        # the tier's callers key off that type.
        raise AssertionError(
            f"PROVISIONING FAILED: scratch project {key} cannot be used for the epic-parent "
            f"cells — Data Center requires 'Epic Name' to create an Epic at all and "
            f"'Epic Link' to attach a child to one. "
            + jira_dc_field_readiness.not_ready_message(
                readiness, base_url=_BASE, budget=field_budget
            )
            + " If the inventory shows the names genuinely changed, re-run the capability "
            "map against the current image and update `_REQUIRED_FIELDS`."
        )

    # RECORD THE SUCCESS, not only the failure (bug 941b-f049-5f29-4410). This wait used to
    # be silent when it worked, so every green run discarded the one measurement anyone could
    # size it from and only expiries ever spoke — and an expiry can only argue "make it
    # bigger", which is how run 30978613228's allowance reached 1800s without fixing a thing.
    # `print` is the right channel here specifically because the harness job runs pytest with
    # `-rA` (see .github/workflows/external-integration.yml), which keeps captured stdout of
    # PASSING tests in the log; under plain `-q` this line would be written where nobody can
    # read it. The probe emits the same sentence through its own logger.
    print(
        f"[941b-field-readiness] project {key}: "
        + jira_dc_field_readiness.ready_message(readiness, base_url=_BASE)
    )

    # ISSUE-TYPE NAME UNIQUENESS, within the provisioned project (bug 2e47-ae62-c0cf-48a0).
    #
    # The capability map found TWO instance-wide issue types both named `Task` (ids 10003 and
    # 10004), which makes any name-based type resolution ambiguous — and `LOCAL_TYPE_TO_JIRA` maps
    # by NAME. The question the map could not answer is whether that ambiguity can actually reach
    # rebar: a create posts `issuetype: {"name": ...}` scoped to a PROJECT, so what matters is
    # whether the project's own issue-type scheme exposes the name more than once.
    #
    # Asserted here rather than in a cell because this is a property of the provisioned
    # environment, and because a future image that starts exposing both `Task`s to the project
    # should fail LOUDLY at provisioning — where the diagnosis is one HTTP response away — instead
    # of non-deterministically binding creates to the wrong type much later.
    #
    # The instance-wide duplicates are REPORTED, not asserted: they exist and are documented, and
    # failing on them would refuse a project that is perfectly usable.
    offered_names = [
        str(issue_type.get("name"))
        for issue_type in (body.get("issueTypes") or [])
        if isinstance(issue_type, dict)
    ]
    ambiguous = sorted({name for name in offered_names if offered_names.count(name) > 1})
    # The instance-wide issue-type inventory, for the record. This reads
    # ``/rest/api/2/issuetype`` — NOT the field inventory. The first version of this filtered
    # ``fields`` (which is ``/rest/api/2/field``) against issue-type NAMES, so it could never
    # match and the evidence line silently never printed: a broken record that looked like a
    # clean one, which is the same class of defect as bug 59b2's vacuous assertions.
    it_status, all_types = _request("/rest/api/2/issuetype")
    instance_wide = (
        [
            (str(t.get("id")), str(t.get("name")))
            for t in all_types
            if isinstance(t, dict) and str(t.get("name")) in set(_REQUIRED_ISSUE_TYPES)
        ]
        if it_status == 200 and isinstance(all_types, list)
        else []
    )
    # Printed UNCONDITIONALLY, including the empty case: "no instance-wide duplicates found" is
    # itself the evidence 2e47's criterion asks for, and a conditional print cannot distinguish
    # "nothing to report" from "the query failed".
    print(
        f"[2e47-issue-type-evidence] project {key} offers {sorted(set(offered_names))}; "
        f"instance-wide entries matching {list(_REQUIRED_ISSUE_TYPES)} "
        f"(GET /rest/api/2/issuetype -> HTTP {it_status}): {instance_wide}"
    )
    if ambiguous:
        raise AssertionError(
            f"PROVISIONING FAILED: scratch project {key} offers the issue-type name(s) "
            f"{ambiguous} MORE THAN ONCE ({offered_names}). rebar's `LOCAL_TYPE_TO_JIRA` resolves "
            f"issue types by NAME, so a create could bind to either one non-deterministically. "
            f"This is bug 2e47's duplicate-`Task` ambiguity actually reaching the project scheme; "
            f"resolve types by ID before running against this image."
        )


@pytest.fixture
def jira_dc_project(track_issue: Callable[[str], None]) -> Iterator[str]:
    """A scratch Jira project, provisioned via REST and torn down after the test.

    Any issue created under this project should ALSO be registered with
    ``track_issue`` by the test, so it is deleted (and confirmed gone) before
    the project itself is deleted.

    The capability assertion runs BEFORE the yield, and that ordering is the whole
    fix for bug 3fe5: a project that does not honour the declared contract must
    abort provisioning here — where the diagnosis is one HTTP status away — rather
    than be handed to tests that will fail confusingly, and much later, for a
    reason that is not about the code under test.
    """
    key = _random_project_key()
    status, created = _create_scratch_project(key)
    assert status == 201, f"scratch project creation failed: {status} {created}"
    _assert_project_capabilities(key)

    yield key

    status, body = _request(f"/rest/api/2/project/{key}", method="DELETE")
    assert status in (204, 200), f"deleting scratch project {key} failed: {status} {body}"
    _poll_until_404(f"/rest/api/2/project/{key}", what=f"project {key}")


@pytest.fixture
def scratch_projects(track_issue: Callable[[str], None]) -> Iterator[dict[str, str]]:
    """FOUR distinct scratch Jira projects for the multi-project bridge rehearsal.

    The four-project analogue of :func:`jira_dc_project`, for the same reason: the
    many-to-many rehearsal (``test_multi_project_rehearsal.py``) drives the M2M bridge
    against several REAL DC projects at once.
    Reuses the in-conftest provisioning helpers IN PLACE — ``_random_project_key``,
    ``_create_scratch_project``, ``_assert_project_capabilities`` and ``_poll_until_404``
    — and deliberately does NOT relocate or re-implement any of them. Those helpers plus
    ``_request`` are pinned to conftest by ticket ccf6 and
    ``tests/unit/test_live_jira_dc_conftest.py`` (unit tests
    ``monkeypatch.setattr(harness, "_request", …)`` in conftest's namespace, which only
    reaches callers defined here); this fixture merely ADDS a new consumer of them.

    The four keys are guaranteed DISTINCT, each is capability-checked BEFORE the yield
    exactly like ``jira_dc_project``, and ALL four are torn down on EVERY exit path (the
    DELETE asserts 204/200/404, then ``_poll_until_404`` confirms absence, ADR 0037 §3;
    deleting a project cascades to its issues). Yields a role→key mapping.
    """
    mapping: dict[str, str] = {}
    keys: list[str] = []
    try:
        for role in ("one", "two", "zero", "legacy"):
            key = _random_project_key()
            while key in keys:
                key = _random_project_key()
            status, created = _create_scratch_project(key)
            assert status == 201, f"scratch project creation failed: {status} {created}"
            _assert_project_capabilities(key)
            keys.append(key)
            mapping[role] = key
        yield mapping
    finally:
        for key in keys:
            status, body = _request(f"/rest/api/2/project/{key}", method="DELETE")
            assert status in (204, 200, 404), (
                f"deleting scratch project {key} failed: {status} {body}"
            )
            _poll_until_404(f"/rest/api/2/project/{key}", what=f"project {key}")


@pytest.fixture(scope="session")
def jira_dc_pat() -> str:
    """A Personal Access Token minted programmatically for the Bearer-auth test.

    ``POST /rest/pat/latest/tokens`` (the Jira DC 8.14+ PAT endpoint),
    authenticated with the admin basic credentials — never a hand-minted token,
    so this fixture is self-contained.

    **SESSION-scoped, and that is load-bearing rather than an optimisation.**
    Jira Data Center caps a user at **10 Personal Access Tokens** — a
    non-obvious limit with no analogue on Cloud. This fixture was function-scoped
    and minted a NEW token per requesting test without ever deleting it, so
    consumption grew as O(tests) against a fixed budget of 10 and the suite died
    at SETUP once the eleventh test asked:

        UserTokenLimitExceededException: You can't create more than 10 tokens.

    Two properties made that expensive to diagnose, and both argue for fixing the
    scope rather than trimming the test count. It MIS-ATTRIBUTES: the error
    surfaces at setup of whichever test happens to run once the budget is spent —
    observed twice in ``test_transport.py``, a module nobody had touched. And it
    LOOKS FLAKY while being deterministic: tokens are minted with
    ``expirationDuration=1``, so they self-clear after a day and whether a run
    fails depends on how recently the instance was used.

    Session scope makes consumption O(1): one token per run regardless of how
    many tests request it, which is what supplies headroom as this tier grows.
    Every consumer needs only *a* valid bearer credential — no test here asserts
    per-token behaviour — so one token is behaviourally identical to one per
    test. A future test that genuinely needs a DISTINCT token (revocation,
    expiry) should get its own fixture rather than widening this one back out.
    """
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    name = f"rebar-j5-harness-{suffix}"
    status, created = _request(
        "/rest/pat/latest/tokens",
        method="POST",
        payload={"name": name, "expirationDuration": 1},
    )
    assert status in (200, 201), f"PAT creation failed: {status} {created}"
    assert created is not None and created.get("rawToken"), f"PAT missing rawToken: {created}"
    return str(created["rawToken"])


@pytest.fixture(scope="session")
def jira_dc_base_url() -> str:
    """The harness base URL, handed to the fixtures in ``_dc_fixtures``.

    Those fixtures moved to a sibling module (ticket ccf6) and need this value.
    Threading it through the fixture graph keeps ``_BASE`` — and its
    ``JIRA_DC_BASE_URL`` env default — defined exactly once; a second copy over
    there would be free to drift from this one without anything noticing.
    """
    return _BASE


@pytest.fixture
def dc_request() -> Any:
    """The harness's authenticated raw-REST helper, exposed to test modules.

    A FIXTURE rather than a cross-module `from conftest import _request`: three different
    `conftest.py` files sit on this suite's path (`tests/`, `tests/external/`, and this one),
    so a bare `import conftest` binds whichever landed in `sys.modules` first — ambiguous by
    construction, and the kind of import that works locally and resolves differently in CI.
    """
    return _request
