"""Dedicated Jira Data Center summary-write executor (REB-3115 S1 T3).

This layer performs ONE logical summary write as a bounded, provider-neutral
sequence of physical requests — GET → PUT → GET — with the transport's SDK-level
retries fully disabled, so the shared retry-budget owner (``retry_budget``) is the
single authority over replay. It only *executes and observes*; it never sleeps and
never replays.

It REUSES the REB-3115 S1 T1 seam rather than rebuilding any of it:

* the outcome value type and the secret-redaction/512-cap helper
  (``operation_outcome.OperationOutcome`` / ``_redact_message``);
* the ambiguous-PUT decision table and replay-safety map
  (``retry_budget.decide_replay`` / ``replay_safety_for``).

The per-physical-call retry policy is the one-attempt/no-sleep parameterization of
the transport's ``_with_connection_retry`` (``attempts=1, backoffs=()``), and the
default client class factory is ``transport._jira_client_class()`` — neither the
legacy transport retry default nor ``build_client_from_settings`` is altered.

Import convention: this package ships as package DATA under ``src/rebar/_engine``
and is on ``sys.path`` as ``rebar_reconciler`` (no ``rebar._engine`` prefix).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from rebar_reconciler._backend import BackendEnvError, BackendHTTPError
from rebar_reconciler.adapters.jira_datacenter import transport as _transport
from rebar_reconciler.adapters.jira_datacenter.retry import (
    _connection_retry_exceptions,
    _with_connection_retry,
)
from rebar_reconciler.operation_outcome import (
    DelaySource,
    Disposition,
    FailureScope,
    OperationOutcome,
    _redact_message,
)
from rebar_reconciler.retry_budget import decide_replay, replay_safety_for

_LOGICAL_ID_NAMESPACE = uuid.UUID("6f5b5d1e-3115-5f3c-9b0a-000000000001")
_MAX_LOG_CODEPOINTS = 1024


class ExecutorClientError(RuntimeError):
    """Raised when the dedicated executor client cannot be built with retries disabled."""


def _logical_id(remote_id: str) -> str:
    """A stable logical id derived from the remote issue id (uuid5 over it)."""
    return str(uuid.uuid5(_LOGICAL_ID_NAMESPACE, remote_id))


def build_executor_client(settings: Any, *, jira_cls: Any = None) -> Any:
    """Build a DEDICATED executor ``jira.JIRA`` client with SDK retries DISABLED.

    Constructed the same way :func:`build_client_from_settings` builds its client
    (``server=settings.url``, ``token_auth=settings.pat``, the same ``verify`` /
    CA-bundle option handling), but with ``max_retries=0`` added so the library
    does not silently re-drive the physical GET/PUT/GET that this executor and the
    shared owner are trying to bound.

    After construction ``client._session.max_retries`` MUST be exactly ``0`` or an
    :class:`ExecutorClientError` is raised (fail loud). ``build_client_from_settings``
    and every legacy path are left untouched.
    """
    if not (getattr(settings, "pat", None) or "").strip():
        raise BackendEnvError(
            "JIRA_PAT is not set. The dedicated summary executor authenticates with the "
            "same Personal Access Token as the rest of the Jira Data Center backend; an "
            "empty PAT would construct an ANONYMOUS client that silently executes the "
            "GET/PUT/GET summary write without authentication. Export it before "
            "reconciling:\n    export JIRA_PAT=<your personal access token>"
        )
    factory = jira_cls if jira_cls is not None else _transport._jira_client_class()
    options: dict[str, Any] = {}
    if getattr(settings, "ca_bundle", None):
        options["verify"] = settings.ca_bundle
    client = factory(
        server=settings.url,
        token_auth=settings.pat,
        options=options or None,
        max_retries=0,
    )
    if getattr(getattr(client, "_session", None), "max_retries", None) != 0:
        raise ExecutorClientError(
            "the dedicated executor client did not disable SDK-level retries "
            "(client._session.max_retries != 0); refusing to use a client that would "
            "double-drive the bounded GET/PUT/GET sequence."
        )
    return client


def _applied_outcome(remote_id: str) -> OperationOutcome:
    return OperationOutcome(
        logical_id=_logical_id(remote_id),
        disposition=Disposition.applied,
        failure_scope=FailureScope.none,
        replay_safety=replay_safety_for(Disposition.applied),
        invocation_count=1,
        request_count=3,
        delay_source=DelaySource.none,
        provider_delay_ms=None,
        retry_not_before=None,
        diagnostics=(),
    )


def _failed_outcome(
    remote_id: str, *, disposition: Disposition, request_count: int
) -> OperationOutcome:
    if disposition == Disposition.permanent_failure:
        scope = FailureScope.ticket
    else:
        scope = FailureScope.endpoint
    return OperationOutcome(
        logical_id=_logical_id(remote_id),
        disposition=disposition,
        failure_scope=scope,
        replay_safety=replay_safety_for(disposition),
        invocation_count=1,
        request_count=request_count,
        delay_source=DelaySource.none,
        provider_delay_ms=None,
        retry_not_before=None,
        diagnostics=(),
    )


def _classify_initial_get_failure(exc: BaseException) -> Disposition:
    """Classify a fault on the initial (pre-mutation) GET.

    The initial GET performs no write, so it is always safe to replay: a
    connection-level/transient transport fault (``requests`` ConnectionError /
    Timeout, or builtin ``TimeoutError`` / ``ConnectionError``) is therefore
    ``retryable_deferred``. A definitive HTTP error (``BackendHTTPError``, i.e. a
    4xx/5xx translated at the transport boundary) is a conclusive
    ``permanent_failure`` — replaying it would not help.
    """
    if isinstance(exc, BackendHTTPError):
        return Disposition.permanent_failure
    if isinstance(exc, (*_connection_retry_exceptions(), TimeoutError, ConnectionError)):
        return Disposition.retryable_deferred
    return Disposition.permanent_failure


def execute_summary_write(client: Any, remote_id: str, new_summary: str) -> OperationOutcome:
    """Execute ONE summary write as exactly GET → PUT → GET, each physical call
    wrapped in the one-attempt/no-sleep executor policy.

    A fault on the initial GET (before any PUT) ends the invocation with
    ``request_count=1``, classified as ``retryable_deferred`` for a transient
    transport fault (the GET mutated nothing, so replay is safe) or
    ``permanent_failure`` for a definitive HTTP error. A fault on the PUT is
    ambiguous (the write may have been applied), so it ends with ``commit_unknown``
    / ``forbidden`` replay-safety and ``request_count=2``; the read-back GET is NOT
    issued in either failure case. On a successful PUT the read-back GET is
    authoritative and IS inspected: the outcome is ``applied`` (``request_count=3``)
    only when the read-back confirms ``summary == new_summary``; if the read-back
    cannot be fetched, or conclusively shows the summary did not change, the outcome
    is ``commit_unknown`` / ``forbidden`` (``request_count=3``) — the write was
    accepted, so this layer neither claims success nor auto-replays. This layer
    never sleeps and never replays — the shared owner decides replay.
    """
    try:
        issue = _with_connection_retry(lambda: client.issue(remote_id), attempts=1, backoffs=())
    except Exception as exc:  # noqa: BLE001 — any transport fault on the initial GET ends it
        return _failed_outcome(
            remote_id, disposition=_classify_initial_get_failure(exc), request_count=1
        )

    try:
        _with_connection_retry(
            lambda: issue.update(fields={"summary": new_summary}), attempts=1, backoffs=()
        )
    except Exception:  # noqa: BLE001 — a fault after PUT dispatch is ambiguous; no read-back
        return _failed_outcome(remote_id, disposition=Disposition.commit_unknown, request_count=2)

    try:
        confirm = _with_connection_retry(lambda: client.issue(remote_id), attempts=1, backoffs=())
    except Exception:  # noqa: BLE001 — PUT was accepted but the read-back could not confirm
        return _failed_outcome(remote_id, disposition=Disposition.commit_unknown, request_count=3)

    observed = getattr(getattr(confirm, "fields", None), "summary", None)
    if observed == new_summary:
        return _applied_outcome(remote_id)
    return _failed_outcome(remote_id, disposition=Disposition.commit_unknown, request_count=3)


def observe_after_ambiguous_put(
    client: Any,
    remote_id: str,
    expected_summary: str,
    *,
    budget_remaining: bool,
) -> OperationOutcome:
    """A SEPARATE physical invocation: ONE GET to observe state after an ambiguous
    PUT, mapped through :func:`decide_replay`.

    * read-back succeeds and its summary == expected → observation ``"desired"``;
    * read-back succeeds and summary != expected → observation ``"old_conclusive"``;
    * the GET itself raises → observation ``"failed"``.

    Never sleeps and never replays.
    """
    try:
        issue = _with_connection_retry(lambda: client.issue(remote_id), attempts=1, backoffs=())
        observed = getattr(getattr(issue, "fields", None), "summary", None)
        observation = "desired" if observed == expected_summary else "old_conclusive"
    except Exception:  # noqa: BLE001 — a failed observation GET maps through decide_replay
        observation = "failed"

    disposition, _replay = decide_replay(observation=observation, budget_remaining=budget_remaining)
    if disposition == Disposition.recovered:
        scope = FailureScope.none
    else:
        scope = FailureScope.endpoint
    return OperationOutcome(
        logical_id=_logical_id(remote_id),
        disposition=disposition,
        failure_scope=scope,
        replay_safety=replay_safety_for(disposition),
        invocation_count=1,
        request_count=1,
        delay_source=DelaySource.none,
        provider_delay_ms=None,
        retry_not_before=None,
        diagnostics=(),
    )


def render_completion_log(outcome: OperationOutcome, *, message: str | None = None) -> str:
    """Return a JSON string with EXACTLY the seven completion-log keys, bounded to
    1024 code points.

    ``cleanup_status`` is the fixed string ``"not_applicable"`` (DC has no subprocess
    to clean up). ``message`` is redacted through the reused T1 redactor (512-cap);
    if the whole serialized string still exceeds 1024 code points, the message value
    is truncated further (with an ellipsis) and re-serialized until it fits.
    """
    text = _redact_message(message if message is not None else "")
    doc = {
        "logical_id": outcome.logical_id,
        "disposition": outcome.disposition.value,
        "invocation_count": outcome.invocation_count,
        "request_count": outcome.request_count,
        "cleanup_status": "not_applicable",
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
