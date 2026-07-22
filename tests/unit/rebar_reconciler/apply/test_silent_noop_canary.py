"""Story E (2359) — outbound sub-op telemetry + the silent-no-op canary.

Both link bugs were invisible in telemetry: the batch outcome reported
``error=None`` / "applied" but never the per-sub-op counts, so a link/comment/label
that silently no-ops (computed but never applied) left the pass green. This pins:

  * **Telemetry** — every outbound UPDATE outcome surfaces ``links_applied`` /
    ``comments_applied`` / ``labels_applied`` (parity with apply_inbound).
  * **Canary** — a kind with sub-ops COMPUTED (post-dedup) but ZERO applied is the
    bug-3f04 failure mode; it is flagged on the outcome (``silent_noop``) and
    WARNed. The test reproduces what the link drop would have triggered.
  * **Warn-first rollout** — default is warn-only (no ``error``); the
    ``REBAR_RECONCILER_FAIL_SILENT_NOOP=1`` flag promotes it to a hard per-mutation
    failure. Promotion and reversion are a pure flag flip (asserted both ways).
  * **No false positive** — an idempotent re-sync whose links are all deduped has
    ``links_computed == 0`` and does NOT fire the canary.

Asserts the count invariant + outcome flags, not specific log strings.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_DIR = REPO_ROOT / "src" / "rebar" / "_engine"
APPLIER_PATH = SCRIPTS_DIR / "rebar_reconciler" / "applier.py"
ACLI_PATH = SCRIPTS_DIR / "rebar_reconciler" / "acli.py"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if "rebar_reconciler" not in sys.modules:
    import types as _types

    _dr = _types.ModuleType("rebar_reconciler")
    _dr.__path__ = [str(SCRIPTS_DIR / "rebar_reconciler")]
    sys.modules["rebar_reconciler"] = _dr
for _sib in ("adf", "comment_limits"):
    _key = f"rebar_reconciler.{_sib}"
    if _key not in sys.modules:
        _spec = importlib.util.spec_from_file_location(
            _key, SCRIPTS_DIR / "rebar_reconciler" / f"{_sib}.py"
        )
        _mod = importlib.util.module_from_spec(_spec)
        sys.modules[_key] = _mod
        _spec.loader.exec_module(_mod)  # type: ignore[union-attr]


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def applier_mod() -> Iterator[ModuleType]:
    name = "applier_silent_noop_canary"
    mod = _load_module(name, APPLIER_PATH)
    try:
        yield mod
    finally:
        sys.modules.pop(name, None)


@pytest.fixture(scope="module")
def acli_mod() -> Iterator[ModuleType]:
    name = "acli_silent_noop_canary"
    mod = _load_module(name, ACLI_PATH)
    try:
        yield mod
    finally:
        sys.modules.pop(name, None)


def _make_fake_acli(acli_mod: ModuleType, client: MagicMock) -> MagicMock:
    # S4: _load_acli returns the transport directly.
    return client


def _client(*, existing_links=None) -> MagicMock:
    c = MagicMock()
    c.update_issue.return_value = {"key": "DIG-1", "ok": True}
    c.get_issue_links.return_value = list(existing_links or [])
    return c


def _link_update(links, *, key="DIG-1") -> dict:
    return {
        "direction": "outbound",
        "action": "update",
        "key": key,
        "fields": {"summary": "scalar that succeeds"},
        "links": links,
        "local_id": "loc-1",
    }


def _apply(applier_mod, acli_mod, mutation, tmp_path, pass_id) -> dict:
    fake = _make_fake_acli(acli_mod, mutation["_client"])
    with patch.object(applier_mod, "_load_acli", return_value=fake):
        applier_mod.apply(
            [{k: v for k, v in mutation.items() if k != "_client"}], pass_id, repo_root=tmp_path
        )
    manifest = tmp_path / "bridge_state" / "snapshots" / f"{pass_id}.manifest.json"
    outcomes = json.loads(manifest.read_text()).get("mutations", []) if manifest.is_file() else []
    return next((o for o in outcomes if o.get("key") == mutation["key"]), {})


def test_subop_telemetry_surfaced_on_success(applier_mod, acli_mod, tmp_path) -> None:
    """A successful link add surfaces links_applied on the outcome."""
    mut = _link_update([{"action": "add", "type": "Blocks", "to_key": "DIG-2"}])
    mut["_client"] = _client(existing_links=[])
    outcome = _apply(applier_mod, acli_mod, mut, tmp_path, f"telem-{time.time_ns()}")
    assert outcome.get("links_applied") == 1
    assert "comments_applied" in outcome and "labels_applied" in outcome
    assert "silent_noop" not in outcome


def test_silent_noop_canary_warns_by_default(applier_mod, acli_mod, tmp_path, caplog) -> None:
    """links computed but applied==0 (write fails) → silent_noop flagged + WARNED,
    but NO error in the default warn-first mode."""
    client = _client(existing_links=[])
    client.set_relationship.side_effect = RuntimeError("write boom")
    mut = _link_update([{"action": "add", "type": "Blocks", "to_key": "DIG-2"}])
    mut["_client"] = client
    with caplog.at_level("WARNING"):
        outcome = _apply(applier_mod, acli_mod, mut, tmp_path, f"warn-{time.time_ns()}")
    assert outcome.get("links_applied") == 0
    assert outcome.get("silent_noop") == ["links"]
    assert not outcome.get("error"), "warn-first default must NOT hard-fail"
    assert any("silent no-op" in r.message for r in caplog.records)


def test_dedup_does_not_trip_canary(applier_mod, acli_mod, tmp_path) -> None:
    """An idempotent re-sync (link already present) is computed==0 → no canary."""
    existing = [{"type": {"name": "Blocks"}, "outwardIssue": {"key": "DIG-2"}}]
    client = _client(existing_links=existing)
    mut = _link_update([{"action": "add", "type": "Blocks", "to_key": "DIG-2"}])
    mut["_client"] = client
    outcome = _apply(applier_mod, acli_mod, mut, tmp_path, f"dedup-{time.time_ns()}")
    assert client.set_relationship.call_count == 0, "deduped link must not be re-added"
    assert outcome.get("links_applied") == 0
    assert "silent_noop" not in outcome, "a fully-deduped re-sync must NOT fire the canary"
    assert not outcome.get("error")


def test_hard_fail_flag_promotes_and_reverts(applier_mod, acli_mod, tmp_path, monkeypatch) -> None:
    """The fail-loud invariant is a pure flag flip: REBAR_RECONCILER_FAIL_SILENT_NOOP=1
    promotes the silent no-op to a per-mutation error; unset reverts to warn-only."""

    def _run() -> dict:
        client = _client(existing_links=[])
        client.set_relationship.side_effect = RuntimeError("write boom")
        mut = _link_update([{"action": "add", "type": "Blocks", "to_key": "DIG-2"}])
        mut["_client"] = client
        return _apply(applier_mod, acli_mod, mut, tmp_path, f"flag-{time.time_ns()}")

    # Promoted: hard-fail records a per-mutation error (counts as a failure).
    monkeypatch.setenv("REBAR_RECONCILER_FAIL_SILENT_NOOP", "1")
    hard = _run()
    assert hard.get("silent_noop") == ["links"]
    assert "silent-noop" in (hard.get("error") or ""), "flag on must hard-fail"

    # Reverted: same code, flag off → warn-only, no error.
    monkeypatch.delenv("REBAR_RECONCILER_FAIL_SILENT_NOOP", raising=False)
    warn = _run()
    assert warn.get("silent_noop") == ["links"]
    assert not warn.get("error"), "flag off must revert to warn-only"
