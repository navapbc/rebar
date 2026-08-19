"""Typed config resolution for the Data Center transport (story J6, epic e369).

Unlike Cloud's ``acli_subprocess.resolve_jira_settings`` — which CATCHES
``ConfigError`` and degrades to env-only defaults — this resolution FAILS LOUD:
``load_config()`` is called directly and any ``ConfigError`` it raises propagates
straight to the caller. PR #120 (the hand-rolled DC client this story replaces)
swallowed ``ConfigError``, which is against rebar's fail-loud posture; a malformed
``[tool.rebar.reconciler]`` / ``[tool.rebar.jira]`` section must never quietly
degrade into a confusing downstream connection failure.

Where each setting comes from:

* ``url`` / ``allow_insecure`` / ``ca_bundle`` — the
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
    """Resolve THIS instance's comment ceiling (characters) — bug 049e.

    PROVENANCE OF THE DEFAULT. Data Center caps comment bodies with the advanced
    setting ``jira.text.field.character.limit``, defined in Jira's own ``jpm.xml``
    as::

        description:   "The maximum number of characters to be entered for a single
                        field. Affects Description, Environment, Comments and Text
                        custom fields. 0 means unlimited."
        default-value: 32767

    JRASERVER-28519 (Resolved/Fixed, fix versions 6.4.1 / 7.0.0) records that
    "starting with JIRA 7.0 and Cloud, ``jira.text.field.character.limit`` will be
    set to 32767 by default". So 32767 is CORRECT for a stock instance and is kept
    as the default of ``[tool.rebar.reconciler].comment_max_chars`` (env override
    ``REBAR_RECONCILER_COMMENT_MAX_CHARS``) — but it is only a DEFAULT: the
    property is admin-settable over ``0..2147483647``, with ``0`` meaning
    UNLIMITED, and rebar must not impose the default on an instance that raised it.

    NOT DISCOVERED FROM THE INSTANCE, deliberately. The value is exposed only via
    ``/rest/api/2/application-properties`` (and its ``/advanced-settings``
    sub-resource), whose documented requirement is the "Administer Jira" GLOBAL
    permission; rebar authenticates as an ordinary user's PAT, so the probe would
    403 for exactly the operators who need it. Discovery is therefore dropped and
    configuration is the whole remedy — see the finding recorded on bug
    ``049e-9fac-a821-4ea2``.

    A non-positive value is returned as ``0`` = unlimited (jpm.xml's own
    convention), which the DC comment truncator treats as "never truncate".
    """
    from rebar.config import resolve_dc_comment_max_chars

    return resolve_dc_comment_max_chars()


def resolve_jira_datacenter_settings() -> JiraDataCenterSettings:
    """Resolve DC connection settings through the typed config — FAIL LOUD.

    No ``except ConfigError`` here: an invalid ``[tool.rebar.reconciler]`` or
    ``[tool.rebar.jira]`` value raises straight out of ``load_config()`` to the
    caller, rather than degrading to env-only defaults (the acli_subprocess
    pattern this story's plan explicitly rejects — see the module docstring).
    """
    from rebar.config import resolve_dc_connection

    url, project, allow_insecure, ca_bundle = resolve_dc_connection()
    # NOTE — the missing-PAT guard deliberately does NOT live here, and that is not an
    # oversight. This function is reached from PROPERTIES (`JiraDataCenterBackend.
    # query_project`), and on Python <= 3.11 `isinstance(x, SomeRuntimeCheckableProtocol)`
    # evaluates properties via `hasattr`, so a raise here breaks every Protocol conformance
    # check. (Python 3.12+ switched to `inspect.getattr_static`, which does NOT execute
    # properties — so this failure is INVISIBLE on a modern local interpreter and only
    # appears on the CI matrix's 3.11 leg. Measured: two backend-facade tests passed on
    # 3.14 and failed on 3.11.) Resolution stays total; the guard lives at client
    # construction — see `build_client_from_settings` (bug cd78).
    pat = os.environ.get("JIRA_PAT", "")

    return JiraDataCenterSettings(
        url=url,
        project=project,
        allow_insecure=allow_insecure,
        ca_bundle=ca_bundle,
        pat=pat,
    )
