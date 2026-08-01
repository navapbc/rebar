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


def _discover_project_templates() -> list[str]:
    """Ask the instance which project templates it actually has.

    Hardcoding a template key does not survive contact with a real instance: the
    first attempt used ``com.pyxis.greenhopper.jira:gh-simplified-kanban-classic``
    — a plausible, widely-cited Software key — and this image rejected it with
    ``400 The project template specified does not exist``. Which templates exist
    depends on the bundled applications and version, so the instance is the only
    authority. Discovery also means a future image bump does not silently break
    the fixture on a key that quietly disappeared.

    Returns an empty list if the endpoint is unavailable, in which case the caller
    falls back to creating without a template.
    """
    status, body = _request("/rest/project-templates/latest/templates")
    if status != 200 or not isinstance(body, dict):
        return []
    keys: list[str] = []
    for group in body.values():
        if not isinstance(group, list):
            continue
        for template in group:
            if isinstance(template, dict) and template.get("projectTemplateModuleCompleteKey"):
                keys.append(str(template["projectTemplateModuleCompleteKey"]))
    return keys


def _create_scratch_project(key: str) -> tuple[int, Any]:
    """Create the scratch project, trying discovered templates then no template.

    Returns the first ``201`` outcome, or the LAST failure with every attempt
    named — so a future breakage reports what was tried rather than just "400".
    """
    base = {
        "key": key,
        "name": f"rebar J5 harness scratch {key}",
        "lead": _ADMIN_USER,
        "description": "Scratch project from tests/external/live_jira_dc — safe to delete.",
    }
    # Discovered templates first (most specific), then a bare software project,
    # then a bare project with no type at all — each is a real create attempt, so
    # whichever the instance supports wins without us having to know in advance.
    candidates: list[dict[str, Any]] = [
        {**base, "projectTypeKey": "software", "projectTemplateKey": template}
        for template in _discover_project_templates()
    ]
    candidates.append({**base, "projectTypeKey": "software"})
    candidates.append(dict(base))

    attempts: list[str] = []
    status, body = 0, None
    for payload in candidates:
        status, body = _request("/rest/api/2/project", method="POST", payload=payload)
        if status == 201:
            return status, body
        attempts.append(f"{payload.get('projectTemplateKey', '<no template>')} -> {status}")
    return status, f"{body} (tried: {'; '.join(attempts)})"


@pytest.fixture
def jira_dc_project(track_issue: Callable[[str], None]) -> Iterator[str]:
    """A scratch Jira project, provisioned via REST and torn down after the test.

    Any issue created under this project should ALSO be registered with
    ``track_issue`` by the test, so it is deleted (and confirmed gone) before
    the project itself is deleted.
    """
    key = _random_project_key()
    status, created = _create_scratch_project(key)
    assert status == 201, f"scratch project creation failed: {status} {created}"

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
