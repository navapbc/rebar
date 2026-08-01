"""J11 — the thin vertical slice: a REAL store copy, scrubbed and isolated, round-tripping
against the Dockerized Data Center harness (epic e369, ticket 5200-e04e-246e-4aae).

WHY THIS EXISTS. Every earlier DC run in this epic converged over an EMPTY or unbound store,
which is indistinguishable from working: the pass exits 0, prints a reassuring "converged"
line, and proves nothing. This module runs the bridge against a copy of the project's ACTUAL
ticket store — real ticket shapes, real link graphs, real volume — and asserts that data
MOVES in both directions.

ISOLATION IS THE PRECONDITION, AND IT IS ASSERTED, NOT ASSUMED. rebar's store auto-commits and
auto-pushes to `sync.remote` on every write, so a test that mutates tickets could push into the
project's real tickets branch, and a misconfigured backend could write into the project's real
Jira. Three independent layers, all verified by the tests below rather than trusted:
  1. BOTH repos — the outer working repo and the `.tickets-tracker/` STORE repo, which is the
     one `sync.remote` would actually push — are fresh `git init`s with NO remote, so there is
     physically nowhere to push;
  2. `REBAR_SYNC_PUSH=off`;
  3. no Cloud credential is present in the environment.
Layer 1 is the primary one because it cannot be defeated by a mis-read setting.

THE STORE COPY MUST LAND IN `.tickets-tracker/`, NOT AT THE REPO ROOT. The orphan `tickets`
branch holds ticket files and `.bridge_state` at ITS OWN root, while the reconciler reads
`repo_root / ".tickets-tracker"` (`reconcile.py:265`). Extracting to the root would put every
ticket one directory above where the pass looks — a store that is empty as far as the
reconciler is concerned, produced by the setup rather than by the product.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

_BASE = os.environ.get("JIRA_DC_BASE_URL", "http://localhost:2990/jira")

# Dot-entries that are NOT tickets. Ticket entries are bare ids. `.git` is not on the branch
# listing but IS in the working copy, because the store is its own repo (see the fixture).
_NON_TICKET_ENTRIES = {
    ".git",
    ".bridge_state",
    ".bridge_state.bak-retarget",
    ".gitattributes",
    ".gitignore",
    ".pre-commit-config.yaml",
    ".store-compat.json",
    ".ticket-write.lock",
}


def _live_jira_ready() -> bool:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"{_BASE}/rest/api/2/serverInfo", timeout=5) as resp:
            return bool(resp.status == 200)
    except (urllib.error.URLError, OSError, ValueError):
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
        f"Jira DC harness not reachable at {_BASE}; start it with "
        "`docker compose -f tests/external/live_jira_dc/docker-compose.yml up -d`"
    ),
)
_skip_no_extra = pytest.mark.skipif(
    not _jira_extra_installed(),
    reason="the [jira-datacenter] extra is not installed",
)


def _source_repo_root() -> Path:
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


def _tickets_branch_entries(source: Path) -> list[str]:
    """Ticket entries on the orphan `tickets` branch, excluding its non-ticket dot-files."""
    subprocess.run(
        ["git", "fetch", "origin", "tickets"], cwd=source, capture_output=True, check=True
    )
    listing = subprocess.run(
        ["git", "ls-tree", "--name-only", "FETCH_HEAD"],
        cwd=source,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.split()
    return [e for e in listing if e not in _NON_TICKET_ENTRIES]


@pytest.fixture
def dc_store_copy_repo(tmp_path: Path, jira_dc_project: str, jira_dc_pat: str, monkeypatch) -> Path:
    """A fresh repo holding a SCRUBBED COPY of the project's real ticket store.

    TWO repos, mirroring the real layout, because the store IS a git repo of its own.
    `.tickets-tracker/` is gitignored by the outer checkout and lives on the orphan `tickets`
    branch, and the reconciler commits into it directly — `git -C <root>/.tickets-tracker
    commit`. A first attempt extracted the tickets into a plain directory inside a single
    outer repo, and every store write then failed with `CalledProcessError(128)` ("not a git
    repository"): the pass reported "binding-store commit to tickets branch failed" and the
    inbound ticket never landed. So the tracker gets its own `git init` on a `tickets` branch,
    and the outer repo gitignores it exactly as a real checkout does.

    NEITHER repo gets a remote — that is the primary isolation layer — and both get a local
    committer identity, since a CI runner has no global one and `git commit` would otherwise
    fail for a second, unrelated reason.
    """
    source = _source_repo_root()
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

    # Materialise the orphan branch INTO .tickets-tracker/ (see the module docstring).
    subprocess.run(
        ["git", "fetch", "origin", "tickets"], cwd=source, capture_output=True, check=True
    )
    archive = subprocess.run(
        ["git", "archive", "FETCH_HEAD"], cwd=source, capture_output=True, check=True
    ).stdout
    subprocess.run(["tar", "-x", "-C", str(tracker)], input=archive, check=True)

    # SCRUB: every binding/snapshot artifact, matched as a GLOB so a renamed sibling
    # (.bridge_state.bak-retarget) cannot survive by not being named explicitly.
    for path in sorted(tracker.glob(".bridge_state*")):
        subprocess.run(["rm", "-rf", str(path)], check=True)

    # The store is its own repo on `tickets`, committed AFTER the scrub so the bindings are
    # absent from history too, not merely from the working tree.
    _init(tracker, "tickets")
    subprocess.run(["git", "add", "-A"], cwd=tracker, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--no-verify", "-m", "scrubbed store copy for J11"],
        cwd=tracker,
        check=True,
    )

    # CONVERGE THE COPY INTO A WRITABLE STORE. A store materialised from `git archive` is
    # NOT yet usable: rebar's store marker `.env-id` is the FIRST line of the tickets branch's
    # own `.gitignore`, so it is absent from the archive by construction, and
    # `composer.edit_core` (`composer.py:400`) refuses every library write with
    # "ticket system not initialized" — the SAME message `event_append`'s `.git` guard emits,
    # which is what made this so slow to place. The reconciler's own writes go through
    # `event_append` and only need `.git`, so the inbound pass succeeds while a library edit
    # fails on the identical store; that asymmetry is real, not a contradiction.
    # `run_ensures` is rebar's idempotent ensure-registry and the sanctioned remedy for exactly
    # this (see `infra/scripts/reviewbot-ensure-tickets.sh`, bug d220, which hit the same
    # `.env-id` gate on a fresh single-branch clone).
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
    # REBAR_ROOT is what a `rebar` SUBPROCESS resolves the store from (`config.py:266`).
    # `rebar.edit_ticket(..., repo_root=...)` shells out to the CLI, and the child does not
    # inherit that argument — so without this it resolved the AMBIENT checkout, which in CI has
    # no `.tickets-tracker` at all (gitignored, never fetched) and failed the store guard
    # `_ensure_initialized` (`event_append.py:120-123`) with "ticket system not initialized".
    monkeypatch.setenv("REBAR_ROOT", str(work))
    # Belt and braces: if a Cloud credential is inherited from the ambient environment, the
    # isolation test below would fail — but so might a mis-routed pass, so clear them here too.
    for cloud_var in ("JIRA_API_TOKEN", "JIRA_EMAIL", "ATLASSIAN_API_TOKEN"):
        monkeypatch.delenv(cloud_var, raising=False)
    return work


def _run_reconcile(repo: Path, mode: str, *, only: str | None = None):
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


def _envelope(cp) -> dict[str, Any]:
    out = cp.stdout.strip()
    for line in reversed([ln for ln in out.splitlines() if ln.strip()]):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    raise AssertionError(f"no JSON envelope on stdout:\n{out}\n--stderr--\n{cp.stderr}")


# ---------------------------------------------------------------------------
# Isolation — the precondition for everything below it
# ---------------------------------------------------------------------------


@_skip
@_skip_no_extra
def test_the_working_repo_is_isolated_from_this_project(dc_store_copy_repo: Path) -> None:
    """All three isolation layers, asserted together because they defend one thing.

    Deliberately NOT asserted via `sync.remote`, which defaults to "origin" whether or not
    that remote exists — reading it would prove nothing about where a push could actually go.
    """
    # BOTH repos, and the tracker is the one that actually matters: it is the store, so it is
    # what `sync.remote` would push. Checking only the outer repo would leave the real hazard
    # unasserted while looking thorough.
    for repo, what in (
        (dc_store_copy_repo, "the working repo"),
        (dc_store_copy_repo / ".tickets-tracker", "the STORE repo"),
    ):
        remotes = subprocess.run(
            ["git", "remote"], cwd=repo, text=True, capture_output=True, check=True
        ).stdout.strip()
        assert remotes == "", (
            f"{what} has git remote(s) {remotes!r} — a store write here could push into this "
            "project's real tickets branch"
        )
    assert os.environ.get("REBAR_SYNC_PUSH") == "off"
    for cloud_var in ("JIRA_API_TOKEN", "JIRA_EMAIL", "ATLASSIAN_API_TOKEN"):
        assert not os.environ.get(cloud_var), (
            f"{cloud_var} is set in the job environment; a mis-routed pass could reach the "
            "project's real Jira Cloud instance"
        )


@_skip
@_skip_no_extra
def test_the_store_copy_is_complete_and_scrubbed(dc_store_copy_repo: Path) -> None:
    """The copy is REAL (count matches the source) and carries no bindings.

    Counting against the source rather than asserting a bare `> 0` is what catches a PARTIAL
    extraction — the failure a floor check waves through. And the count is read from the
    filesystem, NOT from the pass's `scanned` number: `scanned` is `len(curr_snapshot)`, the
    count of REMOTE Jira issues, which says nothing about the local store.
    """
    tracker = dc_store_copy_repo / ".tickets-tracker"
    copied = {p.name for p in tracker.iterdir() if p.name not in _NON_TICKET_ENTRIES}
    expected = set(_tickets_branch_entries(_source_repo_root()))

    assert copied, "the store copy is EMPTY — extraction landed somewhere the reconciler cannot see"
    assert copied == expected, (
        f"the store copy is PARTIAL: {len(copied)} entries vs {len(expected)} on the branch; "
        f"missing {sorted(expected - copied)[:5]}"
    )
    survivors = sorted(str(p.relative_to(tracker)) for p in tracker.rglob(".bridge_state*"))
    assert survivors == [], f"binding/snapshot artifacts survived the scrub: {survivors}"


def _wait_until_searchable(transport: Any, project: str, key: str, timeout: float = 90.0) -> None:
    """Block until `key` is visible to a JQL SEARCH, or fail loudly naming index lag.

    THE REASON THIS EXISTS, learned the expensive way. The inbound cell created an issue and
    ran the pass immediately, and the pass reported `inbound_differ total=0` — it saw NO issue
    at all. The fetch finds issues through `search_issues`, and Jira's Lucene index is
    eventually consistent, so a just-created issue is not searchable yet. The issue existed;
    the search could not see it.

    This is the same eventual-consistency hazard as bug 21fc, and it fails in the worst
    direction: without this wait the cell reports "the DC issue did not reach the local store",
    which reads as a BRIDGE defect when it is really a timing artefact of the test. Waiting
    here — rather than retrying the whole pass — keeps the failure honest: if the issue never
    becomes searchable, that is what the message says.
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


# ---------------------------------------------------------------------------
# The thin vertical slice — one round-trip each way, over the real store copy
# ---------------------------------------------------------------------------


def _seed_searchable_issue(transport: Any, project: str, track_issue: Any, summary: str) -> str:
    """Create an issue in DC and return its key once a JQL search can see it."""
    transport.project = project
    created = transport.create_issue({"summary": summary, "issuetype": "Task"})
    key = created["key"]
    track_issue(key)
    _wait_until_searchable(transport, project, key)
    return key


@_skip
@_skip_no_extra
def test_the_inbound_create_is_PLANNED_for_a_new_dc_issue(
    dc_store_copy_repo: Path, dc_transport: Any, jira_dc_project: str, track_issue: Any
) -> None:
    """DOES THE FETCH+DIFFER EVEN SEE THE ISSUE? Asserted separately from applying it.

    Split out because the round-trip cell below failed three times for three DIFFERENT
    reasons, and each time its one assertion ("the ticket did not appear") could not say
    whether the differ never planned the create or the pass failed to apply it. This cell
    answers only the first question, so the two failures stop being indistinguishable.

    UNFILTERED and DRY-RUN, both deliberately. Unfiltered because `--filter-local-ids` is a
    POST filter — a pass reported "1640 mutations computed, 0 match filter" — so a filtered
    run cannot show whether the create was planned. Dry-run because unfiltered is only SAFE
    in dry-run: the scrub leaves every copied ticket unbound, so an unfiltered WRITING pass
    would file production tickets into the harness (bootstrap-strict's cap=10 would file ten).
    Dry-run computes the plan and writes nothing.
    """
    from rebar_reconciler.inbound_translate import _jira_key_to_local_id

    key = _seed_searchable_issue(
        dc_transport, jira_dc_project, track_issue, "rebar J11 slice — planned"
    )
    local_id = _jira_key_to_local_id(key)

    cp = _run_reconcile(dc_store_copy_repo, "dry-run")
    plan = _envelope(cp).get("plan", [])
    inbound_creates = [
        e for e in plan if e.get("direction") == "inbound" and e.get("action") == "create"
    ]
    mine = [
        e for e in inbound_creates if key in str(e.get("target")) or e.get("local_id") == local_id
    ]

    assert mine, (
        f"the differ planned NO inbound create for {key} even though the issue is searchable. "
        f"inbound creates planned: {len(inbound_creates)}; plan size: {len(plan)}. "
        f"stderr:\n{cp.stderr[-2000:]}"
    )


@_skip
@_skip_no_extra
def test_a_dc_issue_reaches_the_local_store_inbound(
    dc_store_copy_repo: Path, dc_transport: Any, jira_dc_project: str, track_issue: Any
) -> None:
    """INBOUND round-trip over the real store copy: an issue created in DC appears locally."""
    from rebar_reconciler.inbound_translate import _jira_key_to_local_id

    key = _seed_searchable_issue(
        dc_transport, jira_dc_project, track_issue, "rebar J11 slice — inbound"
    )
    local_id = _jira_key_to_local_id(key)

    # PASS BOTH THE LOCAL ID AND THE JIRA KEY. The flag is named --filter-local-ids, but
    # `_build_filter_target_set` (reconcile_helpers.py:419-434) seeds the target set with the
    # strings VERBATIM and can only add a Jira key via `binding_store.get_jira_key(lid)` — a
    # binding that does not exist yet for an issue arriving inbound for the first time. An
    # inbound create's `target` IS the Jira key, so filtering on the local id alone matches
    # NOTHING: an earlier run reported "1640 mutations computed, 0 match filter" and the pass
    # then reported `inbound_differ total=0` POST-filter, which read like the differ had planned
    # nothing at all. It had — the companion dry-run cell above proves it.
    cp = _run_reconcile(dc_store_copy_repo, "bootstrap-strict", only=f"{local_id},{key}")
    assert "Traceback" not in cp.stderr, f"unhandled exception in the pass:\n{cp.stderr}"

    ticket_dir = dc_store_copy_repo / ".tickets-tracker" / local_id
    assert ticket_dir.exists(), (
        f"the DC issue {key} did not reach the local store as {local_id}; "
        f"stdout:\n{cp.stdout}\nstderr:\n{cp.stderr}"
    )


@_skip
@_skip_no_extra
def test_the_scrubbed_copy_plans_no_deletions_or_outbound_updates(
    dc_store_copy_repo: Path,
) -> None:
    """A dry-run over the scrubbed copy must plan ZERO deletions and ZERO outbound updates.

    Both are exactly zero because the scrub removed every binding: a surviving `bindings.json`
    would show up as deletions (its production keys do not exist in the harness), and an
    outbound UPDATE is only ever emitted for a ticket that HAS a binding
    (`outbound_differ.py:518-520`). Any non-zero value here means the scrub failed.
    """
    cp = _run_reconcile(dc_store_copy_repo, "dry-run")
    plan = _envelope(cp).get("plan", [])
    deletions = [e for e in plan if e.get("action") == "delete"]
    updates = [e for e in plan if e.get("direction") == "outbound" and e.get("action") == "update"]
    assert deletions == [], f"the scrub left bindings behind: {len(deletions)} deletions planned"
    assert updates == [], f"unexpected outbound updates over an unbound store: {len(updates)}"


@pytest.fixture
def bound_dc_issue(
    dc_store_copy_repo: Path, dc_transport: Any, jira_dc_project: str, track_issue: Any
):
    """A DC issue that is BOUND to a local ticket in the store copy — `(local_id, dc_key)`.

    Every outbound mutation except create is an UPDATE, and `outbound_differ.py:518-520` routes a
    ticket with no binding to the CREATE path instead. The scrub deliberately removes every
    binding, so without this fixture an outbound "edit" cell would CREATE a new DC issue and then
    its oracle (`fields.summary` on "the" issue) would pass against that fresh issue rather than
    the one it meant to change — green, and proving nothing.

    Both identifiers go to `--filter-local-ids`. Filtering on the local id alone does NOT work for
    the inbound leg: the filter can only derive a Jira key from an existing binding
    (`reconcile_helpers.py:419-434`), which is precisely what does not exist yet, while an inbound
    create's `target` IS the key. The plan specified the local id alone; that was insufficient.
    """
    from rebar_reconciler.binding_store import load_binding_store
    from rebar_reconciler.inbound_translate import _jira_key_to_local_id

    key = _seed_searchable_issue(
        dc_transport, jira_dc_project, track_issue, "rebar J11 — bound fixture"
    )
    local_id = _jira_key_to_local_id(key)

    cp = _run_reconcile(dc_store_copy_repo, "bootstrap-strict", only=f"{local_id},{key}")
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


@_skip
@_skip_no_extra
def test_a_local_edit_reaches_the_dc_issue_outbound(
    dc_store_copy_repo: Path, dc_transport: Any, bound_dc_issue: Any
) -> None:
    """THE EPIC'S UNPROVEN HALF: a local edit surfaces on the DC issue after an outbound pass.

    Everything before this proved DATA ARRIVES (inbound). This is the other direction, and it is
    the criterion the epic has never had evidence for — three live tests existed and none mutated
    a local ticket then asserted the change on the DC issue.

    The assertion reads the value THIS cell wrote, so it is a genuine round-trip rather than a
    re-read of an unchanged field.
    """
    import rebar

    local_id, key = bound_dc_issue
    new_title = f"rebar J11 outbound proof {key}"

    rebar.edit_ticket(local_id, repo_root=dc_store_copy_repo, title=new_title)

    cp = _run_reconcile(dc_store_copy_repo, "bootstrap-strict", only=f"{local_id},{key}")
    assert "Traceback" not in cp.stderr, f"outbound pass raised:\n{cp.stderr[-2000:]}"

    remote = dc_transport.get_issue_by_rest(key)
    summary = (remote.get("fields") or {}).get("summary")
    assert summary == new_title, (
        f"the local edit did NOT surface on {key}: fields.summary is {summary!r}, expected "
        f"{new_title!r}. This is the epic's headline outbound criterion.\n"
        f"stdout:\n{cp.stdout[-1500:]}\nstderr:\n{cp.stderr[-1500:]}"
    )
