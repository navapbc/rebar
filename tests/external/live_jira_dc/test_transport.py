"""Exercise the real Data Center transport against Jira 8.17.1 (J6, epic e369).

Live operations share the unit tier's raw-dict shape contract, catching leaked
``jira.Issue`` objects. ``_live_jira_ready`` enrolls the all-skip canary: an absent
harness skips, but a reachable harness without the DC extra fails loudly. Loopback
tests set ``allow_insecure=true`` to exercise, not bypass, TLS validation.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from typing import Any

import pytest
from _dc_support import derive_rename_target
from _jira_shape_contract import (
    assert_comment_map_shape,
    assert_issuelinks_map_shape,
    assert_search_shape,
)

_BASE = os.environ.get("JIRA_DC_BASE_URL", "http://localhost:2990/jira")
# Mirrors conftest.py's own constant: the harness image's built-in admin account,
# which is the one user guaranteed to exist for the live user-search assertions.
_ADMIN_USER = os.environ.get("JIRA_DC_ADMIN", "admin")


def _live_jira_ready() -> bool:
    """The sentinel ``tests/external/conftest.py`` keys on to apply ``jira_live``
    (enrolling this module in the all-skip canary) — also the readiness predicate
    for the ``skipif`` below."""
    try:
        req = urllib.request.Request(f"{_BASE.rstrip('/')}/rest/api/2/serverInfo")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _jira_extra_installed() -> bool:
    try:
        import jira  # noqa: F401
    except ImportError:
        return False
    return True


_skip = pytest.mark.skipif(
    not _live_jira_ready(),
    reason=(
        "Jira DC harness not reachable at "
        f"{_BASE} — start it with `make jira-dc-up` and run with REBAR_RUN_EXTERNAL=1"
    ),
)
# The extra may be absent only with no harness. When Jira is reachable, fail rather than let
# smoke tests mask an all-skipped transport acceptance module.
_extra_missing_but_harness_up = _live_jira_ready() and not _jira_extra_installed()

_skip_no_extra = pytest.mark.skipif(
    not _jira_extra_installed() and not _extra_missing_but_harness_up,
    reason="the 'jira-datacenter' extra (pycontribs/jira) is not installed — "
    "pip install 'nava-rebar[jira-datacenter]'",
)


@pytest.fixture(autouse=True)
def _fail_if_extra_missing_while_harness_is_up() -> None:
    """Turn "harness reachable but extra absent" into a LOUD failure.

    Without this, that combination silently skips every test below and the job
    reports green — the exact false-negative the external tier exists to prevent.
    """
    if _extra_missing_but_harness_up:
        pytest.fail(
            "the Jira DC harness is reachable at "
            f"{_BASE} but the 'jira-datacenter' extra (pycontribs/jira) is NOT "
            "installed, so the DC transport tests would silently skip and this run "
            "would report green having validated nothing. Install it with: "
            "pip install -e '.[dev,jira-datacenter]'"
        )


@_skip
@_skip_no_extra
def test_create_get_update_transition_roundtrip(
    dc_transport: Any, jira_dc_project: str, track_issue: Any
) -> None:
    """create -> read -> update -> transition-by-name, each asserted against the
    raw shape contract and its observable postcondition on the server."""
    dc_transport.project = jira_dc_project

    created = dc_transport.create_issue(
        {"summary": "rebar J6 live — roundtrip", "issuetype": "Task"}
    )
    assert isinstance(created, dict)
    key = created["key"]
    track_issue(key)

    fetched = dc_transport.get_issue(key)
    assert isinstance(fetched, dict)
    assert fetched["key"] == key
    assert fetched["fields"]["summary"] == "rebar J6 live — roundtrip"
    assert isinstance(fetched["fields"].get("description"), (str, type(None))), (
        "DC descriptions must be plain text, never an ADF dict"
    )

    updated = dc_transport.update_issue(key, summary="rebar J6 live — updated")
    assert updated["fields"]["summary"] == "rebar J6 live — updated"

    # A transition's NAME is not its destination STATUS name: Jira's classic
    # workflow offers "Start Progress" -> status "In Progress", "Resolve Issue" ->
    # "Resolved". Asserting the status equals the transition name therefore fails
    # against a real instance. The transitions payload declares the destination in
    # `to.name`, so drive by name (the transport's contract) and assert the
    # postcondition against the destination the server itself declared.
    transitions = dc_transport._client.transitions(key)
    target = next(
        (
            t
            for t in transitions
            if isinstance(t, dict)
            and t.get("name")
            and isinstance(t.get("to"), dict)
            and t["to"].get("name")
        ),
        None,
    )
    assert target is not None, (
        f"no transition declaring a destination status is available for {key}; got {transitions!r}"
    )
    dc_transport.transition_issue_by_name(key, target["name"])
    after = dc_transport.get_issue(key)
    assert after["fields"]["status"]["name"] == target["to"]["name"]


@_skip
@_skip_no_extra
def test_transition_to_an_unavailable_name_raises(
    dc_transport: Any, jira_dc_project: str, track_issue: Any
) -> None:
    """A transition name the workflow does not offer raises rather than silently
    no-oping."""
    dc_transport.project = jira_dc_project
    created = dc_transport.create_issue(
        {"summary": "rebar J6 live — bad transition", "issuetype": "Task"}
    )
    key = created["key"]
    track_issue(key)

    with pytest.raises(ValueError):
        dc_transport.transition_issue_by_name(key, "definitely-not-a-real-status-name")


@_skip
@_skip_no_extra
def test_comment_and_search_shapes_match_the_shared_contract(
    dc_transport: Any, jira_dc_project: str, track_issue: Any
) -> None:
    dc_transport.project = jira_dc_project
    created = dc_transport.create_issue({"summary": "rebar J6 live — comment", "issuetype": "Task"})
    key = created["key"]
    track_issue(key)

    comment = dc_transport.add_comment(key, "a live comment")
    assert isinstance(comment, dict)

    assert_search_shape(dc_transport.search_issues(f"project = {jira_dc_project}"))
    assert_comment_map_shape(dc_transport.get_comment_map(jira_dc_project))


@_skip
@_skip_no_extra
def test_name_identity_user_search_resolves_a_real_user_authoritatively(
    dc_transport: Any,
) -> None:
    """Prove ``NameIdentity`` resolves a real user authoritatively via live search.

    A missing or non-authoritative resolver would re-emit assignee changes forever; a fake
    cannot validate DC's ``user/search`` endpoint.
    """
    from rebar_reconciler.adapters.jira_datacenter.backend import _search_users_by_username
    from rebar_reconciler.adapters.jira_family.identity_model import NameIdentity

    resolved, authoritative, is_account_id = _search_users_by_username(
        dc_transport._client, _ADMIN_USER
    )
    assert resolved == _ADMIN_USER
    assert authoritative is True, (
        "the live user search IS the authoritative path — a False here is the "
        "permanently-non-authoritative assignee that causes unconvergeable churn"
    )
    assert is_account_id is False, "Data Center has no accountId concept at all"

    # …and the same lookup driving the real identity model: a resolved-but-
    # mismatched DC name emits the freshly resolved username (DC's `name` IS the
    # identity, so `trust_resolved_on_mismatch` is True for NameIdentity).
    model = NameIdentity(resolver=lambda n: _search_users_by_username(dc_transport._client, n))
    assert model.resolve(_ADMIN_USER, {"name": "somebody-else"}) == (_ADMIN_USER, True, False)
    # Converged: the resolved value already matches the remote identity.
    assert model.resolve(_ADMIN_USER, {"name": _ADMIN_USER}) == (None, True, False)


@_skip
@_skip_no_extra
def test_assigning_an_unknown_user_raises_backend_assignee_not_found(
    dc_transport: Any, jira_dc_project: str, track_issue: Any
) -> None:
    """Keep unknown-user resolution separate from assignment failure.

    The resolver returns authoritative-but-unmappable without raising; apply converts the
    assignment into vendor-neutral ``BackendAssigneeNotFoundError`` for core callers.
    """
    from rebar_reconciler._backend import BackendAssigneeNotFoundError
    from rebar_reconciler.adapters.jira_datacenter.backend import _search_users_by_username

    unknown = "definitely-not-a-real-dc-user-9fd4"

    resolved, authoritative, is_account_id = _search_users_by_username(
        dc_transport._client, unknown
    )
    assert (resolved, authoritative, is_account_id) == (None, True, False)

    dc_transport.project = jira_dc_project
    created = dc_transport.create_issue(
        {"summary": "rebar J6 live — unknown assignee", "issuetype": "Task"}
    )
    key = created["key"]
    track_issue(key)

    with pytest.raises(BackendAssigneeNotFoundError):
        dc_transport.update_issue(key, assignee=unknown)


@_skip
@_skip_no_extra
def test_select_backend_resolves_after_importing_adapters() -> None:
    """``select_backend("jira-datacenter")`` resolves once
    ``rebar_reconciler.adapters`` is imported — pinning the self-registration
    import (``adapters/__init__.py``) rather than a transport-only test that
    would never catch a missing registration."""
    import rebar_reconciler.adapters  # noqa: F401 — registers the DC factory
    from rebar_reconciler._backend_registry import _REGISTRY

    assert "jira-datacenter" in _REGISTRY


@_skip
@_skip_no_extra
def test_add_label_and_the_links_surface_against_a_real_instance(
    dc_transport: Any, jira_dc_project: str, track_issue: Any
) -> None:
    """Cover ``add_label`` and every link method omitted by earlier live tests.

    Verify link content through direct issue reads to avoid index lag; the search-backed
    map is held only to its raw-shape contract.
    """
    from rebar_reconciler.adapters.jira_datacenter.backend import JiraDataCenterBackend

    dc_transport.project = jira_dc_project
    blocker = dc_transport.create_issue(
        {"summary": "rebar J6 live — blocker", "issuetype": "Task"}
    )["key"]
    track_issue(blocker)
    blocked = dc_transport.create_issue(
        {"summary": "rebar J6 live — blocked", "issuetype": "Task"}
    )["key"]
    track_issue(blocked)

    # --- TicketTransport.add_label -------------------------------------------------
    dc_transport.add_label(blocker, "rebar-j6-live")
    labels = dc_transport.get_issue(blocker)["fields"].get("labels") or []
    assert "rebar-j6-live" in labels, (
        f"add_label did not reach the server: {blocker} carries {labels!r}"
    )

    # --- SupportsLinks.set_relationship + get_issuelinks_map ------------------------
    dc_transport.set_relationship(blocker, blocked, "Blocks")
    linked_fields = dc_transport.get_issue(blocker)["fields"]
    issuelinks = linked_fields.get("issuelinks") or []
    assert issuelinks, f"set_relationship left no issuelinks on {blocker}"

    assert_issuelinks_map_shape(dc_transport.get_issuelinks_map(jira_dc_project))

    # --- SupportsLinks.map_remote_links, over the REAL server payload ---------------
    # Driving the canonicalizer with a live `issuelinks` payload is the point: a
    # hand-written fixture would only prove it handles the shape we imagined.
    backend = JiraDataCenterBackend(transport=dc_transport, client=dc_transport._client)
    mapped = backend.map_remote_links(linked_fields)
    assert any(remote_key == blocked for _relation, remote_key, _vendor in mapped), (
        f"map_remote_links did not canonicalize the live link to {blocked}: {mapped!r}"
    )
    assert all(vendor_type for _relation, _remote_key, vendor_type in mapped), (
        f"map_remote_links dropped the vendor link type: {mapped!r}"
    )

    # --- SupportsLinks.link_payload_for_relation ------------------------------------
    assert backend.link_payload_for_relation("blocks") == ("Blocks", False)
    assert backend.link_payload_for_relation("depends_on") == ("Blocks", True), (
        "depends_on must invert the Blocks direction"
    )
    assert backend.link_payload_for_relation("not-a-relation") is None


# Bindings use a mutable Jira key, so deletion recovery retries by immutable numeric ID.
# Live tests prove ID lookup and that real deletion fails both lookups. The client cannot
# perform Jira's UI-only project move, so its stale-key response remains unmeasured; recovery
# deliberately handles 200-with-new-key, redirect, or 404 (bug 7c26).


@_skip
@_skip_no_extra
def test_an_issue_resolves_by_its_immutable_numeric_id(
    dc_transport: Any, jira_dc_project: str, track_issue: Any
) -> None:
    """The fix's load-bearing mechanism, live: ``GET /rest/api/2/issue/{idOrKey}``
    accepts the NUMERIC ID and answers with the issue's CURRENT key.

    If DC rejected an id here, the whole id-fallback would be inert — and inert in
    the silent direction, since the recovery falls through to ordinary absence
    bookkeeping on any failure."""
    dc_transport.project = jira_dc_project
    created = dc_transport.create_issue({"summary": "rebar 7c26 live — id", "issuetype": "Task"})
    key = created["key"]
    track_issue(key)

    numeric_id = created.get("id")
    assert isinstance(numeric_id, str) and numeric_id.isdigit(), (
        f"the create response must carry a numeric id to capture; got {numeric_id!r}"
    )

    by_id = dc_transport.get_issue_by_rest(numeric_id)
    assert by_id["key"] == key, "a by-id read must answer with the issue's current key"
    assert by_id["id"] == numeric_id


@_skip
@_skip_no_extra
def test_a_deleted_issue_fails_by_key_AND_by_id_so_deletions_are_not_masked(
    dc_transport: Any, jira_dc_project: str
) -> None:
    """The recovery must never make a real deletion unretirable.

    A deleted issue must be unreachable by BOTH handles: the key 404s (which is
    what starts the absence path) and the recorded numeric id 404s too (which is
    what makes the recovery fall through instead of suppressing the absence)."""
    dc_transport.project = jira_dc_project
    created = dc_transport.create_issue(
        {"summary": "rebar 7c26 live — deleted", "issuetype": "Task"}
    )
    key = created["key"]
    numeric_id = created["id"]
    dc_transport._client.issue(key).delete()

    for handle, what in ((key, "key"), (numeric_id, "numeric id")):
        with pytest.raises(Exception) as excinfo:
            dc_transport.get_issue_by_rest(handle)
        assert "404" in str(excinfo.value) or "does not exist" in str(excinfo.value).lower(), (
            f"a deleted issue must not resolve by {what}; got {excinfo.value!r}"
        )


def _admin_request(
    path: str, *, method: str = "GET", payload: dict[str, Any] | None = None
) -> tuple[int, Any]:
    """Minimal admin-authenticated REST v2 call, returning ``(status, body_or_None)``.

    Mirrors ``conftest._request`` deliberately, for the same reason that helper mirrors
    ``test_harness_smoke.py``'s: this harness speaks raw REST, never a client library, so
    a test that needs the raw STATUS CODE must not route through one.
    """
    import base64
    import json as _json

    url = f"{_BASE.rstrip('/')}{path}"
    body = _json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Accept", "application/json")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    user = os.environ.get("JIRA_DC_ADMIN", "admin")
    password = os.environ.get("JIRA_DC_ADMIN_PASSWORD", "admin")
    req.add_header(
        "Authorization", "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8") or ""
            return resp.status, (_json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, _json.loads(raw) if raw.strip() else None
        except ValueError:
            return exc.code, raw


@_skip
@_skip_no_extra
def test_a_rekeyed_issue_resolves_by_id_and_records_what_the_stale_key_returns(
    dc_transport: Any, jira_dc_project: str
) -> None:
    """Prove immutable-ID recovery under a real project-key rename.

    Rename reaches the ``moved_issue_key`` stale-key path available through REST, though it
    does not prove UI move-wizard parity. Record rather than prescribe the old-key response;
    require numeric-ID lookup to return the new key.
    """
    dc_transport.project = jira_dc_project
    created = dc_transport.create_issue({"summary": "rebar 7c26 live — rekey", "issuetype": "Task"})
    old_issue_key = created["key"]
    numeric_id = created["id"]

    # The shared helper is checked across all final letters; retain the inequality assertion so
    # a collision cannot turn the re-key experiment into a no-op (bug d582).
    new_project_key = derive_rename_target(jira_dc_project)
    assert new_project_key != jira_dc_project

    status, body = _admin_request(
        f"/rest/api/2/project/{jira_dc_project}", method="PUT", payload={"key": new_project_key}
    )
    assert status == 200, (
        f"project key rename is the only re-key this harness can perform; it returned "
        f"{status} {body!r}. If DC 8.17.1 refuses it, the stale-key question cannot be "
        f"settled on this oracle at all and the ticket's AC must say so."
    )
    try:
        expected_new_key = old_issue_key.replace(f"{jira_dc_project}-", f"{new_project_key}-", 1)

        # THE RECORDED READING — the folklore item. Reported, not asserted into a contract.
        stale_status, stale_body = _admin_request(f"/rest/api/2/issue/{old_issue_key}")
        stale_key = stale_body.get("key") if isinstance(stale_body, dict) else None
        print(
            f"[7c26-rekey-evidence] DC {os.environ.get('JIRA_DC_VERSION', '8.17.1')} "
            f"GET /rest/api/2/issue/{{oldKey}} after a project key rename: "
            f"status={stale_status} body_key={stale_key!r} "
            f"(old={old_issue_key} new={expected_new_key} id={numeric_id})"
        )
        assert stale_status in (200, 301, 302, 404), (
            f"unexpected stale-key status {stale_status}; record it and widen this list"
        )

        # THE ASSERTION THAT MATTERS: the numeric id survives the re-key.
        by_id = dc_transport.get_issue_by_rest(numeric_id)
        assert by_id["key"] == expected_new_key, (
            f"after a re-key the immutable id must answer with the NEW key; got "
            f"{by_id['key']!r}, expected {expected_new_key!r} — the whole 7c26 remediation "
            f"rests on this"
        )
        assert by_id["id"] == numeric_id
    finally:
        # Rename back so the scratch-project fixture's teardown (delete by the ORIGINAL
        # key) still works; otherwise this test leaks a project on every run.
        _admin_request(
            f"/rest/api/2/project/{new_project_key}",
            method="PUT",
            payload={"key": jira_dc_project},
        )


def _request_as(
    path: str,
    *,
    token: str | None = None,
    basic_auth: tuple[str, str] | None = None,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    """REST v2 call as an ARBITRARY principal, returning ``(status, body_or_None)``.

    ``_admin_request`` hardcodes the admin basic credentials, which is exactly the principal
    the 275e probe must NOT use: the question is what an ORDINARY user's PAT can read. Same
    raw-urllib shape as its siblings, for the same reason — a permission probe asserts on the
    STATUS CODE, so it must not route through a client library that raises or retries.
    """
    import base64
    import json as _json

    url = f"{_BASE.rstrip('/')}{path}"
    body = _json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Accept", "application/json")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    else:
        user, password = basic_auth if basic_auth is not None else (_ADMIN_USER, "admin")
        req.add_header(
            "Authorization", "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8") or ""
            return resp.status, (_json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, _json.loads(raw) if raw.strip() else None
        except ValueError:
            return exc.code, raw


@_skip
@_skip_no_extra
def test_whether_a_non_admin_pat_can_read_application_properties(jira_dc_project: str) -> None:
    """Measure whether an ordinary PAT can read comment-limit properties (ticket 275e).

    Mint a non-admin PAT because the session token is administrative. Prove its identity and
    lack of ``ADMINISTER``, then compare application-properties and advanced-settings results
    with admin requests. Record supported outcomes rather than encoding an unmeasured guess.
    """
    import random
    import string as _string

    suffix = "".join(random.choices(_string.ascii_lowercase + _string.digits, k=8))
    username = f"rebar-275e-{suffix}"
    password = f"Pw-{suffix}-Aa1"

    create_status, create_body = _request_as(
        "/rest/api/2/user",
        method="POST",
        payload={
            "name": username,
            "password": password,
            "emailAddress": f"{username}@example.invalid",
            "displayName": f"rebar 275e probe {suffix}",
            "applicationKeys": ["jira-software"],
        },
    )
    assert create_status in (200, 201), (
        f"could not create an ordinary user to probe with: {create_status} {create_body!r}. "
        f"Without a NON-ADMIN principal this cell cannot answer 275e at all — an admin PAT "
        f"would measure the wrong privilege level and look like an answer."
    )

    try:
        pat_status, pat_body = _request_as(
            "/rest/pat/latest/tokens",
            method="POST",
            payload={"name": f"rebar-275e-{suffix}", "expirationDuration": 1},
            basic_auth=(username, password),
        )
        assert pat_status in (200, 201) and isinstance(pat_body, dict), (
            f"minting a PAT as the ordinary user failed: {pat_status} {pat_body!r}"
        )
        user_pat = str(pat_body["rawToken"])

        # CONTROL 1 — the credential works, so a later 401/403 is about the endpoint.
        me_status, me_body = _request_as("/rest/api/2/myself", token=user_pat)
        assert me_status == 200 and isinstance(me_body, dict), (
            f"the ordinary user's PAT does not authenticate at all ({me_status} {me_body!r}); "
            f"every reading below would be uninterpretable"
        )
        assert me_body.get("name") == username, (
            f"the PAT authenticates as {me_body.get('name')!r}, not the ordinary user "
            f"{username!r} — the probe would be measuring the wrong principal"
        )

        # CONTROL 2 — the user genuinely lacks Administer Jira.
        perm_status, perm_body = _request_as(
            "/rest/api/2/mypermissions?permissions=ADMINISTER", token=user_pat
        )
        administer: Any = None
        if perm_status == 200 and isinstance(perm_body, dict):
            administer = (perm_body.get("permissions") or {}).get("ADMINISTER", {})
            administer = administer.get("havePermission")
        assert administer is False, (
            f"expected the probe user to LACK Administer Jira; /mypermissions returned "
            f"status={perm_status} havePermission={administer!r}. If this user is an admin the "
            f"readings below say nothing about rebar's privilege level."
        )

        paths = {
            "application-properties": "/rest/api/2/application-properties",
            "advanced-settings": "/rest/api/2/application-properties/advanced-settings",
        }
        version = os.environ.get("JIRA_DC_VERSION", "8.17.1")
        for label, path in paths.items():
            user_status, user_payload = _request_as(path, token=user_pat)
            # CONTROL 3 — the same path as admin, so absence and privilege are separable.
            admin_status, admin_payload = _admin_request(path)

            def _shape(payload: Any) -> str:
                if isinstance(payload, list):
                    return f"list[{len(payload)}]"
                if isinstance(payload, dict):
                    return f"dict(keys={sorted(payload)[:6]})"
                return type(payload).__name__

            print(
                f"[275e-probe] DC {version} GET {path} — "
                f"non_admin_pat: status={user_status} body={_shape(user_payload)} | "
                f"admin_basic: status={admin_status} body={_shape(admin_payload)} | "
                f"label={label} principal={username}"
            )
            assert user_status in (200, 401, 403, 404), (
                f"unexpected status {user_status} for {path} as a non-admin PAT; record it "
                f"and widen this list rather than letting an unmodelled code pass silently"
            )
            assert admin_status in (200, 401, 403, 404), (
                f"unexpected status {admin_status} for {path} as admin; record and widen"
            )
            # The one INFERENCE this cell is willing to make, and only when both readings
            # are in hand: a path admin can read but the ordinary user cannot is a
            # PERMISSION boundary, not an absent endpoint.
            if admin_status == 200 and user_status in (401, 403):
                print(
                    f"[275e-probe] {label}: PRESENT but PRIVILEGED — admin 200, non-admin "
                    f"{user_status}. 049e's documentary claim is CONFIRMED for this path."
                )
            elif admin_status == 404 and user_status == 404:
                print(
                    f"[275e-probe] {label}: ABSENT on DC {version} for both principals — "
                    f"049e's claim is CORRECTED: the drop stands, but not for the stated reason."
                )
            elif user_status == 200:
                print(
                    f"[275e-probe] {label}: READABLE by a non-admin PAT — 049e's claim is "
                    f"CORRECTED; optional auto-discovery is viable on top of the config key."
                )
    finally:
        _request_as(f"/rest/api/2/user?username={username}", method="DELETE")


@_skip
@_skip_no_extra
def test_the_instance_label_ceiling_measured_at_254_and_255(
    dc_transport: Any, jira_dc_project: str, track_issue: Any
) -> None:
    """Measure the live label boundary at 254, 255, and 256 characters (bug 2e47).

    The 254 value controls for a working label field; 255 must reproduce the accepted/read-back
    result that refuted the capability map, and 256 bounds the ceiling above. A live oracle avoids
    testing ``JIRA_LABEL_MAX_CHARS`` against itself or breaking Cloud with a tighter shared value.
    """
    dc_transport.project = jira_dc_project
    version = os.environ.get("JIRA_DC_VERSION", "8.17.1")
    observed: dict[int, tuple[bool, str]] = {}

    for length in (254, 255, 256):
        label = "x" * length
        assert len(label) == length
        try:
            created = dc_transport.create_issue(
                {
                    "summary": f"rebar 2e47 live — label ceiling {length}",
                    "issuetype": "Task",
                    "labels": [label],
                }
            )
        except Exception as exc:  # noqa: BLE001 — the REFUSAL is the measurement
            observed[length] = (False, f"{type(exc).__name__}: {exc}")
            continue
        key = created["key"]
        track_issue(key)
        # Read the label BACK: an accepted-and-silently-dropped label is a third outcome, and
        # treating it as acceptance is how a silent failure gets recorded as a pass (bug 6afc's
        # shape — a rejection that does not surface as an error).
        fetched = dc_transport.get_issue(key)
        landed = list((fetched.get("fields") or {}).get("labels") or [])
        if label in landed:
            observed[length] = (True, f"accepted and read back on {key}")
        else:
            observed[length] = (False, f"accepted by create but ABSENT on read-back of {key}")

    for length, (ok, detail) in sorted(observed.items()):
        print(f"[2e47-label-ceiling] DC {version} label len={length}: ok={ok} — {detail}")

    assert observed[254][0], (
        f"a 254-character label was NOT stored: {observed[254][1]}. This is the control leg — "
        f"without it the readings above cannot be attributed to the ceiling, so fix or "
        f"re-scope this cell before reading anything into them."
    )
    # Pin the reproduced measurement: DC accepted and returned 255 characters, refuting the
    # capability map and matching the shared ceiling. A future rejection is real divergence
    # and must fail loudly (run 30944241768, bug 2e47-ae62-c0cf-48a0).
    assert observed[255][0], (
        f"a 255-character label was NOT stored ({observed[255][1]}). Run 30944241768 measured DC "
        f"{version} ACCEPTING 255, refuting the capability map's req-0071/0072/0073. If this "
        f"instance now rejects it, the divergence with rebar's shared 255-char ceiling is REAL "
        f"after all — re-open bug 2c93-ce1c-127e-4d58 (the per-deployment ceiling) and record this "
        f"run rather than loosening this assertion."
    )
    assert not observed[256][0], (
        f"a 256-character label WAS stored ({observed[256][1]}), so this instance's inclusive "
        f"ceiling is ABOVE Jira's documented 255 'not more than 255 characters'. That is a third "
        f"outcome neither the map nor rebar's constant anticipates: record it and re-derive "
        f"JIRA_LABEL_MAX_CHARS from the measurement instead of from the docs."
    )
