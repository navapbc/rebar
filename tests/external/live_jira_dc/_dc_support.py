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


#: Environment variables that could aim a pass at a REAL Jira instead of the harness.
#: SINGLE-SOURCED deliberately (bug 59b2, Finding A): the isolation cell used to hardcode its own
#: copy of this list, which was the IDENTICAL three names the ``dc_store_copy_repo`` fixture
#: ``delenv``s — so the assertion could not fail, while its message claimed to be checking the JOB
#: environment. One definition, consumed by the fixture that clears them AND by the cell that
#: reports what the job actually provided, means the two can no longer drift apart.
#:
#: BROADER than the original three, also per Finding A: ``JIRA_TOKEN`` and ``JIRA_URL`` are equally
#: plausible ways to reach a real instance and were unchecked.
CLOUD_CREDENTIAL_VARS = (
    "JIRA_API_TOKEN",
    "JIRA_EMAIL",
    "ATLASSIAN_API_TOKEN",
    "JIRA_TOKEN",
    "JIRA_URL",
)

#: Where ``dc_store_copy_repo`` records the environment it INHERITED, before it changed anything.
#: The isolation cell reads this rather than ``os.environ``: after the fixture runs, ``os.environ``
#: reflects the fixture's own edits, so asserting on it proves only that the fixture ran.
INHERITED_ENV_FILE = ".j11-inherited-env.json"


def read_inherited_env(work: Path) -> dict[str, str | None]:
    """The job environment as it was BEFORE ``dc_store_copy_repo`` touched it.

    Written by the fixture at setup (see ``INHERITED_ENV_FILE``). A missing file is a hard error
    rather than an empty dict: silently returning ``{}`` would make every assertion over it pass
    vacuously, which is precisely the defect class this exists to close.
    """
    path = work / INHERITED_ENV_FILE
    assert path.is_file(), (
        f"{INHERITED_ENV_FILE} is absent from {work} — the fixture did not record the inherited "
        f"environment, so any assertion about the JOB environment would pass vacuously"
    )
    return dict(json.loads(path.read_text()))


def collect_base_urls(root: Path) -> dict[str, list[str]]:
    """Every ``base_url = "..."`` assignment under ``root``, mapped to the files declaring it.

    Extracted from the isolation cell so it can be exercised over a tree that DELIBERATELY
    contains a foreign URL (bug 59b2, Finding A). In the live copy the only file carrying a
    ``base_url`` is the ``rebar.toml`` the fixture itself wrote, so the cell's assertion compared
    the fixture against itself and could not detect the stray production URL it exists to catch.
    A function with its own unit test can be shown to SEE one.

    Searches ``rebar.toml``, ``pyproject.toml`` and everything under ``.rebar/`` — the config
    surfaces a reconcile pass reads.
    """
    import re

    pattern = re.compile(r"""^\s*base_url\s*=\s*["']([^"']+)["']""", re.MULTILINE)
    candidates = [root / "rebar.toml", root / "pyproject.toml"]
    rebar_dir = root / ".rebar"
    if rebar_dir.is_dir():
        candidates.extend(sorted(p for p in rebar_dir.rglob("*") if p.is_file()))
    collected: dict[str, list[str]] = {}
    for path in candidates:
        if not path.is_file():
            continue
        for value in pattern.findall(path.read_text()):
            collected.setdefault(value, []).append(str(path.relative_to(root)))
    return collected


def derive_rename_target(project_key: str) -> str:
    """A project key to rename ``project_key`` TO, guaranteed never to equal it (bug d582).

    The rekey cell needs a target key that differs from the source; it previously appended a
    fixed ``"Z"`` to the truncated key, which returns the SOURCE KEY UNCHANGED whenever the key
    already ends in ``Z``. Harness keys are ``RBJ`` + 4 random uppercase letters
    (``conftest._random_project_key``), so that collided on roughly 1 run in 26 — observed on a
    harness run that drew ``RBJDRDZ`` and failed the cell's own setup assertion (bug
    d582-fd5a-7ece-4c32, which records the run id and the raw failure).

    The fix is structural rather than defensive: pick a final character that DIFFERS from the
    current one, so no draw can collide. ``Y`` is the alternate precisely because the only key
    a ``Z`` swap cannot serve is one already ending in ``Z``.

    The result satisfies Jira's project-key rules for any harness-generated key: it is the same
    length as the input (>= 4), stays uppercase A-Z, and keeps the input's leading letter.
    """
    replacement = "Y" if project_key.endswith("Z") else "Z"
    stem = project_key[:-1] if len(project_key) >= 4 else project_key
    return f"{stem}{replacement}"


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
    """Invoke the canonical bridge operation for a rollout profile.

    ``only`` maps to the primary ``--only`` selection contract. Scoping is MANDATORY for
    writing passes here: the scrub removes every binding, so an unscoped writing pass would
    route the whole copied store down the CREATE path (`outbound_differ.py:518-520`) and file
    production tickets as new harness issues.
    """
    if mode == "dry-run":
        return run_bridge(repo, "preview", only=only)
    if mode == "bootstrap-strict":
        return run_bridge(repo, "sync", only=only, max_changes=10)
    if mode == "bootstrap-throttle":
        return run_bridge(repo, "sync", only=only, max_changes=100)
    if mode == "live":
        return run_bridge(repo, "sync", only=only)
    raise AssertionError(f"unsupported bridge profile {mode!r}")


