#!/usr/bin/env python3
"""ACLI process execution + retry + typed mutation errors.

The transport floor of the ACLI client: build the subprocess environment, run
an ACLI command with retry/backoff and fast-abort on auth/assignee errors,
inspect ACLI's lying-success ``--json`` output for structured FAILURE, and the
typed errors that surface those conditions. stdlib only.
"""

from __future__ import annotations

import json
import logging
import os
import random
import subprocess
import sys
import time
import urllib.error
from typing import Any

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

# HTTP status codes eligible for automatic retry with backoff.
_RETRYABLE_HTTP_CODES: frozenset[int] = frozenset({429, 502, 503})


class AssigneeNotFoundError(ValueError):
    """Raised when a requested assignee does not resolve to any assignable Jira user.

    Bug 06a5 / 85a1 (Gap 5 follow-up): mirrors the client-side pre-validation
    pattern used by ``transition_issue_by_name`` (Gap 8). Caught before the
    outbound mutation is dispatched so the bogus-assignee class does not
    silently no-op via ACLI's exit-0-on-failure contract.
    """


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


def _build_env() -> dict[str, str]:
    """Build subprocess environment for ACLI."""
    return os.environ.copy()


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
        raise AcliMutationError(f"ACLI mutation reported FAILURE (exit=0) for {cmd!r}: {detail}")


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
                exc.headers.get("Retry-After") if exc.code == 429 and exc.headers else None
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
                _ASSIGNEE_PERMISSION_ERROR in exc.stderr or _ASSIGNEE_NOT_FOUND_ERROR in exc.stderr
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
