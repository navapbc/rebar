"""Resolve typed Jira Data Center settings with fail-loud config errors.

URL and TLS options come from reconciler config. The Jira-family project uses
``JIRA_PROJECT`` precedence. The bearer PAT comes only from ``JIRA_PAT`` so
credentials cannot enter file config. The returned named tuple is an immutable
settings snapshot.
"""

from __future__ import annotations

import os
from typing import NamedTuple


class JiraDataCenterSettings(NamedTuple):
    """Resolved DC connection settings: the non-secret ``url``/``project``/
    ``allow_insecure``/``ca_bundle`` (typed config) plus the secret ``pat`` (env-only).

    A ``resolved_statuses`` member was dropped by task 549c: it carried
    ``reconciler.resolved_statuses`` to a transport attribute that nothing ever read, once
    task f020 deleted the inbound absence probe. The config key itself is now gone too,
    removed by task f408 and left as a warn-class tombstone."""

    url: str
    project: str
    allow_insecure: bool
    ca_bundle: str
    pat: str


def resolve_comment_max_chars() -> int:
    """Resolve the configured comment ceiling in characters.

    The default is 32767. Zero means unlimited. Configuration replaces instance
    discovery because the required Jira endpoint needs administrator permission.
    """
    from rebar.config import resolve_dc_comment_max_chars

    return resolve_dc_comment_max_chars()


def resolve_jira_datacenter_settings() -> JiraDataCenterSettings:
    """Resolve Data Center settings and propagate config errors unchanged."""
    from rebar.config import resolve_dc_connection

    url, project, allow_insecure, ca_bundle = resolve_dc_connection()
    # Keep settings resolution total because protocol property checks can execute it.
    # Client construction enforces the environment-only PAT requirement.
    pat = os.environ.get("JIRA_PAT", "")

    return JiraDataCenterSettings(
        url=url,
        project=project,
        allow_insecure=allow_insecure,
        ca_bundle=ca_bundle,
        pat=pat,
    )
