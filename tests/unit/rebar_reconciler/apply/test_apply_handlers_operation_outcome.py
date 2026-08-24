"""[P0] RP-03 S1 T4 — the legacy-default summary-executor seam in ``handle_update``.

``BatchApplyContext`` carries an optional, **constructor-injected** ``summary_executor``.
It defaults to ``None`` — the production wiring passes nothing, so every outbound update
stays on the legacy ``dispatch_one.update_one`` path exactly as before (no config key, no
environment key gates it: selection is pure constructor state).

When a test injects a ``summary_executor`` — a provider-neutral callable
``(client, jira_key, new_summary) -> OperationOutcome`` — an outbound update whose fields
are EXACTLY ``{"summary": <str>}`` routes through it instead of the generic path. A confirmed
outcome (``applied`` / ``recovered``) preserves the legacy result and advances the ADR-0026
baseline (``ctx.synced_fields``) with the summary that landed.

This file is the happy-path core. The edge tables — mixed-field non-splitting, the
generic-retry poison bypass, terminal (``commit_unknown`` / ``retryable_deferred``) mapping
with its single redacted ≤512-code-point message and *no* baseline/provenance advance, the
512-boundary, and the ADR-0103 S3-ownership assertion — live in the held-out suite.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_ENGINE = Path(__file__).resolve().parents[4] / "src" / "rebar" / "_engine"
if str(_ENGINE) not in sys.path:  # pragma: no cover - import bootstrap
    sys.path.insert(0, str(_ENGINE))

from rebar_reconciler.apply_handlers import BatchApplyContext, handle_update  # noqa: E402
from rebar_reconciler.operation_outcome import (  # noqa: E402
    DelaySource,
    Disposition,
    FailureScope,
    OperationOutcome,
    ReplaySafety,
)


class _StubClient:
    """A transport recording every legacy ``update_issue`` call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.comments: list[str] = []

    def update_issue(self, jira_key: str, **fields: Any) -> dict[str, Any]:
        self.calls.append(dict(fields))
        return {"key": jira_key}

    def add_comment(self, jira_key: str, body: str) -> None:
        self.comments.append(body)


def _summary_mutation(new_summary: str, *, local_id: str = "loc-1") -> dict[str, Any]:
    return {
        "action": "update",
        "key": "REB-1",
        "local_id": local_id,
        "fields": {"summary": new_summary},
    }


def _outcome(
    disposition: Disposition,
    *,
    retry_not_before: str | None = None,
    diagnostics: tuple = (),
) -> OperationOutcome:
    return OperationOutcome(
        logical_id="op-1",
        disposition=disposition,
        failure_scope=FailureScope.none,
        replay_safety=ReplaySafety.not_applicable,
        invocation_count=1,
        request_count=1,
        delay_source=DelaySource.none,
        provider_delay_ms=None,
        retry_not_before=retry_not_before,
        diagnostics=diagnostics,
    )


def _ctx(tmp_path: Path, client: _StubClient, executor: Any = None) -> BatchApplyContext:
    return BatchApplyContext(
        client=client,
        repo_root=tmp_path,
        pass_id="p1",
        summary_executor=executor,
    )


# ── AC1: default is legacy ────────────────────────────────────────────────────


def test_default_context_routes_summary_only_update_through_legacy(tmp_path: Path) -> None:
    """No injected executor: an exact-summary update takes the legacy transport path and
    advances the baseline, byte-for-byte as before this ticket."""
    client = _StubClient()
    ctx = _ctx(tmp_path, client)

    handle_update(_summary_mutation("new title"), ctx)

    assert client.calls == [{"summary": "new title"}], "legacy update_issue path was used"
    assert ctx.synced_fields == {"loc-1": {"summary": "new title"}}, "baseline advanced"


def test_summary_executor_defaults_to_none(tmp_path: Path) -> None:
    """The selector is an opt-in constructor field; the default context carries no executor."""
    ctx = _ctx(tmp_path, _StubClient())
    assert ctx.summary_executor is None


# ── AC2 + AC3: selected exact-summary routes through the executor ─────────────


def test_selected_exact_summary_routes_through_injected_executor(tmp_path: Path) -> None:
    """With an executor injected, an exact ``{"summary"}`` update is handed to it — the
    legacy transport is NOT called — and an ``applied`` outcome advances the baseline."""
    client = _StubClient()
    seen: list[tuple[Any, str, str]] = []

    def fake_executor(c: Any, jira_key: str, new_summary: str) -> OperationOutcome:
        seen.append((c, jira_key, new_summary))
        return _outcome(Disposition.applied)

    ctx = _ctx(tmp_path, client, executor=fake_executor)

    handle_update(_summary_mutation("new title"), ctx)

    assert seen == [(client, "REB-1", "new title")], "executor received (client, key, summary)"
    assert client.calls == [], "the generic legacy update_issue path was bypassed"
    assert ctx.synced_fields == {"loc-1": {"summary": "new title"}}, "applied advances baseline"


