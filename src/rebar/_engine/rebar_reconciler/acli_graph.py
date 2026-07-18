#!/usr/bin/env python3
"""AcliClient graph/relationship mixin.

The graph-shaped Jira operations of the ACLI client: additive label add/remove,
issue links (create/list/delete + link-type list), bulk parent/comment maps via
paged REST search, comment update/delete, and priority/issuetype edits. Some
dispatch through the ACLI subprocess (``self._run``), others through the REST
helpers (``self._direct_rest_*``) — both resolved via the AcliClient MRO.

Mixed into ``AcliClient`` (``acli.py``); method bodies are unchanged from the
monolith.
"""

from __future__ import annotations

import json
import os
import tempfile
import urllib.error
from typing import TYPE_CHECKING, Any

from rebar_reconciler.adapters.jira.jira_fields import _sanitize_label

if TYPE_CHECKING:
    import subprocess

# ---------------------------------------------------------------------------
# Relation <-> Jira link-type mapping (story 25ae-92e6-2927-49b6, Cycle 1)
# ---------------------------------------------------------------------------
#
# Shared by BOTH differs (outbound_differ / inbound_differ import from here) so
# the relation<->link-type vocabulary lives in exactly one place — mirrors the
# ``_LOCAL_TO_JIRA_*`` constant pattern in outbound_differ.py.
#
# Each entry maps a rebar relation to a tuple ``(jira_link_type, swap_endpoints)``
# where ``swap_endpoints`` records that the rebar direction (A relation B) maps
# to the Jira link with the endpoints reversed (B link A). For ``depends_on``,
# "A depends_on B" is equivalent to the Jira "B blocks A".
#
# Relations with no reliable Jira link type (``duplicates`` / ``supersedes`` /
# ``discovered_from``) are intentionally ABSENT: callers SKIP them (no-op, log a
# single line), never fail on them.
_RELATION_TO_JIRA_LINK: dict[str, tuple[str, bool]] = {
    "blocks": ("Blocks", False),
    "depends_on": ("Blocks", True),  # A depends_on B == B blocks A
    "relates_to": ("Relates", False),
}

# (The Jira-link-type -> rebar-relation direction map now lives once in the
# link_direction module; this dead re-declaration was removed in the bug-4b59
# unification. Nothing in acli_graph referenced it.)


