"""Jira Data Center connection settings (Personal Access Token / bearer auth).

Mirrors ``adapters/jira/acli_subprocess.resolve_jira_settings`` for a Jira Server /
Data Center instance: ``url`` and ``project`` come from the typed config (the
``JIRA_URL`` / ``JIRA_PROJECT`` env vars override the ``[tool.rebar.jira]`` file), and
the secret is a Personal Access Token read env-only from ``JIRA_PAT``. Unlike Jira
Cloud there is no user/email: a Data Center PAT is bearer-authenticated and is bound
to its owner server-side.
"""

from __future__ import annotations

import os
from typing import NamedTuple


class JiraDataCenterSettings(NamedTuple):
    """Resolved connection settings: the non-secret ``url`` / ``project`` (typed
    config) plus the secret ``pat`` (env-only)."""

    url: str
    project: str
    pat: str


def resolve_jira_dc_settings(*, project_default: str = "") -> JiraDataCenterSettings:
    """Resolve Data Center settings through the single config entry point.

    ``url`` / ``project`` come from ``load_config().jira.*`` so a
    ``[tool.rebar.jira]`` value is consumed, with ``JIRA_URL`` / ``JIRA_PROJECT``
    overriding the file. The secret ``JIRA_PAT`` is read from the environment ONLY.
    A malformed config degrades to env-only resolution rather than breaking a pass.
    """
    from rebar.config import ConfigError, load_config

    try:
        jira = load_config().jira
        url, project = jira.url, jira.project
    except ConfigError:
        url = os.environ.get("JIRA_URL", "")
        project = os.environ.get("JIRA_PROJECT", "")
    return JiraDataCenterSettings(
        url=url,
        project=project or project_default,
        pat=os.environ.get("JIRA_PAT", ""),
    )
