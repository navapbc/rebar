"""Fixtures for the read-only, multi-project Jira Cloud rehearsal over S3.

The autouse ``readonly_jira_guard`` replaces every mutating transport method with a raiser.
Scenarios use ``compute_snapshot`` and dry-run ``bridge_preview``, so Jira access remains
read-only.

Execution has three gates. ``REBAR_RUN_EXTERNAL`` enables the external tier,
``live_jira_ready`` requires Jira credentials and ``acli``, and ``rehearsal_store`` requires
the S3 backend. That fixture creates a minimal store whose only remote is a generated or
operator-supplied S3 prefix with sync pushes disabled. Teardown deletes the prefix.
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
