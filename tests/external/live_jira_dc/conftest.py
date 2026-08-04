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
import subprocess
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


def wait_for_jira_dc_ready(timeout: float | None = None) -> None:
    """Poll ``/rest/api/2/serverInfo`` until the harness answers, or fail loudly.

    Default budget is 20 minutes (overridable via ``JIRA_DC_READY_TIMEOUT``,
    seconds), polled every ~5s. On expiry raises ``RuntimeError`` naming both
    `make jira-dc-up` and `REBAR_RUN_EXTERNAL=1` — never a raw connection
    traceback.
    """
    budget = _ready_timeout() if timeout is None else timeout
    deadline = time.monotonic() + budget
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            status, _ = _request("/rest/api/2/serverInfo", timeout=5)
            if status == 200:
                return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
        time.sleep(_READY_POLL_INTERVAL_S)
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
            print(  # noqa: T201 — visible in the CI job log, where this is diagnosed
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
        print(  # noqa: T201 — visible in the CI job log
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
_REQUIRED_FIELDS = ("Epic Link", "Epic Name")


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

    status, fields = _request("/rest/api/2/field")
    if status != 200 or not isinstance(fields, list):
        raise AssertionError(
            f"PROVISIONING FAILED: could not list instance fields to verify "
            f"{list(_REQUIRED_FIELDS)} (HTTP {status}, body {fields!r}). The declared contract "
            f"is UNVERIFIED, and an unverified contract is refused rather than assumed."
        )
    present = {str(field.get("name")) for field in fields if isinstance(field, dict)}
    missing_fields = [name for name in _REQUIRED_FIELDS if name not in present]
    if missing_fields:
        raise AssertionError(
            f"PROVISIONING FAILED: this instance exposes no {missing_fields} field(s), which "
            f"Data Center requires to create an Epic ('Epic Name') and to attach a child to "
            f"one ('Epic Link'). Without them the epic-parent cells cannot run. Re-run the "
            f"capability map against the current image and update `_REQUIRED_FIELDS` if the "
            f"instance genuinely renamed them."
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


@pytest.fixture
def dc_transport(jira_dc_pat: str) -> Any:
    """A REAL ``JiraDataCenterTransport`` against the live harness.

    Builds the client directly from harness fixtures (rather than through ``load_config()``)
    so this suite does not depend on process-wide config discovery — ``allow_insecure=True``
    mirrors what a ``[tool.rebar.reconciler]`` config pointed at this loopback harness would
    need, exercised here as a direct constructor argument instead.

    LIVES IN conftest, not in a test module, because more than one module needs it: it began
    as a local fixture in ``test_transport.py``, and ``test_store_copy_isolation.py`` then
    failed at SETUP with "fixture 'dc_transport' not found" — a module-local fixture is not
    visible to siblings. Sharing it here beats copying twenty lines into each consumer.
    """
    from rebar_reconciler.adapters.jira_datacenter.settings import JiraDataCenterSettings
    from rebar_reconciler.adapters.jira_datacenter.transport import (
        JiraDataCenterTransport,
        build_client_from_settings,
    )

    settings = JiraDataCenterSettings(
        url=_BASE,
        project="",  # overridden per-test via jira_dc_project
        allow_insecure=True,
        ca_bundle="",
        resolved_statuses=frozenset({"Resolved", "Done", "Cancelled"}),
        pat=jira_dc_pat,
    )
    client = build_client_from_settings(settings)
    return JiraDataCenterTransport(client=client, project="")


# ---------------------------------------------------------------------------
# The J11 store-copy fixtures — HERE, not in a test module, because two suites need them
# ---------------------------------------------------------------------------
#
# `dc_store_copy_repo` and `bound_dc_issue` began as module-local fixtures in
# `test_store_copy_isolation.py`. `test_dc_mutations.py` needs the same two, and a
# module-local fixture is NOT visible to a sibling module — the same trap `dc_transport`
# fell into, which cost a full harness cycle and surfaced as an ERROR at setup
# ("fixture 'dc_transport' not found") rather than as a collection failure. Note that
# `pytest --collect-only` does NOT catch this: fixtures resolve at SETUP. Use
# `pytest --fixtures <module>`.


@pytest.fixture
def dc_store_copy_repo(tmp_path: Path, jira_dc_project: str, jira_dc_pat: str, monkeypatch) -> Path:
    """A fresh repo holding a SCRUBBED COPY of the project's real ticket store.

    TWO repos, mirroring the real layout, because the store IS a git repo of its own.
    `.tickets-tracker/` is gitignored by the outer checkout and lives on the orphan `tickets`
    branch, and the reconciler commits into it directly — `git -C <root>/.tickets-tracker
    commit`. A first attempt extracted the tickets into a plain directory inside a single
    outer repo, and every store write then failed with `CalledProcessError(128)` ("not a git
    repository"). So the tracker gets its own `git init` on a `tickets` branch, and the outer
    repo gitignores it exactly as a real checkout does.

    NEITHER repo gets a remote — that is the primary isolation layer — and both get a local
    committer identity, since a CI runner has no global one and `git commit` would otherwise
    fail for a second, unrelated reason.
    """
    import textwrap

    from _dc_support import (
        CLOUD_CREDENTIAL_VARS,
        INHERITED_ENV_FILE,
        is_ticket_entry,
        source_repo_root,
    )

    # SNAPSHOT THE INHERITED ENVIRONMENT FIRST, before this fixture changes any of it (bug 59b2,
    # Finding A). Once the `monkeypatch` calls below have run, `os.environ` describes the FIXTURE,
    # so a cell asserting on it can only ever confirm that the fixture ran. Recording what the JOB
    # supplied is what gives the isolation cell something it did not author to assert about.
    inherited_env = {
        name: os.environ.get(name) for name in (*CLOUD_CREDENTIAL_VARS, "REBAR_SYNC_PUSH")
    }

    source = source_repo_root()
    work = tmp_path / "dc-store-copy"
    tracker = work / ".tickets-tracker"
    tracker.mkdir(parents=True)

    def _init(repo: Path, branch: str) -> None:
        subprocess.run(["git", "init", "-q", "-b", branch], cwd=repo, check=True)
        subprocess.run(
            ["git", "config", "user.email", "harness@example.invalid"], cwd=repo, check=True
        )
        subprocess.run(["git", "config", "user.name", "rebar J11 harness"], cwd=repo, check=True)

    _init(work, "main")
    (work / ".gitignore").write_text(".tickets-tracker/\n")

    subprocess.run(
        ["git", "fetch", "origin", "tickets"], cwd=source, capture_output=True, check=True
    )
    archive = subprocess.run(
        ["git", "archive", "FETCH_HEAD"], cwd=source, capture_output=True, check=True
    ).stdout
    subprocess.run(["tar", "-x", "-C", str(tracker)], input=archive, check=True)

    # Record the expected entry set from THE SAME FETCH_HEAD the archive came from. The
    # tickets branch is LIVE (rebar auto-pushes on every write, and a concurrent agent
    # session writes to it constantly), so re-fetching at assertion time samples a DIFFERENT
    # commit and the counts differ for reasons unrelated to the extraction.
    listing = subprocess.run(
        ["git", "ls-tree", "--name-only", "FETCH_HEAD"],
        cwd=source,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.split()
    (work / ".j11-expected-entries.json").write_text(
        json.dumps(sorted(e for e in listing if is_ticket_entry(e)))
    )
    (work / INHERITED_ENV_FILE).write_text(json.dumps(inherited_env, sort_keys=True))

    # SCRUB: every binding/snapshot artifact, matched as a GLOB so a renamed sibling
    # cannot survive merely because its exact name is not enumerated.
    for path in sorted(tracker.glob(".bridge_state*")):
        subprocess.run(["rm", "-rf", str(path)], check=True)

    _init(tracker, "tickets")
    subprocess.run(["git", "add", "-A"], cwd=tracker, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--no-verify", "-m", "scrubbed store copy for J11"],
        cwd=tracker,
        check=True,
    )

    # CONVERGE THE COPY INTO A WRITABLE STORE. A store materialised from `git archive` is NOT
    # yet usable: rebar's store marker `.env-id` is the FIRST line of the tickets branch's own
    # `.gitignore`, so it is absent from the archive by construction, and `composer.edit_core`
    # refuses every library write with "ticket system not initialized". `run_ensures` is
    # rebar's idempotent ensure-registry and the sanctioned remedy (see bug d220).
    from rebar._store.ensures import run_ensures

    for _outcome in run_ensures(str(tracker)):
        pass
    assert (tracker / ".env-id").is_file(), (
        "ensure-registry did not create the store marker `.env-id`; every library write "
        "against this copy would fail with 'ticket system not initialized'"
    )

    (work / "rebar.toml").write_text(
        textwrap.dedent(f"""
        [reconciler]
        backend = "jira-datacenter"
        base_url = "{_BASE}"
        allow_insecure = true

        [jira]
        project = "{jira_dc_project}"
        """).lstrip()
    )
    monkeypatch.setenv("JIRA_PAT", jira_dc_pat)
    monkeypatch.setenv("JIRA_PROJECT", jira_dc_project)
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    # REBAR_ROOT is what a `rebar` SUBPROCESS resolves the store from. `rebar.edit_ticket(...,
    # repo_root=...)` shells out to the CLI and the child does not inherit that argument.
    monkeypatch.setenv("REBAR_ROOT", str(work))
    for cloud_var in CLOUD_CREDENTIAL_VARS:
        monkeypatch.delenv(cloud_var, raising=False)
    return work


@pytest.fixture
def bound_dc_issue(
    dc_store_copy_repo: Path, dc_transport: Any, jira_dc_project: str, track_issue: Any
):
    """A DC issue BOUND to a local ticket in the store copy — `(local_id, dc_key)`.

    Every outbound mutation except create is an UPDATE, and `outbound_differ.py:518-520`
    routes a ticket with no binding to the CREATE path instead. The scrub deliberately removes
    every binding, so without this fixture an outbound "edit" cell would CREATE a new DC issue
    and its oracle would pass against that fresh issue rather than the one it meant to change —
    green, and proving nothing.

    Both identifiers go to `--filter-local-ids`. Filtering on the local id alone does NOT work
    for the inbound leg: the filter can only derive a Jira key from an EXISTING binding, which
    is precisely what does not exist yet, while an inbound create's `target` IS the key.
    """
    from _dc_support import run_reconcile, seed_searchable_issue
    from rebar_reconciler.binding_store import load_binding_store
    from rebar_reconciler.inbound_translate import _jira_key_to_local_id

    key = seed_searchable_issue(
        dc_transport, jira_dc_project, track_issue, "rebar J11 — bound fixture"
    )
    local_id = _jira_key_to_local_id(key)

    cp = run_reconcile(dc_store_copy_repo, "bootstrap-strict", only=f"{local_id},{key}")
    assert "Traceback" not in cp.stderr, f"binding pass raised:\n{cp.stderr[-2000:]}"

    # ASSERT the binding before yielding. If this pass silently failed, every dependent cell
    # would fall back to the create path and pass for the wrong reason.
    bound = load_binding_store(dc_store_copy_repo).get_jira_key(local_id)
    assert bound == key, (
        f"the fixture did not establish a binding: get_jira_key({local_id!r}) == {bound!r}, "
        f"expected {key!r}. Every outbound UPDATE cell would silently become a CREATE.\n"
        f"stdout:\n{cp.stdout[-1500:]}"
    )
    return local_id, key


@pytest.fixture
def dc_request() -> Any:
    """The harness's authenticated raw-REST helper, exposed to test modules.

    A FIXTURE rather than a cross-module `from conftest import _request`: three different
    `conftest.py` files sit on this suite's path (`tests/`, `tests/external/`, and this one),
    so a bare `import conftest` binds whichever landed in `sys.modules` first — ambiguous by
    construction, and the kind of import that works locally and resolves differently in CI.
    """
    return _request
