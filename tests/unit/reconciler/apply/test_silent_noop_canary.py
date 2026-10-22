"""Story E (2359) — outbound sub-op telemetry + the silent-no-op canary.

Both link bugs were invisible in telemetry: the batch outcome reported
``error=None`` / "applied" but never the per-sub-op counts, so a link/comment/label
that silently no-ops (computed but never applied) left the pass green. This pins:

  * **Telemetry** — every outbound UPDATE outcome surfaces ``links_applied`` /
    ``comments_applied`` / ``labels_applied`` (parity with apply_inbound).
  * **Canary** — a kind with sub-ops COMPUTED (post-dedup) but ZERO applied is the
    bug-3f04 failure mode; it is flagged on the outcome (``silent_noop``) and
    WARNed. The test reproduces what the link drop would have triggered.
  * **Hard-fail** — the silent no-op is ALWAYS a per-mutation failure (``error``
    starts ``silent-noop:``) with no environment set; no flag gates it.
  * **No false positive** — an idempotent re-sync whose links are all deduped has
    ``links_computed == 0`` and does NOT fire the canary.

Asserts the count invariant + outcome flags, not specific log strings.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from rebar.reconciler import scratch_retention, ticket_planner
from rebar.reconciler.mutation import Mutation, MutationAction, MutationDirection
from rebar.reconciler.mutation_payloads import OutboundUpdatePayload

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_DIR = REPO_ROOT / "src" / "rebar" / "_engine"
APPLIER_PATH = SCRIPTS_DIR.parent / "reconciler" / "applier.py"
ACLI_PATH = SCRIPTS_DIR.parent / "reconciler" / "adapters" / "jira" / "acli.py"


def _plans(mutations, *, mode=None, max_changes=None):
    """Ticket plans for the outbound ``mutations``, built as ``run_differs`` builds them.

    The batch form of ``applier.apply`` requires ``ticket_plans`` (ADR 0103 S3). Inbound
    mutations dispatch through ``_apply_typed``, so only the outbound ones are planned.
    """
    _, plans = ticket_planner.plan_pass(
        pass_id="test-pass",
        local_snapshot={},
        remote_snapshot={},
        binding_view={},
        mode=str(getattr(mode, "value", mode) or ""),
        selection={"kind": None, "ids": []},
        limits={"max_changes": max_changes},
        mutations=[
            m
            for m in mutations
            if getattr(getattr(m, "direction", None), "value", None) == "outbound"
        ],
    )
    return plans


@pytest.fixture(scope="module")
def applier_mod() -> Iterator[ModuleType]:
    name = "applier_silent_noop_canary"
    mod = importlib.import_module("rebar.reconciler.applier")
    try:
        yield mod
    finally:
        sys.modules.pop(name, None)


@pytest.fixture(scope="module")
def acli_mod() -> Iterator[ModuleType]:
    name = "acli_silent_noop_canary"
    mod = importlib.import_module("rebar.reconciler.adapters.jira.acli")
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


def _link_update(links, *, key="DIG-1") -> Mutation:
    return Mutation(
        MutationDirection.outbound,
        MutationAction.update,
        key,
        OutboundUpdatePayload(
            changed_fields={"summary": "scalar that succeeds"},
            links=tuple(links),
            local_id="loc-1",
        ),
        {},
    )


def _apply(applier_mod, acli_mod, mutation, client, tmp_path, pass_id) -> dict:
    fake = _make_fake_acli(acli_mod, client)
    with patch.object(applier_mod, "_load_acli", return_value=fake):
        applier_mod.apply([mutation], pass_id, repo_root=tmp_path, ticket_plans=_plans([mutation]))
    manifest = scratch_retention.snapshots_dir(tmp_path) / f"{pass_id}.manifest.json"
    outcomes = json.loads(manifest.read_text()).get("mutations", []) if manifest.is_file() else []
    return next((o for o in outcomes if o.get("key") == mutation.target), {})


def test_subop_telemetry_surfaced_on_success(applier_mod, acli_mod, tmp_path) -> None:
    """A successful link add surfaces links_applied on the outcome."""
    mut = _link_update([{"action": "add", "type": "Blocks", "to_key": "DIG-2"}])
    client = _client(existing_links=[])
    outcome = _apply(applier_mod, acli_mod, mut, client, tmp_path, f"telem-{time.time_ns()}")
    assert outcome.get("links_applied") == 1
    assert "comments_applied" in outcome and "labels_applied" in outcome
    assert "silent_noop" not in outcome


def test_silent_noop_canary_hard_fails_and_warns(applier_mod, acli_mod, tmp_path, caplog) -> None:
    """links computed but applied==0 (write fails) → silent_noop flagged + WARNED, AND
    the mutation hard-fails with a ``silent-noop:`` error, with no environment set."""
    client = _client(existing_links=[])
    client.set_relationship.side_effect = RuntimeError("write boom")
    mut = _link_update([{"action": "add", "type": "Blocks", "to_key": "DIG-2"}])
    with caplog.at_level("WARNING"):
        outcome = _apply(applier_mod, acli_mod, mut, client, tmp_path, f"fail-{time.time_ns()}")
    assert outcome.get("links_applied") == 0
    assert outcome.get("silent_noop") == ["links"]
    assert (outcome.get("error") or "").startswith("silent-noop:"), "must hard-fail"
    assert any("silent no-op" in r.message for r in caplog.records)


def test_dedup_does_not_trip_canary(applier_mod, acli_mod, tmp_path) -> None:
    """An idempotent re-sync (link already present) is computed==0 → no canary."""
    existing = [{"type": {"name": "Blocks"}, "outwardIssue": {"key": "DIG-2"}}]
    client = _client(existing_links=existing)
    mut = _link_update([{"action": "add", "type": "Blocks", "to_key": "DIG-2"}])
    outcome = _apply(applier_mod, acli_mod, mut, client, tmp_path, f"dedup-{time.time_ns()}")
    assert client.set_relationship.call_count == 0, "deduped link must not be re-added"
    assert outcome.get("links_applied") == 0
    assert "silent_noop" not in outcome, "a fully-deduped re-sync must NOT fire the canary"
    assert not outcome.get("error")
