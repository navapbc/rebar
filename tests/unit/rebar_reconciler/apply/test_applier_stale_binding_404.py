"""Bug tan-coin-atone (6614-43cd-3a48-4f63): HTTPError 404 on a stale-binding
update mutation must soft-fail, not abort the whole pass.

Production cron evidence (GHA run 27023829257, 2026-06-05, first run post-chunk-12):

    RECON: batch_outcome action=update key=DIG-5305 ...
    ERROR: reconcile_once raised: HTTP Error 404: Not Found
    ##[error]Process completed with exit code 1.

DIG-5305 is a deleted probe ticket with a stale binding (1e08 class). An
outbound 'update' mutation against it routes status/priority through REST
sub-calls (transition_issue / update_priority) that raise a RAW
``urllib.error.HTTPError`` on non-2xx. The update_one try/except only catches
``JiraAPIError`` (illegal-transition 400), so a raw HTTPError 404 escapes
update_one -> _apply_batch -> reconcile_once -> fatal exit 1.

Fix: per-mutation catch of ``urllib.error.HTTPError`` with ``code == 404`` in
the ``_apply_batch`` update branch — record the error in the batch outcome
(failed count), log a WARNING naming the key + "stale binding (1e08)", and
continue. Only 404 is softened; other HTTP errors keep current behavior.
Mirrors the adjacent AssigneeNotFoundError soft-fail handler.

RED test: a batch with one good update + one update whose target raises
``urllib.error.HTTPError(404)``. Pre-fix, ``apply()`` raises and the good one
never runs. Post-fix, the good mutation applies, ``apply()`` returns without
raising, and a 404 batch-outcome error record lands in the manifest.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
import urllib.error
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_DIR = REPO_ROOT / "src" / "rebar" / "_engine"
APPLIER_PATH = SCRIPTS_DIR / "rebar_reconciler" / "applier.py"
ACLI_PATH = SCRIPTS_DIR / "acli-integration.py"

# acli-integration.py imports ``from rebar_reconciler.adf import text_to_adf``;
# mirror the package-bootstrap pattern from test_applier_assignee_soft_fail.py
# so the loader chain resolves under any cwd.
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
_ADF_PATH = SCRIPTS_DIR / "rebar_reconciler" / "adf.py"
if "rebar_reconciler" not in sys.modules:
    import types as _types

    _dr = _types.ModuleType("rebar_reconciler")
    _dr.__path__ = [str(SCRIPTS_DIR / "rebar_reconciler")]
    sys.modules["rebar_reconciler"] = _dr
if "rebar_reconciler.adf" not in sys.modules:
    _adf_spec = importlib.util.spec_from_file_location("rebar_reconciler.adf", _ADF_PATH)
    _adf_mod = importlib.util.module_from_spec(_adf_spec)
    sys.modules["rebar_reconciler.adf"] = _adf_mod
    _adf_spec.loader.exec_module(_adf_mod)  # type: ignore[union-attr]
# acli-integration.py also imports ``from rebar_reconciler.comment_limits import ...``
# (bug 6afc-20ee-84e5-4dd5). Bootstrap it explicitly alongside adf so the loader
# chain resolves regardless of which sibling test first registered the
# ``rebar_reconciler`` namespace stub.
_CL_PATH = SCRIPTS_DIR / "rebar_reconciler" / "comment_limits.py"
if "rebar_reconciler.comment_limits" not in sys.modules:
    _cl_spec = importlib.util.spec_from_file_location(
        "rebar_reconciler.comment_limits", _CL_PATH
    )
    _cl_mod = importlib.util.module_from_spec(_cl_spec)
    sys.modules["rebar_reconciler.comment_limits"] = _cl_mod
    _cl_spec.loader.exec_module(_cl_mod)  # type: ignore[union-attr]


def _load_module(name: str, path: Path) -> ModuleType:
    """Load ``path`` as a fresh module under ``name``.

    Unlike ``sys.modules.setdefault``, this always executes a fresh module and
    registers it under a file-unique key. The module is registered before
    ``exec_module`` so any self-referential imports resolve. Callers are
    responsible for popping ``name`` from ``sys.modules`` on teardown (see the
    yield fixtures below) so the cache does not leak across test files or
    review cycles (the 4cc1 leakage class).
    """
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def applier_mod() -> Iterator[ModuleType]:
    name = "applier_stale_binding_404"
    mod = _load_module(name, APPLIER_PATH)
    try:
        yield mod
    finally:
        sys.modules.pop(name, None)


@pytest.fixture(scope="module")
def acli_mod() -> Iterator[ModuleType]:
    name = "acli_stale_binding_404"
    mod = _load_module(name, ACLI_PATH)
    try:
        yield mod
    finally:
        sys.modules.pop(name, None)


def _make_fake_acli(acli_mod: ModuleType, client: MagicMock) -> MagicMock:
    """Fake acli module whose AcliClient() returns ``client`` and whose
    exception classes are the REAL ones, so the applier's
    ``except acli.AssigneeNotFoundError`` resolves to a real BaseException
    subclass rather than a MagicMock.
    """
    fake = MagicMock()
    fake.AcliClient.return_value = client
    fake.AssigneeNotFoundError = acli_mod.AssigneeNotFoundError
    return fake


def _make_404() -> urllib.error.HTTPError:
    # Mirrors the production str() form "HTTP Error 404: Not Found".
    return urllib.error.HTTPError(
        url="https://example.atlassian.net/rest/api/3/issue/DIG-5305/transitions",
        code=404,
        msg="Not Found",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )


def _read_manifest_outcomes(repo_root: Path, pass_id: str) -> list[dict]:
    manifest_path = (
        repo_root / "bridge_state" / "snapshots" / f"{pass_id}.manifest.json"
    )
    if not manifest_path.is_file():
        return []
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    return data.get("mutations", [])


def test_http_404_on_update_soft_fails_batch_continues(
    applier_mod: ModuleType,
    acli_mod: ModuleType,
    tmp_path: Path,
) -> None:
    """The exact production scenario: 1 valid update + 1 stale-binding update
    whose target is gone (HTTP 404).

    Pre-fix: the raw urllib.error.HTTPError(404) from client.update_issue
    propagates through applier.apply, killing the whole pass — the valid
    mutation may never apply and the pass exits 1.

    Post-fix: the 404 is caught per-mutation, recorded in the batch outcome,
    a WARNING is logged, and the batch continues. The valid mutation applies;
    apply() returns without raising.
    """
    pass_id = f"test-pass-404-{int(time.time())}"

    good_mutation = {
        "direction": "outbound",
        "action": "update",
        "key": "DIG-4275",
        "fields": {"summary": "still works"},
        "local_id": "good-local-id",
    }
    stale_mutation = {
        "direction": "outbound",
        "action": "update",
        "key": "DIG-5305",  # deleted probe ticket, stale binding (1e08)
        "fields": {"status": "in_progress"},
        "local_id": "stale-local-id",
    }

    fake_client = MagicMock()

    def _update_issue_side_effect(issue_key, **kwargs):
        if issue_key == "DIG-5305":
            raise _make_404()
        return {"key": issue_key, "ok": True}

    fake_client.update_issue.side_effect = _update_issue_side_effect
    fake_acli_mod = _make_fake_acli(acli_mod, fake_client)

    with patch.object(applier_mod, "_load_acli", return_value=fake_acli_mod):
        try:
            applier_mod.apply(
                [good_mutation, stale_mutation],
                pass_id,
                repo_root=tmp_path,
            )
        except urllib.error.HTTPError as exc:
            pytest.fail(
                f"applier.apply propagated HTTPError 404 instead of "
                f"soft-failing the batch: {exc!r}"
            )

    # Both mutations were attempted; the good one DID run.
    update_calls = list(fake_client.update_issue.call_args_list)
    assert len(update_calls) == 2, (
        f"both mutations should have been attempted; got {len(update_calls)} calls"
    )

    # The 404 is recorded in the batch outcome for DIG-5305.
    outcomes = _read_manifest_outcomes(tmp_path, pass_id)
    stale_outcomes = [o for o in outcomes if o.get("key") == "DIG-5305"]
    assert stale_outcomes, f"expected an outcome for DIG-5305; got {outcomes}"
    assert stale_outcomes[0].get("error"), (
        f"DIG-5305 outcome must record the 404 error; got {stale_outcomes[0]}"
    )
    assert "404" in str(stale_outcomes[0]["error"]), (
        f"recorded error should name the 404; got {stale_outcomes[0]['error']!r}"
    )

    # The good mutation succeeded: assert BOTH the absence of an error AND a
    # positive success marker (the recorded ``result`` payload). Asserting only
    # the error's absence would pass vacuously if a regression nulled the
    # result, so pin the success indicator explicitly.
    good_outcomes = [o for o in outcomes if o.get("key") == "DIG-4275"]
    assert good_outcomes and not good_outcomes[0].get("error"), (
        f"good mutation should have no error; got {good_outcomes}"
    )
    assert good_outcomes[0].get("result") == {"key": "DIG-4275", "ok": True}, (
        "good mutation outcome must record the positive update_issue result, "
        f"not just the absence of an error; got {good_outcomes[0]}"
    )


def test_non_404_http_error_still_propagates(
    applier_mod: ModuleType,
    acli_mod: ModuleType,
    tmp_path: Path,
) -> None:
    """Only 404 is softened. A 500 (or any non-404 HTTPError) must keep the
    current behavior and propagate — we do not blanket-catch HTTP errors.
    """
    pass_id = f"test-pass-500-{int(time.time())}"
    mutation = {
        "direction": "outbound",
        "action": "update",
        "key": "DIG-9999",
        "fields": {"status": "in_progress"},
        "local_id": "five-hundred-id",
    }
    fake_client = MagicMock()
    fake_client.update_issue.side_effect = urllib.error.HTTPError(
        url="https://example.atlassian.net/rest/api/3/issue/DIG-9999/transitions",
        code=500,
        msg="Internal Server Error",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )
    fake_acli_mod = _make_fake_acli(acli_mod, fake_client)

    with patch.object(applier_mod, "_load_acli", return_value=fake_acli_mod):
        with pytest.raises(urllib.error.HTTPError):
            applier_mod.apply([mutation], pass_id, repo_root=tmp_path)
