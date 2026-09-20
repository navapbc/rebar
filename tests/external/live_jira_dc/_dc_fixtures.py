"""Shared J11 store-copy and DC-client fixtures for the live Jira DC harness.

This cohesive cluster was extracted from ``conftest.py`` to keep that module below the size
cap. ``conftest.py`` must re-export the fixtures because pytest resolves conftest attributes
at setup; collection alone does not catch a missing fixture. Request, PAT, and provisioning
helpers remain there because path-loaded unit tests monkeypatch its globals—moving them could
send real HTTP. The base URL arrives by fixture so its environment default stays single-sourced.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from _child_diag import assert_child_ran_clean

# Bounded retry budget for the ONE network call this module makes (the `tickets` fetch).
# Retry-then-FAIL, never retry-then-skip: a genuine misconfiguration must still red the lane
# rather than silently dropping the store-copy cells' coverage.
FETCH_ATTEMPTS = 3
FETCH_BACKOFF_SECONDS = 2.0

#: How many ticket directories a store copy carries. The fixtures need a REAL, representative
#: store -- not a byte-complete replica of every ticket ever written -- and copying the whole
#: branch made peak disk track total store size, which exhausted the runner (bug 25e0). The
#: store grows monotonically, so an unbounded copy gets worse every week; a cap does not.
STORE_COPY_TICKET_LIMIT = 200

#: A canonical four-quad ticket id. Everything else in the store's ticket namespace is a
#: bridge-named directory, and those are exactly what a Jira rehearsal exercises, so they all
#: travel rather than being thinned by the sample.
CANONICAL_ID_RE = re.compile(r"[0-9a-f]{4}(?:-[0-9a-f]{4}){3}")


def _is_ticket_entry(name: str) -> bool:
    """Whether a bare store entry is a ticket rather than a dot-prefixed marker.

    This mirrors ``_dc_support.is_ticket_entry`` rather than importing it: that sibling probes
    live Jira at import time, and the slice policy below is driven by repo-tier tests where
    network access is forbidden. ``test_dc_store_copy_bounded_25e0`` pins the two rules
    together so they cannot drift apart.
    """
    return not name.startswith(".")


def store_copy_entries(
    listing: Sequence[str], *, limit: int = STORE_COPY_TICKET_LIMIT
) -> list[str]:
    """Bounded, deterministic slice of a tickets branch to copy.

    Every non-ticket entry (store metadata) and every bridge-named directory travels, because
    the copy cannot converge or reconcile without them. Canonical-id tickets are sampled at an
    even stride so the slice spans the store's whole history instead of one contiguous era, and
    so its size -- the thing that exhausted the runner -- stops tracking the store's.
    """
    tickets = sorted(e for e in listing if _is_ticket_entry(e))
    canonical = [e for e in tickets if CANONICAL_ID_RE.fullmatch(e)]
    carried = [e for e in tickets if not CANONICAL_ID_RE.fullmatch(e)]
    if limit > 0 and len(canonical) > limit:
        stride = len(canonical) // limit
        canonical = canonical[::stride][:limit]
    others = sorted(e for e in listing if not _is_ticket_entry(e))
    return others + sorted(carried + canonical)


def extract_store_snapshot(source: Path | str, tracker: Path, entries: Sequence[str]) -> None:
    """Stream ``git archive`` into ``tar -x`` for exactly ``entries``.

    Capturing the archive first held the whole snapshot in memory on top of the copy on disk;
    a pipe keeps the fixture's memory cost constant regardless of how large the slice is.
    """
    with subprocess.Popen(
        ["git", "archive", "FETCH_HEAD", "--", *entries],
        cwd=str(source),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as archive:
        assert archive.stdout is not None
        try:
            subprocess.run(["tar", "-x", "-C", str(tracker)], stdin=archive.stdout, check=True)
        finally:
            archive.stdout.close()
        # Read inside the context: leaving it closes the pipes, and git's message is the whole
        # point of reporting a failed archive at all.
        archive.wait()
        failure = (archive.stderr.read() if archive.stderr else b"").decode("utf-8", "replace")
        status = archive.returncode
    if status != 0:
        raise RuntimeError(
            f"git archive of {len(entries)} entries failed in {source} "
            f"(exit {status}): {failure.strip() or '<git wrote no stderr>'}"
        )


def reclaim(work: Path) -> None:
    """Delete a finished store copy so concurrent modules do not sum their peaks.

    ``tmp_path`` retains recent runs by design, so without this the copies of every module in
    a session coexist on the runner's disk.
    """
    shutil.rmtree(work, ignore_errors=True)


def run_git(
    argv: Sequence[str],
    cwd: Path | str,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> Any:
    """Run Git with captured output and include its command, status, and stderr on failure.

    The capture remains available for ``git archive`` while failures still distinguish
    authentication, missing refs, partial-clone refspecs, and network errors.
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
    """Fetch ``branch`` with bounded retries and re-raise ``run_git``'s final diagnostic."""
    for attempt in range(1, attempts + 1):
        try:
            return run_git(["git", "fetch", remote, branch], cwd=source, runner=runner)
        except RuntimeError:
            if attempt == attempts:
                raise
            sleep(FETCH_BACKOFF_SECONDS * attempt)
    raise AssertionError("unreachable: attempts must be >= 1")  # pragma: no cover


