#!/usr/bin/env python3
"""Module-level ACLI issue operations (the free-function CLI surface).

Stateless ``create_issue``/``update_priority``/``get_issue``/``add_comment``/
``get_comments`` and the create-path helpers that build and dispatch ACLI
commands through the ``acli_subprocess`` transport floor. The ``AcliClient``
methods in ``acli.py`` delegate here; credentials, when a REST verify is needed,
are taken only from the explicit ``client`` argument (never ambient env).

Calls into the subprocess seam are module-qualified (``acli_subprocess._run_acli``)
so a single patch point covers both these free functions and ``AcliClient._run``.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from typing import Any

from rebar_reconciler import acli_subprocess
from rebar_reconciler.acli_subprocess import (
    _ASSIGNEE_NOT_FOUND_ERROR,
    _ASSIGNEE_PERMISSION_ERROR,
)
from rebar_reconciler.adapters.jira.jira_fields import (
    _LOCAL_PRIORITY_TO_JIRA,
    _sanitize_comment,
)
from rebar_reconciler.adf import text_to_adf as _text_to_adf  # canonical location

logger = logging.getLogger(__name__)


def _verify_created_issue(
    stdout: str,
    *,
    acli_cmd: list[str] | None = None,
    client: Any = None,
) -> dict[str, Any]:
    """Parse ACLI create output, verify the issue exists, and return it.

    Uses direct REST GET (immediately consistent) instead of JQL search,
    which is subject to Jira Cloud's eventual-consistency index lag.

    Credentials for the REST GET come ONLY from the explicit *client*
    (AcliClient), never from ``os.environ`` (bug 7689). Reading ambient env
    here made create-path test behaviour depend on whatever JIRA_* variables
    happened to be set in the developer/CI process — tests that mocked only
    ``subprocess.run`` silently switched to the urllib REST path. With the
    credential source pinned to the caller's client, behaviour is determined
    solely by what the caller passes: a client carrying creds → REST GET
    (production: ``AcliClient.create_issue`` forwards ``client=self``, whose
    creds are read from the environment at construction); no client / no creds
    → the deterministic subprocess ``get_issue`` path.
    """
    created = json.loads(stdout)
    jira_key = created.get("key", "")
    if not jira_key:
        msg = f"ACLI create returned no key: {created}"
        raise RuntimeError(msg)

    # Credentials come from the explicit client (AcliClient always sets these
    # three attributes in __init__). Access them directly rather than via
    # getattr-with-default so a malformed client (not None but missing an
    # attribute) fails loudly instead of silently degrading to the subprocess
    # path with a half-populated credential set.
    jira_url = client.jira_url if client is not None else ""
    jira_user = client.user if client is not None else ""
    jira_token = client.api_token if client is not None else ""
    if jira_url and jira_user and jira_token:
        path = f"/rest/api/3/issue/{jira_key}"
        url = f"{jira_url.rstrip('/')}{path}"
        creds = base64.b64encode(f"{jira_user}:{jira_token}".encode()).decode()
        req = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Authorization": f"Basic {creds}",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, OSError):
            pass  # REST GET failed: fall through to JQL path

    verified = get_issue(jira_key=jira_key, acli_cmd=acli_cmd)
    if not verified:
        msg = f"Verify-after-create failed: issue {jira_key} not found"
        raise RuntimeError(msg)
    return verified


def _extract_parent_key(raw: Any) -> str | None:
    """Normalise an outbound create-payload parent value to a bare Jira key.

    Accepts the two shapes a create payload may carry (ticket 8b25):
      - a bare Jira key string (``"DIG-123"``) — the shape
        ``outbound_differ._map_local_to_jira_fields`` actually emits today;
      - a Jira REST nested object ``{"key": "DIG-123"}`` — accepted defensively
        so a future differ change does not silently drop the parent.

    Returns the key string, or ``None`` when no usable parent is present.
    """
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    if isinstance(raw, dict):
        key = raw.get("key")
        if isinstance(key, str) and key.strip():
            return key.strip()
    return None


def _attach_parent_guarded(client: Any, child_key: str, parent_key: str) -> None:
    """Attach *child_key* under *parent_key* via ``client.set_parent``, guarded.

    Hierarchy guard (ticket 8b25): on this next-gen project only an Epic may be
    a parent — a Task→Task reparent is rejected by Jira with HTTP 400 (and a
    misleading "same project" message). Any HTTP 400 from the parent op is
    treated as a hierarchy rejection: log a WARNING and continue the pass
    (generic 400-skip — also covers Epic-as-child and other unmet hierarchy
    constraints without bespoke probing). Non-400 errors propagate.
    """
    import logging as _logging

    try:
        client.set_parent(child_key, parent_key)
    except urllib.error.HTTPError as exc:
        if exc.code == 400:
            _logging.getLogger(__name__).warning(
                "parent sync skipped: Jira hierarchy rejected %s→%s (HTTP 400)",
                child_key,
                parent_key,
            )
            return
        raise


def create_issue(
    project: str,
    issue_type: str,
    summary: str,
    *,
    acli_cmd: list[str] | None = None,
    client: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Create a Jira issue via ACLI and verify it exists.

    Priority is set via ``--from-json`` with ``additionalAttributes``
    because ACLI does not expose a ``--priority`` CLI flag.

    Parent (ticket 8b25): when ``parent`` is supplied, the no-JSON path
    attaches it at create time via ``--parent <key>`` (live-proven working).
    The ``--from-json`` path cannot attach parent inline, so after a
    successful create it falls back to ``client.set_parent(new_key, parent)``
    — the universal fallback. Both parent ops are wrapped so a Jira hierarchy
    rejection (HTTP 400) logs a WARNING and continues rather than aborting the
    create.
    """
    priority = kwargs.pop("priority", None)
    parent_key = kwargs.pop("parent", None)

    # When priority is requested, use --from-json so we can pass
    # additionalAttributes.priority (the only ACLI-supported path).
    if priority is not None:
        created = _create_issue_from_json(
            project,
            issue_type,
            summary,
            priority,
            acli_cmd=acli_cmd,
            client=client,
            **kwargs,
        )
        # --from-json has no inline parent attachment — set_parent fallback.
        if parent_key and client is not None:
            new_key = created.get("key")
            if new_key:
                _attach_parent_guarded(client, new_key, parent_key)
        return created

    if parent_key:
        kwargs["parent"] = parent_key

    result = _create_issue_no_json(project, issue_type, summary, acli_cmd=acli_cmd, **kwargs)
    # field is present in the ACLI command. _create_issue_no_json returns None only
    # on that specific permission error. When no assignee kwarg is provided, the
    # --assignee flag is never sent, so this error cannot occur and result will
    # always be a CompletedProcess (or an exception is raised). Therefore, no
    # separate "result is None without assignee" branch is needed.
    if result is None and kwargs.get("assignee"):
        print(
            "Warning: assignee cannot be assigned — retrying without assignee",
            file=sys.stderr,
        )
        no_assignee_kwargs = {k: v for k, v in kwargs.items() if k != "assignee"}
        result = _create_issue_no_json(
            project, issue_type, summary, acli_cmd=acli_cmd, **no_assignee_kwargs
        )
        if result is None:
            msg = "ACLI create failed on retry without assignee"
            raise RuntimeError(msg)

    assert result is not None  # Guaranteed: either we have a result or raised above
    return _verify_created_issue(result.stdout, acli_cmd=acli_cmd, client=client)


