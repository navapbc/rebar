"""Typed config resolution for the Data Center transport (story J6, epic e369).

Unlike Cloud's ``acli_subprocess.resolve_jira_settings`` — which CATCHES
``ConfigError`` and degrades to env-only defaults — this resolution FAILS LOUD:
``load_config()`` is called directly and any ``ConfigError`` it raises propagates
straight to the caller. PR #120 (the hand-rolled DC client this story replaces)
swallowed ``ConfigError``, which is against rebar's fail-loud posture; a malformed
``[tool.rebar.reconciler]`` / ``[tool.rebar.jira]`` section must never quietly
degrade into a confusing downstream connection failure.

Where each setting comes from:

* ``url`` / ``allow_insecure`` / ``ca_bundle`` / ``resolved_statuses`` — the
  vendor-neutral ``[tool.rebar.reconciler]`` section (``rebar._config_schema``).
  These are Data-Center-only reconciler concerns (Cloud drives an ACLI
  subprocess and never resolves a base URL or a TLS/CA setting here), so they
  live on ``ReconcilerConfig`` rather than the Cloud-owned ``JiraConfig``. The
  TLS/scheme validator (non-``https`` rejected unless ``allow_insecure=true``)
  runs INSIDE ``ReconcilerConfig`` at config-load time (``_config_schema.py``),
  so a rejected URL never reaches this module in the first place — by the time
  ``resolve_jira_datacenter_settings`` returns, ``url`` is already validated.
* ``project`` — reuses ``[tool.rebar.jira].project`` (env override
  ``JIRA_PROJECT``), the one project setting every Jira-family backend shares.
* ``JIRA_PAT`` — ENV-ONLY, never a file-config key (so a Personal Access Token,
  Jira 8.14+'s bearer-auth credential, can never be committed to a config file).
"""

from __future__ import annotations

import os
from typing import NamedTuple

#: DC's absence-probe default resolved-status set, identical to Cloud/DIG's
#: (`adapters/jira/probe.py::RESOLVED_STATUS_NAMES`) — the sensible default for
#: an unconfigured `[tool.rebar.reconciler].resolved_statuses`, overridable per
#: self-hosted workflow.
DEFAULT_RESOLVED_STATUSES: frozenset[str] = frozenset({"Resolved", "Done", "Cancelled"})


class JiraDataCenterSettings(NamedTuple):
    """Resolved DC connection settings: the non-secret ``url``/``project``/
    ``allow_insecure``/``ca_bundle``/``resolved_statuses`` (typed config) plus the
    secret ``pat`` (env-only)."""

    url: str
    project: str
    allow_insecure: bool
    ca_bundle: str
    resolved_statuses: frozenset[str]
    pat: str


def resolve_jira_datacenter_settings() -> JiraDataCenterSettings:
    """Resolve DC connection settings through the typed config — FAIL LOUD.

    No ``except ConfigError`` here: an invalid ``[tool.rebar.reconciler]`` or
    ``[tool.rebar.jira]`` value raises straight out of ``load_config()`` to the
    caller, rather than degrading to env-only defaults (the acli_subprocess
    pattern this story's plan explicitly rejects — see the module docstring).
    """
    from rebar.config import ConfigError, load_config

    config = load_config()
    reconciler = config.reconciler
    resolved_statuses = (
        frozenset(reconciler.resolved_statuses)
        if reconciler.resolved_statuses
        else DEFAULT_RESOLVED_STATUSES
    )
    # FAIL CLOSED on a missing credential. `os.environ.get("JIRA_PAT", "")` used to hand an
    # EMPTY bearer token to `token_auth=` (transport.py), which constructs fine and then
    # issues every request ANONYMOUSLY. Two consequences made that worse than a plain error:
    #
    #   * it MISATTRIBUTES — Jira answers an anonymous search with "The value 'X' does not
    #     exist for the field 'project'" (it hides projects the caller cannot browse rather
    #     than leaking their existence), so an operator who simply forgot the export goes
    #     hunting project keys and permissions. Observed live, CI run 30652534806.
    #   * on an instance where anonymous CAN browse, there is no error at all: the pass reads
    #     a partial or empty view and reports a converged, successful run.
    #
    # `docs/user-guide.md` already promises this behaviour as a security property ("a missing
    # JIRA_PAT fails with an error naming the variable rather than falling back to anonymous
    # access"); this is the code catching up with the documented contract (bug cd78).
    #
    # Checked HERE, at resolution, rather than in `build_client_from_settings`: this is the
    # single choke point every DC path (CLI, library, MCP) goes through, and it guarantees
    # the failure happens before anything can reach the network. A blank value is rejected
    # alongside an absent one — `export JIRA_PAT=` is the same mistake wearing a different
    # shape.
    pat = os.environ.get("JIRA_PAT", "")
    if not pat.strip():
        raise ConfigError(
            "JIRA_PAT is not set. The Jira Data Center backend authenticates with a "
            "Personal Access Token read from the environment — it is environment-only and "
            "is never accepted from a config file, so the credential cannot be committed by "
            "accident. Export it before reconciling:\n"
            "    export JIRA_PAT=<your personal access token>\n"
            "Without it the reconciler would fall back to ANONYMOUS access, which typically "
            'surfaces as a misleading "project does not exist" error (Jira hides projects '
            "you cannot browse) or, on a permissive instance, as a silently empty pass."
        )

    return JiraDataCenterSettings(
        url=reconciler.base_url,
        project=config.jira.project,
        allow_insecure=reconciler.allow_insecure,
        ca_bundle=reconciler.ca_bundle,
        resolved_statuses=resolved_statuses,
        pat=pat,
    )
