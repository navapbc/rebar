"""S2 (e042) happy-path oracle — reconciler operation-bindings runtime.

Contract under test (the NEW public seam this story adds):

    rebar_reconciler.runtime.compose_reconciler_runtime(
        *, repo_root=None, cli_overrides=None) -> ReconcilerRuntime

composes ONE immutable settings/runtime projection from the S1 operation
snapshot (``rebar._operation_config.compose_operation_snapshot``) and builds
exactly the *selected* Jira Cloud or Data Center backend. The composed
settings are CAPTURED at composition time: ``backend.project`` /
``backend.query_project`` return the composed scope and do NOT re-resolve
ambient env/config on each access.

Only the happy path lives here (this is the file the implementer sees). The
poisoned-ambient stability, only-selected-construction, fail-closed, secret
canary, and cloud/DC interface behaviors are the held-out oracle
(``tests/interfaces/store/test_reconciler_operation_bindings.py`` and the
edge cases there), run by the orchestrator against code the implementer never
saw.

The package conftest puts the engine dir on ``sys.path`` and autouse-poisons
``JIRA_URL/JIRA_USER/JIRA_PROJECT/JIRA_API_TOKEN`` + ``REBAR_ROOT`` so a
default compose selects the Cloud backend with project ``DIG``.
"""

from __future__ import annotations

import pytest

from rebar_reconciler.runtime import compose_reconciler_runtime


def test_compose_selects_cloud_backend_with_captured_project() -> None:
    """Default config (reconciler.backend='jira') builds the Cloud backend and
    exposes the composed project/query scope from the snapshot."""
    runtime = compose_reconciler_runtime()
    backend = runtime.build_backend()

    assert backend.project == "DIG"
    assert backend.query_project == "DIG"


def test_compose_selects_datacenter_backend_when_configured() -> None:
    """With reconciler.backend='jira-datacenter' (+ a valid base_url and PAT),
    compose builds the DC backend, not the Cloud one."""
    overrides = {
        "reconciler.backend": "jira-datacenter",
        "reconciler.base_url": "https://jira.example.internal",
    }
    runtime = compose_reconciler_runtime(cli_overrides=overrides)
    backend = runtime.build_backend()

    # DC backend project comes from jira.project (DIG, from the poisoned env).
    assert backend.project == "DIG"


def test_datacenter_build_succeeds_without_pat(monkeypatch) -> None:
    """Ticket 4698-d85c lazy-preserved half: the build-time non-secret scope assert
    must NOT require JIRA_PAT — the credential (and the jira extra) stays lazy in
    _LazyDataCenterClient, so a PAT-less compose+build still constructs the backend."""
    monkeypatch.delenv("JIRA_PAT", raising=False)
    runtime = compose_reconciler_runtime(
        cli_overrides={
            "reconciler.backend": "jira-datacenter",
            "reconciler.base_url": "https://jira.example.internal",
        }
    )
    backend = runtime.build_backend()
    assert backend.project == "DIG"


def test_datacenter_build_fails_closed_on_missing_base_url() -> None:
    """Ticket 4698-d85c fail-closed half: an empty DC base url raises the TYPED
    BackendEnvError at build — before any transport/client work — mirroring
    _build_cloud's assert_cloud_scope_ready placement."""
    from rebar_reconciler._backend import BackendEnvError

    runtime = compose_reconciler_runtime(cli_overrides={"reconciler.backend": "jira-datacenter"})
    with pytest.raises(BackendEnvError) as exc:
        runtime.build_backend()
    assert "url" in str(exc.value)


def test_datacenter_build_fails_closed_on_empty_project(monkeypatch) -> None:
    """Ticket 4698-d85c fail-closed half: an empty DC project scope raises the TYPED
    BackendEnvError at build, so an unset project can never query every project."""
    from rebar_reconciler._backend import BackendEnvError

    monkeypatch.setenv("JIRA_PROJECT", "")
    runtime = compose_reconciler_runtime(
        cli_overrides={
            "reconciler.backend": "jira-datacenter",
            "reconciler.base_url": "https://jira.example.internal",
        }
    )
    with pytest.raises(BackendEnvError) as exc:
        runtime.build_backend()
    assert "project" in str(exc.value).lower()


def test_runtime_carries_tracker_layout() -> None:
    """The composed reconciler settings expose the tracker dir and branch the
    read/ref owners must use (default layout here)."""
    runtime = compose_reconciler_runtime()

    assert runtime.settings.tracker_dir.name == ".tickets-tracker"
    assert runtime.settings.tracker_branch == "tickets"