def _create_issue_no_json(
    project: str,
    issue_type: str,
    summary: str,
    *,
    acli_cmd: list[str] | None = None,
    **kwargs: Any,
) -> subprocess.CompletedProcess[str] | None:
    """Build and run the non-JSON ACLI create command, returning the result.

    Returns ``None`` if ACLI fails with an assignee error ("cannot be
    assigned" or "User not found for email:") so the caller can retry
    without the assignee field — matching the same contract as
    ``_create_from_json_payload``.
    """
    cmd = [
        "jira",
        "workitem",
        "create",
        "--project",
        project,
        "--type",
        issue_type,
        "--summary",
        summary,
        "--json",
    ]
    for field in ("description", "assignee"):
        if field in kwargs and kwargs[field] is not None:
            cmd.extend([f"--{field}", str(kwargs[field])])
    # Parent sync (ticket 8b25): ACLI ``workitem create`` DOES accept
    # ``--parent <key>`` for parent attachment at create time (live-proven).
    # Previously the parent was dropped silently on the create path.
    if kwargs.get("parent"):
        cmd.extend(["--parent", str(kwargs["parent"])])
    try:
        return acli_subprocess._run_acli(cmd, acli_cmd=acli_cmd)
    except subprocess.CalledProcessError as exc:
        if exc.stderr and (
            _ASSIGNEE_PERMISSION_ERROR in exc.stderr or _ASSIGNEE_NOT_FOUND_ERROR in exc.stderr
        ):
            return None
        raise


