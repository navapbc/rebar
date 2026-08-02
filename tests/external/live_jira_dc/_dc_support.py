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
