"""HELD-OUT edge/teeth tests for the backend registry (S3, epic bbf1).

The semantics the happy path can't pin: idempotent vs conflicting registration, the
snapshot/restore round-trip, the unknown-key error, the transport equivalence with the
pre-story direct construction, and the teeth check that selection is honoured (not
hard-coded to Jira). Held out from the implementation subagent.
"""

from __future__ import annotations

import dataclasses

import pytest

from rebar.config import load_config
from rebar_reconciler._backend_registry import (
    BackendRegistryError,
    _reset_registry_for_test,
    register,
    select_backend,
)
from rebar_reconciler.adapters.jira.backend import JiraBackend

from .backend_support import FakeBackend


def _config_with_backend(key: str):
    base = load_config()
    return dataclasses.replace(base, reconciler=dataclasses.replace(base.reconciler, backend=key))


def test_register_same_key_and_factory_is_idempotent():
    factory = lambda config: FakeBackend()  # noqa: E731
    with _reset_registry_for_test():
        register("dup")(factory)
        register("dup")(factory)  # no-op, must not raise
        assert isinstance(select_backend(_config_with_backend("dup")), FakeBackend)


def test_register_conflicting_factory_raises():
    with _reset_registry_for_test():
        register("clash")(lambda config: FakeBackend())
        with pytest.raises(BackendRegistryError):
            register("clash")(lambda config: FakeBackend())  # different object


def test_reset_registry_round_trips():
    # A key registered inside the context is gone after it (state restored).
    with _reset_registry_for_test():
        register("ephemeral")(lambda config: FakeBackend())
        assert isinstance(select_backend(_config_with_backend("ephemeral")), FakeBackend)
    with pytest.raises(BackendRegistryError):
        select_backend(_config_with_backend("ephemeral"))


def test_select_unknown_backend_raises_naming_registered_keys():
    with pytest.raises(BackendRegistryError) as exc:
        select_backend(_config_with_backend("does-not-exist"))
    assert "jira" in str(exc.value)


def test_jira_transport_matches_resolve_jira_settings():
    # Equivalence: select_backend builds a JiraBackend whose transport is an
    # AcliClient constructed from the SAME resolve_jira_settings() values as the
    # pre-story direct construction (asserted on resolved settings, not a live call).
    from rebar_reconciler.adapters.jira import acli, acli_subprocess

    settings = acli_subprocess.resolve_jira_settings()
    transport = select_backend(load_config()).transport
    assert isinstance(transport, acli.AcliClient)
    assert transport.jira_url == settings.url
    assert transport.user == settings.user
    assert transport.api_token == settings.api_token


def test_teeth_selection_is_honoured_not_hardcoded():
    with _reset_registry_for_test():
        register("teeth-fake")(lambda config: FakeBackend())
        selected = select_backend(_config_with_backend("teeth-fake"))
        assert isinstance(selected, FakeBackend)
        assert not isinstance(selected, JiraBackend)
    with pytest.raises(BackendRegistryError):
        select_backend(_config_with_backend("teeth-fake"))