def _create_from_json_payload(
    payload: dict[str, Any],
    *,
    acli_cmd: list[str] | None = None,
) -> subprocess.CompletedProcess[str] | None:
    """Write *payload* to a temp file, run ACLI ``--from-json``, and return the result.

    Returns ``None`` if ACLI fails with an assignee error ("cannot be
    assigned" or "User not found for email:") so the caller can retry
    without the assignee field.
    """
    fd, json_path = tempfile.mkstemp(suffix=".json", prefix="acli-create-")
    try:
        # os.fdopen transfers ownership of fd to the file object. After fdopen
        # succeeds (fd_owned=True), the context manager's __exit__ closes fd —
        # so os.close(fd) is correctly skipped. If fdopen itself fails
        # (fd_owned=False), we must close fd manually. If json.dump raises after
        # fdopen succeeded, the exception propagates through the inner except
        # (which skips os.close because fd_owned=True), then through the outer
        # try — the finally block runs os.unlink correctly. The outer except
        # only catches CalledProcessError (from _run_acli), so json.dump
        # exceptions propagate to the caller as-is.
        fd_owned = False
        try:
            with os.fdopen(fd, "w") as f:
                fd_owned = True  # fd is now owned by the file object
                json.dump(payload, f)
        except Exception:  # noqa: BLE001 — fd cleanup on write failure: close the fd if unowned, then re-raise (never swallowed)
            if not fd_owned:
                os.close(fd)
            raise
        cmd = ["jira", "workitem", "create", "--from-json", json_path, "--json"]
        return acli_subprocess._run_acli(cmd, acli_cmd=acli_cmd)
    except subprocess.CalledProcessError as exc:
        if exc.stderr and (
            _ASSIGNEE_PERMISSION_ERROR in exc.stderr or _ASSIGNEE_NOT_FOUND_ERROR in exc.stderr
        ):
            return None
        raise
    finally:
        os.unlink(json_path)


