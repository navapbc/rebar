"""Cross-project safety guard + HEAD-drift detection for the legacy batch applier.

Split out of ``applier.py`` for module-size headroom (ticket 7153-e5ad-5e20-4ae9,
ahead of the typed-payload cutover). This cluster is self-contained: the two
exception types (``HeadDriftError`` / ``CrossProjectTargetError``) are raised by
the two pure/near-pure checks below (``_cross_project_targets`` / ``_recheck_drift``)
and by nothing else in the package.

Re-exported from ``applier.py`` (mirroring the ``apply_base`` / ``apply_handlers`` /
``apply_inbound`` / ``apply_outbound`` / ``batch_dispatch`` / ``pass_io`` /
``rebar_id_audit`` / ``typed_dispatch`` splits already in that file) so
``applier.<name>`` keeps resolving for ``_apply_batch``'s own bare-name references,
``batch_dispatch.py``'s ``applier.HeadDriftError`` attribute read, and the test
suite.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

from rebar_reconciler.apply_outbound import _drift_is_benign, _get_commit_subject


class HeadDriftError(Exception):
    """Raised when the tickets-branch HEAD changes mid-pass, indicating concurrent write."""


class CrossProjectTargetError(Exception):
    """Raised when an outbound mutation targets a Jira project other than jira.project.

    A fail-closed safety guard (bug 626d): stale bindings/labels from a prior sync to
    another project would otherwise silently push updates/deletes at the wrong
    project's issues. Raised pre-flight (before any Jira write) so a misconfiguration
    cannot leak even a single mutation.
    """


# A real Jira issue key: PROJECTKEY-NUMBER (e.g. "DIG-1234"). Create mutations
# carry a local-id placeholder here, not a real key, so they don't match.
_JIRA_KEY_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_]*)-\d+$")


def _cross_project_targets(
    mutations: list[dict], allowed: str | Iterable[str]
) -> list[tuple[str, str]]:
    """Return ``(key, project)`` for outbound update/delete mutations whose target
    Jira key belongs to a project OUTSIDE the allowed set.

    ``allowed`` is either a single configured project (a bare string — the legacy
    single-project case) or the store's project SET (story d19d, many-to-many): a
    mutation targeting any project in the set passes. Creates are excluded — their
    ``key`` is a local-id placeholder and their project is resolved on the create
    path, not here. Inbound mutations are excluded. An empty/unset ``allowed``
    disables the check (returns ``[]``) so it never fires on shims that don't
    configure a project.
    """
    if isinstance(allowed, str):
        allowed_set = {allowed.upper()} if allowed else set()
    else:
        allowed_set = {str(p).upper() for p in allowed if p}
    if not allowed_set:
        return []
    offenders: list[tuple[str, str]] = []
    for m in mutations:
        if (m.get("direction") or "outbound") == "inbound":
            continue
        if m.get("action") not in ("update", "delete"):
            continue
        key = str(m.get("key") or m.get("local_id") or "")
        match = _JIRA_KEY_RE.match(key)
        if not match:
            continue
        proj = match.group(1).upper()
        if proj not in allowed_set:
            offenders.append((key, match.group(1)))
    return offenders


def _recheck_drift(concurrency, repo_root: Path, head_pin: str) -> str:
    """Re-check the tickets-branch HEAD before a mutation; return the (possibly
    refreshed) pin, or raise HeadDriftError on a competing reconciler write.

    Bug f058: the tickets orphan branch is shared with the ticket CLI
    (auto-commits via rebar create / transition / etc.) and the suggestion
    subsystem. A parallel Claude session running `rebar transition <id> closed`
    triggers auto-compact, which commits `ticket: COMPACT <id>` to tickets — that
    doesn't conflict with the in-flight outbound mutations, but a strict-equality
    drift check would abort the pass. Resolution: inspect the intervening commit's
    subject. If it matches a benign external pattern (ticket-CLI, suggestion,
    pass-lock), refresh the pin and continue. Only raise HeadDriftError when the
    subject indicates a competing reconciler outbound write — the original intent
    of the detector.
    """
    current_head = concurrency.snapshot_head(repo_root)
    if current_head == head_pin:
        return head_pin
    drift_subject = _get_commit_subject(repo_root, current_head)
    if _drift_is_benign(drift_subject):
        # Benign external writer — accept the new HEAD and continue. Log so
        # operators can see the writer.
        print(
            f"tolerated_drift: {head_pin[:8]}→{current_head[:8]} subject={drift_subject!r}",
            file=sys.stderr,
        )
        return current_head
    raise HeadDriftError(f"drift: {head_pin[:8]}→{current_head[:8]} subject={drift_subject!r}")
