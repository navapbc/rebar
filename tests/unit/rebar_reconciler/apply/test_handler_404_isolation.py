"""A Jira 404 on ANY outbound action is a per-mutation failure, never pass-fatal
(bug 449f-f9bf-be90-47fe, mode 3).

`handle_update` soft-fails a 404 (`apply_handlers.py`, bug tan-coin-atone/6614) and
its comment states the rule in general terms: "A 404 on a single mutation's target
means the issue is gone: this is a PER-MUTATION failure, never pass-fatal."
`handle_create` and `handle_delete` never got that treatment — they call
`create_one` / `delete_one` with no `try` at all. `applier._apply_one` re-raises
`urllib.error.HTTPError` ABOVE its per-mutation backstop (`record_backstop_failure`)
precisely because it assumes "404 soft-failed in the handler", so a 404 from a
create or a delete escapes `reconcile_once` and aborts the whole pass.

Observed in production: GHA run 30465914822 (2026-07-29T15:34:55Z) died with
`ERROR: reconcile_once raised: HTTP Error 404: Not Found` after applying 1 of 30
planned mutations — 29 valid mutations silently skipped. That directly violates the
acceptance contract of closed bug e534-5154-2401-40fb ("the reconciler applies every
other (valid) mutation in the pass ... no valid mutation is silently skipped").

These tests drive the real sequencer `applier._apply_one`, not the handler in
isolation, because the contract under test is "the BATCH continues" — the property
e534 pinned and the one the production failure broke.
"""

from __future__ import annotations

import urllib.error
from pathlib import Path
from typing import Any

import pytest

from rebar_reconciler import applier, apply_handlers


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://example.invalid/rest/api/3/issue/REB-1",
        code=code,
        msg="Not Found" if code == 404 else "Server Error",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )


@pytest.fixture
def ctx(tmp_path: Path) -> apply_handlers.BatchApplyContext:
    return apply_handlers.BatchApplyContext(
        client=object(), repo_root=tmp_path, pass_id="test-pass"
    )


def _run_batch(mutations: list[dict], ctx: apply_handlers.BatchApplyContext) -> list[dict]:
    """Drive the real sequencer over a batch, as `_apply_batch` does."""
    outcomes: list[dict] = []
    for mutation in mutations:
        applier._apply_one(mutation, ctx, outcomes)
    return outcomes


@pytest.mark.parametrize(
    ("action", "leaf"),
    [("create", "create_one"), ("delete", "delete_one")],
)
def test_404_on_create_or_delete_is_isolated_and_batch_continues(
    action: str,
    leaf: str,
    ctx: apply_handlers.BatchApplyContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 404 from create/delete records a per-mutation failure; the batch goes on.

    `update` already behaves this way; create and delete must match, because
    `_apply_one` re-raises HTTPError on the assumption that the handler soft-failed
    it. The second mutation is the load-bearing assertion: e534's contract is that a
    single bad mutation does not skip the valid ones behind it.
    """

    def _raise_404(*args: Any, **kwargs: Any) -> Any:
        raise _http_error(404)

    monkeypatch.setattr(apply_handlers, leaf, _raise_404)
    # The follower must reach its leaf for the batch-continues claim to mean anything.
    reached: list[str] = []

    def _ok_update(*args: Any, **kwargs: Any) -> Any:
        reached.append("update")
        return {"status": "ok"}

    monkeypatch.setattr(apply_handlers, "update_one", _ok_update)

    failing = {"action": action, "key": "REB-404", "local_id": "aaaa-bbbb-cccc-dddd"}
    follower = {"action": "update", "key": "REB-OK", "local_id": "1111-2222-3333-4444"}

    outcomes = _run_batch([failing, follower], ctx)

    assert len(outcomes) == 2, (
        f"a 404 from {leaf} must not abort the batch — both mutations must record "
        f"an outcome, got {len(outcomes)}: {outcomes}"
    )
    assert reached == ["update"], (
        "the mutation AFTER the 404 must still dispatch (e534: no valid mutation is "
        f"silently skipped); reached={reached}"
    )
    assert "404" in str(outcomes[0].get("error") or ""), (
        "the 404 mutation must record a per-mutation error naming the 404, got "
        f"{outcomes[0].get('error')!r}"
    )


@pytest.mark.parametrize(
    ("action", "leaf"),
    [("create", "create_one"), ("delete", "delete_one")],
)
def test_non_404_http_error_still_propagates(
    action: str,
    leaf: str,
    ctx: apply_handlers.BatchApplyContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contrast case: only 404 is soft-failed; 5xx keeps the fail-fast contract.

    Guards against over-broadening the fix into "swallow every HTTPError", which
    would hide real outages. `handle_update` draws the line at `exc.code != 404`
    and the other handlers must draw it in the same place.
    """

    def _raise_500(*args: Any, **kwargs: Any) -> Any:
        raise _http_error(500)

    monkeypatch.setattr(apply_handlers, leaf, _raise_500)
    mutation = {"action": action, "key": "REB-500", "local_id": "dead-beef-dead-beef"}

    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _run_batch([mutation], ctx)
    assert excinfo.value.code == 500
