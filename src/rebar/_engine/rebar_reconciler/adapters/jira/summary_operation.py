"""Dedicated Jira Cloud summary-write operation (REB-3115 S1 T2).

This layer performs ONE logical Cloud summary write and its primary-store
observation as bounded, provider-neutral steps that EXECUTE and OBSERVE only —
it never sleeps and never replays. The single logical retry budget
(``retry_budget``) is the sole authority over replay; this adapter maps outcome
classes into the shared contract and returns metadata to that owner.

It REUSES the REB-3115 S1 T1 seam rather than rebuilding any of it:

* the outcome value type, bounded-diagnostics helper, and the
  secret-redaction/512-cap helper (``operation_outcome.OperationOutcome`` /
  ``bound_diagnostics`` / ``_redact_message``);
* the ambiguous-observation decision table and replay-safety map
  (``retry_budget.decide_replay`` / ``replay_safety_for``).

The Cloud side differs from the Data Center executor (``jira_datacenter.
summary_executor``): the write crosses an ACLI subprocess (``acli jira workitem
edit``) and the observation is a single urllib REST GET of the primary store,
opted into the transport's one-attempt / no-sleep per-call policy
(:data:`ONE_ATTEMPT_NO_SLEEP`, re-exported here so exactly one shared singleton
is threaded through the seam).

Import convention: this package ships as package DATA under ``src/rebar/_engine``
and is on ``sys.path`` as ``rebar_reconciler`` (no ``rebar._engine`` prefix).
"""

from __future__ import annotations

import json
import urllib.error
import uuid
from typing import Any

from rebar_reconciler.adapters.jira import acli_subprocess
from rebar_reconciler.adapters.jira.acli_rest import ONE_ATTEMPT_NO_SLEEP  # re-export SAME object
from rebar_reconciler.operation_outcome import (
    DelaySource,
    Disposition,
    FailureScope,
    OperationOutcome,
    _redact_message,
    bound_diagnostics,
)
from rebar_reconciler.retry_budget import decide_replay, replay_safety_for

__all__ = [
    "ONE_ATTEMPT_NO_SLEEP",
    "classify_rest_error",
    "execute_cloud_summary_write",
    "observe_summary_via_rest",
    "render_completion_log",
]

_LOGICAL_ID_NAMESPACE = uuid.UUID("6f5b5d1e-3115-5f3c-9b0a-000000000002")
_MAX_LOG_CODEPOINTS = 1024
_DEFAULT_CLEANUP_STATUS = "not_applicable"


def _logical_id(jira_key: str) -> str:
    """A stable logical id derived from the Cloud issue key (uuid5 over it)."""
    return str(uuid.uuid5(_LOGICAL_ID_NAMESPACE, jira_key))


def _scope_for(disposition: Disposition) -> FailureScope:
    if disposition == Disposition.applied:
        return FailureScope.none
    if disposition == Disposition.permanent_failure:
        return FailureScope.ticket
    return FailureScope.endpoint


def _outcome(
    jira_key: str,
    *,
    disposition: Disposition,
    diagnostics: tuple[Any, ...] = (),
) -> OperationOutcome:
    """Build an ``OperationOutcome`` for a ONE-invocation, ONE-request Cloud step.

    ``replay_safety`` is derived through the shared T1 map (``forbidden`` only for
    ``commit_unknown``); ``diagnostics`` are bounded + redacted through the reused
    T1 ``bound_diagnostics`` (≤8 entries, each message redacted and capped — AC7).
    No delay is ever produced here: this adapter neither sleeps nor replays."""
    return OperationOutcome(
        logical_id=_logical_id(jira_key),
        disposition=disposition,
        failure_scope=_scope_for(disposition),
        replay_safety=replay_safety_for(disposition),
        invocation_count=1,
        request_count=1,
        delay_source=DelaySource.none,
        provider_delay_ms=None,
        retry_not_before=None,
        diagnostics=bound_diagnostics(diagnostics),
    )


def _classify_write_error(exc: BaseException) -> Disposition:
    """Map a subprocess-write fault into the shared contract WITHOUT replaying.

    * a rejected credential (``AcliAuthError``) is deterministic → ``permanent_failure``;
    * an ACLI structured mutation FAILURE (``AcliMutationError``) is a data/permission
      rejection Jira will make again → ``permanent_failure``;
    * a WRITE timeout (``AcliTimeoutError``) is ambiguous — the edit may have committed
      server-side — so it is ``commit_unknown`` (replay ``forbidden``);
    * everything else (a bare non-zero exit / exhausted transport retries) is a
      transient connectivity class → ``retryable_deferred``.
    """
    if isinstance(exc, acli_subprocess.AcliAuthError):
        return Disposition.permanent_failure
    if isinstance(exc, acli_subprocess.AcliMutationError):
        return Disposition.permanent_failure
    if isinstance(exc, acli_subprocess.AcliTimeoutError):
        return Disposition.commit_unknown
    return Disposition.retryable_deferred