def scrub_bridge_state(tracker: Path, *, commit: bool = False) -> list[str]:
    """Recursively remove every ``.bridge_state*`` cache and return the removed names.

    Recursive globbing matches the isolation assertion and catches nested or renamed caches.
    With ``commit=True``, commit the post-ensure scrub: ``projects-seed`` recreates and commits
    ``.bridge_state/projects.json`` after the initial removal, so convergence otherwise leaves
    the supposedly isolated copy bound.
    """
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
    """Build a real live-harness transport directly from shared fixtures.

    Direct construction avoids process-wide config discovery and explicitly mirrors the
    loopback harness's insecure setting. ``conftest.py`` re-exports this fixture so sibling
    test modules can resolve it at setup.
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


# Shared J11 store-copy fixtures. They must remain conftest-exported so both the isolation and
# mutation modules resolve them at setup; ``--collect-only`` does not validate fixture lookup.


@pytest.fixture
def dc_store_copy_repo(
    tmp_path: Path,
    jira_dc_project: str,
    jira_dc_pat: str,
    jira_dc_base_url: str,
    monkeypatch,
) -> Iterator[Path]:
    """Create a scrubbed ticket-store copy in the same two-repository layout as production.

    The gitignored ``.tickets-tracker`` is its own ``tickets`` repository because reconciler
    writes commit there directly. Neither it nor the outer repository has a remote, providing
    the primary isolation boundary; both receive local CI-safe committer identities.
    """
    import textwrap

    from _dc_support import (
        CLOUD_CREDENTIAL_VARS,
        INHERITED_ENV_FILE,
        is_ticket_entry,
        source_repo_root,
    )

    # Capture the job's inherited environment before monkeypatching; the resulting snapshot lets
    # the isolation cell test evidence it did not create itself.
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
    listing = (
        run_git(["git", "ls-tree", "--name-only", "FETCH_HEAD"], cwd=source)
        .stdout.decode("utf-8")
        .split()
    )
    # Derive the slice from the archive's own FETCH_HEAD. The live tickets branch may advance
    # concurrently, so a later fetch would extract and census different snapshots.
    entries = store_copy_entries(listing)
    extract_store_snapshot(source, tracker, entries)

    (work / ".j11-expected-entries.json").write_text(
        json.dumps(sorted(e for e in entries if is_ticket_entry(e)))
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

    # Converge the archive into a writable store. Its ignored ``.env-id`` marker is absent by
    # construction; the idempotent ensure registry restores it before any library write.
    from rebar._store.ensures import run_ensures

    for _outcome in run_ensures(str(tracker)):
        pass
    assert (tracker / ".env-id").is_file(), (
        "ensure-registry did not create the store marker `.env-id`; every library write "
        "against this copy would fail with 'ticket system not initialized'"
    )

    # Re-scrub after convergence because ``projects-seed`` recreates and commits its mapping;
    # commit the removal so the inspected copy contains no bridge state.
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
    yield work
    reclaim(work)


@pytest.fixture
def bound_dc_issue(
    dc_store_copy_repo: Path, dc_transport: Any, jira_dc_project: str, track_issue: Any
):
    """Bind a DC issue to a copied-store ticket and return ``(local_id, dc_key)``.

    Without the binding, outbound edits take the create path and can pass against the wrong
    issue. Bootstrap filtering includes both identifiers: before a binding exists, the inbound
    leg cannot derive the Jira key from the local ID, while the inbound-create target is the key.
    """
    from _dc_support import run_reconcile, seed_searchable_issue
    from rebar_reconciler.binding_store import load_binding_store
    from rebar_reconciler.inbound_translate import _jira_key_to_local_id

    key = seed_searchable_issue(
        dc_transport, jira_dc_project, track_issue, "rebar J11 — bound fixture"
    )
    local_id = _jira_key_to_local_id(key)

    cp = run_reconcile(dc_store_copy_repo, "bootstrap-strict", only=f"{local_id},{key}")
    assert_child_ran_clean(cp, what="binding pass")

    # ASSERT the binding before yielding. If this pass silently failed, every dependent cell
    # would fall back to the create path and pass for the wrong reason.
    bound = load_binding_store(dc_store_copy_repo).get_jira_key(local_id)
    assert bound == key, (
        f"the fixture did not establish a binding: get_jira_key({local_id!r}) == {bound!r}, "
        f"expected {key!r}. Every outbound UPDATE cell would silently become a CREATE.\n"
        f"stdout:\n{cp.stdout[-1500:]}"
    )
    return local_id, key