def test_build_backend_is_idempotent_selection() -> None:
    """Building the backend twice from one runtime yields the same provider
    selection and the same captured scope (composition is the authority)."""
    runtime = compose_reconciler_runtime()
    first = runtime.build_backend()
    second = runtime.build_backend()

    assert type(first) is type(second)
    assert first.project == second.project == "DIG"


def test_compose_returns_immutable_settings() -> None:
    """The composed settings are frozen — an operation cannot mutate its own
    binding mid-run."""
    runtime = compose_reconciler_runtime()
    with pytest.raises((AttributeError, TypeError, Exception)):
        runtime.settings.tracker_branch = "mutated"  # type: ignore[misc]


def test_cloud_build_threads_captured_cli_timeout() -> None:
    """Ticket 2048-d289: _build_cloud passes the captured jira_cli_timeout into
    the constructed AcliClient, so the operation's subprocess calls are bound
    by the compose-captured value rather than re-resolving it ambiently."""
    runtime = compose_reconciler_runtime(cli_overrides={"reconciler.jira_cli_timeout": 45})
    assert runtime.settings.jira_cli_timeout == 45
    backend = runtime.build_backend()
    assert backend.transport._call_timeout == 45


def test_datacenter_build_threads_captured_comment_max_chars(monkeypatch) -> None:
    """Ticket 2048-d289: a DC backend built from a captured scope binds the
    compose-captured comment_max_chars into its sanitizer — the ambient
    resolve_comment_max_chars must never be consulted."""
    from rebar_reconciler.adapters.jira_datacenter import settings as dc_settings

    def _boom() -> int:
        raise AssertionError("ambient resolve_comment_max_chars must not be consulted")

    monkeypatch.setattr(dc_settings, "resolve_comment_max_chars", _boom)
    runtime = compose_reconciler_runtime(
        cli_overrides={
            "reconciler.backend": "jira-datacenter",
            "reconciler.base_url": "https://jira.example.internal",
            "reconciler.comment_max_chars": 77,
        }
    )
    backend = runtime.build_backend(transport=object())
    assert backend.sanitizer.comment_max_chars() == 77


def test_datacenter_build_without_scope_keeps_lazy_ambient_resolve(monkeypatch) -> None:
    """Legacy floor: a DC backend constructed WITHOUT a captured scope keeps the
    lazy ambient resolve (bug 049e) — resolution happens on first use, not init."""
    from rebar_reconciler.adapters.jira_datacenter import settings as dc_settings
    from rebar_reconciler.adapters.jira_datacenter.backend import JiraDataCenterBackend

    monkeypatch.setattr(dc_settings, "resolve_comment_max_chars", lambda: 123)
    backend = JiraDataCenterBackend(transport=object())
    assert backend.sanitizer.comment_max_chars() == 123


def test_acli_client_forwards_call_timeout_to_every_subprocess_site(monkeypatch) -> None:
    """Ticket 2048-d289: every AcliClient method that spawns ACLI — including the
    delegations that bypass _run (create/update/get/comment/delete) — reaches
    _run_acli with the client's call_timeout, never the ambient resolve."""
    import json as _json
    import subprocess as _subprocess

    from rebar_reconciler.adapters.jira import acli as acli_mod
    from rebar_reconciler.adapters.jira import acli_subprocess

    recorded: list[tuple[list[str], object]] = []

    def _fake_run_acli(cmd, *, acli_cmd=None, retry_on_timeout=False, call_timeout=None):
        recorded.append((list(cmd), call_timeout))
        stdout = (
            _json.dumps([{"key": "DIG-9"}]) if "search" in cmd else _json.dumps({"key": "DIG-9"})
        )
        return _subprocess.CompletedProcess(list(cmd), 0, stdout, "")

    monkeypatch.setattr(acli_subprocess, "_run_acli", _fake_run_acli)
    client = acli_mod.AcliClient(
        jira_url="https://example.atlassian.net",
        user="u@example.com",
        api_token="",  # empty -> _verify_created_issue takes the subprocess get_issue path
        jira_project="DIG",
        call_timeout=45,
    )
    client.create_issue({"ticket_type": "task", "title": "t"})
    client.update_issue("DIG-9", labels="x")
    client.get_issue("DIG-9")
    client.add_comment("DIG-9", "hello")
    client.delete_issue("DIG-9")

    assert recorded, "expected ACLI subprocess dispatches"
    assert all(timeout == 45 for _, timeout in recorded), recorded
