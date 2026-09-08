"""Fixtures for live Jira Data Center 8.17.1 harness tests.

Provisioning and teardown use raw REST v2 through stdlib ``urllib``, independent of the Jira
client under test. Because Jira search is eventually consistent, teardown validates DELETE
status and polls each issue or project by its direct endpoint until 404, with a bounded budget
separate from readiness.
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

# Share field readiness with the deterministic probe. ``scripts/`` is not an installed package,
# so expose it explicitly rather than relying on the working directory.
_SCRIPTS_DIR = _TESTS_DIR.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import jira_dc_field_readiness  # noqa: E402

# Re-export sibling J11 fixtures so pytest resolves them as conftest attributes at setup.
# Add this non-package directory explicitly because by-path unit loading lacks pytest's usual
# prepend-import path behavior.
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
    """Return the post-project budget for GreenHopper's Epic-field registration.

    Keep it independent from server REST readiness, which covers Maven-heavy startup and a
    different failure. Epic fields cannot exist before the first software project, so this
    timeout applies only to the post-create capability check.
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
    """Wait for Jira's server-info REST endpoint or fail with harness-start guidance.

    The default readiness budget is configurable via ``JIRA_DC_READY_TIMEOUT``. Do not wait
    for Epic fields here: GreenHopper creates them only after the first software project, so a
    session-start field gate deadlocks on work it prevents. ``_assert_project_capabilities``
    checks them immediately after project creation instead.
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
    """List ``RBJ`` scratch projects left by interrupted teardown.

    Read the direct project endpoint rather than the lagging search index, which could hide
    real residue or report already-deleted projects.
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
    """List prefixed harness PATs left by interrupted runs.

    Leftovers consume Jira DC's ten-token user limit. Restricting the direct token-list result
    to ``_PAT_NAME_PREFIX`` protects human-created PATs. An unreadable or malformed list yields
    no sweep candidates; the subsequent mint remains the authoritative headroom check.
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
    """Best-effort delete leaked harness PATs and return successfully swept names.

    Unlike a dirty project, a token cannot falsify assertions; it only consumes mint budget.
    Report failed deletes and let the following mint provide Jira's authoritative limit error.
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
    """Wait for Jira, reclaim leaked PATs, and refuse stale scratch projects.

    The full readiness wait protects direct fixture callers after collection's quick probe.
    Container provenance cannot prove cleanliness, so the fixture checks the observable
    invariant: no interrupted-run project residue that could falsify later assertions.
    """
    wait_for_jira_dc_ready()

    # Reclaim leaked PATs once, before minting; they consume the ten-token budget but do not
    # corrupt assertions as stale projects do.
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
    """Register issue keys for index-independent teardown.

    Accept DELETE 404 because project teardown may have cascaded first, then always poll the
    direct issue endpoint to prove absence. Search is never a teardown oracle.
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


# Declare the owned image's measured provisioning contract: one pinned Scrum template, required
# issue types, and Epic fields. Capability checks turn image drift into an immediate failure.

#: The project template every scratch project is created from — the Scrum software
#: development template. Pinned rather than discovered: see
#: :func:`_create_scratch_project`.
_PROJECT_TEMPLATE = "com.pyxis.greenhopper.jira:gh-scrum-template"

#: Issue type NAMES this suite needs the scratch project to offer. `Epic` is the
#: one that actually broke (3fe5): a degraded template offered only
#: ``['Sub-task', 'Task']`` and the epic-parent cells died 35 minutes into the run.
_REQUIRED_ISSUE_TYPES = ("Task", "Sub-task", "Epic")

#: Shared instance-wide Epic field names: ``Epic Name`` creates an Epic and ``Epic Link`` attaches
#: a child. Both the harness and probe consume this tuple and validate it via the field endpoint.
_REQUIRED_FIELDS = jira_dc_field_readiness.REQUIRED_FIELDS


def _create_scratch_project(key: str) -> tuple[int, Any]:
    """Create once from the pinned template and return ``(status, body)``.

    Do not add fallback templates: accepting the first successful but weaker project defers a
    missing-Epic failure deep into the run. The capability check validates the declared result,
    and failures name the refused template for image-drift diagnosis.
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
    """Verify the new project against declared issue-type and Epic-field capabilities.

    A pinned template can drift after an image update, so validate provisioned reality before
    tests consume it. Missing and unreadable capabilities both raise with expected and observed
    evidence; inability to check must not pass as conformity.
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

    # This is the sole Epic-field wait and follows the first software-project create—the first
    # time GreenHopper can register them. Poll for the full field budget because registration is
    # not atomic with the 201 response; unreadable inventories remain not-ready, not assumed valid.
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

    # Record successful readiness so green ``pytest -rA`` logs retain timing evidence for sizing
    # the budget; failures alone can only suggest making it larger.
    print(
        f"[941b-field-readiness] project {key}: "
        + jira_dc_field_readiness.ready_message(readiness, base_url=_BASE)
    )

    # Require issue-type names to be unique within this project's scheme because creates resolve
    # by name. Report known instance-wide duplicates, but fail only if project-scoped ambiguity can
    # reach rebar; detect that during provisioning rather than a later nondeterministic create.
    offered_names = [
        str(issue_type.get("name"))
        for issue_type in (body.get("issueTypes") or [])
        if isinstance(issue_type, dict)
    ]
    ambiguous = sorted({name for name in offered_names if offered_names.count(name) > 1})
    # Record instance-wide types from the issue-type endpoint; filtering the field inventory by
    # type names would silently produce empty evidence.
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
    """Yield a capability-checked scratch project and tear it down by direct REST.

    Tests also register created issues with ``track_issue``. Validate capabilities before the
    yield so a weaker project fails during provisioning rather than inside a later test.
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
    """Yield four distinct capability-checked projects for the many-to-many rehearsal.

    Reuse conftest's monkeypatchable provisioning and direct-teardown helpers in place. Every
    exit path deletes all four projects, accepts already-cascaded absence, and confirms 404;
    the yielded mapping associates each rehearsal role with its key.
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
    """Mint one self-contained admin PAT for all Bearer-auth tests in the session.

    Session scope is required: Jira DC limits a user to 10 PATs, so per-test minting exhausts
    the fixed budget and misattributes setup failure to the eleventh consumer. Current tests
    need only a valid credential; revocation or expiry tests should use a distinct fixture.
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
    """Provide sibling fixtures the single-sourced harness URL and environment default."""
    return _BASE


@pytest.fixture
def dc_request() -> Any:
    """Expose authenticated raw REST without an ambiguous cross-module ``conftest`` import.

    Several conftest modules share the import path, so a bare import depends on ``sys.modules``
    order and can resolve differently in CI.
    """
    return _request
