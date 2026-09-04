"""S2 (e042) HELD-OUT oracle — reconciler operation-bindings, cross-surface.

This file is withheld from the implementer. It asserts the acceptance criteria
as OBSERVABLE behavior through the reconciler's public seams — never private
structure — against code the implementer wrote seeing only the happy path.

Contract recap (the new seam):
    rebar_reconciler.runtime.compose_reconciler_runtime(
        *, repo_root=None, cli_overrides=None) -> ReconcilerRuntime
    ReconcilerRuntime.build_backend(*, transport=None) -> backend
      - captures the composed scope/settings at COMPOSE time (no ambient
        re-resolution on property access or at build)
      - constructs ONLY the selected provider
      - ``transport`` (test seam) injects a fake transport for read/map E2E
    ReconcilerRuntime.settings -> frozen ReconcilerSettings with
      ``tracker_dir`` (Path), ``tracker_branch`` (str), ``repo_root`` (str/Path)
"""

from __future__ import annotations

import importlib
import sys

import pytest

from rebar._config_coercion import ConfigError

# The engine's ``rebar_reconciler`` package and the ``tests/unit/rebar_reconciler``
# test package share the SAME top-level name. Importing the engine seam at module
# (collection) scope caches ``sys.modules['rebar_reconciler']`` as the engine, which
# shadows the test package and breaks the relative-import collection of
# ``tests/unit/rebar_reconciler/classify/*`` ("not a package"). So the engine dir is
# put on ``sys.path`` and the seam imported only at RUN time (after collection), the
# same deferral the sibling held-out tests here use.
compose_reconciler_runtime = None
BackendEnvError = None


@pytest.fixture(autouse=True, scope="module")
def _engine_seam():
    global compose_reconciler_runtime, BackendEnvError
    from rebar._engine import engine_dir

    engine = str(engine_dir())
    if engine not in sys.path:
        sys.path.insert(0, engine)
    from rebar_reconciler._backend import BackendEnvError as _BackendEnvError
    from rebar_reconciler.runtime import (
        compose_reconciler_runtime as _compose_reconciler_runtime,
    )

    compose_reconciler_runtime = _compose_reconciler_runtime
    BackendEnvError = _BackendEnvError
    return


# The ambient resolvers S2 must stop re-reading after composition.
_CLOUD_RESOLVER = ("rebar_reconciler.adapters.jira.acli_subprocess", "resolve_jira_settings")
_DC_RESOLVER = (
    "rebar_reconciler.adapters.jira_datacenter.settings",
    "resolve_jira_datacenter_settings",
)


def _poison(monkeypatch, module_path: str, attr: str) -> None:
    mod = importlib.import_module(module_path)

    def _boom(*_a, **_k):
        raise AssertionError(f"ambient resolver {module_path}.{attr} re-read after compose")

    monkeypatch.setattr(mod, attr, _boom, raising=True)


@pytest.fixture(autouse=True)
def _default_jira_env(monkeypatch):
    """Pin hermetic Cloud provider env for this subtree (mirrors the reconciler
    unit conftest's ``_default_jira_project``). This subtree's conftest does not
    poison the Jira env, so tests asserting the ``DIG`` scope set it here;
    per-test overrides (empty project, absent token, DC PAT) run after and win."""
    monkeypatch.setenv("JIRA_PROJECT", "DIG")
    monkeypatch.setenv("JIRA_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_USER", "reconciler-tests@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "test-api-token")
    return


# --------------------------------------------------------------------------- #
# AC1 + AC3 — one authority: scope captured at compose, ambient poisoned after #
# --------------------------------------------------------------------------- #
def test_cloud_scope_survives_poisoned_ambient_after_compose(monkeypatch) -> None:
    runtime = compose_reconciler_runtime()
    _poison(monkeypatch, *_CLOUD_RESOLVER)
    backend = runtime.build_backend()  # build must use captured settings
    assert backend.project == "DIG"
    assert backend.query_project == "DIG"


def test_datacenter_scope_survives_poisoned_ambient_after_compose(monkeypatch) -> None:
    runtime = compose_reconciler_runtime(
        cli_overrides={
            "reconciler.backend": "jira-datacenter",
            "reconciler.base_url": "https://jira.example.internal",
        }
    )
    _poison(monkeypatch, *_DC_RESOLVER)
    backend = runtime.build_backend()
    assert backend.project == "DIG"


def test_backend_project_stable_after_env_mutation(monkeypatch) -> None:
    """AC3: once composed, mutating JIRA_PROJECT does not change the captured
    scope; a fresh compose observes the new value."""
    runtime = compose_reconciler_runtime()
    backend = runtime.build_backend()
    assert backend.project == "DIG"

    monkeypatch.setenv("JIRA_PROJECT", "OTHER")
    assert backend.project == "DIG"  # captured — unchanged

    fresh = compose_reconciler_runtime().build_backend()
    assert fresh.project == "OTHER"  # new composition sees the mutation


# --------------------------------------------------------------------------- #
# AC2 — supplied config / custom tracker layout reaches read owners; root      #
#       isolation                                                              #
# --------------------------------------------------------------------------- #
def test_custom_tracker_layout_reaches_settings() -> None:
    runtime = compose_reconciler_runtime(
        cli_overrides={"tracker.dir": "custom-tracker", "tracker.branch": "mybranch"}
    )
    assert runtime.settings.tracker_dir.name == "custom-tracker"
    assert runtime.settings.tracker_branch == "mybranch"


def test_rootA_operation_unaffected_by_rootB_env_mutation(monkeypatch, tmp_path) -> None:
    root_a = tmp_path / "rootA"
    (root_a / ".git").mkdir(parents=True)
    runtime = compose_reconciler_runtime(repo_root=str(root_a))
    captured = str(runtime.settings.repo_root)

    monkeypatch.setenv("REBAR_ROOT", str(tmp_path / "rootB"))
    assert str(runtime.settings.repo_root) == captured
    assert captured == str(root_a)


# --------------------------------------------------------------------------- #
# AC4 — exactly the selected backend is constructed; fail-closed before I/O    #
# --------------------------------------------------------------------------- #
def test_only_selected_cloud_backend_is_constructed(monkeypatch) -> None:
    """Poison the REAL unselected (DC) backend class that ``_build_datacenter`` constructs;
    a Cloud compose+build must never touch it. (Poisoning the registry factory would be
    vacuous — the cloud path dispatches straight to ``_build_cloud`` and never calls it.)"""
    dc_backend = importlib.import_module("rebar_reconciler.adapters.jira_datacenter.backend")
    monkeypatch.setattr(
        dc_backend,
        "JiraDataCenterBackend",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("unselected DC backend constructed")),
        raising=True,
    )
    backend = compose_reconciler_runtime().build_backend()
    assert backend.project == "DIG"


