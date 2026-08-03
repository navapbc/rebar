"""Shared helpers for the J11 live Data Center suites (epic e369, ticket 5200).

NOT a test module — the name is deliberately not ``test_*.py`` so pytest does not collect
it and `tests/unit/test_external_isolation.py` (which rglobs ``test_*.py``) does not treat
it as an uncovered external test.

These were module-local to ``test_store_copy_isolation.py`` until the comprehensive mutation
suite needed them too. Plain FUNCTIONS live here; FIXTURES live in ``conftest.py``, because a
fixture is only visible to a sibling module when pytest resolves it — importing a fixture
function across modules does not register it. That distinction cost a full harness cycle once
already (``dc_transport`` was module-local and the mutation cell errored at SETUP with
"fixture 'dc_transport' not found"), and `pytest --collect-only` does NOT catch it: fixtures
resolve at setup, so use `pytest --fixtures <module>`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

BASE = os.environ.get("JIRA_DC_BASE_URL", "http://localhost:2990/jira")
# The harness's admin account — the ONE user guaranteed to exist and be assignable on a
# freshly provisioned instance, and the same value `conftest` uses to create the scratch
# project's lead. Read from the environment with the same default so the two cannot drift.
ADMIN_USER = os.environ.get("JIRA_DC_ADMIN", "admin")


def is_ticket_entry(name: str) -> bool:
    """A ticket entry is a bare rebar id; NOTHING that is a ticket starts with a dot.

    Filtering structurally rather than enumerating dot-files is deliberate: an enumerated
    list silently miscounts the moment the store gains a new marker, which is exactly what
    happened when `run_ensures` convergence started creating `.env-id` — the copy became a
    SUPERSET and the assertion reported "PARTIAL ... missing []", contradicting itself.
    """
    return not name.startswith(".")


def live_jira_ready() -> bool:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"{BASE}/rest/api/2/serverInfo", timeout=5) as resp:
            return bool(resp.status == 200)
    except (urllib.error.URLError, OSError, ValueError):
        return False


def jira_extra_installed() -> bool:
    try:
        import jira  # noqa: F401
    except ImportError:
        return False
    return True


skip_no_harness = pytest.mark.skipif(
    not live_jira_ready(),
    reason=(
        f"Jira DC harness not reachable at {BASE}; start it with "
        "`docker compose -f tests/external/live_jira_dc/docker-compose.yml up -d`"
    ),
)
#: The harness is UP but the extra is missing — the one combination in which skipping is a LIE.
#: Without this, a CI lane that forgot `[jira-datacenter]` would silently skip every DC transport
#: cell and report green having validated nothing. `test_transport.py` has carried this guard
#: since J6; `_dc_support` did not, so the modules that import from here (the mutation table and
#: the store-copy slice) were unprotected.
extra_missing_but_harness_up = live_jira_ready() and not jira_extra_installed()

skip_no_extra = pytest.mark.skipif(
    not jira_extra_installed() and not extra_missing_but_harness_up,
    reason="the [jira-datacenter] extra is not installed",
)


def fail_if_extra_missing_while_harness_is_up() -> None:
    """Turn a silent all-skip into a loud failure when the harness is reachable."""
    if extra_missing_but_harness_up:
        pytest.fail(
            f"the Jira DC harness is reachable at {BASE} but the 'jira-datacenter' extra "
            "(pycontribs/jira) is NOT installed, so these tests would silently skip and this "
            "run would report green having validated nothing. Install it with: "
            "pip install -e '.[dev,jira-datacenter]'"
        )


def source_repo_root() -> Path:
    """The checkout this test file lives in — the SOURCE of the store copy."""
    return Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=Path(__file__).resolve().parent,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
    )


def run_reconcile(repo: Path, mode: str, *, only: str | None = None):
    """Invoke the reconciler subprocess directly so BOTH streams are observable.

    ``only`` maps to ``--filter-local-ids``. Scoping is MANDATORY for writing passes here:
    the scrub removes every binding, so an unscoped writing pass would route the whole copied
    store down the CREATE path (`outbound_differ.py:518-520`) and file production tickets as
    new harness issues.
    """
    from rebar._engine import engine_env

    argv = [sys.executable, "-m", "rebar_reconciler", "--mode", mode, "--repo-root", str(repo)]
    if only is not None:
        argv += ["--filter-local-ids", only]
    return subprocess.run(
        argv, env=engine_env(str(repo)), text=True, capture_output=True, check=False
    )


def envelope(cp) -> dict[str, Any]:
    out = cp.stdout.strip()
    for line in reversed([ln for ln in out.splitlines() if ln.strip()]):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    raise AssertionError(f"no JSON envelope on stdout:\n{out}\n--stderr--\n{cp.stderr}")


def wait_until_searchable(transport: Any, project: str, key: str, timeout: float = 90.0) -> None:
    """Block until `key` is visible to a JQL SEARCH, or fail loudly naming index lag.

    THE REASON THIS EXISTS, learned the expensive way. The inbound cell created an issue and
    ran the pass immediately, and the pass reported `inbound_differ total=0` — it saw NO issue
    at all. The fetch finds issues through `search_issues`, and Jira's Lucene index is
    eventually consistent, so a just-created issue is not searchable yet. The issue existed;
    the search could not see it.

    This is the same eventual-consistency hazard as bug 21fc, and it fails in the worst
    direction: without this wait the cell reports "the DC issue did not reach the local store",
    which reads as a BRIDGE defect when it is really a timing artefact of the test.
    """
    import time

    deadline = time.monotonic() + timeout
    attempts = 0
    while time.monotonic() < deadline:
        attempts += 1
        hits = transport.search_issues(f'project = "{project}" AND key = "{key}"')
        if any(h.get("key") == key for h in hits):
            return
        time.sleep(2.0)
    raise AssertionError(
        f"{key} was created but never became searchable within {timeout:.0f}s "
        f"({attempts} attempts) — Jira's index is lagging further than this suite allows. "
        "This is NOT a bridge defect: the issue exists, the search cannot see it."
    )


def seed_searchable_issue(
    transport: Any,
    project: str,
    track_issue: Any,
    summary: str,
    *,
    issuetype: str = "Task",
    extra: dict[str, Any] | None = None,
) -> str:
    """Create an issue in DC and return its key once a JQL search can see it."""
    transport.project = project
    payload: dict[str, Any] = {"summary": summary, "issuetype": issuetype}
    if extra:
        payload.update(extra)
    created = transport.create_issue(payload)
    key = created["key"]
    track_issue(key)
    wait_until_searchable(transport, project, key)
    return key


def read_local_ticket(repo: Path, local_id: str) -> dict[str, Any]:
    """The local ticket as JSON — the inbound oracle's read side.

    Reads the store through the library rather than by parsing event files, so the oracle
    sees the same PROJECTION the product serves rather than a re-implementation of it.
    """
    import rebar

    return rebar.show_ticket(local_id, repo_root=repo)


def forget_identity_mapping(repo: Path, provider: str, external_id: str) -> list[str]:
    """Remove every identity in the STORE COPY that maps ``(provider, external_id)``.

    Returns the ids removed (possibly empty). Used to (re-)establish the "this user has no
    identity yet" precondition an inbound-mint oracle needs.

    WHY THIS IS NEEDED AT ALL, since the fixture's scrub already leaves the copy identity-free.
    It does: every identity on the real `tickets` branch carries ``mappings: []``, so nothing
    in the copied store maps a Jira user. The pre-existing mapping the oracle trips over is
    minted DURING the test, by `bound_dc_issue`'s own binding pass — the seeded issue is
    default-assigned to the project lead (the harness admin, `conftest._create_scratch_project`
    passes ``lead=admin`` and no ``assigneeType``), and `_apply_inbound_create`
    (`apply_inbound_records.py:200-203`) mints on any inbound ``fields["assignee"]``. So the
    subject cannot simply be "a user the scrub leaves unmapped": the scrub leaves them ALL
    unmapped, and the fixture re-mints whichever one the seeded issue is assigned to.

    Removal is a directory removal against the throwaway copy, matching how the fixture's own
    scrub removes `.bridge_state*` (`conftest.py:581-583`). There is no library delete for a
    ticket, and `identity._iter_identities` reads the WORKING TREE, so this is what makes
    `resolve_mapping` miss. The loop drains multiple carriers of the same mapping and refuses
    to spin: a second sighting of an id already removed is raised rather than retried.
    """
    import shutil

    import rebar

    removed: list[str] = []
    tracker = Path(repo) / ".tickets-tracker"
    while True:
        identity_id = rebar.resolve_mapping(provider, external_id, repo_root=repo)
        if identity_id is None:
            return removed
        if identity_id in removed:
            raise AssertionError(
                f"{provider}/{external_id!r} still resolves to {identity_id!r} after that "
                f"identity was removed from {tracker} — removal is not what makes "
                f"resolve_mapping miss, so the oracle's precondition cannot be established"
            )
        directory = tracker / identity_id
        if not directory.is_dir():
            raise AssertionError(
                f"{provider}/{external_id!r} resolves to {identity_id!r} but there is no "
                f"ticket directory at {directory} to remove"
            )
        shutil.rmtree(directory)
        removed.append(identity_id)


def assert_mint_registered(repo: Path, external_id: str) -> str:
    """The inbound-mint oracle's registry half: MINTED, a PLACEHOLDER, and NOT forked.

    Returns the resolved identity id. Lives here rather than inline in the cell so the
    harness-free mutation check can run THE ORACLE ITSELF (see
    ``tests/unit/rebar_reconciler/test_inbound_assignee_oracle_discriminates_5200.py``) rather
    than a paraphrase of it — a paraphrase can stay red while the live cell has gone vacuous.

    Read through ``rebar.resolve_mapping``, which is a pure READ: it returns None rather than
    creating, so a caller that establishes the absence first can attribute the presence here to
    the pass and to nothing else.
    """
    import rebar

    minted = rebar.resolve_mapping("jira", external_id, repo_root=repo)
    assert minted is not None, (
        f"THE PASS MINTED NOTHING: jira/{external_id!r} still resolves to no identity in the "
        f"store copy after an inbound pass that carried that assignee. This is bug 5f48's "
        f"silent-swallow signature — the mint is best-effort and swallows its own failure, so "
        f"the registry is the only place it is observable."
    )
    assert rebar.is_placeholder(minted, repo_root=repo), (
        f"the identity the pass minted for {external_id!r} ({minted!r}) is not a PLACEHOLDER. "
        f"The inbound mint is documented to create a GHOST identity a later outbound pass can "
        f"key on; a non-placeholder means it adopted or overwrote a real person's identity."
    )
    forked = rebar.resolve_mapping("jira-datacenter", external_id, repo_root=repo)
    assert forked is None, (
        f"the DC pass ALSO minted under a `jira-datacenter` provider ({forked!r}), forking the "
        f"identity namespace the epic decided the two deployments share. The deployment belongs "
        f"in `RemoteRef.instance`, not in the provider string."
    )
    return minted


# ---------------------------------------------------------------------------
# The three oracles repaired for ticket 5200 (J11 verification gaps 1-3)
# ---------------------------------------------------------------------------
#
# All three live HERE rather than inline in the cell for the reason
# ``assert_mint_registered`` does: the harness is linux/amd64-only and never boots on an
# arm64 workstation, so the ONLY way to show these oracles can fail is to drive them
# harness-free. ``tests/unit/rebar_reconciler/test_dc_live_oracles_discriminate_5200.py``
# runs each of them VERBATIM, red and green. A paraphrase there could stay red while the
# live cell had quietly gone vacuous, which is this epic's signature failure mode.


def assert_local_assignee_is(
    ticket: dict[str, Any], expected_user: str, *, stage: str = "the inbound assign"
) -> None:
    """Row 8 inbound oracle: the local ``.assignee`` is EXACTLY this DC user.

    WHY EXACT AND NOT TRUTHY, which is what this oracle used to be. ``bound_dc_issue``'s
    seeded issue arrives ALREADY ASSIGNED to the project lead — the scratch project is
    created with ``lead=admin`` and no ``assigneeType``, so DC default-assigns to it, and
    the fixture's own binding pass therefore imports that assignee before any cell runs
    (J11's harness — ticket 5200-e04e-246e-4aae — proved it by finding ``jira/'admin'``
    already mapped). A
    truthiness check on ``.assignee`` is thus satisfied by the BINDING PASS, not by the
    mutation under test: the cell passed whether or not inbound assignee sync worked at all.
    The repaired cell drives the assignee to EMPTY first and asserts that, so the value
    checked here is one only the pass under test can have written.

    ``expected_user`` IS THE DC USERNAME, not a rebar identity id, and that is not a
    weakening. The local ticket stores the assignee as a bare human-readable string:
    ``apply_inbound_records`` writes ``_extract_name(fields["assignee"])`` on both the create
    (``apply_inbound_records.py:210``) and the update (``:370``) path, and
    ``inbound_translate._extract_name`` (``:285-294``) returns ``name`` before
    ``displayName`` — which on Data Center is the username. The identity the pass mints is a
    REGISTRY entry, not a field on the ticket JSON (``_ensure_inbound_assignee_identity``
    "NEVER changes the human-readable name extraction"), and it is asserted where it lives,
    by ``assert_mint_registered`` in the dedicated mint cell.

    Passing ``expected_user=""`` asserts the complementary state — unassigned — which is how
    the repaired cell gates its own precondition instead of hoping for it.
    """
    got = ticket.get("assignee") or ""
    if not expected_user:
        assert not got, (
            f"{stage}: the local ticket is STILL ASSIGNED to {got!r}, expected the assignee to "
            f"be EMPTY. Until it is empty, an assignment afterwards is not a CHANGE and the "
            f"oracle below could pass on the value the binding pass already imported."
        )
        return
    assert got == expected_user, (
        f"{stage}: the local .assignee is {ticket.get('assignee')!r}, expected EXACTLY "
        f"{expected_user!r} — the DC username `_extract_name` puts on the ticket "
        f"(`apply_inbound_records.py:210,370` -> `inbound_translate.py:285-294`, which prefers "
        f"`name` over `displayName`). A non-empty but DIFFERENT value means the pass did not "
        f"carry this assignment: the value on the ticket is the one the binding pass imported "
        f"when the seeded issue arrived default-assigned to the project lead."
    )


#: The provenance label the outbound create actually writes — the COLON form. Established
#: from the three writers, all of which emit ``f"rebar-id:{local_id}"``:
#: ``dispatch_one.py:306`` (the outbound create), ``apply_inbound_records.py:290`` (the
#: inbound-create write-back) and ``binding_store.py:706`` (pending-binding recovery). The
#: HYPHEN form ``rebar-id-<local_id>`` is READ-ONLY legacy: ``binding_walk.py:352`` and
#: ``inbound_translate.py:77-78`` accept it on read and ``binding_store.py:715`` searches it
#: as a fallback, but NOTHING writes it. So the outbound oracle asserts the colon form; a
#: hyphen-only issue is a finding, not an equivalent.
REBAR_ID_LABEL_PREFIX = "rebar-id:"
LEGACY_REBAR_ID_LABEL_PREFIX = "rebar-id-"


def assert_outbound_provenance_markers(
    local_id: str, labels: list[Any], property_status: int, property_body: Any
) -> None:
    """Row 1 outbound oracle: the created DC issue carries BOTH provenance markers.

    The two markers are written together and neither is redundant: the label is what the
    dedup JQL finds (``dispatch_one.py:214`` searches ``labels = "rebar-id:<local_id>"``) and
    the entity property is what inbound consumers correlate on. A cell asserting only one
    would pass a build that lost the other, and losing either re-creates the duplicate-issue
    class the dedup exists to prevent.

    ``property_status``/``property_body`` come from a RAW REST
    ``GET /rest/api/2/issue/{key}/properties/local_id``, deliberately NOT from
    ``transport.get_entity_property``: reading a value back through the same abstraction that
    wrote it cannot distinguish "stored correctly" from "stored and re-read consistently
    wrong". Bug 0b27 is exactly that failure — a Cloud implementation wrapped the value as
    ``{"value": …}``, storing the wrong shape and breaking correlation WITHOUT raising — which
    is why the shape is asserted here and not only the presence.
    """
    expected_label = f"{REBAR_ID_LABEL_PREFIX}{local_id}"
    label_strings = [lbl for lbl in labels if isinstance(lbl, str)]
    if expected_label not in label_strings:
        legacy = f"{LEGACY_REBAR_ID_LABEL_PREFIX}{local_id}"
        hint = (
            f" The issue carries the LEGACY HYPHEN form {legacy!r} instead. That form is "
            f"read-only compatibility (`binding_walk.py:352`, `inbound_translate.py:77-78`); "
            f"no writer emits it, so an issue created by this pass carrying it means the "
            f"create wrote through an unexpected path."
            if legacy in label_strings
            else ""
        )
        raise AssertionError(
            f"the created DC issue does NOT carry the provenance label {expected_label!r} — "
            f"labels are {label_strings!r}. The outbound create writes it at "
            f"`dispatch_one.py:306`; without it the dedup JQL at `dispatch_one.py:214` cannot "
            f"re-find the issue and the next pass creates a DUPLICATE.{hint}"
        )
    assert property_status == 200, (
        f"the entity property `local_id` is NOT READABLE on the created DC issue: raw REST "
        f"GET .../properties/local_id returned HTTP {property_status} (body "
        f"{str(property_body)[:200]}). The outbound create writes it at "
        f"`dispatch_one.py:307`; a 404 means the write never landed, and the label alone does "
        f"not satisfy row 1 — inbound consumers correlate on the property."
    )
    assert isinstance(property_body, dict), (
        f"the entity-property read returned {property_body!r}, not a JSON object; the endpoint "
        f"returns {{'key': 'local_id', 'value': …}} and the oracle cannot read a value out of "
        f"anything else."
    )
    value = property_body.get("value")
    assert value == local_id, (
        f"the entity property `local_id` on the created DC issue is {value!r}, expected the "
        f"local id {local_id!r} VERBATIM. The value is PUT unwrapped "
        f"(`jira_datacenter/transport.py:615-632` — 'the value is passed verbatim'), so a "
        f"nested {{'value': …}} here is bug 0b27's wrong-shape signature: stored without "
        f"raising, and correlation silently broken."
    )


def raw_indexed_issue_count(
    dc_request: Any, project: str, *, page_size: int = 50, max_requests: int = 200
) -> int:
    """How many issues in ``project`` a JQL search can see, counted over RAW REST paging.

    EXISTS SO THE PAGINATION CELL DOES NOT MEASURE ITS PRECONDITION WITH ITS OWN SUBJECT.
    That cell waited for the index by calling ``dc_transport._paged_search(...)`` — but
    ``_paged_search`` IS the pagination fix under test (ticket 9263). If it truncated again,
    the cell failed at its PRECONDITION with a message reading "the index is lagging further
    than this suite allows. NOT a pagination defect", pointing the next reader away from the
    exact defect the cell exists to catch. This counts the same thing through a path the fix
    does not touch, so a truncating ``_paged_search`` reaches the real assertion and is named
    there.

    Pages EXPLICITLY with ``startAt``/``maxResults`` and advances by the number of issues the
    server ACTUALLY RETURNED — never by the number requested. DC silently clamps
    ``maxResults`` to ``jira.search.views.default.max``, so a short page is NORMAL rather than
    the end of the result set; advancing by the requested size skips whatever the clamp
    withheld, and stopping on a short page truncates. Those two mistakes ARE defects 1105 /
    9263 / deac, and this yardstick must not repeat the bug it is used to measure.
    """
    seen: set[str] = set()
    start_at = 0
    for _ in range(max_requests):
        status, body = dc_request(
            f"/rest/api/2/search?jql=project%3D{project}"
            f"&startAt={start_at}&maxResults={page_size}&fields=key"
        )
        assert status == 200 and isinstance(body, dict), (
            f"the raw paged count for {project} failed at startAt={start_at}: HTTP {status}, "
            f"body {str(body)[:200]}"
        )
        issues = [i for i in (body.get("issues") or []) if isinstance(i, dict)]
        if not issues:
            return len(seen)
        seen.update(str(i["key"]) for i in issues if i.get("key"))
        start_at += len(issues)
        total = body.get("total")
        if isinstance(total, int) and start_at >= total:
            return len(seen)
    raise AssertionError(
        f"the raw paged count for {project} did not terminate within {max_requests} requests "
        f"(startAt={start_at}, {len(seen)} distinct keys). Either the project holds more issues "
        f"than this measurement is budgeted for, or the search endpoint is returning pages "
        f"without advancing — do NOT read the partial count as an index-lag verdict."
    )


def assert_remote_parent_is(
    key: str,
    issue_status: int,
    issue_body: Any,
    expected_parent: str,
    *,
    previous_parent: str = "",
    stage: str = "the outbound parent set",
) -> None:
    """Row 12 outbound oracle: ``fields.parent`` on ``key`` is EXACTLY ``expected_parent``.

    READ FROM A RAW REST DOCUMENT, not through ``get_issue_by_rest`` and not through
    ``get_parent_map``. Two different reasons, both load-bearing:

      * the write goes out as ``issue.update(fields={"parent": {"key": …}})``
        (``jira_datacenter/transport.py:711-712``), so reading back through the same
        transport cannot separate "DC stored it" from "the client object we just mutated
        reports what we set";
      * ``get_parent_map`` is a JQL PAGED SEARCH (the read the INBOUND path uses), so it is
        both eventually consistent and the subject of a different row. A parent that is
        genuinely on the issue but not yet indexed would read as a failed write.

    ``previous_parent`` is the value the issue carried BEFORE the mutation. Naming it in the
    failure message is what distinguishes DC's signature failure — a SILENT NO-OP, which
    leaves the old parent in place and raises nothing — from a write that landed somewhere
    unexpected or cleared the field. Every "Cloud has the translation, DC never got its half"
    defect in this epic (d067, 8d68, 751e, 2b16, 88d9) presented exactly that way: no
    traceback, pass reported OK, field unchanged.
    """
    assert issue_status == 200 and isinstance(issue_body, dict), (
        f"{stage}: {key} is not readable by raw REST (HTTP {issue_status}, body "
        f"{str(issue_body)[:200]}), so the parent cannot be asserted at all."
    )
    parent = (issue_body.get("fields") or {}).get("parent")
    got = parent.get("key") if isinstance(parent, dict) else None
    if got == expected_parent:
        return
    if previous_parent and got == previous_parent:
        raise AssertionError(
            f"{stage}: fields.parent on {key} is STILL {got!r} — the parent it had BEFORE the "
            f"mutation. Expected {expected_parent!r}. This is the silent-no-op signature: "
            f"`set_parent` writes `fields.parent` for a sub-task "
            f"(`jira_datacenter/transport.py:711-712`) and every core caller swallows its "
            f"failure (`dispatch_one.py:571-578` warns and continues), so an unchanged field is "
            f"the ONLY place the failure is observable."
        )
    raise AssertionError(
        f"{stage}: fields.parent on {key} is {parent!r} (key {got!r}), expected {expected_parent!r}"
        + (f" (it was {previous_parent!r} before)" if previous_parent else "")
        + ". A null/absent parent means the write cleared the field instead of setting it; any "
        "other key means it landed on the wrong issue."
    )
