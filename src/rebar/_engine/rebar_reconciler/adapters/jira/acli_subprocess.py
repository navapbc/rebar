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
import re
import subprocess
import sys
import time
from typing import Any, NamedTuple

from rebar_reconciler._errors import RetryExhaustedError  # noqa: F401 — re-exported via acli

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_ACLI_CMD: list[str] = ["acli"]
_MAX_ATTEMPTS: int = 3  # initial + 2 retries
_AUTH_FAILURE_CODE: int = 401

# --- Subprocess timeout / process-group reaping (bug d843) -----------------
# A hung ``acli`` child (interactive prompt, stuck socket, JVM/network-helper
# grandchild holding the capture pipe) must never freeze a reconcile pass.
# ``subprocess.run(timeout=)`` only reaps the DIRECT child — a grandchild on
# the pipe defeats it (CPython bpo-30154). So we Popen(start_new_session=True),
# communicate(timeout=), and on TimeoutExpired reap the whole process GROUP
# (SIGTERM -> grace -> SIGKILL), bounding the post-kill drain so a D-state
# child can't block forever. Worst-case per call = call_timeout + GRACE + DRAIN.
_DEFAULT_ACLI_TIMEOUT: int = 120  # seconds; acli does OAuth + network
_ACLI_GRACE_SECONDS: int = 3  # SIGTERM grace before SIGKILL (JVM flush headroom)
_ACLI_DRAIN_SECONDS: int = 2  # bounded post-SIGKILL reap/drain (D-state safe)


def _acli_call_timeout() -> int:
    """Per-call subprocess timeout (seconds), resolved through the typed config:
    the config-file key ``[tool.rebar.reconciler].jira_cli_timeout``, overridden by
    env ``REBAR_JIRA_CLI_TIMEOUT`` (deprecated alias ``REBAR_ACLI_TIMEOUT``), then by
    ``rebar -c reconciler.jira_cli_timeout=…``.

    Defaults to :data:`_DEFAULT_ACLI_TIMEOUT` (120s). The typed default (0 = unset)
    and any non-positive or unreadable value fall back to the default rather than
    failing the call — a zero/negative timeout would make ``communicate(timeout=0)``
    time out every call instantly.
    """
    from rebar.config import ConfigError, load_config

    try:
        value = load_config().reconciler.jira_cli_timeout
    except ConfigError:
        return _DEFAULT_ACLI_TIMEOUT
    return value if value > 0 else _DEFAULT_ACLI_TIMEOUT


class JiraSettings(NamedTuple):
    """Resolved Jira connection settings: the non-secret ``url``/``user``/``project``
    (from the typed Config) plus the secret ``api_token`` (env-only)."""

    url: str
    user: str
    project: str
    api_token: str


def resolve_jira_settings(*, project_default: str = "") -> JiraSettings:
    """Resolve the Jira connection settings through the single config entry point.

    ``url`` / ``user`` / ``project`` come from ``load_config().jira.*`` so a
    ``[tool.rebar.jira]`` / ``rebar.toml`` / legacy ``.rebar/config.conf`` value is
    actually consumed, with the Atlassian-standard env vars ``JIRA_URL`` /
    ``JIRA_USER`` / ``JIRA_PROJECT`` overriding the file (they are the canonical env
    layer). The SECRET ``JIRA_API_TOKEN`` is read from the environment ONLY — it is
    never a config-file key. ``project_default`` substitutes for an empty project
    (e.g. ``"DIG"``, which ACLI requires on CREATE — bug 4fa9). A malformed config
    degrades to the prior env-only behavior rather than breaking a reconcile pass.
    """
    from rebar.config import ConfigError, load_config

    try:
        jira = load_config().jira
        url, user, project = jira.url, jira.user, jira.project
    except ConfigError:
        url = os.environ.get("JIRA_URL", "")
        user = os.environ.get("JIRA_USER", "")
        project = os.environ.get("JIRA_PROJECT", "")
    return JiraSettings(
        url=url,
        user=user,
        project=project or project_default,
        api_token=os.environ.get("JIRA_API_TOKEN", ""),
    )


