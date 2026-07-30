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
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from typing import Any

import pytest

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


def _random_project_key() -> str:
    # Jira project keys: 2-10 uppercase letters/digits, must start with a letter.
    suffix = "".join(random.choices(string.ascii_uppercase, k=4))
    return f"RBJ{suffix}"


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


@pytest.fixture(scope="session", autouse=True)
def _jira_dc_harness_ready() -> None:
    """Wait for the harness before any fixture below talks REST to it.

    ``test_harness_smoke.py``'s own module-level ``_live_jira_ready()`` sentinel
    already gates collection with a quick single check, so by the time this
    session fixture runs the instance has typically already answered. This is
    the defensive, spec-mandated readiness wait (20 min default budget) for the
    case where it answered once at collection time but is still settling, or a
    caller invokes fixtures directly.
    """
    wait_for_jira_dc_ready()


@pytest.fixture
def track_issue() -> Iterator[Callable[[str], None]]:
    """Register a Jira issue key for index-independent teardown (ADR 0037 §3).

    Teardown asserts each DELETE's HTTP status, then polls the issue's direct
    ``/rest/api/2/issue/{key}`` endpoint until 404 — never search.
    """
    keys: list[str] = []

    def _track(key: str) -> None:
        keys.append(key)

    yield _track

    for key in keys:
        status, body = _request(f"/rest/api/2/issue/{key}", method="DELETE")
        assert status in (204, 200), f"deleting issue {key} failed: {status} {body}"
        _poll_until_404(f"/rest/api/2/issue/{key}", what=f"issue {key}")


@pytest.fixture
def jira_dc_project(track_issue: Callable[[str], None]) -> Iterator[str]:
    """A scratch Jira project, provisioned via REST and torn down after the test.

    Any issue created under this project should ALSO be registered with
    ``track_issue`` by the test, so it is deleted (and confirmed gone) before
    the project itself is deleted.
    """
    key = _random_project_key()
    status, created = _request(
        "/rest/api/2/project",
        method="POST",
        payload={
            "key": key,
            "name": f"rebar J5 harness scratch {key}",
            # This harness's base image is Jira SOFTWARE standalone, so a
            # software-type project (a classic, non-next-gen Kanban board) is
            # what is guaranteed licensed/available — a Core/Business template
            # is not a safe assumption on this image.
            "projectTypeKey": "software",
            "projectTemplateKey": "com.pyxis.greenhopper.jira:gh-simplified-kanban-classic",
            "lead": _ADMIN_USER,
            "description": "Scratch project from tests/external/live_jira_dc — safe to delete.",
        },
    )
    assert status == 201, f"scratch project creation failed: {status} {created}"

    yield key

    status, body = _request(f"/rest/api/2/project/{key}", method="DELETE")
    assert status in (204, 200), f"deleting scratch project {key} failed: {status} {body}"
    _poll_until_404(f"/rest/api/2/project/{key}", what=f"project {key}")


@pytest.fixture
def jira_dc_pat() -> str:
    """A Personal Access Token minted programmatically for the Bearer-auth test.

    ``POST /rest/pat/latest/tokens`` (the Jira DC 8.14+ PAT endpoint),
    authenticated with the admin basic credentials — never a hand-minted token,
    so this fixture is self-contained.
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