def run_bridge(
    repo: Path,
    command: str,
    *,
    only: str | None = None,
    max_changes: int | None = None,
):
    """Invoke a primary ``preview`` or ``sync`` reconciler command.

    ``only`` uses the primary selection contract and therefore narrows examination as well
    as writes.
    """
    from rebar._engine import engine_env

    argv = [sys.executable, "-m", "rebar_reconciler", command]
    if max_changes is not None:
        argv += ["--max-changes", str(max_changes)]
    if only is not None:
        argv += ["--only", only]
    argv += ["--repo-root", str(repo)]
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


def force_issue_reindex(dc_request: Any, key: str) -> tuple[int, Any]:
    """Force a synchronous per-issue reindex so a JQL SEARCH reflects ``key`` NOW.

    Jira DC's Lucene index is eventually consistent and its background reindex latency is
    UNBOUNDED (ADR 0037 §3): on the ephemeral CI instance under load the reindex thread can be
    starved for minutes, so a field written a moment ago — visible immediately to a direct GET —
    can stay invisible to the JQL SEARCH the reconcile pass reads from past any fixed budget
    (bug 2c60: a pushed ``description`` never reflected within 240s / 120 attempts because the
    whole DC step was running ~2x slow). Waiting longer cannot fix an unbounded wait; this drives
    the specific issue through the admin ``IssueIndexingService`` REST resource directly, which
    does not depend on the background thread's schedule, so the following search read is
    deterministic.

    It is an ACCELERATOR, not a new hard dependency: it reads the numeric id first (the reindex
    resource addresses issues by id, not key) and, on any failure to obtain that id — a non-200
    read or an id-less body — returns without reindexing and WITHOUT raising, so the caller
    simply falls back to its existing search wait. A missing/404 reindex resource likewise leaves
    the caller no worse off than before this helper existed.
    """
    status, body = dc_request(f"/rest/api/2/issue/{key}?fields=id")
    if status != 200 or not isinstance(body, dict):
        return status, body
    issue_id = body.get("id")
    if not issue_id:
        return status, body
    return dc_request(
        f"/rest/api/2/reindex/issue?issueId={issue_id}"
        "&indexComments=true&indexChangeHistory=true&indexWorklogs=false",
        method="POST",
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


_ALERT_STORE_SUBPATH = ("bridge_state", "bridge_alerts")


def assert_bridge_alert_for_mutation(
    cp: subprocess.CompletedProcess,
    repo: Path,
    local_id: str,
    *,
    key: str | None = None,
) -> list[dict[str, Any]]:
    """Read the `bridge_alerts` records naming `local_id` (or `key`) from a PROVEN-CLEAN pass.

    Returns the matching records (possibly empty) for the CALLER to interpret — whether a
    record is present or absent is a diagnostic finding specific to the cell asking, not
    something this helper can judge on its own. See [rebar:18a5-2bd8-3e56-4bd8] for the cell
    that needs this and [rebar:1a9f-50c0-e7a5-4fda] for the record it correlates: a mutation
    swallowed by ``record_backstop_failure`` (`apply_handlers.py:355-387`) writes exactly one
    of these, shaped ``{"kind": "mutation-error", "key": ..., "local_id": ..., "action": ...,
    "pass_id": ..., "timestamp_ns": ..., "reason": ...}``.

    ``cp`` IS REQUIRED, and the two assertions below run BEFORE this reads a single byte of
    the alert store. ``alert_store.append`` (`alert_store.py:40-59`) creates
    ``bridge_state/bridge_alerts/`` only on its FIRST write, so an absent directory is
    indistinguishable on disk from "a clean pass wrote nothing" — the only way to tell those
    apart is evidence that a pass actually RAN and completed. Skipping this precondition would
    make this helper exactly the vacuous oracle this epic keeps producing: "no alert" would
    mean "nobody looked" as often as it meant "nothing went wrong".

    Matches on `local_id` (every writer of this shape carries it — `apply_handlers.py:377`)
    and, if given, also on `key` (`apply_handlers.py:376`), since an UPDATE-path failure may
    have a Jira key but never gained a local_id in the record, or vice versa for a CREATE.
    """
    assert cp.returncode == 0, (
        f"the pass exited {cp.returncode}, not 0 — a failed pass's alert store proves nothing "
        f"about whether a MUTATION was swallowed, only that the pass itself did not complete. "
        f"stdout:\n{cp.stdout[-1500:]}\nstderr:\n{cp.stderr[-1500:]}"
    )
    assert "Traceback" not in cp.stderr, (
        f"the pass raised (a traceback is on stderr), so it did not run to completion and its "
        f"alert store is not evidence of anything either way:\n{cp.stderr[-2000:]}"
    )

    store_dir = repo.joinpath(*_ALERT_STORE_SUBPATH)
    records: list[dict[str, Any]] = []
    if store_dir.is_dir():
        for jsonl_file in sorted(store_dir.glob("*.jsonl")):
            for line in jsonl_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                if rec.get("local_id") == local_id or (key is not None and rec.get("key") == key):
                    records.append(rec)
    return records


def probe_subtask_parent_put(
    dc_request: Any, key: str, new_parent: str | None, *, verb: str = "fields"
) -> tuple[int, Any]:
    """Raw REST `PUT` on `key`'s parent, bypassing pycontribs — for [rebar:1a9f-50c0-e7a5-4fda].

    Settles that ticket's AC1 (does DC 8.17.1 return SUCCESS or an ERROR for a sub-task reparent
    that does not take effect?) DIRECTLY: `dc_transport.set_parent` calls pycontribs'
    `issue.update(...)`, which does not surface the raw HTTP status/body to its caller, so
    nothing in the existing code path can answer this. This probe issues the identical
    operation over ``dc_request`` and returns exactly what DC said.

    ``verb="fields"`` (the default) sends the SAME shape `set_parent` sends —
    ``{"fields": {"parent": {...}}}`` (`jira_datacenter/transport.py:711-712`).
    ``verb="update"`` sends the alternative ``{"update": {"parent": [{"set": ...}]}}`` form
    instead, one of the two cheap falsifiers recorded on [rebar:37e7-d751-0042-4b94]: if the two
    verbs disagree (one accepted, one rejected), the `fields` shape itself — not sub-task
    reparenting in general — is implicated.

    ``new_parent=None`` sends a clearing PUT (`{"parent": None}` / `{"set": None}`); a non-None
    value sends a set/reparent. Purely a DIAGNOSTIC CAPTURE — this does not assert anything;
    the caller folds the returned `(status, body)` into its own assertion message so a CI
    reader can see what DC returned without needing this probe to pass or fail on its own.
    """
    target = {"key": new_parent} if new_parent else None
    if verb == "update":
        payload: dict[str, Any] = {"update": {"parent": [{"set": target}]}}
    else:
        payload = {"fields": {"parent": target}}
    return dc_request(f"/rest/api/2/issue/{key}", method="PUT", payload=payload)


def probe_subtask_parent_editmeta_ops(dc_request: Any, key: str) -> tuple[int, list[str]]:
    """The operations `/editmeta` lists for `key`'s `parent` field — the other cheap falsifier.

    Recorded on [rebar:37e7-d751-0042-4b94]: if `parent` exposes no operations (or is absent
    from `editmeta` entirely), DC is declaring the field non-editable through this endpoint
    independent of whatever a raw `PUT` returns, which would point the reparent question at a
    field-permission problem rather than at DC silently no-op'ing an accepted write.

    Returns ``(status, [])`` on anything but a clean 200/dict body, so a caller never has to
    guess whether an empty list meant "no operations" or "the read itself failed" — the status
    code carries that distinction. Diagnostic only, like `probe_subtask_parent_put`: it does
    not assert, so the caller decides what to do with the result.
    """
    status, body = dc_request(f"/rest/api/2/issue/{key}/editmeta")
    if status != 200 or not isinstance(body, dict):
        return status, []
    parent_meta = (body.get("fields") or {}).get("parent") or {}
    ops = parent_meta.get("operations") or []
    return status, [str(op) for op in ops]
