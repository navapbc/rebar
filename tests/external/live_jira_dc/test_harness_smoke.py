"""Smoke-test the Dockerized Jira Data Center harness (story J5, epic e369).

Raw REST v2 proves startup, Server/DC deployment and version, PAT authentication
contrast, and cleanup without depending on rebar's backend. External opt-in still
applies; ``_live_jira_ready`` enrolls the all-skip canary, while an absent harness
skips so Dockerless external CI remains valid.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

import pytest

_BASE = os.environ.get("JIRA_DC_BASE_URL", "http://localhost:2990/jira")
_ADMIN = (
    os.environ.get("JIRA_DC_ADMIN", "admin"),
    os.environ.get("JIRA_DC_ADMIN_PASSWORD", "admin"),
)


def _request(
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    token: str | None = None,
    timeout: int = 30,
) -> tuple[int, Any]:
    """Minimal REST v2 call. Returns ``(status, decoded_body_or_None)``.

    Uses basic auth by default; passes a Bearer token instead when ``token`` is
    given, which is how the PAT path is exercised.
    """
    import base64

    url = f"{_BASE.rstrip('/')}{path}"
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Accept", "application/json")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    else:
        creds = base64.b64encode(f"{_ADMIN[0]}:{_ADMIN[1]}".encode()).decode()
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


def _live_jira_ready() -> bool:
    """The sentinel ``tests/external/conftest.py`` keys on to apply ``jira_live``.

    Also the readiness predicate: True only when the harness answers REST.
    """
    try:
        status, _ = _request("/rest/api/2/serverInfo", timeout=5)
    except (urllib.error.URLError, TimeoutError, OSError):
        return False
    return status == 200


_skip = pytest.mark.skipif(
    not _live_jira_ready(),
    reason=(
        "Jira DC harness not reachable at "
        f"{_BASE} — start it with `make jira-dc-up` (native amd64 runner strongly "
        "preferred; an emulated arm64 host cannot finish booting) and run with "
        "REBAR_RUN_EXTERNAL=1"
    ),
)


# ---------------------------------------------------------------------------
# The harness is a real Jira Data Center of the expected generation
# ---------------------------------------------------------------------------


@_skip
def test_instance_is_a_server_deployment_at_the_pinned_version() -> None:
    """Not merely "something answered": assert it is a SERVER/DC deployment (not
    Cloud) at 8.x, because the whole point is exercising DC's REST v2 semantics.

    Also guards the version floor that makes the PAT test meaningful: Personal
    Access Tokens arrived in Jira 8.14, so a lower version would make the auth
    path below silently untestable.
    """
    status, info = _request("/rest/api/2/serverInfo")

    assert status == 200, f"serverInfo returned {status}"
    assert info is not None
    version = str(info.get("version", ""))
    assert version.startswith("8."), f"expected a Jira 8.x instance, got {version!r}"
    major, minor = (int(p) for p in version.split(".")[:2])
    assert (major, minor) >= (8, 14), (
        f"Jira {version} predates 8.14, which introduced Personal Access Tokens — "
        f"the PAT bearer-auth path could not be exercised for real"
    )
    # deploymentType is 'Server' for DC/Server and 'Cloud' for Cloud.
    assert info.get("deploymentType") != "Cloud", (
        "this harness must be a Server/Data Center deployment; a Cloud instance "
        "would exercise v3 + ADF, not the v2 + wiki-markup path DC needs"
    )


# ---------------------------------------------------------------------------
# Round-trip through raw REST v2
# ---------------------------------------------------------------------------


@_skip
def test_issue_created_via_rest_v2_reads_back_with_the_same_fields(
    jira_dc_project: str, track_issue: Any
) -> None:
    """The core usability proof: create an issue and read it back.

    Asserts the round-tripped VALUES, not merely that a key came back — a create
    that silently dropped the summary would still return 201.
    """
    summary = "rebar J5 harness smoke — round trip"
    status, created = _request(
        "/rest/api/2/issue",
        method="POST",
        payload={
            "fields": {
                "project": {"key": jira_dc_project},
                "summary": summary,
                "description": "Plain text, because DC REST v2 carries wiki markup not ADF.",
                "issuetype": {"name": "Task"},
            }
        },
    )
    assert status == 201, f"create failed: {status} {created}"
    key = created["key"]
    track_issue(key)

    read_status, fetched = _request(f"/rest/api/2/issue/{key}")
    assert read_status == 200
    assert fetched["fields"]["summary"] == summary
    # v2 carries the description as a plain STRING; v3/Cloud would nest an ADF doc.
    assert isinstance(fetched["fields"]["description"], str), (
        "DC REST v2 must return description as plain text — a dict would mean we are "
        "talking to an ADF (Cloud) deployment"
    )


# ---------------------------------------------------------------------------
# PAT bearer auth — the DC auth mode the transport will use
# ---------------------------------------------------------------------------


@_skip
def test_personal_access_token_authenticates_via_bearer(jira_dc_pat: str) -> None:
    """DC uses PAT bearer auth where Cloud uses the ACLI subprocess. The token is
    minted programmatically by the fixture (POST /rest/pat/latest/tokens), so this
    proves the real auth mode rather than assuming it."""
    status, info = _request("/rest/api/2/myself", token=jira_dc_pat)

    assert status == 200, f"Bearer auth rejected with {status}: {info}"
    assert info is not None
    # DC identifies users by `name`; Cloud has only an opaque accountId.
    assert info.get("name"), "DC /myself must carry a `name` — that is DC's user identity"


@_skip
def test_a_bogus_bearer_token_is_rejected() -> None:
    """Contrast case: proves the previous test passed because the PAT is VALID, not
    because the endpoint accepts anything (or ignores the header and falls back to
    an authenticated session)."""
    status, _ = _request("/rest/api/2/myself", token="not-a-real-token")

    assert status in (401, 403), f"a bogus bearer token must be rejected, got {status}"