def execute_cloud_summary_write(client: Any, jira_key: str, new_summary: str) -> OperationOutcome:
    """Launch EXACTLY ONE ACLI ``jira workitem edit`` process to set the summary (AC1).

    Routes through the ``acli_subprocess`` seam so a fake ``_run_acli`` observes
    exactly one call. On success returns ``OperationOutcome(applied,
    invocation_count=1, request_count=1)``. On a classified permanent failure returns
    ``permanent_failure``; on a transient/ambiguous failure returns the appropriate
    non-applied disposition WITHOUT sleeping or replaying. Never replays here — the
    shared retry budget owns replay.
    """
    cmd = [
        "jira",
        "workitem",
        "edit",
        "--key",
        jira_key,
        "--summary",
        new_summary,
        "--json",
    ]
    try:
        acli_subprocess._run_acli(
            cmd,
            acli_cmd=getattr(client, "_acli_cmd", None),
            retry_on_timeout=False,  # a WRITE: a timed-out edit is ambiguous, never retried
            call_timeout=getattr(client, "_call_timeout", None),
        )
    except Exception as exc:  # noqa: BLE001 — every fault is mapped into the shared contract
        disposition = _classify_write_error(exc)
        return _outcome(
            jira_key,
            disposition=disposition,
            diagnostics=({"stage": "cloud_summary_write", "message": str(exc)},),
        )
    return _outcome(jira_key, disposition=Disposition.applied)


def observe_summary_via_rest(
    client: Any,
    jira_key: str,
    expected_summary: str,
    *,
    budget_remaining: bool,
) -> OperationOutcome:
    """Observe the PRIMARY store with EXACTLY ONE REST request and no sleep (AC2).

    Issues ``client.get_issue_by_rest(jira_key, retry_policy=ONE_ATTEMPT_NO_SLEEP)``
    — a REST GET of the issue from the primary store (no ACLI search, no JQL, no
    inner sleep) — and maps the observation through the shared ``decide_replay``:

    * ``result["fields"]["summary"] == expected_summary`` → ``"desired"``;
    * ``!= expected_summary`` → ``"old_conclusive"``;
    * the call (or the field read) raises → ``"failed"``.

    Never sleeps, never replays; classification/delay metadata is returned to the
    shared owner via the outcome.
    """
    diagnostics: tuple[Any, ...] = ()
    try:
        result = client.get_issue_by_rest(jira_key, retry_policy=ONE_ATTEMPT_NO_SLEEP)
        observed = result["fields"]["summary"]
        observation = "desired" if observed == expected_summary else "old_conclusive"
    except Exception as exc:  # noqa: BLE001 — a failed observation maps through decide_replay
        observation = "failed"
        diagnostics = ({"stage": "cloud_summary_observe", "message": str(exc)},)

    disposition, _replay = decide_replay(observation=observation, budget_remaining=budget_remaining)
    return _outcome(jira_key, disposition=disposition, diagnostics=diagnostics)


def classify_rest_error(exc: BaseException) -> Disposition:
    """Classify a REST error into the shared contract WITHOUT replaying (AC4).

    * ``urllib.error.HTTPError`` (any 4xx/5xx — auth 401/403, permission, invalid) →
      ``permanent_failure`` (checked FIRST, since ``HTTPError`` subclasses ``URLError``);
    * ``urllib.error.URLError`` wrapping a timeout/connection fault, or a bare
      ``ConnectionError`` / ``TimeoutError`` → ``retryable_deferred``;
    * any other ``URLError`` (a TLS/cert failure, name resolution) is not eligible
      network → ``permanent_failure``.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return Disposition.permanent_failure
    if isinstance(exc, urllib.error.URLError):
        if isinstance(exc.reason, (TimeoutError, ConnectionError)):
            return Disposition.retryable_deferred
        return Disposition.permanent_failure
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return Disposition.retryable_deferred
    return Disposition.permanent_failure


def render_completion_log(
    outcome: OperationOutcome,
    *,
    message: str | None = None,
    cleanup_status: str = _DEFAULT_CLEANUP_STATUS,
) -> str:
    """Return a JSON string with EXACTLY the seven completion-log keys, bounded to
    1024 code points (AC8) — identical contract to
    ``summary_executor.render_completion_log``.

    Keys: ``logical_id``, ``disposition`` (= ``outcome.disposition.value``),
    ``invocation_count``, ``request_count``, ``cleanup_status`` (the Cloud
    subprocess cleanup status; defaults to ``"not_applicable"``), ``retry_not_before``,
    and ``message`` (redacted through the reused T1 512-cap redactor). If the whole
    serialized string still exceeds 1024 code points, the message value is shrunk
    (with an ellipsis) and re-serialized until it fits.
    """
    text = _redact_message(message if message is not None else "")
    doc = {
        "logical_id": outcome.logical_id,
        "disposition": outcome.disposition.value,
        "invocation_count": outcome.invocation_count,
        "request_count": outcome.request_count,
        "cleanup_status": cleanup_status,
        "retry_not_before": outcome.retry_not_before,
        "message": text,
    }
    serialized = json.dumps(doc, ensure_ascii=False)
    while len(serialized) > _MAX_LOG_CODEPOINTS and doc["message"]:
        overflow = len(serialized) - _MAX_LOG_CODEPOINTS
        current = doc["message"]
        keep = max(0, len(current) - overflow - 1)
        doc["message"] = (current[:keep] + "\u2026") if keep > 0 else ""
        serialized = json.dumps(doc, ensure_ascii=False)
    return serialized
