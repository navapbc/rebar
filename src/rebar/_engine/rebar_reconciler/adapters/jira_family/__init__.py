"""``rebar_reconciler.adapters.jira_family`` — the Jira-family shared layer (story J2,
epic e369).

Jira Cloud and Jira Data Center share a REST-ish issue model, so the units that are
Jira-family-*general* (not Cloud-specific) live here, under **public** names, so a
second Jira-family backend consumes ONE implementation instead of forking Cloud's
(the mistake PR #120 made, which ADR 0035 never sanctioned).

What lives here, and why:

* the local <-> Jira value maps (``LOCAL_STATUS_TO_JIRA`` / ``LOCAL_PRIORITY_TO_JIRA``
  / ``LOCAL_TYPE_TO_JIRA``) and the link-relation vocabulary
  (``RELATION_TO_JIRA_LINK``) — pure data, Jira-family general;
* the field sanitizers (``sanitize_label`` / ``sanitize_summary`` are pure; ``sanitize_
  description`` / ``sanitize_comment`` take their vendor dependency (the rich-text fit
  function / the comment-truncation function) as an INJECTED contract parameter,
  never an import — Cloud and DC each construct their own vendor-bound wrapper);
* the ``rebar-id:`` identity convention (``JiraIdentityConvention``);
* the absence-probe classifier (``classify_probe_response``), which takes the
  resolved-status set as a REQUIRED keyword-only parameter rather than a baked-in
  constant — Cloud/DIG's ``Resolved``/``Done``/``Cancelled`` are workflow names a
  self-hosted DC install could name differently.

**Dependency direction is one-way**: this package imports NOTHING from
``adapters/jira/`` or ``adapters/jira_datacenter/`` — no ``adf``, no
``comment_limits``, no ``outbound_fields``, no ``acli*``. Concrete backends import
this package; it never imports them back. See
``docs/adr/0035-reconciler-vendor-adapter-seam.md``.
"""

from __future__ import annotations

from rebar_reconciler.adapters.jira_family.deployment import instance_from_base_url
from rebar_reconciler.adapters.jira_family.identity import JiraIdentityConvention
from rebar_reconciler.adapters.jira_family.probe import classify_probe_response
from rebar_reconciler.adapters.jira_family.sanitizers import (
    InvalidLabelError,
    sanitize_comment,
    sanitize_description,
    sanitize_label,
    sanitize_summary,
)
from rebar_reconciler.adapters.jira_family.value_maps import (
    JIRA_LABEL_MAX_CHARS,
    JIRA_SUMMARY_MAX_CHARS,
    LOCAL_PRIORITY_TO_JIRA,
    LOCAL_STATUS_TO_JIRA,
    LOCAL_TYPE_TO_JIRA,
    RELATION_TO_JIRA_LINK,
)

__all__ = [
    "JIRA_LABEL_MAX_CHARS",
    "JIRA_SUMMARY_MAX_CHARS",
    "LOCAL_PRIORITY_TO_JIRA",
    "LOCAL_STATUS_TO_JIRA",
    "LOCAL_TYPE_TO_JIRA",
    "RELATION_TO_JIRA_LINK",
    "InvalidLabelError",
    "JiraIdentityConvention",
    "classify_probe_response",
    "instance_from_base_url",
    "sanitize_comment",
    "sanitize_description",
    "sanitize_label",
    "sanitize_summary",
]