_ASSIGNEE_PERMISSION_ERROR: str = "cannot be assigned"
_ASSIGNEE_NOT_FOUND_ERROR: str = (
    "User not found for email:"  # prefix match — email value varies per call
)

# C4 (943f): rate-limit (429) backoff for the live _run_acli subprocess loop. ACLI is a
# subprocess, so a 429 surfaces only as text in stderr (its exit code + Retry-After
# availability are provider-dependent and unverified) — so we detect it from stderr
# markers and honor a Retry-After value IFF present. Cap any delay at _MAX_BACKOFF_S so a
# huge/hostile Retry-After (or continuous 429s) can never hang the pass; _MAX_ATTEMPTS
# still bounds the total attempts and the terminal error is unchanged (CalledProcessError).
_MAX_BACKOFF_S: float = 60.0
_RATE_LIMIT_MARKERS: tuple[str, ...] = ("429", "too many requests", "rate limit", "rate-limit")
_RETRY_AFTER_RE = re.compile(r"retry[-\s]?after[:\s]+(\d+(?:\.\d+)?)", re.IGNORECASE)


def _rate_limit_backoff(attempt: int, stderr: str | None) -> float | None:
    """Return a backoff delay (seconds) if *stderr* looks like an HTTP 429 rate-limit,
    else None (the caller keeps its default uniform backoff — this is add-on, not a
    replacement of the general retry policy). Honors ``Retry-After`` when parseable,
    otherwise jittered exponential backoff; both are capped at _MAX_BACKOFF_S."""
    text = (stderr or "").lower()
    if not any(m in text for m in _RATE_LIMIT_MARKERS):
        return None
    m = _RETRY_AFTER_RE.search(stderr or "")
    if m:
        try:
            delay = min(float(m.group(1)), _MAX_BACKOFF_S)
            logger.warning("acli: 429 rate-limited; honoring Retry-After=%.1fs", delay)
            return delay
        except ValueError:
            pass
    delay = min(2.0 ** (attempt + 1), _MAX_BACKOFF_S) + random.uniform(0, 1)
    logger.warning(
        "acli: 429 rate-limited; no Retry-After — jittered backoff %.1fs (attempt %d)",
        delay,
        attempt + 1,
    )
    return delay


class AssigneeNotFoundError(ValueError):
    """Raised when a requested assignee does not resolve to any assignable Jira user.

    Bug 06a5 / 85a1 (Gap 5 follow-up): mirrors the client-side pre-validation
    pattern used by ``transition_issue_by_name`` (Gap 8). Caught before the
    outbound mutation is dispatched so the bogus-assignee class does not
    silently no-op via ACLI's exit-0-on-failure contract.
    """


# `RetryExhaustedError` is the UNIFIED type from `_errors` (epic romp-swath-wince); imported at
# the top (not defined here) so the `acli` surface re-exports the SAME object `applier` does. Its
# MRO still subclasses RuntimeError, so this module's `except RuntimeError` sites are unaffected.


class AcliTimeoutError(Exception):
    """An ACLI subprocess exceeded its wall-clock budget and was reaped (bug d843).

    Terminal: raised when ``_run_acli`` times out and either the call is a
    non-retryable WRITE or the read retries are exhausted. The child (and its
    whole process group) has already been SIGTERM/SIGKILL-reaped.

    Deliberately **NOT** a subclass of the builtin :class:`TimeoutError`
    (validation spike E4): ``apply_outbound._call_with_retry`` catches
    ``TimeoutError`` and would otherwise blindly re-retry a timed-out write,
    re-introducing the duplicate-write bug Jira's non-idempotent create/link
    makes dangerous.

    Carries the command and any partial stdout/stderr captured from the
    original :class:`subprocess.TimeoutExpired` for diagnostics.
    """

    def __init__(
        self,
        cmd: list[str],
        timeout: float,
        *,
        partial_stdout: str | None = None,
        partial_stderr: str | None = None,
    ) -> None:
        self.cmd = cmd
        self.timeout = timeout
        self.partial_stdout = partial_stdout
        self.partial_stderr = partial_stderr
        super().__init__(f"ACLI command timed out after {timeout}s: {cmd!r}")


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


