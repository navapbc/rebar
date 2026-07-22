"""Backend registry + select_backend() — happy path (S3, epic bbf1).

The in-tree registry maps ``config.reconciler.backend`` to a factory that builds a
``Backend``. ``select_backend(config)`` lazily imports the adapters package (so the
JiraBackend factory registers as an import side-effect), looks up the factory, and
constructs it. No consumer is rewired here (that is S4).

Engine dir is on sys.path via the package conftest (flat imports).
"""

from __future__ import annotations

import dataclasses

from rebar.config import load_config
from rebar_reconciler._backend_registry import (
    _reset_registry_for_test,
    register,
    select_backend,
)
from rebar_reconciler.adapters.jira.backend import JiraBackend

from .backend_support import FakeBackend


def _config_with_backend(key: str):
    """A Config pointed at ``key`` — constructed directly so it bypasses the
    load-time ``_as_choice`` coercer (which only permits registered production keys)."""
    base = load_config()
    return dataclasses.replace(base, reconciler=dataclasses.replace(base.reconciler, backend=key))


def test_select_backend_returns_jira_by_default():
    backend = select_backend(load_config())
    assert isinstance(backend, JiraBackend)
    assert backend.vendor == "jira"


def test_register_then_select_finds_the_backend():
    with _reset_registry_for_test():
        register("temp-be")(lambda config: FakeBackend())
        assert isinstance(select_backend(_config_with_backend("temp-be")), FakeBackend)