def _create_issue_from_json(
    project: str,
    issue_type: str,
    summary: str,
    priority: str | int | dict[str, Any],
    *,
    acli_cmd: list[str] | None = None,
    client: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Create a Jira issue using ``--from-json`` to set priority.

    ACLI's ``workitem create`` does not have a ``--priority`` flag, but
    the ``--from-json`` path accepts ``additionalAttributes`` which maps
    directly to Jira REST API fields. Priority requires
    ``{"name": "<Jira priority name>"}`` in the ACLI payload.

    Accepted ``priority`` input shapes (all normalized to a name string before
    payload assembly):
      - ``int`` (0-4): mapped through ``_LOCAL_PRIORITY_TO_JIRA`` (e.g., 1 -> "High").
      - ``dict``: Jira REST-shape priority object (the reconciler's differ
        propagates this verbatim from fetcher snapshots). ``.get("name")`` is
        preferred; if absent, falls back to ``.get("id")`` mapped through the
        reverse of ``_LOCAL_PRIORITY_TO_JIRA``; if both absent, defaults to
        ``"Medium"``. See bug 5010-1c6a-9387-4b5b.
      - ``str``: passed through verbatim (caller-supplied Jira priority name).
    """
    # Convert priority to a Jira priority name.
    # - Integer (0-4): map through _LOCAL_PRIORITY_TO_JIRA.
    # - Jira REST-shape dict ({"name": ..., "id": ..., "iconUrl": ..., "self": ...}):
    #   extract .name, falling back to a reverse-id lookup. The reconciler's
    #   differ propagates Jira's snapshot priority dict verbatim (fetcher.py
    #   → differ.py → applier.py → client.create_issue), so this branch is
    #   load-bearing — without it, str(<dict>) produces a Python-repr that
    #   ACLI rejects with "The priority selected is invalid"
    #   (bug 5010-1c6a-9387-4b5b).
    # - String: use as-is.
    if isinstance(priority, int):
        jira_priority_name = _LOCAL_PRIORITY_TO_JIRA.get(priority, "Medium")
    elif isinstance(priority, dict):
        _name = priority.get("name")
        if _name:
            jira_priority_name = str(_name)
        else:
            _id = priority.get("id")
            if _id is None:
                jira_priority_name = "Medium"
            else:
                try:
                    jira_priority_name = _LOCAL_PRIORITY_TO_JIRA[int(_id) - 1]
                except (TypeError, ValueError, KeyError, IndexError):
                    jira_priority_name = "Medium"
    else:
        jira_priority_name = str(priority)

    payload: dict[str, Any] = {
        "projectKey": project,
        "type": issue_type,
        "summary": summary,
        "additionalAttributes": {
            "priority": {"name": jira_priority_name},
        },
    }
    if kwargs.get("description"):
        payload["description"] = _text_to_adf(str(kwargs["description"]))
    if kwargs.get("assignee"):
        payload["assignee"] = str(kwargs["assignee"])

    result = _create_from_json_payload(payload, acli_cmd=acli_cmd)

    # If the assignee field caused a permission error, retry without it.
    # _ASSIGNEE_PERMISSION_ERROR, which requires an assignee in the payload.
    # When no assignee is present, the error cannot occur, so we only need
    # the "assignee in payload" branch — no separate elif for result is None
    # without assignee.
    if result is None and "assignee" in payload:
        print(
            f"Warning: assignee '{payload['assignee']}' cannot be assigned — "
            f"retrying without assignee",
            file=sys.stderr,
        )
        del payload["assignee"]
        result = _create_from_json_payload(payload, acli_cmd=acli_cmd)
        if result is None:
            msg = "ACLI create failed on retry without assignee"
            raise RuntimeError(msg)

    assert result is not None  # Guaranteed: either we have a result or raised above
    return _verify_created_issue(result.stdout, acli_cmd=acli_cmd, client=client)


def update_priority(
    jira_key: str,
    priority_name: str,
    *,
    acli_cmd: list[str] | None = None,
) -> None:
    """Update priority on a Jira issue via REST PUT.

    ACLI does not support priority edit — uses direct REST API:
    PUT /rest/api/3/issue/{key} with {"fields":{"priority":{"name":"..."}}}
    Probe-validated: returns 204 on success.
    """
    # This function needs credentials. When called from the module-level
    # update_issue (which has no client instance), we resolve url/user through the
    # typed config (JIRA_URL/JIRA_USER env override the [tool.rebar.jira] file) and
    # the secret api_token from the environment only.
    _s = acli_subprocess.resolve_jira_settings()
    jira_url, user, api_token = _s.url, _s.user, _s.api_token
    if not all([jira_url, user, api_token]):
        logger.warning(
            "Cannot update priority on %s via REST (missing JIRA_URL/JIRA_USER/"
            "JIRA_API_TOKEN env vars). Priority '%s' skipped.",
            jira_key,
            priority_name,
        )
        return
    url = f"{jira_url.rstrip('/')}/rest/api/3/issue/{jira_key}"
    creds = base64.b64encode(f"{user}:{api_token}".encode()).decode()
    data = json.dumps({"fields": {"priority": {"name": priority_name}}}, ensure_ascii=False).encode(
        "utf-8"
    )
    req = urllib.request.Request(
        url,
        data=data,
        method="PUT",
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def get_issue(
    jira_key: str,
    *,
    acli_cmd: list[str] | None = None,
) -> dict[str, Any]:
    """Get a Jira issue via ACLI search (single-key JQL).

    Note: ``acli workitem view --json`` produces empty stdout (probe-confirmed
    broken). We use ``search --jql "key = KEY"`` as the reliable alternative.
    """
    cmd = [
        "jira",
        "workitem",
        "search",
        "--jql",
        f"key = {jira_key}",
        "-f",
        "key,summary,description,status,priority,issuetype,assignee,labels",
        "--json",
    ]
    result = acli_subprocess._run_acli(cmd, acli_cmd=acli_cmd, retry_on_timeout=True)  # READ
    parsed = json.loads(result.stdout)
    issues = parsed if isinstance(parsed, list) else parsed.get("issues", [])
    if not issues:
        raise RuntimeError(f"Issue {jira_key} not found")
    return issues[0]


def add_comment(
    jira_key: str,
    body: str,
    *,
    acli_cmd: list[str] | None = None,
) -> dict[str, Any]:
    """Add a comment to a Jira issue via ACLI."""
    # Bug 6afc-20ee-84e5-4dd5: guard Jira's 32,767-char comment limit before the
    # send (ACLI exits 0 on an over-length rejection, so an unguarded body fails
    # silently and re-emits every pass).
    body = _sanitize_comment(body)
    cmd = [
        "jira",
        "workitem",
        "comment",
        "create",
        "--key",
        jira_key,
        "--body",
        body,
        "--json",
    ]
    result = acli_subprocess._run_acli(cmd, acli_cmd=acli_cmd)
    return json.loads(result.stdout)


def _parse_acli_comments(parsed: Any) -> list[dict[str, Any]]:
    """Normalise an ACLI comments response to a flat list of comment dicts.

    ACLI may return a bare list, a wrapped dict with a 'comments' key, or an
    unrecognised shape (error dict, scalar, None).  All unrecognised shapes
    intentionally produce [] — callers must not interpret unknown payloads as
    comment data, and surfacing raw error dicts as comment lists would silently
    corrupt downstream processing.
    """
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    if isinstance(parsed, dict):
        comments = parsed.get("comments", [])
        return (
            [item for item in comments if isinstance(item, dict)]
            if isinstance(comments, list)
            else []
        )
    return []


def _parse_paginated_comments(stdout: str) -> list[dict[str, Any]]:
    """Flatten ACLI ``comment list --paginate --json`` output to ALL comment dicts.

    With ``--paginate`` ACLI emits ONE JSON object PER PAGE, concatenated (NOT a
    single document) — verified live: a 5000-comment issue yields ~101 back-to-back
    ``{"comments": [...], "isLast": …, "startAt": …}`` objects. A single
    ``json.loads`` raises ``JSONDecodeError('Extra data')`` on the second page and
    would silently drop every comment past page 1 — the bug that let 13 issues
    re-post to the 5000-comment cap (bug 1f3d).

    Decode each object in sequence (``raw_decode``) and concatenate its comments via
    :func:`_parse_acli_comments` (which normalises the per-page wrapped-dict / bare-list
    shapes and drops non-dict noise). A single-object or bare-list payload (the
    non-paginated shape) is handled as a one-iteration case, so this is a safe
    superset of ``_parse_acli_comments(json.loads(stdout))``.
    """
    decoder = json.JSONDecoder()
    comments: list[dict[str, Any]] = []
    idx, length = 0, len(stdout)
    while idx < length:
        while idx < length and stdout[idx] in " \t\r\n":
            idx += 1
        if idx >= length:
            break
        obj, end = decoder.raw_decode(stdout, idx)
        idx = end
        comments.extend(_parse_acli_comments(obj))
    return comments


def get_comments(
    jira_key: str,
    *,
    acli_cmd: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Get ALL comments on a Jira issue via ACLI.

    ``--paginate`` is REQUIRED: without it ACLI returns only the first page (default
    ``--limit 50``, oldest first), which made the outbound dedup re-post everything
    past page 1 and inflate high-traffic issues to Jira's 5000-comment cap (bug 1f3d).
    ``--paginate`` streams one JSON object per page, so parse via
    :func:`_parse_paginated_comments`, not a single ``json.loads``.
    """
    cmd = [
        "jira",
        "workitem",
        "comment",
        "list",
        "--key",
        jira_key,
        "--paginate",
        "--json",
    ]
    result = acli_subprocess._run_acli(cmd, acli_cmd=acli_cmd, retry_on_timeout=True)  # READ
    return _parse_paginated_comments(result.stdout)