def _backoff_sleep(seconds: float) -> None:
    """Sleep *seconds* between ACLI retry attempts (the single retry-backoff seam).

    Deliberately a distinct, patchable indirection over ``time.sleep`` rather than
    a bare ``time.sleep`` call. Tests that assert the retry-backoff schedule can
    patch THIS function to observe only the retry delays. Patching the module-global
    ``time.sleep`` instead would also capture the sub-second poll sleeps
    CPython's ``subprocess.Popen._wait`` busy-wait emits from inside
    ``communicate()`` (an exponential 0.0005→0.05s series whose iteration count
    depends on how quickly the OS reaps the child) — an unrelated, load-dependent
    signal that made the 429-backoff assertion flaky under load. Same behavior as
    ``time.sleep``; the split exists purely to isolate the observable seam.
    """
    time.sleep(seconds)


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


def _decode_partial(data: Any) -> str | None:
    """Decode partial stdout/stderr from a TimeoutExpired for diagnostics.

    CPython leaves ``TimeoutExpired.stdout``/``.stderr`` as the UNDECODED bytes
    read before the timeout even in text mode. Decode with ``errors='replace'``
    so a truncated multibyte lead (spike E3) never raises here.
    """
    if data is None:
        return None
    if isinstance(data, str):
        return data
    if isinstance(data, (bytes, bytearray)):
        return bytes(data).decode("utf-8", errors="replace")
    return None


def _reap_process_group(p: subprocess.Popen[str]) -> None:
    """Reap a timed-out ACLI child + its process group via the shared reaper (bug d843).

    Thin wrapper over :func:`rebar._proc.reap_process_group` — the single source of
    truth for the SIGTERM → grace → SIGKILL → bounded-drain, ESRCH/EPERM-guarded,
    D-state-safe group reap (formerly duplicated byte-for-byte in the grounding
    harness). This caller pins its OWN timing constants and log identity
    (``label="acli"``, this module's ``logger``); update the shared helper — not a
    private copy — for any behavior change.

    Imported function-locally, mirroring this module's other ``rebar.*`` imports
    (``rebar.config``): the reconciler subprocess always has ``rebar`` importable
    (``rebar_reconciler.__main__`` imports it at startup and puts the package parent
    on ``sys.path`` as a fallback), and ``rebar._proc`` is a stdlib-only leaf.
    """
    from rebar._proc import reap_process_group

    reap_process_group(
        p,
        grace=_ACLI_GRACE_SECONDS,
        drain=_ACLI_DRAIN_SECONDS,
        label="acli",
        logger=logger,
    )


