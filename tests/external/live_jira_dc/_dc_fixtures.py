"""The J11 store-copy + DC-client fixtures for the live Jira DC harness.

Split out of this directory's ``conftest.py`` (ticket ccf6): that file had grown to
932 lines, past AGENTS.md's 800-line hard cap. This is the seam the policy asks
for — an already-cohesive cluster that calls among itself (``bound_dc_issue``
consumes both ``dc_store_copy_repo`` and ``dc_transport``, and all three draw on
``_dc_support``), lifted whole rather than carved to hit a number.

**These fixtures are re-exported by ``conftest.py`` and must stay that way.**
pytest discovers fixtures as attributes of the conftest module; a fixture that
merely *lives* here is invisible to the suite, and the failure is an ERROR at
SETUP ("fixture 'dc_transport' not found") that ``--collect-only`` does not
catch. ``tests/unit/test_live_jira_dc_conftest.py`` pins the re-export.

**What deliberately did NOT move.** The unit tier drives the harness by loading
``conftest.py`` by path and calling ``monkeypatch.setattr(harness, "_request",
...)``. That rebinds a name in *conftest's* globals, so it only reaches callers
defined in conftest. Everything on that stub path — ``_request``,
``wait_for_jira_dc_ready``, the PAT cluster, the scratch-project provisioning
cluster — therefore stays in conftest. Moving any of it here would leave the unit
tests resolving the real ``_request`` and issuing live HTTP: a failure that opens
rather than closes. The same unit test file pins that boundary too.

The harness base URL arrives through the ``jira_dc_base_url`` fixture rather than
a second copy of conftest's ``_BASE`` constant, so the env-var default is defined
in exactly one place.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

# Bounded retry budget for the ONE network call this module makes (the `tickets` fetch).
# Retry-then-FAIL, never retry-then-skip: a genuine misconfiguration must still red the lane
# rather than silently dropping the store-copy cells' coverage.
FETCH_ATTEMPTS = 3
FETCH_BACKOFF_SECONDS = 2.0


def run_git(
    argv: Sequence[str],
    cwd: Path | str,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> Any:
    """Run a git command, raising with git's OWN stderr when it fails.

    ``subprocess.run(..., capture_output=True, check=True)`` is a diagnostic dead end: the
    capture routes git's stderr into a buffer and ``CalledProcessError``'s string form then
    reports only the exit status, so a run log records "exit 128" and nothing about whether
    that was auth, a missing ref, a refspec miss under CI's partial clone, or the network
    (bug ``ancient-domestic-orca``, live run 31587452003). This keeps the capture — the
    fixture consumes ``git archive``'s stdout — and does the check itself, so the raised
    message carries the full command (hence the remote and branch), the status, and git's
    verbatim stderr.
    """
    result = runner(argv, cwd=cwd, capture_output=True)
    if result.returncode != 0:
        stderr = result.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        raise RuntimeError(
            f"{' '.join(argv)} failed in {cwd} (exit {result.returncode}): "
            f"{(stderr or '').strip() or '<git wrote no stderr>'}"
        )
    return result


def fetch_tickets(
    source: Path | str,
    remote: str = "origin",
    branch: str = "tickets",
    *,
    attempts: int = FETCH_ATTEMPTS,
    runner: Callable[..., Any] = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """Fetch ``branch`` from ``remote``, tolerating a transient failure.

    A single un-retried network call on the critical path of a 41-minute live job turns any
    blip into an ERROR at fixture setup. Retries are bounded and the final failure re-raises
    the diagnostic error from `run_git` — so a real misconfiguration still fails, and now
    says why.
    """
    for attempt in range(1, attempts + 1):
        try:
            return run_git(["git", "fetch", remote, branch], cwd=source, runner=runner)
        except RuntimeError:
            if attempt == attempts:
                raise
            sleep(FETCH_BACKOFF_SECONDS * attempt)
    raise AssertionError("unreachable: attempts must be >= 1")  # pragma: no cover


def scrub_bridge_state(tracker: Path, *, commit: bool = False) -> list[str]:
    """Remove every ``.bridge_state*`` binding/snapshot cache from *tracker*.

    Matched by ``rglob`` so the removal spans exactly what the isolation cell's
    ``tracker.rglob('.bridge_state*')`` assertion inspects — a nested artifact cannot
    survive a root-only sweep — and as a GLOB so a renamed sibling cannot survive merely
    because its exact name is not enumerated. Returns the names removed (for a caller that
    wants to assert on them).

    ``commit=True`` also stages the removal and commits it, for the SECOND scrub pass — the
    one that runs AFTER :func:`run_ensures`. The registry's ``projects-seed`` unit
    (``rebar._store.project_ensures.seed_projects_mapping_unit``, ticket 462d / epic 0303)
    unconditionally re-creates and COMMITS ``.bridge_state/projects.json`` when its
    tree-check no longer finds the blob — which is exactly the state the first scrub leaves.
    That seed unit post-dates this scrub (added 2026-08-13, after the J11 store-copy fixture),
    so converging the copy re-introduces the very cache the scrub removed; re-scrubbing and
    committing after convergence is what keeps the copy free of it (bug 91aa)."""
    removed: list[str] = []
    # Parent-first so ``rm -rf`` on a `.bridge_state` dir clears any nested match under it;
    # a later `rm -rf` on an already-removed descendant is a no-op (``-f`` ignores absence).
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


@pytest.fixture
def dc_transport(jira_dc_pat: str, jira_dc_base_url: str) -> Any:
    """A REAL ``JiraDataCenterTransport`` against the live harness.

    Builds the client directly from harness fixtures (rather than through ``load_config()``)
    so this suite does not depend on process-wide config discovery — ``allow_insecure=True``
    mirrors what a ``[tool.rebar.reconciler]`` config pointed at this loopback harness would
    need, exercised here as a direct constructor argument instead.

    LIVES IN conftest, not in a test module, because more than one module needs it: it began
    as a local fixture in ``test_transport.py``, and ``test_store_copy_isolation.py`` then
    failed at SETUP with "fixture 'dc_transport' not found" — a module-local fixture is not
    visible to siblings. Sharing it here beats copying twenty lines into each consumer.
    """
    from rebar_reconciler.adapters.jira_datacenter.settings import JiraDataCenterSettings
    from rebar_reconciler.adapters.jira_datacenter.transport import (
        JiraDataCenterTransport,
        build_client_from_settings,
    )

    settings = JiraDataCenterSettings(
        url=jira_dc_base_url,
        project="",  # overridden per-test via jira_dc_project
        allow_insecure=True,
        ca_bundle="",
        pat=jira_dc_pat,
    )
    client = build_client_from_settings(settings)
    return JiraDataCenterTransport(client=client, project="")


# ---------------------------------------------------------------------------
# The J11 store-copy fixtures — HERE, not in a test module, because two suites need them
# ---------------------------------------------------------------------------
#
# `dc_store_copy_repo` and `bound_dc_issue` began as module-local fixtures in
# `test_store_copy_isolation.py`. `test_dc_mutations.py` needs the same two, and a
# module-local fixture is NOT visible to a sibling module — the same trap `dc_transport`
# fell into, which cost a full harness cycle and surfaced as an ERROR at setup
# ("fixture 'dc_transport' not found") rather than as a collection failure. Note that
# `pytest --collect-only` does NOT catch this: fixtures resolve at SETUP. Use
# `pytest --fixtures <module>`.


@pytest.fixture
def dc_store_copy_repo(
    tmp_path: Path,
    jira_dc_project: str,
    jira_dc_pat: str,
    jira_dc_base_url: str,
    monkeypatch,
) -> Path:
    """A fresh repo holding a SCRUBBED COPY of the project's real ticket store.

    TWO repos, mirroring the real layout, because the store IS a git repo of its own.
    `.tickets-tracker/` is gitignored by the outer checkout and lives on the orphan `tickets`
    branch, and the reconciler commits into it directly — `git -C <root>/.tickets-tracker
    commit`. A first attempt extracted the tickets into a plain directory inside a single
    outer repo, and every store write then failed with `CalledProcessError(128)` ("not a git
    repository"). So the tracker gets its own `git init` on a `tickets` branch, and the outer
    repo gitignores it exactly as a real checkout does.

    NEITHER repo gets a remote — that is the primary isolation layer — and both get a local
    committer identity, since a CI runner has no global one and `git commit` would otherwise
    fail for a second, unrelated reason.
    """
    import textwrap

    from _dc_support import (
        CLOUD_CREDENTIAL_VARS,
        INHERITED_ENV_FILE,
        is_ticket_entry,
        source_repo_root,
    )

    # SNAPSHOT THE INHERITED ENVIRONMENT FIRST, before this fixture changes any of it (bug 59b2,
    # Finding A). Once the `monkeypatch` calls below have run, `os.environ` describes the FIXTURE,
    # so a cell asserting on it can only ever confirm that the fixture ran. Recording what the JOB
    # supplied is what gives the isolation cell something it did not author to assert about.
    inherited_env = {
        name: os.environ.get(name) for name in (*CLOUD_CREDENTIAL_VARS, "REBAR_SYNC_PUSH")
    }

    source = source_repo_root()
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

    fetch_tickets(source)
    archive = run_git(["git", "archive", "FETCH_HEAD"], cwd=source).stdout
    subprocess.run(["tar", "-x", "-C", str(tracker)], input=archive, check=True)

    # Record the expected entry set from THE SAME FETCH_HEAD the archive came from. The
    # tickets branch is LIVE (rebar auto-pushes on every write, and a concurrent agent
    # session writes to it constantly), so re-fetching at assertion time samples a DIFFERENT
    # commit and the counts differ for reasons unrelated to the extraction.
    listing = (
        run_git(["git", "ls-tree", "--name-only", "FETCH_HEAD"], cwd=source)
        .stdout.decode("utf-8")
        .split()
    )
    (work / ".j11-expected-entries.json").write_text(
        json.dumps(sorted(e for e in listing if is_ticket_entry(e)))
    )
    (work / INHERITED_ENV_FILE).write_text(json.dumps(inherited_env, sort_keys=True))

    # SCRUB: every binding/snapshot artifact, matched as a GLOB so a renamed sibling
    # cannot survive merely because its exact name is not enumerated.
    scrub_bridge_state(tracker)

    _init(tracker, "tickets")
    subprocess.run(["git", "add", "-A"], cwd=tracker, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--no-verify", "-m", "scrubbed store copy for J11"],
        cwd=tracker,
        check=True,
    )

    # CONVERGE THE COPY INTO A WRITABLE STORE. A store materialised from `git archive` is NOT
    # yet usable: rebar's store marker `.env-id` is the FIRST line of the tickets branch's own
    # `.gitignore`, so it is absent from the archive by construction, and `composer.edit_core`
    # refuses every library write with "ticket system not initialized". `run_ensures` is
    # rebar's idempotent ensure-registry and the sanctioned remedy (see bug d220).
    from rebar._store.ensures import run_ensures

    for _outcome in run_ensures(str(tracker)):
        pass
    assert (tracker / ".env-id").is_file(), (
        "ensure-registry did not create the store marker `.env-id`; every library write "
        "against this copy would fail with 'ticket system not initialized'"
    )

    # RE-SCRUB after converging. `run_ensures` runs the `projects-seed` unit (ticket 462d /
    # epic 0303), which re-creates and commits `.bridge_state/projects.json` because the first
    # scrub deleted the blob its tree-check looks for. That unit was added AFTER this fixture,
    # so its seed silently resurrects the cache the scrub removed (bug 91aa) — remove it again,
    # committing the deletion so the copy the isolation cell inspects carries no `.bridge_state`.
    scrub_bridge_state(tracker, commit=True)

    (work / "rebar.toml").write_text(
        textwrap.dedent(f"""
        [reconciler]
        backend = "jira-datacenter"
        base_url = "{jira_dc_base_url}"
        allow_insecure = true

        [jira]
        project = "{jira_dc_project}"
        """).lstrip()
    )
    monkeypatch.setenv("JIRA_PAT", jira_dc_pat)
    monkeypatch.setenv("JIRA_PROJECT", jira_dc_project)
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    # REBAR_ROOT is what a `rebar` SUBPROCESS resolves the store from. `rebar.edit_ticket(...,
    # repo_root=...)` shells out to the CLI and the child does not inherit that argument.
    monkeypatch.setenv("REBAR_ROOT", str(work))
    for cloud_var in CLOUD_CREDENTIAL_VARS:
        monkeypatch.delenv(cloud_var, raising=False)
    return work


@pytest.fixture
def bound_dc_issue(
    dc_store_copy_repo: Path, dc_transport: Any, jira_dc_project: str, track_issue: Any
):
    """A DC issue BOUND to a local ticket in the store copy — `(local_id, dc_key)`.

    Every outbound mutation except create is an UPDATE, and `outbound_differ.py:518-520`
    routes a ticket with no binding to the CREATE path instead. The scrub deliberately removes
    every binding, so without this fixture an outbound "edit" cell would CREATE a new DC issue
    and its oracle would pass against that fresh issue rather than the one it meant to change —
    green, and proving nothing.

    Both identifiers go to `--filter-local-ids`. Filtering on the local id alone does NOT work
    for the inbound leg: the filter can only derive a Jira key from an EXISTING binding, which
    is precisely what does not exist yet, while an inbound create's `target` IS the key.
    """
    from _dc_support import run_reconcile, seed_searchable_issue
    from rebar_reconciler.binding_store import load_binding_store
    from rebar_reconciler.inbound_translate import _jira_key_to_local_id

    key = seed_searchable_issue(
        dc_transport, jira_dc_project, track_issue, "rebar J11 — bound fixture"
    )
    local_id = _jira_key_to_local_id(key)

    cp = run_reconcile(dc_store_copy_repo, "bootstrap-strict", only=f"{local_id},{key}")
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