# ═══════════════════════════════════════════════════════════════════════════════
# HELD-OUT edge/boundary suite (RP-03 S1 T4). Appended after the blind
# implementation. Every assertion targets observable behaviour through
# handle_update / BatchApplyContext / ctx.synced_fields — never a private name.
# ═══════════════════════════════════════════════════════════════════════════════

import os  # noqa: E402

import pytest  # noqa: E402

import rebar_reconciler.dispatch_one as _dispatch_one  # noqa: E402
from rebar_reconciler.apply_handlers import HandlerResult  # noqa: E402
from rebar_reconciler.operation_outcome import bound_diagnostics  # noqa: E402


def _mixed_mutation(local_id: str = "loc-1") -> dict:
    return {
        "action": "update",
        "key": "REB-1",
        "local_id": local_id,
        "fields": {"summary": "new title", "description": "new body"},
    }


def _diag(message: str) -> tuple:
    return bound_diagnostics([{"stage": "execute", "category": "error", "message": message}])


def _raw_diag(message: str) -> tuple:
    """A diagnostic whose message is NOT pre-bounded, to prove the handler's own cap."""
    from types import MappingProxyType

    return (MappingProxyType({"stage": "execute", "category": "error", "message": message}),)


# ── AC2: mixed fields never split; stay a single legacy call ──────────────────


def test_mixed_fields_stay_one_legacy_call_even_with_selector(tmp_path: Path) -> None:
    client = _StubClient()
    seen: list = []

    def fake_executor(c: Any, jira_key: str, new_summary: str) -> OperationOutcome:
        seen.append((jira_key, new_summary))
        return _outcome(Disposition.applied)

    ctx = _ctx(tmp_path, client, executor=fake_executor)

    handle_update(_mixed_mutation(), ctx)

    assert seen == [], "a mixed-field update must NOT be routed to the summary executor"
    assert client.calls == [{"summary": "new title", "description": "new body"}], (
        "mixed fields stay ONE unsplit legacy update_issue call — never dual-sent"
    )


# ── AC2: the selected route bypasses the generic retry wrapper ────────────────


def test_selected_summary_route_does_not_touch_generic_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def poison(*_a: Any, **_k: Any):
        raise AssertionError("the selected summary route must not use _call_with_retry")

    # Poison the generic retry wrapper at BOTH the source definition AND the
    # dispatch_one re-export, so a violating route is caught no matter which import
    # path it reaches the wrapper through.
    import rebar_reconciler.dispatch_apply_phases as _dap

    monkeypatch.setattr(_dispatch_one, "_call_with_retry", poison)
    monkeypatch.setattr(_dap, "_call_with_retry", poison)

    client = _StubClient()
    ctx = _ctx(tmp_path, client, executor=lambda *_a: _outcome(Disposition.applied))

    handle_update(_summary_mutation("new title"), ctx)  # must not raise

    assert ctx.synced_fields == {"loc-1": {"summary": "new title"}}


def test_legacy_route_still_uses_generic_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = _dispatch_one._call_with_retry
    seen: list = []

    def spy(fn: Any, *a: Any, **k: Any):
        seen.append(getattr(fn, "__name__", repr(fn)))
        return real(fn, *a, **k)

    monkeypatch.setattr(_dispatch_one, "_call_with_retry", spy)

    ctx = _ctx(tmp_path, _StubClient())  # no executor → legacy
    handle_update(_summary_mutation("new title"), ctx)

    assert "update_issue" in seen, "the unselected legacy route retains generic retry"


# ── AC4: terminal outcomes — one redacted message, no advance, batch continues ─


@pytest.mark.parametrize(
    "disposition", [Disposition.commit_unknown, Disposition.retryable_deferred]
)
def test_terminal_outcome_records_one_message_and_advances_no_baseline(
    tmp_path: Path, disposition: Disposition
) -> None:
    client = _StubClient()
    outcome = _outcome(
        disposition,
        retry_not_before="2026-08-24T00:00:00Z",
        diagnostics=_diag("ambiguous commit"),
    )
    ctx = _ctx(tmp_path, client, executor=lambda *_a: outcome)

    result = handle_update(_summary_mutation("new title"), ctx)

    assert isinstance(result, HandlerResult), "the handler returns — the batch continues"
    assert ctx.synced_fields == {}, "a terminal outcome advances NO baseline"
    err = result.outcome.get("error")
    assert isinstance(err, str) and err, "exactly one per-mutation error/disposition message"
    assert len(err) <= 512, "the message is capped at 512 Unicode code points"
    assert disposition.value in err, "the message names the terminal disposition"
    assert not result.outcome.get("result"), "no successful legacy result on a terminal outcome"


