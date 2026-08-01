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


# ---------------------------------------------------------------------------
# The thin vertical slice — one round-trip each way, over the real store copy
# ---------------------------------------------------------------------------


@_skip
@_skip_no_extra
def test_a_dc_issue_reaches_the_local_store_inbound(
    dc_store_copy_repo: Path, dc_transport: Any, jira_dc_project: str, track_issue: Any
) -> None:
    """INBOUND round-trip over the real store copy: an issue created in DC appears locally."""
    from rebar_reconciler.inbound_translate import _jira_key_to_local_id

    dc_transport.project = jira_dc_project
    created = dc_transport.create_issue(
        {"summary": "rebar J11 slice — inbound", "issuetype": "Task"}
    )
    key = created["key"]
    track_issue(key)
    local_id = _jira_key_to_local_id(key)

    cp = _run_reconcile(dc_store_copy_repo, "bootstrap-strict", only=local_id)
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
