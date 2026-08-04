"""Inbound probe — neutral vocabulary for classifying issues that disappeared from
a vendor's working set.

When a local ticket's bound remote key vanishes from a fetcher pass, a backend's
absence-probe capability (``SupportsAbsenceProbe.probe_remote``, see ``_backend.py``)
fetches the item directly and classifies the result into one of 4 branches:

  1. PRESENT_RESOLVED    — item still exists; status was changed to
                           Resolved/Done/Cancelled (out of working set)
  2. PRESENT_FILTERED    — item still exists but no longer matches the working-set
                           filter for other reasons
  3. ARCHIVED_OR_MOVED   — the item has been deleted, archived, or moved out of scope
  4. UNREACHABLE         — transient network / auth error; do not classify, leave for retry

This module holds ONLY the vendor-neutral vocabulary (``ProbeBranch``, ``ProbeResult``,
``ProbeConfigError``); the Jira mechanics live in ``adapters/jira/probe.py``. It stays
at the package root (loaded by filename elsewhere) and MUST NOT import anything from
``adapters.jira``.

DORMANT (bug 3b5f). This vocabulary and the ``SupportsAbsenceProbe`` capability that
returns it currently have NO consumer: the only producer of ``(inbound, probe)``
mutations could never fire from the real call site and was removed, along with the
dispatch arm and the router. They are kept deliberately rather than deleted, because
removing the DC adapter's ``probe_remote`` would remove DC's only import of the shared
``classify_probe_response``, leaving that classifier Cloud-only and weakening epic
``e369``'s AC5 (one implementation in ``jira_family`` imported by BOTH backends).
Trading an epic-level AC to delete a dormant Protocol is a separate decision; see the
follow-up ticket ``f020-0960-2c42-4d61`` and ADR 0028's corrected Consequences. Deletion detection
itself is NOT dormant — it lives outbound, in ``outbound_differ._safe_get_issue`` +
``binding_walk``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ProbeConfigError(RuntimeError):
    """Raised when required env vars are missing."""


class ProbeBranch(StrEnum):
    PRESENT_RESOLVED = "present_resolved"
    PRESENT_FILTERED = "present_filtered"
    ARCHIVED_OR_MOVED = "archived_or_moved"
    UNREACHABLE = "unreachable"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    branch: ProbeBranch
    issue_key: str
    detail: dict[str, Any]
