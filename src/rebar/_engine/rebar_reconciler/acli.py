#!/usr/bin/env python3
"""ACLI subprocess wrapper for Jira issue operations.

Provides create_issue, update_issue, and get_issue functions that invoke
the Atlassian CLI (ACLI) via subprocess calls. Includes retry with
exponential backoff on transient failures and fast-abort on auth errors.

No external dependencies — stdlib only (subprocess, json, time, os, base64, urllib).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from rebar_reconciler.adf import text_to_adf as _text_to_adf  # canonical location
from rebar_reconciler.comment_limits import (  # shared send/diff truncation
    _JIRA_COMMENT_MAX_CHARS,
    truncate_comment_body as _truncate_comment_body,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_ACLI_CMD: list[str] = ["acli"]
_MAX_ATTEMPTS: int = 3  # initial + 2 retries
_AUTH_FAILURE_CODE: int = 401
_ASSIGNEE_PERMISSION_ERROR: str = "cannot be assigned"
_ASSIGNEE_NOT_FOUND_ERROR: str = (
    "User not found for email:"  # prefix match — email value varies per call
)

# Local priority integer (0-4) → Jira priority name.
_LOCAL_PRIORITY_TO_JIRA: dict[int, str] = {
    0: "Highest",
    1: "High",
    2: "Medium",
    3: "Low",
    4: "Lowest",
}

# Jira hard limits we defend against (verified against Jira Cloud REST API 2026).
# Note the deliberate off-by-one divergence between the two constants:
#   - Summary: Jira's error is "Summary must be less than 255 characters"
#     (strict less-than), so the INCLUSIVE max is 254. A 255-char title is
#     REJECTED. Sources: Atlassian Community thread 989632 + GitHub
#     tenable/integration-jira-cloud issue #322 + GitHub-prior-art audit
#     (2026-05-24, run a52143da).
#   - Label: Jira's error is "Labels can't have spaces or be more than 255
#     characters" (not-more-than), so the INCLUSIVE max is 255. Source:
#     Forge custom-field community thread 55277.
_JIRA_SUMMARY_MAX_CHARS: int = 254
_JIRA_LABEL_MAX_CHARS: int = 255


class AssigneeNotFoundError(ValueError):
    """Raised when a requested assignee does not resolve to any assignable Jira user.

    Bug 06a5 / 85a1 (Gap 5 follow-up): mirrors the client-side pre-validation
    pattern used by ``transition_issue_by_name`` (Gap 8). Caught before the
    outbound mutation is dispatched so the bogus-assignee class does not
    silently no-op via ACLI's exit-0-on-failure contract.
    """


class InvalidLabelError(ValueError):
    """A label value would be rejected by Jira (whitespace, comma, empty, oversize)."""


def _sanitize_label(label: str) -> str:
    """Validate a Jira label, raising InvalidLabelError on rejection.

    Jira labels are single tokens — no whitespace, no commas, non-empty, length
    <= 255 chars. ACLI does not validate client-side; sending an invalid label
    surfaces as a confusing server-side error or (worse) silently corrupts the
    label set. We sanitize here so the reconciler fails fast with a clear
    message instead of issuing a malformed mutation against live Jira.

    Whitespace is stripped from the input before validation. A label that
    contains internal whitespace (e.g., "with space") is REJECTED rather than
    silently mangled — the reconciler should never invent a label name that
    differs from what the caller asked for.
    """
    if not isinstance(label, str):
        raise InvalidLabelError(
            f"Label must be str, got {type(label).__name__}: {label!r}"
        )
    stripped = label.strip()
    if not stripped:
        raise InvalidLabelError(f"Label is empty after strip: {label!r}")
    if any(c.isspace() for c in stripped):
        raise InvalidLabelError(
            f"Label contains internal whitespace (not allowed by Jira): {label!r}"
        )
    if "," in stripped:
        raise InvalidLabelError(
            f"Label contains comma (not allowed by Jira): {label!r}"
        )
    if len(stripped) > _JIRA_LABEL_MAX_CHARS:
        raise InvalidLabelError(
            f"Label exceeds Jira's {_JIRA_LABEL_MAX_CHARS}-char limit "
            f"({len(stripped)} chars): {label!r}"
        )
    return stripped


def _sanitize_summary(summary: str) -> str:
    """Validate and truncate a Jira summary string.

    Jira's REST API rejects summaries > 255 chars with a confusing error.
    We truncate with a visible '... [truncated]' suffix so the reconciler
    can complete the mutation rather than crashing the pass on a single
    oversize ticket. Truncation is reversible (an operator can update the
    ticket later); reconciler crashes are not.

    A truncation warning is emitted so the operator can investigate.
    """
    if not isinstance(summary, str):
        raise ValueError(
            f"Summary must be str, got {type(summary).__name__}: {summary!r}"
        )
    stripped = summary.strip()
    if not stripped:
        raise ValueError(f"Summary is empty after strip: {summary!r}")
    if len(stripped) <= _JIRA_SUMMARY_MAX_CHARS:
        return stripped
    suffix = " [truncated]"
    keep = _JIRA_SUMMARY_MAX_CHARS - len(suffix)
    truncated = stripped[:keep] + suffix
    logger.warning(
        "Summary exceeded Jira's %d-char limit (%d chars); truncated to %d chars",
        _JIRA_SUMMARY_MAX_CHARS,
        len(stripped),
        len(truncated),
    )
    return truncated


def _sanitize_comment(body: str) -> str:
    """Truncate an over-length comment body to fit Jira's hard limit.

    Bug 6afc-20ee-84e5-4dd5. Jira Cloud rejects comment bodies > 32,767 chars,
    but ``acli ... comment create`` exits 0 on the rejection; ``_check_mutation_
    failure`` then raises ``AcliMutationError`` and the comment never lands —
    driving the outbound comment-sync loop (re-emitted every pass). Truncating
    here (mirroring ``_sanitize_summary``) lets the comment land.

    The actual truncation rule lives in the shared ``rebar_reconciler.comment_
    limits.truncate_comment_body`` helper so the differ's comparison path
    (``outbound_differ._diff_comments``) applies the IDENTICAL transform and the
    diff converges. A truncation warning is emitted so an operator can
    investigate; the local ticket store is never mutated (truncation is
    in-memory, send-side only).
    """
    if not isinstance(body, str):
        raise ValueError(
            f"Comment body must be str, got {type(body).__name__}: {body!r}"
        )
    truncated = _truncate_comment_body(body)
    if truncated is not body and len(truncated) != len(body):
        logger.warning(
            "Comment exceeded Jira's %d-char limit (%d chars); truncated to %d chars",
            _JIRA_COMMENT_MAX_CHARS,
            len(body),
            len(truncated),
        )
    return truncated


# Local status string → Jira workflow state name.
# status.capitalize() produces "In_progress" for snake_case inputs; this mapping
# ensures correct Jira state names are used in ACLI transition commands.
# ticket 929a: blocked/cancelled map to the nearest live DIG workflow state
# ({To Do, In Progress, In Review, Done} only); lossless information is
# preserved via rebar-status: annotation labels managed by outbound_differ.
_LOCAL_STATUS_TO_JIRA: dict[str, str] = {
    "open": "To Do",
    "in_progress": "In Progress",
    "closed": "Done",
    "blocked": "In Progress",
    "cancelled": "Done",
}


# ---------------------------------------------------------------------------
# ADF helpers
# ---------------------------------------------------------------------------


# _text_to_adf is imported from rebar_reconciler.adf (canonical location)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_env() -> dict[str, str]:
    """Build subprocess environment for ACLI."""
    return os.environ.copy()


class RetryExhaustedError(RuntimeError):
    """All retry attempts exhausted after transient HTTP/network errors."""


class AcliMutationError(RuntimeError):
    """ACLI mutation exited 0 but the structured --json output reports FAILURE.

    Bug 44de: ``acli jira workitem edit/transition/assign/comment/...`` returns
    exit=0 even when the underlying Jira operation fails. ACLI v1.3.18+ exposes
    structured failure info under ``--json``::

        {
          "results": [{"status": "FAILURE", "message": "...", "id": "..."}],
          "totalCount": 1,
          "successCount": 0
        }

    ``_run_acli`` parses stdout and raises this when ``successCount == 0`` or
    any ``results[].status == "FAILURE"``. Without it the reconciler marks
    mutations applied while Jira state diverges silently, corrupting
    binding-store invariants and breaking idempotent convergence.
    """


def _check_mutation_failure(stdout: str, cmd: list[str]) -> None:
    """Inspect ACLI ``--json`` stdout for the structured-failure shape and raise.

    Read-only commands (search, get) and successful mutations parse to shapes
    that lack ``successCount``/``results``, so the check is a no-op for them.
    Non-JSON stdout is treated as "no signal" — fall back to the exit-code
    contract that ``subprocess.run(check=True)`` already enforces.

    Raises:
        AcliMutationError: ``successCount == 0`` OR any ``results[].status``
            equals ``"FAILURE"`` (case-insensitive).
    """
    if not stdout or not stdout.strip():
        return
    try:
        parsed = json.loads(stdout)
    except (ValueError, TypeError):
        return  # Non-JSON output — defer to exit-code semantics.
    if not isinstance(parsed, dict):
        return  # search/get return lists; nothing to check.

    results = parsed.get("results")
    success_count = parsed.get("successCount")
    has_shape = isinstance(results, list) or success_count is not None
    if not has_shape:
        return  # Not the mutation-result shape (e.g., a created issue dict).

    failure_messages: list[str] = []
    if isinstance(results, list):
        for item in results:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "").strip().upper()
            if status == "FAILURE":
                msg = str(item.get("message") or "").strip()
                ident = str(item.get("id") or "").strip()
                failure_messages.append(
                    f"{ident}: {msg}" if ident and msg else (msg or ident or "FAILURE")
                )

    if failure_messages or success_count == 0:
        detail = (
            "; ".join(failure_messages)
            if failure_messages
            else f"successCount=0 (totalCount={parsed.get('totalCount')!r})"
        )
        raise AcliMutationError(
            f"ACLI mutation reported FAILURE (exit=0) for {cmd!r}: {detail}"
        )


# HTTP status codes eligible for automatic retry with backoff.
_RETRYABLE_HTTP_CODES: frozenset[int] = frozenset({429, 502, 503})


def _call_with_backoff(
    fn: Any,
    *args: Any,
    max_retries: int = 5,
    **kwargs: Any,
) -> Any:
    """Call fn with exponential backoff on transient HTTP/network errors.

    Retries up to *max_retries* times when ``fn`` raises:
    - ``urllib.error.HTTPError`` with status 429, 502, or 503
    - ``urllib.error.URLError`` (connection refused, DNS failure, timeout)

    Each retry waits with exponential backoff (2s base, capped at 60s) plus
    random jitter (0-1s).  If a ``Retry-After`` header is present on a 429
    response, that value is used instead of the computed delay.

    Raises RetryExhaustedError after all retries are exhausted.
    """
    last_error: urllib.error.HTTPError | urllib.error.URLError | None = None
    for attempt in range(max_retries + 1):  # initial + max_retries
        try:
            return fn(*args, **kwargs)
        except urllib.error.HTTPError as exc:
            if exc.code not in _RETRYABLE_HTTP_CODES:
                raise
            last_error = exc
            if attempt >= max_retries:
                break
            # Compute delay: exponential backoff capped at 60s, with jitter
            base_delay = min(2 ** (attempt + 1), 60)
            retry_after = (
                exc.headers.get("Retry-After")
                if exc.code == 429 and exc.headers
                else None
            )
            if retry_after is not None:
                try:
                    delay = float(retry_after)
                except (ValueError, TypeError):
                    delay = base_delay
            else:
                delay = base_delay
            delay += random.random()  # 0-1s jitter  # noqa: S311
            time.sleep(delay)
        except urllib.error.URLError as exc:
            # Transient network error — connection refused, DNS failure, timeout
            last_error = exc
            if attempt >= max_retries:
                break
            base_delay = min(2 ** (attempt + 1), 60)
            delay = base_delay + random.random()  # noqa: S311
            time.sleep(delay)

    assert last_error is not None
    raise RetryExhaustedError(
        f"All {max_retries} retries exhausted after transient errors"
    ) from last_error


def _run_acli(
    cmd: list[str],
    *,
    acli_cmd: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run an ACLI command with retry and exponential backoff.

    Retries up to 2 times (3 total attempts) on CalledProcessError,
    with backoff delays of 2s and 4s. Auth failures (exit code 401)
    and deterministic assignee errors ("cannot be assigned" or "User not
    found for email:") abort immediately without retrying.

    Raises CalledProcessError if all attempts are exhausted.
    """
    base = acli_cmd if acli_cmd is not None else _DEFAULT_ACLI_CMD
    full_cmd = base + cmd
    env = _build_env()

    last_error: subprocess.CalledProcessError | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            result = subprocess.run(
                full_cmd,
                capture_output=True,
                text=True,
                check=True,
                env=env,
            )
            # Bug 44de: ACLI exits 0 even when a mutation fails. Inspect the
            # structured --json output and raise AcliMutationError if the
            # response indicates FAILURE. Read-only and create-issue shapes
            # short-circuit harmlessly inside the helper.
            _check_mutation_failure(result.stdout, full_cmd)
            return result
        except subprocess.CalledProcessError as exc:
            last_error = exc
            # Fast-abort on auth failure
            if exc.returncode == _AUTH_FAILURE_CODE:
                raise
            # Fast-abort on deterministic assignee errors — retrying is pointless.
            # Callers print a contextual warning; no stderr print here to avoid duplication.
            if exc.stderr and (
                _ASSIGNEE_PERMISSION_ERROR in exc.stderr
                or _ASSIGNEE_NOT_FOUND_ERROR in exc.stderr
            ):
                raise
            # If more retries remain, sleep with exponential backoff
            if attempt < _MAX_ATTEMPTS - 1:
                delay = 2 ** (attempt + 1)  # 2s, 4s
                time.sleep(delay)

    # All attempts exhausted — include stderr in the error message for debugging
    assert last_error is not None
    if last_error.stderr:
        print(f"ACLI stderr: {last_error.stderr.strip()}", file=sys.stderr)
    raise last_error


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


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

    result = _create_issue_no_json(
        project, issue_type, summary, acli_cmd=acli_cmd, **kwargs
    )
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
        return _run_acli(cmd, acli_cmd=acli_cmd)
    except subprocess.CalledProcessError as exc:
        if exc.stderr and (
            _ASSIGNEE_PERMISSION_ERROR in exc.stderr
            or _ASSIGNEE_NOT_FOUND_ERROR in exc.stderr
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
        except Exception:
            if not fd_owned:
                os.close(fd)
            raise
        cmd = ["jira", "workitem", "create", "--from-json", json_path, "--json"]
        return _run_acli(cmd, acli_cmd=acli_cmd)
    except subprocess.CalledProcessError as exc:
        if exc.stderr and (
            _ASSIGNEE_PERMISSION_ERROR in exc.stderr
            or _ASSIGNEE_NOT_FOUND_ERROR in exc.stderr
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


def transition_issue(
    jira_key: str,
    status: str,
    *,
    acli_cmd: list[str] | None = None,
) -> dict[str, Any]:
    """Transition a Jira issue to *status* via REST (bug 85a1, Gap 8).

    Status changes go through ``POST /rest/api/3/issue/{key}/transitions``,
    NOT ACLI's ``workitem transition``. ACLI's transition subcommand exits
    0 even when the transition is rejected (Gap 5 — the lying-success bug);
    REST surfaces failures as HTTP 4xx/5xx, which propagate as
    ``urllib.error.HTTPError`` for the caller.

    *status* may be either a local-side name (``in_progress``, ``open``)
    or a Jira-side name (``In Progress``, ``To Do``). The former is mapped
    via ``_LOCAL_STATUS_TO_JIRA``; the latter is passed through. The
    ``transition_issue_by_name`` method on ``AcliClient`` then matches
    case-insensitively against each transition's ``name`` and ``to.name``,
    so workflows that use ``Move to <state>`` transition names are handled.

    Returns ``{"key": jira_key, "status": <resolved_name>}`` on success.
    The ``acli_cmd`` argument is accepted for backward compatibility but
    is no longer used.
    """
    resolved = _LOCAL_STATUS_TO_JIRA.get(status, status.replace("_", " ").title())
    client = AcliClient(
        jira_url=os.environ.get("JIRA_URL", ""),
        user=os.environ.get("JIRA_USER", ""),
        api_token=os.environ.get("JIRA_API_TOKEN", ""),
    )
    client.transition_issue_by_name(jira_key, resolved)
    return {"key": jira_key, "status": resolved}


def update_issue(
    jira_key: str,
    *,
    acli_cmd: list[str] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Update a Jira issue via ACLI.

    If ``status`` is in kwargs, it is routed to ``transition_issue``
    (Jira status changes require transitions, not field edits).
    Remaining fields are sent via ``workitem edit``.

    **Priority**: ACLI does not support editing priority via CLI flags.
    Priority updates are routed to ``update_priority()`` which uses the
    REST API directly (PUT /rest/api/3/issue/{key}).
    """
    status = kwargs.pop("status", None)
    priority = kwargs.pop("priority", None)
    if priority is not None:
        # Resolve priority to a Jira name string, then update via REST.
        if isinstance(priority, int):
            priority_name = _LOCAL_PRIORITY_TO_JIRA.get(priority, "Medium")
        elif isinstance(priority, dict):
            priority_name = priority.get("name") or "Medium"
        else:
            priority_name = str(priority)
        update_priority(jira_key, priority_name, acli_cmd=acli_cmd)

    if status is not None:
        transition_issue(jira_key, status, acli_cmd=acli_cmd)

    if not kwargs:
        # No editable fields remain (status/priority were already handled above)
        if status is not None:
            return {"key": jira_key, "status": status}
        return {"key": jira_key}

    cmd = [
        "jira",
        "workitem",
        "edit",
        "--key",
        jira_key,
        "--json",
    ]
    for field, value in kwargs.items():
        if field == "description":
            # Convert description to ADF (same as create_issue) — Jira REST API
            # v3 requires ADF format for description fields.
            cmd.extend([f"--{field}", json.dumps(_text_to_adf(str(value)))])
        else:
            cmd.extend([f"--{field}", str(value)])

    result = _run_acli(cmd, acli_cmd=acli_cmd)
    return json.loads(result.stdout)


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
    # update_issue (which has no client instance), we read from env vars.
    jira_url = os.environ.get("JIRA_URL", "")
    user = os.environ.get("JIRA_USER", "")
    api_token = os.environ.get("JIRA_API_TOKEN", "")
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
    data = json.dumps(
        {"fields": {"priority": {"name": priority_name}}}, ensure_ascii=False
    ).encode("utf-8")
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
    result = _run_acli(cmd, acli_cmd=acli_cmd)
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
    result = _run_acli(cmd, acli_cmd=acli_cmd)
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


def get_comments(
    jira_key: str,
    *,
    acli_cmd: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Get all comments on a Jira issue via ACLI."""
    cmd = [
        "jira",
        "workitem",
        "comment",
        "list",
        "--key",
        jira_key,
        "--json",
    ]
    result = _run_acli(cmd, acli_cmd=acli_cmd)
    return _parse_acli_comments(json.loads(result.stdout))


# ---------------------------------------------------------------------------
# AcliClient class — used by the rebar_reconciler bands (fetcher, applier,
# stale_band, open_count_skew_band) and the capability / forward-compat probes.
# ---------------------------------------------------------------------------


class AcliClient:
    """Client wrapping ACLI Go binary for Jira operations.

    Provides the method interface consumed by the rebar_reconciler:
    create_issue, update_issue, delete_issue, get_issue, search_issues,
    get_myself, get_server_info, get_comments, set_relationship, plus
    per-issue property read/write helpers.

    Credentials are injected into the subprocess environment on each call
    so ACLI can authenticate without requiring prior ``acli auth`` setup.
    """

    def __init__(
        self,
        jira_url: str,
        user: str,
        api_token: str,
        *,
        jira_project: str = "",
        acli_cmd: list[str] | None = None,
    ) -> None:
        self.jira_url = jira_url
        self.user = user
        self.api_token = api_token
        self.jira_project = jira_project
        self._acli_cmd = acli_cmd

    def _run(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Run an ACLI command.

        ACLI Go reads auth from its config file (set by ``acli auth login``).
        Credentials stored on self are available for callers that need them
        (e.g., direct REST calls), but are not injected into the subprocess
        environment — ACLI does not read env vars for auth.
        """
        return _run_acli(cmd, acli_cmd=self._acli_cmd)

    # --- Outbound API methods (local → Jira) ---

    def create_issue(self, ticket_data: dict[str, Any]) -> dict[str, Any]:
        """Create a Jira issue from a ticket data dict.

        Uses self.jira_project as the project key. Extracts ticket_type,
        title, description, priority, and assignee from ticket_data
        (matching the CREATE event data schema).
        """
        project = self.jira_project
        issue_type = ticket_data.get("ticket_type", "Task").capitalize()
        raw_summary = (ticket_data.get("title") or "").strip()
        if not raw_summary:
            raise ValueError(
                f"Cannot create Jira issue: title/summary is empty "
                f"(ticket_data keys: {list(ticket_data.keys())})"
            )
        # Defend against untrusted user input — truncate oversize titles
        # rather than crashing the reconciler pass on Jira's 255-char limit.
        summary = _sanitize_summary(raw_summary)
        optional_fields: dict[str, Any] = {}
        if ticket_data.get("description"):
            optional_fields["description"] = ticket_data["description"]
        if ticket_data.get("priority") is not None:
            optional_fields["priority"] = ticket_data["priority"]
        if ticket_data.get("assignee"):
            optional_fields["assignee"] = ticket_data["assignee"]
        # Parent sync (ticket 8b25): outbound_differ emits the resolved Jira
        # parent key into the create payload. The differ writes a BARE string
        # (``_map_local_to_jira_fields`` sets ``result["parent"] = jira_key``),
        # but accept the Jira REST nested shape ``{"key": K}`` too so a future
        # differ change does not silently drop the parent. create_issue then
        # attaches the parent at create time (--parent) or via set_parent
        # fallback (--from-json path). Previously dropped silently — bug 8b25.
        parent_key = _extract_parent_key(ticket_data.get("parent"))
        if parent_key:
            optional_fields["parent"] = parent_key
        return create_issue(
            project,
            issue_type,
            summary,
            acli_cmd=self._acli_cmd,
            client=self,
            **optional_fields,
        )

    def update_issue(self, jira_key: str, **kwargs: Any) -> dict[str, Any]:
        """Update a Jira issue via ACLI.

        Bug 85a1 (Fix D7): assignee=None/empty is routed through
        ``unassign_issue`` (REST PUT /assignee with ``{"accountId": null}``)
        rather than passed to ACLI as ``--assignee ""``, which ACLI silently
        no-ops (the probe Phase 2 verify-assignee-unassigned regression).

        Bug 06a5 (Gap 5 follow-up): non-empty assignee values are pre-validated
        against ``/rest/api/3/user/assignable/search`` and normalised to the
        matched ``accountId`` before the ACLI dispatch. Bogus assignees raise
        ``AssigneeNotFoundError`` here rather than silently no-op via ACLI's
        exit-0-on-failure contract.

        unassign_issue failures are caught and logged so a transient REST
        error does not abort the entire batch — the rest of the update_one
        body (label/comment dispatch, field edits) must still run.
        """
        if "assignee" in kwargs:
            if kwargs["assignee"] in (None, ""):
                kwargs.pop("assignee")
                try:
                    self.unassign_issue(jira_key)
                except Exception as exc:  # noqa: BLE001
                    print(  # noqa: T201
                        f"update_issue: unassign_issue({jira_key}) failed: {exc!r}",
                        file=sys.stderr,
                    )
            else:
                kwargs["assignee"] = self.validate_assignee_exists(
                    kwargs["assignee"], issue_key=jira_key
                )
        return update_issue(jira_key, acli_cmd=self._acli_cmd, **kwargs)

    def get_issue(self, jira_key: str) -> dict[str, Any]:
        """Get a Jira issue via ACLI."""
        return get_issue(jira_key, acli_cmd=self._acli_cmd)

    def get_issue_by_rest(self, jira_key: str) -> dict[str, Any]:
        """Get a Jira issue via direct REST GET (immediately consistent).

        Unlike get_issue (which uses ACLI's JQL search internally), this
        hits GET /rest/api/3/issue/{key} which reads from the primary store
        and is not subject to Jira Cloud's search index lag.
        """
        path = f"/rest/api/3/issue/{jira_key}"
        return self._direct_rest_get(path)

    def add_comment(self, jira_key: str, body: str) -> dict[str, Any]:
        """Add a comment to a Jira issue via ACLI."""
        return add_comment(jira_key, body, acli_cmd=self._acli_cmd)

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
        result = self._run(cmd)
        parsed = json.loads(result.stdout or "[]")
        if isinstance(parsed, list):
            return parsed
        # Some ACLI versions wrap the list in a dict under "issueLinkTypes"
        if isinstance(parsed, dict) and "issueLinkTypes" in parsed:
            return parsed["issueLinkTypes"]
        return []

    def search_issues(
        self,
        jql: str,
        start_at: int = 0,
        max_results: int = 50,
    ) -> list[dict[str, Any]]:
        """Search Jira issues via JQL, returning a page slice.

        ACLI Go has no offset flag, so --paginate fetches all results in one
        call. Results are cached per-JQL to avoid redundant fetches when the
        caller paginates. Returns a slice of ``[start_at:start_at+max_results]``
        to satisfy the reconciler's pagination loop contract.
        """
        # Cache the full result set for this JQL to avoid re-fetching
        if not hasattr(self, "_search_cache"):
            self._search_cache: dict[str, list[dict[str, Any]]] = {}

        if jql not in self._search_cache:
            cmd = [
                "jira",
                "workitem",
                "search",
                "--jql",
                jql,
                "-f",
                # Bug 5328: ``labels`` MUST be in this list. Without it the
                # batch snapshot has labels=[] for every issue, which makes
                # both differs hallucinate divergence symmetrically (outbound
                # emits ADD-every-tag, inbound emits REMOVE-every-tag, and
                # bidir suppression cancels them out). Any Jira-side label
                # ADD on a bound ticket then becomes invisible to inbound
                # because the snapshot pretends Jira has no labels at all.
                # Mirrors the single-issue ``get_issue`` field list above.
                "issuetype,key,assignee,priority,status,summary,description,labels",
                "--paginate",
                "--json",
            ]
            result = self._run(cmd)
            parsed = json.loads(result.stdout)
            if isinstance(parsed, list):
                all_issues = parsed
            elif isinstance(parsed, dict) and "issues" in parsed:
                all_issues = parsed["issues"]
            else:
                logging.warning(
                    "search_issues: unexpected ACLI JSON shape (type=%s); "
                    "treating as empty result. Response prefix: %.200r",
                    type(parsed).__name__,
                    parsed,
                )
                all_issues = []
            self._search_cache[jql] = all_issues

        all_issues = self._search_cache[jql]
        return all_issues[start_at : start_at + max_results]

    def get_server_info(self) -> dict[str, Any]:
        """Get Jira server info for timezone verification.

        Jira Cloud always stores timestamps in UTC. The legacy Java ACLI
        needed a JVM timezone flag to avoid locale-dependent serialization;
        the Go ACLI has no such issue. Connectivity is already verified by
        the workflow's ``acli auth login`` step — a redundant API call here
        would add latency and a failure mode with no diagnostic value.
        """
        return {"timeZone": "UTC", "serverTitle": "Jira Cloud"}

    def get_myself(self) -> dict[str, Any]:
        """Return the authenticated user's Jira profile via GET /rest/api/2/myself.

        Used to retrieve the service account's profile timezone, which Jira Cloud
        uses when interpreting unqualified JQL datetime strings. Cached per instance.
        """
        if hasattr(self, "_myself_cache"):
            return self._myself_cache  # type: ignore[return-value]
        url = f"{self.jira_url.rstrip('/')}/rest/api/2/myself"
        creds = base64.b64encode(f"{self.user}:{self.api_token}".encode()).decode()
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Basic {creds}", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                self._myself_cache: dict[str, Any] = json.loads(
                    resp.read().decode("utf-8")
                )
        except (urllib.error.URLError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            logging.warning("get_myself: failed to fetch /rest/api/2/myself: %s", exc)
            # missing keys gracefully (defaulting to UTC), and caching prevents a
            # second network failure on the same run from the verify+fetch double-call.
            self._myself_cache = {}
        return self._myself_cache

    def _rest_urlopen_with_retry(
        self,
        req: urllib.request.Request,
        *,
        timeout: int = 10,
    ) -> Any:
        """Execute urlopen(req, timeout=timeout) with transient-fault retry.

        Retries up to 2 times (3 total attempts) on transient connectivity
        errors: builtin ``TimeoutError`` (read-timeout from ssl/socket layer),
        ``urllib.error.URLError`` whose reason is a ``TimeoutError`` or
        ``ConnectionError``, and bare ``ConnectionError``.  Backoff delays are
        2 s after the first failure, 5 s after the second.

        Does NOT retry on ``urllib.error.HTTPError`` (4xx / 5xx) — HTTP-level
        error semantics are unchanged.  Raises the original exception after all
        attempts are exhausted.

        Retries are logged to stderr at WARNING level so they appear in the
        probe run log without polluting normal output.
        """
        _BACKOFFS = (2, 5)  # seconds between attempt 1→2 and 2→3
        last_exc: BaseException | None = None
        for attempt in range(3):
            try:
                return urllib.request.urlopen(req, timeout=timeout)
            except urllib.error.HTTPError:
                # HTTP errors (4xx/5xx) are deterministic — do not retry.
                raise
            except (TimeoutError, ConnectionError) as exc:
                last_exc = exc
            except urllib.error.URLError as exc:
                # URLError wraps lower-level errors in .reason; only retry
                # when the root cause is a timeout or connection failure.
                if isinstance(exc.reason, (TimeoutError, ConnectionError)):
                    last_exc = exc
                else:
                    raise
            if attempt < 2:
                delay = _BACKOFFS[attempt]
                print(
                    f"[REST-retry] attempt {attempt + 1} failed "
                    f"({last_exc!r}); retrying in {delay}s …",
                    file=sys.stderr,
                )
                time.sleep(delay)
        assert last_exc is not None
        raise last_exc

    def _direct_rest_put(self, path: str, data: Any) -> None:
        """PUT JSON data to a Jira issue-properties REST path using stored credentials.

        Wraps the body as ``{"value": data}`` per the Jira issue-properties
        API contract (used by set_issue_property). Do NOT use this for any
        other PUT endpoint (e.g. /rest/api/3/issue/{key} updates) — use
        _direct_rest_put_raw() instead so the body is sent unwrapped.

        Spike confirmed ACLI has no issue properties subcommand.
        Raises urllib.error.HTTPError on non-2xx response.
        """
        self._direct_rest_put_raw(path, {"value": data})

    def _direct_rest_post_raw(self, path: str, body: Any) -> None:
        """POST JSON body to a Jira REST path verbatim (no wrapping).

        Used for endpoints that take their own JSON shape — e.g.
        ``/rest/api/3/issue/{key}/transitions`` with
        ``{"transition": {"id": "..."}}``.

        Bug 85a1 (Gap 8): status outbound now uses REST instead of ACLI to
        avoid ACLI's silent-exit-0-on-failure (Gap 5). Returns None on 2xx;
        raises urllib.error.HTTPError on non-2xx.
        """
        url = f"{self.jira_url.rstrip('/')}{path}"
        creds = base64.b64encode(f"{self.user}:{self.api_token}".encode()).decode()
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        with self._rest_urlopen_with_retry(req, timeout=10) as resp:
            resp.read()

    def _direct_rest_put_raw(self, path: str, body: Any) -> None:
        """PUT JSON body to a Jira REST path verbatim (no wrapping).

        Used for endpoints that take their own JSON shape — e.g.
        /rest/api/3/issue/{key} with ``{"update": {"labels": [...]}}``,
        and issue-property writes (PUT /rest/api/3/issue/{key}/properties/{prop}
        whose request body IS the property value verbatim).
        Raises urllib.error.HTTPError on non-2xx response.
        """
        url = f"{self.jira_url.rstrip('/')}{path}"
        creds = base64.b64encode(f"{self.user}:{self.api_token}".encode()).decode()
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
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
        with self._rest_urlopen_with_retry(req, timeout=10) as resp:
            resp.read()

    def set_issue_property(self, jira_key: str, property_key: str, value: Any) -> None:
        """Set a Jira issue property via REST PUT.

        Calls /rest/api/3/issue/{jira_key}/properties/{property_key} with the
        value sent as the request body verbatim. Jira's issue-properties API
        stores whatever JSON is PUT as the property's value (the docs are
        explicit: "Request body: The value of the property. Must be valid
        JSON"). The earlier wrapping path (`_direct_rest_put` adding a
        `{"value": ...}` envelope) was incorrect — it caused the property to
        be stored as the literal `{"value": uuid}` dict instead of the uuid
        string. Bug 0b27-b785-dea8-49a0 surfaced this via the cfd6 live probe
        (STEP_PROPERTY_READ returned `{'value': uuid}` instead of `uuid`).

        Now uses `_direct_rest_put_raw` so the value is PUT exactly as-is.
        """
        path = f"/rest/api/3/issue/{jira_key}/properties/{property_key}"
        self._direct_rest_put_raw(path, value)

    def _direct_rest_get(self, path: str) -> Any:
        """GET JSON data from a Jira REST path using stored credentials.

        Follows the same urllib pattern as _direct_rest_put().
        Raises urllib.error.HTTPError on non-2xx response.

        Returns whatever json.loads decodes from the response body. Most Jira
        endpoints return a JSON object, but a few (e.g. issue-properties value
        when set to a scalar) return list/str/int/None. Callers that require a
        dict shape must validate explicitly.
        """
        url = f"{self.jira_url.rstrip('/')}{path}"
        creds = base64.b64encode(f"{self.user}:{self.api_token}".encode()).decode()
        req = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Authorization": f"Basic {creds}",
                "Accept": "application/json",
            },
        )
        with self._rest_urlopen_with_retry(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get_issue_property(self, jira_key: str, property_key: str) -> Any:
        """Get a Jira issue property via REST GET.

        Calls /rest/api/3/issue/{jira_key}/properties/{property_key} and returns
        the 'value' field from the response per the Jira issue properties API contract.

        Raises:
            urllib.error.HTTPError: from the underlying _direct_rest_get. Note
                that Jira returns 404 when the property does NOT exist on the
                issue — that case surfaces as HTTPError, NOT as KeyError below.
                Callers that need to handle "property not yet set" should catch
                HTTPError and inspect ``.code``.
            KeyError: only when the response IS a 2xx but the body shape is
                malformed (response is not a dict, or it lacks the 'value'
                field). This is a transport/proxy anomaly, NOT the
                missing-property signal. The exception message includes a
                truncated repr of the response for diagnostics; long bodies
                are clipped to 200 chars to avoid leaking credentials or PII
                from upstream error pages.
        """
        path = f"/rest/api/3/issue/{jira_key}/properties/{property_key}"
        response = self._direct_rest_get(path)
        if not isinstance(response, dict) or "value" not in response:
            # Clip the response repr so corporate-gateway error bodies that
            # may include auth headers or session cookies cannot leak in full
            # to logs / StepResult.details.
            _repr = repr(response)
            if len(_repr) > 200:
                _repr = _repr[:200] + f"...(truncated, {len(_repr)} chars total)"
            raise KeyError(
                f"Jira issue-property response for {jira_key}/{property_key} "
                f"missing 'value' field: {_repr}"
            )
        return response["value"]

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
        except Exception:
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
        except Exception:
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

    def set_entity_property(self, issue_key: str, prop_name: str, value: Any) -> None:
        """Alias for set_issue_property — sets a Jira entity property."""
        return self.set_issue_property(issue_key, prop_name, value)

    def get_entity_property(self, issue_key: str, prop_name: str) -> Any:
        """Alias for get_issue_property — retrieves a Jira entity property.

        Inherits the same Raises contract as get_issue_property:
        urllib.error.HTTPError on transport/4xx (including 404 for absent
        properties), KeyError only when the 2xx body shape is malformed.
        """
        return self.get_issue_property(issue_key, prop_name)

    def transition_issue_by_name(self, jira_key: str, target_status: str) -> None:
        """Transition a Jira issue to *target_status* via REST.

        Bug 85a1 (Gap 8): replaces the previous ACLI-based ``transition_issue``
        which silently exited 0 on bogus transitions (Gap 5). Uses direct
        REST so HTTP status codes reliably surface failure:

          1. GET /rest/api/3/issue/{key}/transitions to list available
          2. Match *target_status* (case-insensitive) against each
             transition's ``name`` first, then ``to.name``. Workflows that
             use "Move to <state>" transition names with a distinct
             target-state name are handled by the ``to.name`` fallback.
          3. POST /rest/api/3/issue/{key}/transitions with
             ``{"transition": {"id": "<id>"}}``.

        Raises a ``RuntimeError`` (with available transition names listed)
        when no transition reaches *target_status* — the workflow does not
        allow it from the current state. Raises ``urllib.error.HTTPError``
        on non-2xx response from the POST.

        Per-issue lookup, not cached: transitions are issue-state-specific
        (depend on current status + workflow + caller permissions). Caching
        by project+issuetype produces incorrect hits for an issue mid-
        workflow.
        """
        transitions_resp = self._direct_rest_get(
            f"/rest/api/3/issue/{jira_key}/transitions"
        )
        transitions = (
            transitions_resp.get("transitions", [])
            if isinstance(transitions_resp, dict)
            else []
        )
        target_lower = target_status.strip().lower()
        match_id = None
        for t in transitions:
            if not isinstance(t, dict):
                continue
            name = (t.get("name") or "").strip().lower()
            to_name = ((t.get("to") or {}).get("name") or "").strip().lower()
            if target_lower in (name, to_name):
                match_id = t.get("id")
                if match_id:
                    break
        if not match_id:
            available = [
                f"{t.get('name')!r}->{(t.get('to') or {}).get('name')!r}"
                for t in transitions
                if isinstance(t, dict)
            ]
            raise RuntimeError(
                f"transition_issue_by_name: no transition reaches "
                f"{target_status!r} on {jira_key}. Available: "
                f"{available if available else '[none]'}"
            )
        self._direct_rest_post_raw(
            f"/rest/api/3/issue/{jira_key}/transitions",
            {"transition": {"id": str(match_id)}},
        )

    def validate_assignee_exists(
        self,
        assignee: str,
        *,
        issue_key: str | None = None,
        project_key: str | None = None,
    ) -> str:
        """Validate *assignee* resolves to an assignable user; return accountId.

        Mirrors the client-side pre-validation pattern from
        ``transition_issue_by_name`` (Gap 8). GETs
        ``/rest/api/3/user/assignable/search?query=<assignee>&issueKey=<key>``
        (or ``&project=<project>`` when called from a CREATE path with no
        issue key yet), then returns the matched ``accountId``. Callers should
        forward this resolved accountId to ACLI rather than the raw input to
        eliminate display-name/email ambiguity at the API boundary.

        Raises ``AssigneeNotFoundError`` when no user matches. Raises
        ``ValueError`` when neither scope arg is supplied.
        """
        if not (issue_key or project_key):
            raise ValueError(
                "validate_assignee_exists: issue_key or project_key required"
            )
        query_part = f"query={urllib.parse.quote(assignee)}"
        scope_part = (
            f"issueKey={urllib.parse.quote(issue_key)}"
            if issue_key
            else f"project={urllib.parse.quote(project_key or '')}"
        )
        path = f"/rest/api/3/user/assignable/search?{query_part}&{scope_part}"
        users = self._direct_rest_get(path)
        if not isinstance(users, list) or not users:
            scope_label = (
                f"issue={issue_key!r}" if issue_key else f"project={project_key!r}"
            )
            raise AssigneeNotFoundError(
                f"validate_assignee_exists: no assignable user matches "
                f"{assignee!r} for {scope_label}"
            )
        # Prefer exact match on emailAddress / accountId / displayName;
        # fall back to the first result (Jira's relevance ordering).
        for u in users:
            if not isinstance(u, dict):
                continue
            if assignee in (
                u.get("emailAddress"),
                u.get("accountId"),
                u.get("displayName"),
            ):
                acct = u.get("accountId")
                if acct:
                    return acct
        first = users[0]
        if isinstance(first, dict) and first.get("accountId"):
            return first["accountId"]
        raise AssigneeNotFoundError(
            f"validate_assignee_exists: assignable search returned results "
            f"with no accountId for {assignee!r}"
        )

    def unassign_issue(self, jira_key: str) -> None:
        """Explicitly unassign a Jira issue via REST v3 PUT.

        Uses direct REST v3 (not ACLI binary) because the /assignee endpoint
        requires body {"accountId": null} at root level — ACLI's _direct_rest_put
        wraps body as {"value": data} which is rejected by the assignee endpoint.
        Empirically verified: direct REST PUT is the de-facto pattern used by
        pycontribs/jira and atlassian-python-api for null-accountId unassign.
        """
        path = f"/rest/api/3/issue/{jira_key}/assignee"
        url = f"{self.jira_url.rstrip('/')}{path}"
        creds = base64.b64encode(f"{self.user}:{self.api_token}".encode()).decode()
        body = json.dumps({"accountId": None}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="PUT",
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/json",
            },
        )
        with self._rest_urlopen_with_retry(req, timeout=10) as resp:
            resp.read()

    def get_comments(self, jira_key: str) -> list[dict[str, Any]]:
        """Get all comments on a Jira issue."""
        cmd = [
            "jira",
            "workitem",
            "comment",
            "list",
            "--key",
            jira_key,
            "--json",
        ]
        result = self._run(cmd)
        return _parse_acli_comments(json.loads(result.stdout))

    def set_parent(self, jira_key: str, parent_key: str | None) -> None:
        """Set or clear the parent of a Jira issue via REST PUT.

        ACLI edit does NOT support --parent reparenting (verified live — ticket
        8b25-ae7a-efc3-47f6).  Uses direct REST:
        PUT /rest/api/3/issue/{key} {"fields":{"parent":{"key":"..."}}}

        When ``parent_key`` is None or empty, clears the parent by passing
        ``{"fields": {"parent": None}}``.

        Probe-validated: returns 204 on success.
        """
        if parent_key:
            body: Any = {"fields": {"parent": {"key": parent_key}}}
        else:
            body = {"fields": {"parent": None}}
        self._direct_rest_put_raw(f"/rest/api/3/issue/{jira_key}", body)

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
            except Exception as exc:  # noqa: BLE001
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
            except Exception as exc:  # noqa: BLE001
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

    def _direct_rest_post_json(self, path: str, body: Any) -> Any:
        """POST JSON to a Jira REST path and return the decoded JSON response.

        Unlike ``_direct_rest_post_raw`` (which discards the response body),
        this helper returns the parsed JSON — needed by ``get_parent_map`` to
        read search results.

        Raises ``urllib.error.HTTPError`` on non-2xx responses.
        """
        url = f"{self.jira_url.rstrip('/')}{path}"
        creds = base64.b64encode(f"{self.user}:{self.api_token}".encode()).decode()
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        with self._rest_urlopen_with_retry(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

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

    def update_comment(
        self, jira_key: str, comment_id: str, body: str
    ) -> dict[str, Any]:
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

    def _direct_rest_delete(self, path: str) -> None:
        """DELETE a Jira REST resource using stored credentials.

        Raises urllib.error.HTTPError on non-2xx response.
        """
        url = f"{self.jira_url.rstrip('/')}{path}"
        creds = base64.b64encode(f"{self.user}:{self.api_token}".encode()).decode()
        req = urllib.request.Request(
            url,
            method="DELETE",
            headers={
                "Authorization": f"Basic {creds}",
                "Accept": "application/json",
            },
        )
        with self._rest_urlopen_with_retry(req, timeout=10) as resp:
            resp.read()

    def set_relationship(
        self,
        from_key: str,
        to_key: str,
        link_type: str = "Blocks",
    ) -> dict[str, Any]:
        """Create a link between two Jira issues.

        Raises subprocess.CalledProcessError on ACLI failure.
        """
        cmd = [
            "jira",
            "workitem",
            "link",
            "create",
            "--out",
            from_key,
            "--in",
            to_key,
            "--type",
            link_type,
            # Bug 44de: --json enables structured-failure detection.
            "--json",
        ]
        self._run(cmd)  # raises on failure — no silent swallowing
        return {"status": "created", "from": from_key, "to": to_key}

    def get_issue_links(self, jira_key: str) -> list[dict[str, Any]]:
        """Get existing issue links for a Jira issue.

        Returns a list of link dicts matching the Jira REST API format:
        ``[{"type": {"name": ...}, "inwardIssue": {...}|None, "outwardIssue": {...}|None}]``

        Used by the LINK handler for pre-create deduplication.
        Raises subprocess.CalledProcessError on ACLI failure.
        """
        cmd = [
            "jira",
            "workitem",
            "link",
            "list",
            "--key",
            jira_key,
            "--json",
        ]
        result = self._run(cmd)
        parsed = json.loads(result.stdout or "[]")
        if isinstance(parsed, list):
            return parsed
        # Some ACLI versions wrap results in a dict with an "issuelinks" key
        if isinstance(parsed, dict):
            return parsed.get("issuelinks", [])
        return []

    def delete_issue(
        self,
        jira_key: str,
    ) -> dict[str, Any]:
        """Delete a Jira issue via ACLI.

        Uses ``jira workitem delete --key KEY`` to permanently remove the issue.

        - 404 response (issue already gone) is treated as idempotent success.
        - 403 response (permission denied) raises ``PermissionError`` so callers
          can write a BRIDGE_ALERT and skip deletion without crashing.

        Raises:
            PermissionError: When ACLI exits with a 403 permission error.
            subprocess.CalledProcessError: On other ACLI failures (single attempt — no retry).
        """
        base = self._acli_cmd if self._acli_cmd is not None else _DEFAULT_ACLI_CMD
        # `--yes` skips ACLI's interactive confirmation prompt. Without it,
        # `acli jira workitem delete` waits on stdin for confirmation and
        # exits non-zero in non-TTY contexts (bug 3256-f960-4ae6-4943
        # surfaced by the live cfd6 capability probe run).
        full_cmd = base + [
            "jira",
            "workitem",
            "delete",
            "--key",
            jira_key,
            "--yes",
            # Bug 44de: --json so structured-failure detection runs on the
            # exit=0-on-failure path that ACLI exposes for delete too.
            "--json",
        ]
        try:
            completed = subprocess.run(
                full_cmd,
                capture_output=True,
                text=True,
                check=True,
                env=_build_env(),
            )
            # Bug 44de: delete bypasses _run_acli, so call the check here too.
            _check_mutation_failure(completed.stdout, full_cmd)
        except subprocess.CalledProcessError as exc:
            err_text = (exc.stderr or "") + (exc.stdout or "")
            if "404" in err_text or "not found" in err_text.lower():
                # Already deleted — idempotent success
                return {"status": "not_found", "key": jira_key}
            if "403" in err_text or "forbidden" in err_text.lower():
                msg = f"Permission denied deleting {jira_key}: {err_text.strip()}"
                raise PermissionError(msg) from exc
            raise
        return {"status": "deleted", "key": jira_key}

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
            # Bug 44de: --json enables structured-failure detection.
            "--json",
        ]
        self._run(cmd)
        return {"status": "deleted", "link_id": link_id}
