"""``run_guarded_pass`` resolves selection on every route before any lock read.

Story unadhesive-caloric-sapsucker: selection tokens resolve first (preview included),
an unresolved token is an ``INVALID_INVOCATION`` before the advisory lock is touched,
and a resolved token reaches the preview pass body as a ``set``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from rebar.reconciler import guarded_pass
from rebar.reconciler.mode import Mode
from rebar.reconciler.request import ReconcileRequest


class _SelectionError(ValueError):
    pass


def _helpers(resolved: set[str] | None):
    def resolve_selection(_root, tokens):
        if resolved is None:
            raise _SelectionError("unresolved --only/--except identifier(s): " + ",".join(tokens))
        return resolved

    return SimpleNamespace(resolve_selection=resolve_selection, SelectionError=_SelectionError)


@pytest.fixture
def loads(monkeypatch: pytest.MonkeyPatch):
    """Route sibling loads through fakes and record every module key requested."""
    requested: list[str] = []
    state: dict[str, object] = {"helpers": _helpers(None)}

    def fake_load(key: str, _filename: str):
        requested.append(key)
        if key == guarded_pass._PASS_SUPPORT_KEY:
            return state["helpers"]
        raise AssertionError(f"unexpected load of {key} before selection resolved")

    monkeypatch.setattr(guarded_pass, "_load_sibling_keyed", fake_load)
    return SimpleNamespace(requested=requested, state=state)


@pytest.mark.parametrize("route", ["preview", "sync"])
def test_an_unresolved_token_is_invalid_before_any_lock_read(route: str, loads, tmp_path: Path):
    request = ReconcileRequest(
        route=route,
        repo_root=tmp_path,
        target_mode=Mode.DRY_RUN if route == "preview" else Mode.LIVE,
        selection_kind="only",
        selection_tokens=("nope",),
    )

    result = guarded_pass.run_guarded_pass(request)

    assert result.disposition is guarded_pass._Disposition.INVALID_INVOCATION
    assert result.canonical_message.startswith("ERROR: unresolved --only/--except")
    assert loads.requested == [guarded_pass._PASS_SUPPORT_KEY]


def test_a_resolved_token_reaches_the_preview_body_as_a_set(
    loads, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    loads.state["helpers"] = _helpers({"t-1"})
    body = MagicMock(return_value=guarded_pass.PassResult(guarded_pass._Disposition.CONVERGED, {}))
    monkeypatch.setattr(guarded_pass, "run_pass_result", body)
    request = ReconcileRequest(
        route="preview",
        repo_root=tmp_path,
        target_mode=Mode.DRY_RUN,
        selection_kind="only",
        selection_tokens=("t-1",),
    )

    result = guarded_pass.run_guarded_pass(request)

    assert result.disposition is guarded_pass._Disposition.CONVERGED
    assert body.call_args.kwargs["selection_ids"] == {"t-1"}
    assert body.call_args.kwargs["route"] == "preview"