def _run_acli(
    cmd: list[str],
    *,
    acli_cmd: list[str] | None = None,
    retry_on_timeout: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run an ACLI command with retry, exponential backoff, and a bounded timeout.

    Retries up to 2 times (3 total attempts) on CalledProcessError,
    with backoff delays of 2s and 4s. Auth failures (exit code 401)
    and deterministic assignee errors ("cannot be assigned" or "User not
    found for email:") abort immediately without retrying.

    Each invocation is bounded by ``REBAR_JIRA_CLI_TIMEOUT`` (deprecated alias
    ``REBAR_ACLI_TIMEOUT``; default 120s) and run
    in its own process session, so a hung ``acli`` child (or a pipe-holding
    grandchild) is reaped rather than freezing the pass (bug d843). On timeout:

    - ``retry_on_timeout=True`` (READS only — they are idempotent) retries the
      timed-out call within the existing attempt loop with backoff.
    - ``retry_on_timeout=False`` (the default; WRITES) raises
      :class:`AcliTimeoutError` immediately — a timed-out Jira write is
      ambiguous (may have committed server-side) and Jira create/link is
      non-idempotent, so a blind retry would duplicate. The terminal error is
      deliberately not a builtin ``TimeoutError`` so the outer
      ``_call_with_retry`` won't re-retry it.

    Raises:
        CalledProcessError: if all CalledProcessError attempts are exhausted.
        AcliTimeoutError: on a non-retryable (write) timeout, or read timeout
            with all attempts exhausted. Terminal — raised BEFORE
            ``_check_mutation_failure`` so a killed child never fabricates a
            success.
    """
    base = acli_cmd if acli_cmd is not None else _DEFAULT_ACLI_CMD
    full_cmd = base + cmd
    env = _build_env()
    call_timeout = _acli_call_timeout()

    last_error: subprocess.CalledProcessError | None = None
    for attempt in range(_MAX_ATTEMPTS):
        # --- Spawn in its own session so killpg can reap the whole group. -----
        popen_kwargs: dict[str, Any] = dict(
            stdin=subprocess.DEVNULL,  # any unexpected acli prompt fails fast
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",  # spike E3: SIGKILL mid-multibyte must not crash the reap
            env=env,
        )
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True  # POSIX-only (killpg needs it)
        p = subprocess.Popen(full_cmd, **popen_kwargs)
        try:
            out, err = p.communicate(timeout=call_timeout)
        except subprocess.TimeoutExpired as exc:
            # M3: partial output comes from the ORIGINAL exception — the
            # post-kill communicate() calls return ('', ''). Even in text mode,
            # TimeoutExpired.stdout/.stderr carry the UNDECODED bytes read so
            # far (CPython never decodes the partial), so decode them ourselves
            # with errors='replace' (spike E3: a truncated multibyte lead must
            # not crash this diagnostic path).
            partial_out = _decode_partial(exc.stdout)
            partial_err = _decode_partial(exc.stderr)
            _reap_process_group(p)
            if retry_on_timeout and attempt < _MAX_ATTEMPTS - 1:
                _backoff_sleep(2 ** (attempt + 1))  # 2s, 4s — retry within the loop
                continue
            # Terminal: raised BEFORE _check_mutation_failure (never fabricate a
            # success on a killed child).
            raise AcliTimeoutError(
                full_cmd,
                call_timeout,
                partial_stdout=partial_out,
                partial_stderr=partial_err,
            ) from exc

        result = subprocess.CompletedProcess(full_cmd, p.returncode, out, err)
        if p.returncode != 0:
            # Preserve the previous check=True semantics.
            cpe = subprocess.CalledProcessError(p.returncode, full_cmd, out, err)
            last_error = cpe
            # Fast-abort on auth failure
            if cpe.returncode == _AUTH_FAILURE_CODE:
                raise cpe
            # Fast-abort on deterministic assignee errors — retrying is pointless.
            # Callers print a contextual warning; no stderr print here to avoid duplication.
            if cpe.stderr and (
                _ASSIGNEE_PERMISSION_ERROR in cpe.stderr or _ASSIGNEE_NOT_FOUND_ERROR in cpe.stderr
            ):
                raise cpe
            # If more retries remain, sleep before retrying. A detected 429 rate-limit
            # gets add-on jittered backoff (honoring Retry-After when present); every
            # other non-zero exit keeps the uniform exponential sleep — no double-sleep,
            # and the terminal contract (CalledProcessError) is unchanged.
            if attempt < _MAX_ATTEMPTS - 1:
                rl_delay = _rate_limit_backoff(attempt, cpe.stderr)
                _backoff_sleep(rl_delay if rl_delay is not None else 2 ** (attempt + 1))
            continue

        # Bug 44de: ACLI exits 0 even when a mutation fails. Inspect the
        # structured --json output and raise AcliMutationError if the response
        # indicates FAILURE. Only reached on a real, completed run — never on a
        # killed child. Read-only and create-issue shapes short-circuit harmlessly.
        _check_mutation_failure(result.stdout, full_cmd)
        return result

    # All attempts exhausted — include stderr in the error message for debugging
    assert last_error is not None
    if last_error.stderr:
        print(f"ACLI stderr: {last_error.stderr.strip()}", file=sys.stderr)
    raise last_error
