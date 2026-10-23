"""A Jira 404 on create, update, or delete is a per-mutation failure; later mutations still
run. Other HTTP failures remain pass-fatal."""

from __future__ import annotations

import urllib.error
from pathlib import Path
from typing import Any

import pytest

from rebar.reconciler import apply_handlers, batch_dispatch


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
    """Drive the real per-mutation backstop over a batch, as the reroutes do."""
    outcomes: list[dict] = []
    for mutation in mutations:
        outcomes.append(batch_dispatch._dispatch_with_backstop(mutation, ctx).outcome)
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
    """Create and delete 404s record isolated failures and permit the next mutation to run,
    matching update."""

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
    """Only 404 is isolated; other HTTP errors propagate so outages remain visible."""

    def _raise_500(*args: Any, **kwargs: Any) -> Any:
        raise _http_error(500)

    monkeypatch.setattr(apply_handlers, leaf, _raise_500)
    mutation = {"action": action, "key": "REB-500", "local_id": "dead-beef-dead-beef"}

    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _run_batch([mutation], ctx)
    assert excinfo.value.code == 500