def test_retryable_deferred_carries_retry_not_before(tmp_path: Path) -> None:
    outcome = _outcome(
        Disposition.retryable_deferred,
        retry_not_before="2026-08-24T12:00:00Z",
        diagnostics=_diag("deferred"),
    )
    ctx = _ctx(tmp_path, _StubClient(), executor=lambda *_a: outcome)

    result = handle_update(_summary_mutation("new title"), ctx)

    assert result.outcome.get("retry_not_before") == "2026-08-24T12:00:00Z"
    assert ctx.synced_fields == {}


def test_terminal_message_capped_at_512_code_points(tmp_path: Path) -> None:
    outcome = _outcome(Disposition.commit_unknown, diagnostics=_raw_diag("x" * 10_000))
    ctx = _ctx(tmp_path, _StubClient(), executor=lambda *_a: outcome)

    result = handle_update(_summary_mutation("new title"), ctx)

    assert len(result.outcome["error"]) <= 512


def test_terminal_message_is_redacted(tmp_path: Path) -> None:
    outcome = _outcome(
        Disposition.commit_unknown, diagnostics=_raw_diag("auth failed Bearer s3cr3t-token-value")
    )
    ctx = _ctx(tmp_path, _StubClient(), executor=lambda *_a: outcome)

    result = handle_update(_summary_mutation("new title"), ctx)

    assert "s3cr3t-token-value" not in result.outcome["error"], "the secret is redacted"


# ── AC3: recovered is a confirmed success (advances baseline like applied) ─────


def test_recovered_outcome_advances_baseline(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, _StubClient(), executor=lambda *_a: _outcome(Disposition.recovered))

    handle_update(_summary_mutation("new title"), ctx)

    assert ctx.synced_fields == {"loc-1": {"summary": "new title"}}


def test_already_satisfied_is_defensive_forward_compat_success(tmp_path: Path) -> None:
    """``already_satisfied`` is NOT emitted by the S1 T2/T3 summary executors (they
    emit ``applied``/``recovered`` on success); it is included in the success cohort
    as a defensive/forward-compat no-op mapping. Pin that mapping so a future
    executor that returns it advances the baseline identically to ``applied``, with
    the synced value derived from the routed mutation's own ``fields["summary"]``.
    """
    ctx = _ctx(
        tmp_path, _StubClient(), executor=lambda *_a: _outcome(Disposition.already_satisfied)
    )

    handle_update(_summary_mutation("new title"), ctx)

    assert ctx.synced_fields == {"loc-1": {"summary": "new title"}}


# ── AC5: no durable deferral written in S1; unrelated batch work continues ─────


def test_terminal_outcome_persists_no_new_files(tmp_path: Path) -> None:
    before = {p for p in tmp_path.rglob("*") if p.is_file()}
    outcome = _outcome(Disposition.commit_unknown, diagnostics=_diag("ambiguous"))
    ctx = _ctx(tmp_path, _StubClient(), executor=lambda *_a: outcome)

    handle_update(_summary_mutation("new title"), ctx)

    after = {p for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before, "S1 persists no durable deferral (that is RP-03 S3)"


# ── AC1: no environment key gates the selector ────────────────────────────────


def test_no_environment_key_gates_the_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in list(os.environ):
        if any(tok in key.upper() for tok in ("SUMMARY", "EXECUTOR", "SELECT", "ATOMIC", "ROUTE")):
            monkeypatch.delenv(key, raising=False)

    client = _StubClient()
    # default → legacy regardless of environment
    handle_update(_summary_mutation("a"), _ctx(tmp_path, client))
    assert client.calls == [{"summary": "a"}]

    # injected → executor regardless of environment
    seen: list = []
    ctx = _ctx(
        tmp_path,
        _StubClient(),
        executor=lambda c, k, s: seen.append((k, s)) or _outcome(Disposition.applied),
    )
    handle_update(_summary_mutation("b"), ctx)
    assert seen == [("REB-1", "b")]


# ── AC6: ADR 0103 names S3 as the sole cutover / fuse / retirement owner ───────


def test_adr_0103_names_s3_as_cutover_owner() -> None:
    adr = (
        Path(__file__).resolve().parents[4]
        / "docs"
        / "adr"
        / "0103-reconciler-operation-coordination.md"
    ).read_text(encoding="utf-8")
    lowered = adr.lower()
    assert "s3" in lowered, "the ADR names S3"
    assert "cutover" in lowered, "S3 owns production cutover"
    assert "fuse" in lowered, "S3 owns the fuse / deferral consumer"
    assert "retire" in lowered or "retirement" in lowered, "S3 owns bridge retirement"