class AcliGraphMixin:
    """Labels, links, parent/comment maps, and field-edit ops for AcliClient."""

    if TYPE_CHECKING:
        # Transport helpers provided by the composed ``AcliClient`` (``_run``
        # from acli.py; ``_direct_rest_*`` from AcliRestMixin), resolved via the
        # MRO at runtime. Declared type-only so mypy sees this mixin's surface.
        def _run(
            self,
            cmd: list[str],
            *,
            retry_on_timeout: bool = ...,
        ) -> subprocess.CompletedProcess[str]: ...

        def _direct_rest_get(self, path: str) -> Any: ...

        def _direct_rest_put_raw(self, path: str, body: Any) -> None: ...

        def _direct_rest_post_json(self, path: str, body: Any) -> Any: ...

        def _direct_rest_delete(self, path: str) -> None: ...

    def get_issue_link_types(self) -> list[dict[str, Any]]:
        """Return all available Jira issue link types via ACLI.

        Uses ``jira workitem link type list --json`` to query Jira for the
        full set of configured link types. Returns a list of dicts, each
        containing at minimum ``id`` and ``name`` fields (plus ``inward``
        and ``outward`` when the ACLI response includes them).

        Raises subprocess.CalledProcessError on ACLI failure.
        """
        cmd = [
            "jira",
            "workitem",
            "link",
            "type",
            "list",
            "--json",
        ]
        result = self._run(cmd, retry_on_timeout=True)  # READ — idempotent
        parsed = json.loads(result.stdout or "[]")
        if isinstance(parsed, list):
            return parsed
        # Some ACLI versions wrap the list in a dict under "issueLinkTypes"
        if isinstance(parsed, dict) and "issueLinkTypes" in parsed:
            return parsed["issueLinkTypes"]
        return []

    def add_label(self, jira_key: str, label: str) -> None:
        # Sanitize before reaching ACLI so we fail fast on invalid labels rather
        # than emitting a malformed mutation against live Jira.
        label = _sanitize_label(label)
        return self._add_label_impl(jira_key, label)

    def _add_label_impl(self, jira_key: str, label: str) -> None:
        """Additively add a label to a Jira issue via ACLI workitem edit.

        Uses ``acli jira workitem edit --from-json <file> --yes`` with payload
        ``{"issues": ["<KEY>"], "labelsToAdd": ["<label>"]}``. The ``labelsToAdd``
        operation is ADDITIVE — existing labels are preserved (verified live
        against DIG-3802 2026-05-24 per bug c916-74a1-ed06-40e4).

        Per ACLI v1.3.18:
          - The singular ``--label`` flag DOES NOT EXIST and is rejected with
            'unknown flag: --label'.
          - The plural ``--labels`` flag is a SET-REPLACE — passing
            ``--labels foo`` clobbers all existing labels, leaving only ``foo``.
            That semantic is incompatible with the reconciler's conflict policy
            ('additive content merged inbound: labels added') because it would
            destroy Jira-only labels on every rebar-id stamp.
          - The ``--from-json`` payload schema (exposed via
            ``acli jira workitem edit --generate-json``) includes
            ``labelsToAdd`` and ``labelsToRemove`` as the documented additive
            operations. This is the correct surface.
          - ``--from-json`` writes require ``--yes`` to skip the interactive
            'You're about to edit N work item(s). (y/N)' prompt.

        The ``--from-json`` path is single-call (no read-then-write race) and
        idempotent at the ACLI layer — calling with a label that already
        exists on the issue succeeds silently.
        """
        payload = {"issues": [jira_key], "labelsToAdd": [label]}
        fd, json_path = tempfile.mkstemp(suffix=".json", prefix="acli-edit-")
        fd_owned = False
        try:
            with os.fdopen(fd, "w") as f:
                fd_owned = True
                json.dump(payload, f)
        except Exception:  # noqa: BLE001 — fd cleanup on write failure: close the fd if unowned, then re-raise (never swallowed)
            if not fd_owned:
                os.close(fd)
            raise
        try:
            # Bug 44de: --json so _run_acli can parse the structured-failure
            # shape and raise AcliMutationError on exit=0 + FAILURE result.
            cmd = [
                "jira",
                "workitem",
                "edit",
                "--from-json",
                json_path,
                "--yes",
                "--json",
            ]
            self._run(cmd)
        finally:
            os.unlink(json_path)

    def remove_label(self, jira_key: str, label: str) -> None:
        # Sanitize so we reject obviously-malformed label values before issuing
        # the mutation. ACLI may accept invalid labels silently in remove mode.
        label = _sanitize_label(label)
        return self._remove_label_impl(jira_key, label)

    def _remove_label_impl(self, jira_key: str, label: str) -> None:
        """Additively remove a label from a Jira issue via ACLI workitem edit.

        Counterpart to ``add_label``. Uses ``--from-json`` with the
        ``labelsToRemove`` operation, which is target-specific — only the
        named label is removed; all other labels are preserved. Verified
        live against DIG-3802 2026-05-24 per bug c916-74a1-ed06-40e4.

        Idempotent at the ACLI layer — calling with a label that does not
        exist on the issue succeeds silently.
        """
        payload = {"issues": [jira_key], "labelsToRemove": [label]}
        fd, json_path = tempfile.mkstemp(suffix=".json", prefix="acli-edit-")
        fd_owned = False
        try:
            with os.fdopen(fd, "w") as f:
                fd_owned = True
                json.dump(payload, f)
        except Exception:  # noqa: BLE001 — fd cleanup on write failure: close the fd if unowned, then re-raise (never swallowed)
            if not fd_owned:
                os.close(fd)
            raise
        try:
            # Bug 44de: --json for structured-failure detection (see add_label).
            cmd = [
                "jira",
                "workitem",
                "edit",
                "--from-json",
                json_path,
                "--yes",
                "--json",
            ]
            self._run(cmd)
        finally:
            os.unlink(json_path)

    def get_parent_map(
        self,
        project: str,
        jql: str | None = None,
    ) -> dict[str, str | None]:
        """Return a {jira_key → parent_key | None} map via REST search.

        Issues one paged REST search (POST ``/rest/api/3/search/jql``) with
        ``fields=["parent"]`` so we get parent data without hitting ACLI's
        field-selector restriction (ACLI rejects ``-f parent``).

        Endpoint contract (ticket 8b25, live-proven): the legacy
        ``POST /rest/api/3/search`` endpoint is RETIRED (HTTP 410). The
        replacement ``/rest/api/3/search/jql`` paginates via an opaque
        ``nextPageToken`` cursor — there is NO ``total`` field and sending
        ``startAt`` is rejected with HTTP 400. The first request body carries
        ``{jql, fields, maxResults}``; each subsequent request adds
        ``{nextPageToken: <token>}``. The loop terminates when the response
        reports ``isLast: true`` or yields a null/absent ``nextPageToken``.

        Paginates until the cursor is exhausted.  Returns an empty dict and
        logs on any REST failure (fetcher degrades gracefully — ticket 8b25).
        An HTTP 410 (endpoint retirement) is logged at ERROR (loud — API
        retirements must be noticed); transient faults stay at WARNING.

        Args:
            project: Jira project key (e.g. "DIG").
            jql: Optional JQL override.  Defaults to ``project = <project>``.
        """
        import logging as _logging

        _log = _logging.getLogger(__name__)

        effective_jql = jql or f"project = {project}"
        result: dict[str, str | None] = {}
        page_size = 100
        next_page_token: str | None = None

        while True:
            body: dict[str, Any] = {
                "jql": effective_jql,
                "maxResults": page_size,
                "fields": ["parent"],
            }
            if next_page_token is not None:
                body["nextPageToken"] = next_page_token
            try:
                resp = self._direct_rest_post_json("/rest/api/3/search/jql", body)
            except urllib.error.HTTPError as exc:
                if exc.code == 410:
                    _log.error(
                        "get_parent_map: endpoint POST /rest/api/3/search/jql "
                        "returned HTTP 410 GONE — the Jira search endpoint has "
                        "been RETIRED; parent enrichment is unavailable this pass. "
                        "This is an API retirement, not a transient fault: %r",
                        exc,
                    )
                else:
                    _log.warning(
                        "get_parent_map: REST search failed (HTTP %s): %r; "
                        "degrading gracefully — parent data absent this pass",
                        exc.code,
                        exc,
                    )
                break
            except Exception as exc:  # noqa: BLE001 — fail-open: degrade gracefully, parent data absent
                _log.warning(
                    "get_parent_map: REST search failed: %r; "
                    "degrading gracefully — parent data will be absent this pass",
                    exc,
                )
                break

            if not isinstance(resp, dict):
                break
            issues = resp.get("issues") or []
            if not isinstance(issues, list):
                break
            for issue in issues:
                if not isinstance(issue, dict):
                    continue
                key = issue.get("key")
                if not key:
                    continue
                fields = issue.get("fields") or {}
                parent_raw = fields.get("parent")
                parent_key_val: str | None = None
                if isinstance(parent_raw, dict):
                    parent_key_val = parent_raw.get("key") or None
                result[key] = parent_key_val

            # nextPageToken cursor contract: stop when isLast or token absent.
            if resp.get("isLast"):
                break
            next_page_token = resp.get("nextPageToken")
            if not next_page_token:
                break

        return result

    def get_comment_map(
        self,
        project: str,
        jql: str | None = None,
    ) -> dict[str, Any]:
        """Return a {jira_key → comment-field dict} map via ONE paged REST search.

        Comment-state enrichment (Action viability): the live comment fetch
        previously issued one ``acli comment list`` call per commented ticket
        every pass (~1-2s each, fleet-wide — measured multi-hour passes). This
        method amortises that into a SINGLE paged ``POST /rest/api/3/search/jql``
        with ``fields=["comment"]`` so the differ can dedup comments without a
        per-ticket round-trip.

        Returns ``{jira_key: <comment field dict>}`` where the value is the raw
        Jira ``comment`` field (``{"comments": [...], "total": N, ...}``) — the
        exact shape ``outbound_differ._diff_comments`` reads from a snapshot
        entry's ``comment`` key. Keys whose ``comment`` field is absent are
        omitted so the caller can fall back to the per-ticket ``get_comments``
        path for them (the never-emit-blind invariant stays intact).

        Pagination + degradation contract mirror ``get_parent_map``: opaque
        ``nextPageToken`` cursor (no ``startAt`` / ``total``); HTTP 410 →
        ERROR (endpoint retirement is loud); other faults → WARNING; an empty
        dict is returned on failure so the fetcher degrades gracefully.

        Args:
            project: Jira project key (e.g. "DIG").
            jql: Optional JQL override.  Defaults to ``project = <project>``.
        """
        import logging as _logging

        _log = _logging.getLogger(__name__)

        effective_jql = jql or f"project = {project}"
        result: dict[str, Any] = {}
        page_size = 100
        next_page_token: str | None = None

        while True:
            body: dict[str, Any] = {
                "jql": effective_jql,
                "maxResults": page_size,
                "fields": ["comment"],
            }
            if next_page_token is not None:
                body["nextPageToken"] = next_page_token
            try:
                resp = self._direct_rest_post_json("/rest/api/3/search/jql", body)
            except urllib.error.HTTPError as exc:
                if exc.code == 410:
                    _log.error(
                        "get_comment_map: endpoint POST /rest/api/3/search/jql "
                        "returned HTTP 410 GONE — the Jira search endpoint has "
                        "been RETIRED; comment enrichment is unavailable this pass. "
                        "Per-ticket get_comments fallback applies. API retirement, "
                        "not a transient fault: %r",
                        exc,
                    )
                else:
                    _log.warning(
                        "get_comment_map: REST search failed (HTTP %s): %r; "
                        "degrading gracefully — per-ticket fallback applies",
                        exc.code,
                        exc,
                    )
                break
            except Exception as exc:  # noqa: BLE001 — fail-open: degrade to per-ticket fallback
                _log.warning(
                    "get_comment_map: REST search failed: %r; "
                    "degrading gracefully — per-ticket fallback applies",
                    exc,
                )
                break

            if not isinstance(resp, dict):
                break
            issues = resp.get("issues") or []
            if not isinstance(issues, list):
                break
            for issue in issues:
                if not isinstance(issue, dict):
                    continue
                key = issue.get("key")
                if not key:
                    continue
                fields = issue.get("fields") or {}
                comment_field = fields.get("comment")
                # Only record keys the search actually returned a comment field
                # for; omit the rest so the caller falls back to get_comments
                # (preserves the never-emit-blind invariant).
                if isinstance(comment_field, dict):
                    result[key] = comment_field

            if resp.get("isLast"):
                break
            next_page_token = resp.get("nextPageToken")
            if not next_page_token:
                break

        return result

    def get_issuelinks_map(
        self,
        project: str,
        jql: str | None = None,
    ) -> dict[str, Any]:
        """Return a ``{jira_key → issuelinks list}`` map via ONE paged REST search.

        Bug 3f04: the snapshot previously carried NO ``issuelinks`` — the fetcher
        enriched ``parent`` and ``comment`` but not links — so BOTH the inbound
        link differ (``inbound_differ._diff_links_inbound``) and the outbound
        differ's dedup (``outbound_differ._existing_jira_links``) read
        ``jira_fields.get("issuelinks")`` and always saw nothing. Inbound link
        sync was structurally dead and outbound re-emitted every link each pass.

        Amortises what would otherwise be a per-ticket ``get_issue_links`` REST
        round-trip into a SINGLE paged ``POST /rest/api/3/search/jql`` with
        ``fields=["issuelinks"]``. Returns ``{jira_key: [<issuelink>, ...]}`` in
        the exact REST-nested shape the differs read (``type.name`` +
        ``inwardIssue``/``outwardIssue`` keys). Mirrors ``get_comment_map``'s
        pagination + fail-open degradation contract (410 → ERROR, other faults →
        WARNING, empty dict on failure so the pass still completes).
        """
        import logging as _logging

        _log = _logging.getLogger(__name__)

        effective_jql = jql or f"project = {project}"
        result: dict[str, Any] = {}
        page_size = 100
        next_page_token: str | None = None

        while True:
            body: dict[str, Any] = {
                "jql": effective_jql,
                "maxResults": page_size,
                "fields": ["issuelinks"],
            }
            if next_page_token is not None:
                body["nextPageToken"] = next_page_token
            try:
                resp = self._direct_rest_post_json("/rest/api/3/search/jql", body)
            except urllib.error.HTTPError as exc:
                if exc.code == 410:
                    _log.error(
                        "get_issuelinks_map: endpoint POST /rest/api/3/search/jql "
                        "returned HTTP 410 GONE — the Jira search endpoint has been "
                        "RETIRED; issuelink enrichment is unavailable this pass. "
                        "API retirement, not a transient fault: %r",
                        exc,
                    )
                else:
                    _log.warning(
                        "get_issuelinks_map: REST search failed (HTTP %s): %r; "
                        "degrading gracefully (no issuelink enrichment this pass)",
                        exc.code,
                        exc,
                    )
                break
            except Exception as exc:  # noqa: BLE001 — fail-open: degrade to no enrichment
                _log.warning(
                    "get_issuelinks_map: REST search failed: %r; "
                    "degrading gracefully (no issuelink enrichment this pass)",
                    exc,
                )
                break

            if not isinstance(resp, dict):
                break
            issues = resp.get("issues") or []
            if not isinstance(issues, list):
                break
            for issue in issues:
                if not isinstance(issue, dict):
                    continue
                key = issue.get("key")
                if not key:
                    continue
                fields = issue.get("fields") or {}
                links = fields.get("issuelinks")
                # Record any issue the search returned an issuelinks list for
                # (including the empty list — an authoritative "no links" that
                # lets the outbound differ's dedup treat the issue as known).
                if isinstance(links, list):
                    result[key] = links

            if resp.get("isLast"):
                break
            next_page_token = resp.get("nextPageToken")
            if not next_page_token:
                break

        return result

    def update_priority(self, jira_key: str, priority_name: str) -> None:
        """Update priority on a Jira issue via REST PUT.

        ACLI does not support priority edit. Uses direct REST API:
        PUT /rest/api/3/issue/{key} with {"fields":{"priority":{"name":"..."}}}
        Probe-validated: returns 204 on success.
        """
        self._direct_rest_put_raw(
            f"/rest/api/3/issue/{jira_key}",
            {"fields": {"priority": {"name": priority_name}}},
        )

    def update_issuetype(self, jira_key: str, type_name: str) -> None:
        """Update issue type on a Jira issue via REST PUT.

        ACLI does not support issuetype edit. Uses direct REST API:
        PUT /rest/api/3/issue/{key} with {"fields":{"issuetype":{"name":"..."}}}
        Probe-validated: returns 204 on success.
        """
        self._direct_rest_put_raw(
            f"/rest/api/3/issue/{jira_key}",
            {"fields": {"issuetype": {"name": type_name}}},
        )

    def update_comment(self, jira_key: str, comment_id: str, body: str) -> dict[str, Any]:
        """Update an existing comment on a Jira issue via ACLI.

        Probe-validated: ``acli jira workitem comment update`` works correctly.
        """
        cmd = [
            "jira",
            "workitem",
            "comment",
            "update",
            "--key",
            jira_key,
            "--id",
            str(comment_id),
            "--body",
            body,
            "--json",
        ]
        result = self._run(cmd)
        return json.loads(result.stdout) if result.stdout.strip() else {}

    def delete_comment(self, jira_key: str, comment_id: str) -> None:
        """Delete a comment from a Jira issue via REST DELETE.

        ACLI has no comment delete subcommand. Uses direct REST API:
        DELETE /rest/api/3/issue/{key}/comment/{id}
        Probe-validated: returns 204 on success.
        """
        path = f"/rest/api/3/issue/{jira_key}/comment/{comment_id}"
        self._direct_rest_delete(path)

    def set_relationship(
        self,
        from_key: str,
        to_key: str,
        link_type: str = "Blocks",
    ) -> dict[str, Any]:
        """Create a link so that ``from_key <link_type> to_key`` (e.g. "from blocks to").

        Raises subprocess.CalledProcessError on ACLI failure.
        """
        # Bug 3b86: ACLI's ``--out``/``--in`` are INVERTED relative to the naive reading.
        # Empirically (live-validated 2026-07-16): ``link create --out X --in Y --type Blocks``
        # creates "Y blocks X" — the ``--in`` issue is the BLOCKER (outward), the ``--out``
        # issue is the BLOCKED (inward). So to make ``from_key blocks to_key`` we must pass
        # ``--out to_key --in from_key``. (The earlier ``--out from_key --in to_key`` reversed
        # every written link; it looked correct only because rebar reads links direction-
        # agnostically and most links were adopted from Jira rather than written by us.)
        #
        # Story 25ae: the installed ACLI rejects an unconditional ``--json`` on
        # ``link create`` ("unknown flag"). Run WITHOUT ``--json``; only retry
        # with it for forward-version tolerance (older builds that DO accept it
        # behave identically either way — we never read the structured output
        # here, the return is synthesized).
        cmd = [
            "jira",
            "workitem",
            "link",
            "create",
            "--out",
            to_key,
            "--in",
            from_key,
            "--type",
            link_type,
        ]
        self._run(cmd)  # raises on failure — no silent swallowing
        return {"status": "created", "from": from_key, "to": to_key}

    def get_issue_links(self, jira_key: str) -> list[dict[str, Any]]:
        """Get existing issue links for a Jira issue, in REST-nested shape:
        ``[{"id", "type": {"name", "inward", "outward"},
            "inwardIssue": {"key", ...}|absent, "outwardIssue": {"key", ...}|absent}]``.

        Reads via the REST API rather than the ACLI ``link list`` command: the
        latter does not report the linked issue key (it returns
        ``outwardIssueKey: null``), so it cannot identify what a link points to.
        ``GET /rest/api/3/issue/{key}?fields=issuelinks`` returns the canonical
        shape with the linked-issue keys the differs and callers need.

        Raises on REST failure (via ``_direct_rest_get``).
        """
        data = self._direct_rest_get(f"/rest/api/3/issue/{jira_key}?fields=issuelinks")
        fields = data.get("fields") if isinstance(data, dict) else None
        links = fields.get("issuelinks") if isinstance(fields, dict) else None
        return links if isinstance(links, list) else []

    def delete_issue_link(self, link_id: str) -> dict[str, Any]:
        """Delete a Jira issue link by its ID via ACLI.

        Uses ``jira workitem link delete --id LINK_ID`` to remove the link.
        Raises subprocess.CalledProcessError on ACLI failure (e.g. 404 if
        the link was already deleted, or 409 on concurrent modification).
        Callers should treat 404/409 as idempotent success.
        """
        cmd = [
            "jira",
            "workitem",
            "link",
            "delete",
            "--id",
            link_id,
            # Story 25ae: the installed ACLI rejects ``--json`` on link delete
            # ("unknown flag"); ``_run`` raises on a nonzero exit, so the
            # synthesized return below stands in for structured-failure detection.
        ]
        self._run(cmd)
        return {"status": "deleted", "link_id": link_id}
