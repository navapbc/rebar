"""jira-datacenter backend registration + advertised capabilities.

Mirrors ``test_backend_registry`` for the Data Center adapter: the factory
registers as an import side-effect of ``rebar_reconciler.adapters``, and
``select_backend`` builds a :class:`JiraDataCenterBackend` that satisfies the
``Backend`` port plus the links / comments / absence-probe capabilities.
"""

from __future__ import annotations

import dataclasses

from rebar.config import load_config
from rebar_reconciler._backend import (
    Backend,
    SupportsAbsenceProbe,
    SupportsComments,
    SupportsLinks,
)
from rebar_reconciler._backend_registry import select_backend
from rebar_reconciler.adapters.jira_datacenter.backend import JiraDataCenterBackend


def _config_with_backend(key: str):
    base = load_config()
    return dataclasses.replace(base, reconciler=dataclasses.replace(base.reconciler, backend=key))


def test_select_backend_returns_jira_datacenter():
    backend = select_backend(_config_with_backend("jira-datacenter"))
    assert isinstance(backend, JiraDataCenterBackend)
    assert backend.vendor == "jira-datacenter"


def test_jira_datacenter_advertises_links_comments_probe():
    backend = select_backend(_config_with_backend("jira-datacenter"))
    assert isinstance(backend, Backend)
    assert isinstance(backend, SupportsLinks)
    assert isinstance(backend, SupportsComments)
    assert isinstance(backend, SupportsAbsenceProbe)
