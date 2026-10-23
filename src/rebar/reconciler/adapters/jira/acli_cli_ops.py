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

from rebar.reconciler.adapters.jira import acli_subprocess
from rebar.reconciler.adapters.jira.acli_subprocess import (
    _ASSIGNEE_NOT_FOUND_ERROR,
    _ASSIGNEE_PERMISSION_ERROR,
)
from rebar.reconciler.adapters.jira.adf import text_to_adf as _text_to_adf  # canonical location
from rebar.reconciler.adapters.jira_family import (
    LOCAL_PRIORITY_TO_JIRA as _LOCAL_PRIORITY_TO_JIRA,
)

logger = logging.getLogger(__name__)


def _verify_created_issue(
    stdout: str,
    *,
    acli_cmd: list[str] | None = None,
    client: Any = None,
    call_timeout: float | None = None,
) -> dict[str, Any]:
    """Parse ACLI create output and verify the issue exists.

    An explicit client with credentials enables immediate REST GET verification;
    otherwise use the ACLI search path. Ambient credentials are never read here.
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

    verified = get_issue(jira_key=jira_key, acli_cmd=acli_cmd, call_timeout=call_timeout)
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
    """Attach a parent, logging and recording HTTP 400 hierarchy rejection.

    Other errors propagate. The alert omits ``local_id`` because this transport
    has none. Use an absolute local import for by-path module loading.
    """
    import logging as _logging

    from rebar.reconciler.pass_io import record_parent_divergence

    try:
        client.set_parent(child_key, parent_key)
    except urllib.error.HTTPError as exc:
        if exc.code == 400:
            _logging.getLogger(__name__).warning(
                "parent sync skipped: Jira hierarchy rejected %s→%s (HTTP 400)",
                child_key,
                parent_key,
            )
            record_parent_divergence("outbound-parent-rejected", child_key, None, parent_key, exc)
            return
        raise


def create_issue(
    project: str,
    issue_type: str,
    summary: str,
    *,
    acli_cmd: list[str] | None = None,
    client: Any = None,
    call_timeout: float | None = None,
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
            call_timeout=call_timeout,
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

    result = _create_issue_no_json(
        project, issue_type, summary, acli_cmd=acli_cmd, call_timeout=call_timeout, **kwargs
    )
    # field is present in the ACLI command. _create_issue_no_json returns None only
    # on that specific permission error. When no assignee kwarg is provided, the
    # --assignee flag is never sent, so this error cannot occur and result will
    # always be a CompletedProcess (or an exception is raised). Therefore, no
    # separate "result is None without assignee" branch is needed.
    if result is None and kwargs.get("assignee"):
        print(  # noqa: T201
            "Warning: assignee cannot be assigned — retrying without assignee",
            file=sys.stderr,
        )
        no_assignee_kwargs = {k: v for k, v in kwargs.items() if k != "assignee"}
        result = _create_issue_no_json(
            project,
            issue_type,
            summary,
            acli_cmd=acli_cmd,
            call_timeout=call_timeout,
            **no_assignee_kwargs,
        )
        if result is None:
            msg = "ACLI create failed on retry without assignee"
            raise RuntimeError(msg)

    assert result is not None  # Guaranteed: either we have a result or raised above
    return _verify_created_issue(
        result.stdout, acli_cmd=acli_cmd, client=client, call_timeout=call_timeout
    )


def _create_issue_no_json(
    project: str,
    issue_type: str,
    summary: str,
    *,
    acli_cmd: list[str] | None = None,
    call_timeout: float | None = None,
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
        return acli_subprocess._run_acli(cmd, acli_cmd=acli_cmd, call_timeout=call_timeout)
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
    call_timeout: float | None = None,
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
        except Exception:
            if not fd_owned:
                os.close(fd)
            raise
        cmd = ["jira", "workitem", "create", "--from-json", json_path, "--json"]
        return acli_subprocess._run_acli(cmd, acli_cmd=acli_cmd, call_timeout=call_timeout)
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
    call_timeout: float | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Create via ``--from-json`` so ACLI can set priority.

    Integer priorities use the local map; dict priorities prefer ``name`` then
    reverse-map ``id``. Unmapped integers and dicts without a truthy name or
    mapped ID use Medium; strings pass through unchanged.
    """
    # Normalize integer or REST-dict priorities to Jira names; keep strings.
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

    result = _create_from_json_payload(payload, acli_cmd=acli_cmd, call_timeout=call_timeout)

    # If the assignee field caused a permission error, retry without it.
    # _ASSIGNEE_PERMISSION_ERROR, which requires an assignee in the payload.
    # When no assignee is present, the error cannot occur, so we only need
    # the "assignee in payload" branch — no separate elif for result is None
    # without assignee.
    if result is None and "assignee" in payload:
        print(  # noqa: T201
            f"Warning: assignee '{payload['assignee']}' cannot be assigned — "
            f"retrying without assignee",
            file=sys.stderr,
        )
        del payload["assignee"]
        result = _create_from_json_payload(payload, acli_cmd=acli_cmd, call_timeout=call_timeout)
        if result is None:
            msg = "ACLI create failed on retry without assignee"
            raise RuntimeError(msg)

    assert result is not None  # Guaranteed: either we have a result or raised above
    return _verify_created_issue(result.stdout, acli_cmd=acli_cmd, client=client)