def test_unknown_backend_fails_closed_before_construction(monkeypatch) -> None:
    """AC4: an invalid backend name fails closed with a typed ConfigError at COMPOSE time
    (the config layer validates the enum), never a bare KeyError and never a silent default
    to Cloud — so an unregistered backend can never reach transport construction."""
    with pytest.raises(ConfigError) as exc:
        compose_reconciler_runtime(cli_overrides={"reconciler.backend": "nonesuch"})
    assert "nonesuch" in str(exc.value)


def test_empty_cloud_scope_fails_closed_before_transport(monkeypatch) -> None:
    """AC4: empty Cloud read scope (jira.project) fails closed with the TYPED BackendEnvError
    at build time — before any transport/network client is constructed."""
    monkeypatch.setenv("JIRA_PROJECT", "")
    with pytest.raises(BackendEnvError) as exc:
        compose_reconciler_runtime().build_backend()
    assert "project" in str(exc.value).lower()


def test_missing_auth_fails_closed(monkeypatch) -> None:
    """AC4: a missing Cloud credential fails closed with the TYPED BackendEnvError before
    any transport is built (never a silent anonymous transport)."""
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    with pytest.raises(BackendEnvError) as exc:
        compose_reconciler_runtime().build_backend()
    assert "JIRA_API_TOKEN" in str(exc.value)


# --------------------------------------------------------------------------- #
# AC5 — secret canaries absent from every named boundary; adapter still auths  #
# --------------------------------------------------------------------------- #
def test_secret_token_absent_from_runtime_and_backend_repr(monkeypatch) -> None:
    sentinel = "s3cr3t-PAT-do-not-leak"
    monkeypatch.setenv("JIRA_API_TOKEN", sentinel)
    runtime = compose_reconciler_runtime()
    backend = runtime.build_backend()

    for blob in (repr(runtime), repr(runtime.settings), repr(backend), str(runtime)):
        assert sentinel not in blob


def test_secret_absent_from_dc_carrier_repr(monkeypatch) -> None:
    sentinel = "s3cr3t-DC-PAT"
    monkeypatch.setenv("JIRA_PAT", sentinel)
    runtime = compose_reconciler_runtime(
        cli_overrides={
            "reconciler.backend": "jira-datacenter",
            "reconciler.base_url": "https://jira.example.internal",
        }
    )
    assert sentinel not in repr(runtime)
    assert sentinel not in repr(runtime.settings)


def test_captured_credential_still_retrievable_for_auth(monkeypatch) -> None:
    """AC5 second half — 'adapter still auths': the secret is hidden from every repr but the
    captured auth carrier still REVEALS the exact token at authentication time (a repr that
    scrubbed the value by dropping it would pass the absence tests yet break real auth)."""
    sentinel = "s3cr3t-still-usable"
    monkeypatch.setenv("JIRA_API_TOKEN", sentinel)
    runtime = compose_reconciler_runtime()
    assert runtime.auth.reveal() == sentinel
    assert runtime.auth.present() is True
    assert sentinel not in repr(runtime)


# --------------------------------------------------------------------------- #
# override normalization — dotted + nested spellings both reach settings       #
# --------------------------------------------------------------------------- #
def test_mixed_dotted_and_nested_overrides_reach_settings() -> None:
    runtime = compose_reconciler_runtime(
        cli_overrides={
            "tracker.dir": "dotted-tracker",
            "tracker": {"branch": "nested-branch"},
        }
    )
    assert runtime.settings.tracker_dir.name == "dotted-tracker"
    assert runtime.settings.tracker_branch == "nested-branch"
