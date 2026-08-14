"""Fixtures for the live multi-project bridge rehearsal (story 368f).

This tier is the OPT-IN, LIVE-ONLY canary for the many-to-many Jira bridge: it
rehearses two real Jira Cloud projects (REB + DIG) against an ISOLATED S3 copy of
the tickets store, so cross-project isolation is proven against real Jira rather
than a fake. It never runs in the default suite — the parent
``tests/external/conftest.py`` makes every external test inert unless
``REBAR_RUN_EXTERNAL`` is truthy — and every fixture here adds a second, live-only
precondition (Jira creds + ``acli``; the operator-provided S3 remote) so a normal
CI run collects and SKIPS cleanly.

WHY the fixtures live here and not in a test module: pytest discovers fixtures as
attributes of a conftest, and a fixture that merely *lives* in a test module is
invisible to its siblings (an ERROR at setup, not a collection failure). Mirrors
the split-residency layout of ``tests/external/live_jira_dc``.

The store-copy machinery deliberately INLINES minimal equivalents of the
``live_jira_dc`` helpers (``run_git`` / ``scrub_bridge_state`` / the two-repo
build) rather than importing them: those helpers live behind a bare-name import
(``from _dc_support import ...``) that only resolves when *that* directory is on
``sys.path``, which is not guaranteed from this directory, and the story forbids
modifying the ``live_jira_dc`` files. Keeping self-contained copies here is the
safer seam.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import rebar

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The two real Jira Cloud projects this rehearsal drives.
REB_PROJECT = "REB"
DIG_PROJECT = "DIG"

#: A configured-but-EMPTY project (zero repos) and a project UNKNOWN to Jira — the
#: harness seeds both so the single/two/zero-repo and "configured-but-empty" /
#: "unknown-to-tracker" resolution paths are all exercised by one mapping.
EMPTY_PROJECT = "REBEMPTY"
UNKNOWN_PROJECT = "REBGHOST"

#: The project an absent ``bridge_project`` field resolves to (scenario 4).
LEGACY_DEFAULT = REB_PROJECT

#: A store git remote is an S3 store copy iff its URL carries one of these schemes
#: (mirrors ``rebar._store.push._require_s3_helper_if_s3_url``).
S3_SCHEMES = ("s3://", "s3+zip://")

#: The remote name this harness wires the S3 copy onto, and pins as ``sync.remote``.
REHEARSAL_REMOTE_NAME = "rehearsal-s3"

#: Cloud-cred env vars a live-Cloud client needs — the rehearsal REQUIRES these
#: (unlike the DC harness, which strips them). Absence means "skip, not run".
_CLOUD_CRED_VARS = ("JIRA_URL", "JIRA_USER", "JIRA_API_TOKEN")


# ---------------------------------------------------------------------------
# Readiness predicates (drive skips; no side effects)
# ---------------------------------------------------------------------------


def _live_jira_ready() -> bool:
    """True iff live Jira creds AND the ``acli`` binary are present.

    Named identically to the DC/link harnesses so the parent conftest's
    marker-agnostic canary bookkeeping treats this module consistently; the test
    module carries its own copy for the ``jira_live`` auto-marker.
    """
    import shutil

    creds = all(os.environ.get(k) for k in _CLOUD_CRED_VARS)
    return bool(creds) and shutil.which("acli") is not None


# ---------------------------------------------------------------------------
# Inlined store helpers (minimal equivalents of live_jira_dc/_dc_support)
# ---------------------------------------------------------------------------


def _run_git(argv: list[str], cwd: Path | str) -> subprocess.CompletedProcess[bytes]:
    """Run a git command, raising with git's OWN stderr on failure.

    ``check=True`` alone collapses every failure to "exit N"; keeping the capture and
    doing the check here surfaces auth / missing-ref / refspec detail in the message.
    """
    result = subprocess.run(argv, cwd=cwd, capture_output=True)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace")
        raise RuntimeError(
            f"{' '.join(argv)} failed in {cwd} (exit {result.returncode}): "
            f"{stderr.strip() or '<git wrote no stderr>'}"
        )
    return result


def _source_repo_root() -> Path:
    """The checkout this file lives in — the SOURCE of the store copy."""
    return Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=Path(__file__).resolve().parent,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
    )


def _scrub_bridge_state(tracker: Path, *, commit: bool = False) -> list[str]:
    """Remove every ``.bridge_state*`` binding/snapshot cache from *tracker*.

    Matched as a GLOB (``rglob``) so a renamed sibling cannot survive. ``commit=True``
    also stages+commits the removal, for the SECOND pass after :func:`run_ensures` —
    the ``projects-seed`` ensure re-creates and commits ``.bridge_state/projects.json``,
    so converging the copy resurrects the cache the first scrub removed (mirrors the DC
    fixture's bug-91aa handling).
    """
    removed: list[str] = []
    for path in sorted(tracker.rglob(".bridge_state*")):
        subprocess.run(["rm", "-rf", str(path)], check=True)
        removed.append(path.name)
    if commit and removed:
        subprocess.run(["git", "add", "-A"], cwd=tracker, check=True)
        subprocess.run(
            ["git", "commit", "-q", "--no-verify", "-m", "re-scrub .bridge_state after converge"],
            cwd=tracker,
            check=True,
        )
    return removed


def _engine_on_path() -> None:
    """Put ``<repo>/src/rebar/_engine`` on ``sys.path`` so ``rebar_reconciler`` resolves.

    The reconciler ships as a stdlib-only package UNDER the wheel rather than as a
    top-level install (mirrors ``tests/external/test_link_sync_live.py``).
    """
    engine_dir = Path(__file__).resolve().parents[3] / "src" / "rebar" / "_engine"
    if str(engine_dir) not in sys.path:
        sys.path.insert(0, str(engine_dir))


def build_cloud_client(project: str = REB_PROJECT) -> Any:
    """A live-Cloud ``AcliClient`` built from JIRA_URL/JIRA_USER/JIRA_API_TOKEN.

    ``project`` seeds ``jira_project`` (the default project for a bare create); a
    caller that queries or creates ACROSS projects passes a full ``project = "..."``
    JQL clause or a ``_bridge_target_project`` payload key, so the default never
    silently mis-routes. Build a FRESH client for any post-mutation / zero-result
    verification query: ``search_issues`` caches per-JQL PER instance, so a client
    whose cache predates the mutation would answer stale.
    """
    _engine_on_path()
    from rebar_reconciler.adapters.jira import acli as mod

    return mod.AcliClient(
        jira_url=os.environ["JIRA_URL"],
        user=os.environ["JIRA_USER"],
        api_token=os.environ["JIRA_API_TOKEN"],
        jira_project=project,
    )


# ---------------------------------------------------------------------------
# Isolation precondition — asserted BEFORE any mutation, aborts on failure
# ---------------------------------------------------------------------------


def _configured_sync_remote(tracker: Path) -> str:
    """The remote name a store write would push to — env, then git config, then default."""
    env_remote = os.environ.get("REBAR_SYNC_REMOTE", "").strip()
    if env_remote:
        return env_remote
    config = subprocess.run(
        ["git", "-C", str(tracker), "config", "--get", "sync.remote"],
        text=True,
        capture_output=True,
        check=False,
    ).stdout.strip()
    return config or "origin"


def assert_isolated_s3_remote(tracker: Path) -> str:
    """Assert the store's configured remote is an S3 COPY, never production.

    Runs BEFORE any mutation so a mis-wired remote aborts the rehearsal before a
    single write. Requires the resolved URL to carry an S3 scheme AND — when the
    operator supplies ``REBAR_REHEARSAL_PRODUCTION_REMOTE`` — to differ from it.
    Returns the resolved URL for logging.
    """
    remote = _configured_sync_remote(tracker)
    url = subprocess.run(
        ["git", "-C", str(tracker), "remote", "get-url", remote],
        text=True,
        capture_output=True,
        check=False,
    ).stdout.strip()
    if not url:
        pytest.fail(
            f"the rehearsal store's configured sync remote {remote!r} has no URL — refusing "
            "to run before the isolated S3 store copy is wired (see the runbook)"
        )
    if not url.startswith(S3_SCHEMES):
        pytest.fail(
            f"the rehearsal store's remote {remote!r} is {url!r}, which is not an S3 store copy "
            f"(expected an {' / '.join(S3_SCHEMES)} URL). Refusing to run against a non-isolated "
            "store; wire REBAR_REHEARSAL_S3_REMOTE to the encrypted S3 copy per the runbook."
        )
    production = os.environ.get("REBAR_REHEARSAL_PRODUCTION_REMOTE", "").strip()
    if production and url == production:
        pytest.fail(
            f"the rehearsal store's remote resolves to the PRODUCTION remote {production!r} — "
            "this would mutate the real tickets store. Point REBAR_REHEARSAL_S3_REMOTE at a "
            "COPY, never production."
        )
    return url


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def run_label() -> str:
    """A unique per-run label stamped on every issue this run creates.

    Everything the run touches carries it, so the always-run teardown sweep and the
    zero-remaining verification query can select exactly this run's issues by
    ``labels = "<run_label>"`` — no run can strand another run's issues. It is printed
    so an operator can recover it (and manually sweep) if a run is killed before
    teardown (see the runbook in ``docs/jira-sync-setup.md``).
    """
    label = f"rebar-rehearsal-{uuid.uuid4().hex[:12]}"
    print(f"\n[live_multi_project] run label: {label}")
    return label


@pytest.fixture(scope="session")
def cloud_transport() -> Any:
    """A live-Cloud ``AcliClient`` (jira_project defaults REB); skips without creds."""
    if not _live_jira_ready():
        pytest.skip("no live Jira creds / acli binary")
    return build_cloud_client(REB_PROJECT)


class _LabelTracker:
    """Collects created Jira keys so the session finalizer can guarantee cleanup.

    Individual tests ALSO delete their own keys in try/finally; this is the always-run
    backstop that fires even when a scenario aborts mid-flight, combining a
    sweep-by-label with explicit-key registration so neither a label-less probe issue
    nor a label-carrying bridge issue can be stranded.
    """

    def __init__(self, run_label: str) -> None:
        self.run_label = run_label
        self.keys: set[str] = set()

    def add(self, key: str | None) -> None:
        if key:
            self.keys.add(key)


@pytest.fixture(scope="session")
def label_cleanup(run_label: str) -> Iterator[_LabelTracker]:
    """Session backstop: sweep BOTH projects by label + explicit key, assert zero remain.

    The try/finally binding runs at session teardown even if a scenario raised. It
    builds a FRESH client per project (so no stale per-JQL cache masks a survivor),
    deletes every issue matching ``labels = "<run_label>"`` PLUS every explicitly
    registered key, then asserts a fresh follow-up query returns zero in each project.
    A no-op when creds are absent (the whole tier skipped).
    """
    tracker = _LabelTracker(run_label)
    try:
        yield tracker
    finally:
        # A no-op when creds are absent (the whole tier skipped); otherwise sweep.
        if _live_jira_ready():
            _sweep_run_label(tracker)


def _issue_key(issue: dict[str, Any]) -> str | None:
    return issue.get("key") or issue.get("issueKey") or (issue.get("issue") or {}).get("key")


def _sweep_run_label(tracker: _LabelTracker) -> None:
    """Delete every issue carrying this run's label (both projects) + registered keys.

    Idempotent (``delete_issue`` treats 404 as success) and self-verifying: after the
    sweep a fresh query in each project must return zero, or the sweep failed.
    """
    label_jql = f'labels = "{tracker.run_label}"'
    for key in sorted(tracker.keys):
        try:
            build_cloud_client().delete_issue(key)
        except Exception as exc:  # noqa: BLE001 — best-effort per-key cleanup
            print(f"CLEANUP WARNING: delete_issue({key}) failed: {exc!r}")
    for project in (REB_PROJECT, DIG_PROJECT):
        jql = f'project = "{project}" AND {label_jql}'
        for issue in build_cloud_client(project).search_issues(jql):
            hit = _issue_key(issue)
            if not hit:
                continue
            try:
                build_cloud_client(project).delete_issue(hit)
            except Exception as exc:  # noqa: BLE001 — best-effort sweep
                print(f"CLEANUP WARNING: delete_issue({hit}) in {project} failed: {exc!r}")
    for project in (REB_PROJECT, DIG_PROJECT):
        jql = f'project = "{project}" AND {label_jql}'
        survivors = [
            k for k in (_issue_key(i) for i in build_cloud_client(project).search_issues(jql)) if k
        ]
        assert not survivors, (
            f"the session sweep left issues labelled {tracker.run_label!r} in {project}: "
            f"{survivors} — the run stranded live issues"
        )


def _seed_mapping(work: Path) -> None:
    """Seed the many-to-many mapping: single / two / zero-repo + unknown-to-Jira.

    REB→one repo, DIG→two repos exercise the single/two-repo configs; ``EMPTY``→[]
    is the configured-but-empty path; ``UNKNOWN``→a repo the tracker knows but Jira
    does not. The legacy default is stamped directly on the record so scenario 4's
    "project field ABSENT" ticket has somewhere to resolve to.
    """
    rebar.bridge_projects_set(REB_PROJECT, ["rebar"], repo_root=str(work))
    rebar.bridge_projects_set(DIG_PROJECT, ["rebar-web", "rebar-api"], repo_root=str(work))
    rebar.bridge_projects_set(EMPTY_PROJECT, [], repo_root=str(work))
    rebar.bridge_projects_set(UNKNOWN_PROJECT, ["rebar-ghost"], repo_root=str(work))
    _set_legacy_default(work, LEGACY_DEFAULT)


def _projects_record_path(work: Path) -> Path:
    return work / ".tickets-tracker" / ".bridge_state" / "projects.json"


def _set_legacy_default(work: Path, key: str) -> None:
    """Stamp ``legacy_default`` on the projects record (no library setter exists).

    ``bridge_projects_set`` preserves ``legacy_default`` on write but cannot SET it,
    so the harness edits the committed record in place — the same JSON shape the
    reconciler reads (``{"version","legacy_default","projects"}``).
    """
    path = _projects_record_path(work)
    record = json.loads(path.read_text())
    record["legacy_default"] = key
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")


@pytest.fixture
def rehearsal_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated S3-backed COPY of the real ticket store, mapped for REB + DIG.

    Mirrors ``live_jira_dc``'s two-repo store copy (outer ``main`` + inner
    ``.tickets-tracker`` on ``tickets``), scrubs every binding, converges it with
    ``run_ensures``, re-scrubs, then wires the operator-provided S3 remote as
    ``sync.remote`` and ASSERTS that remote is an isolated S3 copy BEFORE returning —
    so no scenario can write against a mis-wired (or production) store.

    Live-only precondition: if ``REBAR_REHEARSAL_S3_REMOTE`` is absent this skips
    (in addition to the creds skipif on each test), because there is no isolated
    store to rehearse against.
    """
    s3_remote = os.environ.get("REBAR_REHEARSAL_S3_REMOTE", "").strip()
    if not s3_remote:
        pytest.skip(
            "REBAR_REHEARSAL_S3_REMOTE is unset — the multi-project rehearsal needs an "
            "isolated S3 copy of the tickets store to run against (see the runbook)"
        )

    from rebar._store.ensures import run_ensures

    source = _source_repo_root()
    work = tmp_path / "rehearsal-store"
    tracker = work / ".tickets-tracker"
    tracker.mkdir(parents=True)

    def _init(repo: Path, branch: str) -> None:
        subprocess.run(["git", "init", "-q", "-b", branch], cwd=repo, check=True)
        subprocess.run(
            ["git", "config", "user.email", "rehearsal@example.invalid"], cwd=repo, check=True
        )
        subprocess.run(["git", "config", "user.name", "rebar 368f rehearsal"], cwd=repo, check=True)

    _init(work, "main")
    (work / ".gitignore").write_text(".tickets-tracker/\n")

    # Extract a snapshot of the live tickets branch into the inner store repo.
    _run_git(["git", "fetch", "origin", "tickets"], cwd=source)
    archive = _run_git(["git", "archive", "FETCH_HEAD"], cwd=source).stdout
    subprocess.run(["tar", "-x", "-C", str(tracker)], input=archive, check=True)

    _scrub_bridge_state(tracker)
    _init(tracker, "tickets")
    subprocess.run(["git", "add", "-A"], cwd=tracker, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--no-verify", "-m", "scrubbed store copy for 368f rehearsal"],
        cwd=tracker,
        check=True,
    )

    # Converge into a writable store (creates the `.env-id` marker) and re-scrub the
    # cache the `projects-seed` ensure resurrects.
    for _outcome in run_ensures(str(tracker)):
        pass
    assert (tracker / ".env-id").is_file(), (
        "ensure-registry did not create the store marker `.env-id`; every library write "
        "against this copy would fail with 'ticket system not initialized'"
    )
    _scrub_bridge_state(tracker, commit=True)

    # Wire the isolated S3 remote and pin it as the store's sync remote, THEN assert it.
    subprocess.run(
        ["git", "-C", str(tracker), "remote", "add", REHEARSAL_REMOTE_NAME, s3_remote], check=True
    )
    subprocess.run(
        ["git", "-C", str(tracker), "config", "sync.remote", REHEARSAL_REMOTE_NAME], check=True
    )
    monkeypatch.setenv("REBAR_SYNC_REMOTE", REHEARSAL_REMOTE_NAME)
    monkeypatch.setenv("REBAR_ROOT", str(work))
    assert_isolated_s3_remote(tracker)

    _seed_mapping(work)
    return work
