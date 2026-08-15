"""Fixtures for the READ-ONLY, S3-backed live Jira Cloud multi-project rehearsal.

Opt-in, LIVE-ONLY canary for the many-to-many Jira bridge over the **S3 store
backend** and **real Cloud ticket volume/diversity**. It complements the Jira Data
Center harness in ``tests/external/live_jira_dc`` (sibling story 368f): DC is
git/file-backed and uses low-volume throwaway projects, so it can exercise neither
the S3 backend nor real Cloud volume. This suite drives the reconciler against the
two REAL Cloud projects **REB** and **DIG** over an ISOLATED, S3-backed store, using
ONLY read paths that are proven non-mutating on Jira:

  * inbound fetch via ``rebar_reconciler.fetcher.compute_snapshot`` — the read-only
    counterpart of ``fetch_snapshot`` (it writes nothing; it only issues JQL
    searches), whose per-project JQL fan-out is driven by the store's projects.json
    mapping, and
  * ``rebar.bridge_preview`` — a dry run (``Mode.DRY_RUN`` -> ``MODE_CAPS = 0`` ->
    ``persist = False``), so no leaf applier runs and nothing is written to Jira or
    the store.

CARDINAL RULE — READ-ONLY ON JIRA CLOUD. No outbound writes to Jira. The invariant
is enforced STRUCTURALLY by :func:`readonly_jira_guard` (autouse): it monkeypatches
every mutating method on the Cloud transport class to raise, so a scenario is
literally unable to invoke an outbound Jira mutation. Read methods are untouched.

Shared constants and helpers live in the sibling ``_cloud_s3_support`` module (pytest
``prepend`` import mode puts this dir on ``sys.path``); this file holds only the
fixtures pytest must discover here.

Gating (three independent layers, all off the default lane):
  1. the parent ``tests/external/conftest.py`` autouse skip on ``REBAR_RUN_EXTERNAL``;
  2. ``live_jira_ready()`` (Jira creds + ``acli``) via ``@_skip`` on each test;
  3. the ``rehearsal_store`` fixture's skip when the S3 backend cannot be provisioned
     (``git-remote-s3`` absent, or AWS/bucket unreachable).
Defining a module-level ``_live_jira_ready`` in the test module earns the
``jira_live`` marker from the parent conftest's ``pytest_collection_modifyitems``.

Isolation is STRUCTURAL, not asserted against a production URL: the store is a FRESH,
minimal store whose ONLY git remote is a throwaway ``s3://<bucket>/<unique-prefix>``,
with ``REBAR_SYNC_PUSH=off``. Store CONTENT is irrelevant to a read-only fetch (the
volume comes from live Jira, not the local store), so a minimal store is the correct,
lighter isolation boundary — it cannot reach the production tickets remote. The
prefix is deleted on teardown.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from _cloud_s3_support import (
    DIG_PROJECT,
    DIG_REPOS,
    MUTATING_TRANSPORT_METHODS,
    REB_PROJECT,
    REB_REPOS,
    REHEARSAL_REMOTE_NAME,
    JiraWriteForbidden,
    delete_s3_prefix,
    git_run,
    live_jira_ready,
    s3_backend_ready,
    s3_url,
    transport_class,
)

import rebar


@pytest.fixture(autouse=True)
def readonly_jira_guard(monkeypatch: pytest.MonkeyPatch) -> type:
    """Structurally forbid every outbound Jira mutation for the whole test.

    Autouse, so it wraps EVERY test in this suite — a scenario cannot forget it. Each
    mutating method on the transport class is replaced with a raiser, so any code path
    that tries to create/update/label/transition/delete a Jira issue fails loudly with
    :class:`JiraWriteForbidden` instead of touching Cloud. Read methods are untouched,
    so ``compute_snapshot`` / ``bridge_preview`` still work. Returns the transport
    class so ``test_read_only_guard_is_real`` can prove the guard is not vacuous.
    """
    cls = transport_class()

    def _forbidden(name: str) -> Any:
        def _raise(*_args: Any, **_kwargs: Any) -> Any:
            raise JiraWriteForbidden(
                f"read-only rehearsal attempted an outbound Jira mutation via "
                f"{cls.__name__}.{name}() — this suite must never write to Jira Cloud"
            )

        return _raise

    for method in MUTATING_TRANSPORT_METHODS:
        # Every name is a real attribute on the class or its mixins; guard against a
        # rename silently disarming the guard.
        assert hasattr(cls, method), f"{cls.__name__} has no {method!r} to guard (renamed?)"
        monkeypatch.setattr(cls, method, _forbidden(method), raising=False)
    return cls


@pytest.fixture
def rehearsal_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A FRESH, isolated, S3-backed store mapped for REB + DIG.

    Skips (rather than fails) when the S3 backend cannot be provisioned. Builds a
    minimal store (``init_repo`` on an empty repo — store CONTENT is irrelevant to a
    read-only fetch), wires its ONLY remote to a throwaway ``s3://`` prefix as
    ``sync.remote`` with ``REBAR_SYNC_PUSH=off`` (so no write path can reach a real
    remote), seeds the REB + DIG mapping, and yields the work root. The S3 prefix is
    deleted on teardown.
    """
    if not live_jira_ready():
        pytest.skip("no live Jira creds / acli binary")
    ready, reason = s3_backend_ready()
    if not ready:
        pytest.skip(reason)

    work = tmp_path / "cloud-s3-store"
    work.mkdir()
    git_run(["git", "init", "-q", "-b", "main"], cwd=work)
    git_run(["git", "config", "user.email", "rehearsal@example.invalid"], cwd=work)
    git_run(["git", "config", "user.name", "rebar cloud-s3 rehearsal"], cwd=work)
    (work / "rebar.toml").write_text('[jira]\nproject = "REB"\n')

    monkeypatch.setenv("REBAR_ROOT", str(work))
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    monkeypatch.setenv("JIRA_PROJECT", REB_PROJECT)

    rebar.init_repo(repo_root=str(work), force_new_store=True)
    rebar.bridge_projects_set(REB_PROJECT, REB_REPOS, repo_root=str(work))
    rebar.bridge_projects_set(DIG_PROJECT, DIG_REPOS, repo_root=str(work))

    tracker = work / ".tickets-tracker"
    # ``bridge_projects_set`` writes projects.json into the working tree but does not
    # commit it (a later auto-commit would). Commit it here so the mapping is durably
    # recorded on the tickets branch — the state the S3 round-trip must preserve.
    git_run(["git", "add", ".bridge_state/projects.json"], cwd=tracker)
    git_run(
        ["git", "commit", "-q", "-m", "rehearsal: record REB + DIG bridge mapping"],
        cwd=tracker,
    )

    url = s3_url()
    git_run(["git", "remote", "add", REHEARSAL_REMOTE_NAME, url], cwd=tracker)
    git_run(["git", "config", "sync.remote", REHEARSAL_REMOTE_NAME], cwd=tracker)

    # The store's ONLY remote is the throwaway S3 prefix — the structural isolation.
    remotes = git_run(["git", "remote"], cwd=tracker).stdout.split()
    assert remotes == [REHEARSAL_REMOTE_NAME], (
        f"the rehearsal store must have exactly one (S3) remote; got {remotes}"
    )

    try:
        yield work
    finally:
        delete_s3_prefix(url)