def update_priority(jira_key: str, priority_name: str) -> None:
    """PUT priority through REST; log and skip when credentials are incomplete.

    Unlike ACLI functions, this uses resolved Jira settings. A client with its
    own credentials uses ``AcliClient.update_priority`` instead.
    """
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
    call_timeout: float | None = None,
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
    result = acli_subprocess._run_acli(
        cmd, acli_cmd=acli_cmd, retry_on_timeout=True, call_timeout=call_timeout
    )  # READ
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
    call_timeout: float | None = None,
) -> dict[str, Any]:
    """Add an ACLI comment after fitting text and encoding its wire body.

    Fit before JSON encoding so size limits apply to the rendered body. The
    marker-preserving fitter truncates content while retaining the reconciler
    echo marker at the end; the codec follows the rich-text cutover.
    """
    from rebar.reconciler.adapters.jira.rich_text_codec import AdfCodec
    from rebar.reconciler.adapters.jira_family.rich_text import cutover_clients
    from rebar.reconciler.outbound_comments import fit_preserving_marker

    codec = AdfCodec(rich="cloud" in cutover_clients())
    # comment-fit-ok: the adapter's DEFENSIVE fit before the transport, not a second
    # dedup-key derivation. The key is taken once in outbound_comments.fit_for_send and
    # travels on the mutation as wire_body; re-fitting an already-fitted body is
    # byte-identical (story 4cee AC5), so this cannot move the bytes after the key was
    # taken - it only protects a body that reached the transport unfitted.
    body_arg = json.dumps(codec.to_wire(fit_preserving_marker(body, codec.fit_outbound)))
    cmd = [
        "jira",
        "workitem",
        "comment",
        "create",
        "--key",
        jira_key,
        "--body",
        body_arg,
        "--json",
    ]
    result = acli_subprocess._run_acli(cmd, acli_cmd=acli_cmd, call_timeout=call_timeout)
    return _parse_comment_created(result.stdout)


def _parse_comment_created(stdout: str) -> dict[str, Any]:
    """Normalize comment-create output to a comment resource.

    Return a plain resource unchanged. Preserve an ACLI batch envelope under
    ``acli_envelope`` with ``id=None`` because its result IDs name work items,
    not comments. Reject other shapes.
    """
    parsed = json.loads(stdout)
    if isinstance(parsed, dict):
        if "results" not in parsed and "successCount" not in parsed:
            return parsed
        return {"id": None, "acli_envelope": parsed}
    msg = f"ACLI comment create returned an unrecognised payload: {parsed!r}"
    raise RuntimeError(msg)


def _parse_acli_comments(parsed: Any) -> list[dict[str, Any]]:
    """Flatten list or wrapped comments; ignore unknown shapes and non-dicts."""
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
    """Decode concatenated ACLI page objects and flatten their comments.

    A single object or list uses the same path; ``raw_decode`` prevents later
    pages from being dropped as extra JSON data.
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
    call_timeout: float | None = None,
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
    result = acli_subprocess._run_acli(
        cmd, acli_cmd=acli_cmd, retry_on_timeout=True, call_timeout=call_timeout
    )  # READ
    return _parse_paginated_comments(result.stdout)
