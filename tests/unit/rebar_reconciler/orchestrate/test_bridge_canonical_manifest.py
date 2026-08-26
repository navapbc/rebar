"""Happy-path contract for canonical bridge preview/sync presentation."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from rebar_reconciler import __main__ as reconciler_main
from rebar_reconciler import applier, manifest_renderer
from rebar_reconciler.mutation import (
    Mutation,
    MutationAction,
    MutationDirection,
    serialize_manifest,
)
from rebar_reconciler.operation_outcome import (
    DelaySource,
    Disposition,
    FailureScope,
    OperationOutcome,
    ReplaySafety,
    bound_diagnostics,
)

# REB-3115 S5 T2 AC1 — the sealed versioned outcomes are ADDITIVE: the canonical
# mutation array, its bytes, and its hash are UNCHANGED. The canonical hash lives in
# ``mutation.serialize_manifest`` (sorted array of
# ``{direction, action, target, payload, provenance}``) and the new ``render_pass_outcomes``
# section never participates in it.
_CANONICAL_MUTATIONS = (
    (
        MutationDirection.outbound,
        MutationAction.update,
        "DIG-41",
        {"summary": "s", "priority": "High"},
        {"local_id": "local-41"},
    ),
    (
        MutationDirection.outbound,
        MutationAction.create,
        "local-42",
        {"summary": "new"},
        {"local_id": "local-42"},
    ),
    (
        MutationDirection.inbound,
        MutationAction.clean_label,
        "DIG-7",
        {"label": "x"},
        {"local_id": "local-7"},
    ),
)
_CANONICAL_MUTATION_HASH = "ad5bfa390730c7aa763d8187c8dde666d4429cead32f8228a0e53a7e6699f83d"


def _canonical_mutation_set() -> list[Mutation]:
    return [Mutation(d, a, t, dict(p), dict(prov)) for d, a, t, p, prov in _CANONICAL_MUTATIONS]


def test_canonical_mutation_hash_is_a_byte_stable_golden() -> None:
    """AC1: the canonical mutation-array hash over a fixed mutation set is a pinned golden —
    the sealed additive outcomes section must never perturb these bytes."""
    _text, digest = serialize_manifest(_canonical_mutation_set())
    assert digest == _CANONICAL_MUTATION_HASH


def test_rendering_pass_outcomes_leaves_the_canonical_mutation_hash_identical() -> None:
    """AC1: rendering the additive versioned outcomes section is a pure sibling projection —
    computing it before/after does not change the canonical mutation-array bytes or hash."""
    muts = _canonical_mutation_set()
    _text_before, digest_before = serialize_manifest(muts)

    outcome = OperationOutcome(
        logical_id="local-41",
        disposition=Disposition.applied,
        failure_scope=FailureScope.none,
        replay_safety=ReplaySafety.safe,
        invocation_count=1,
        request_count=1,
        delay_source=DelaySource.none,
        provider_delay_ms=None,
        retry_not_before=None,
        diagnostics=bound_diagnostics([]),
    )
    section = manifest_renderer.render_pass_outcomes([outcome])
    assert section["outcomes"][0]["logical_id"] == "local-41"

    _text_after, digest_after = serialize_manifest(_canonical_mutation_set())
    assert digest_after == digest_before == _CANONICAL_MUTATION_HASH


def test_preview_bypasses_advisory_lock_and_forwards_existing_route(
    tmp_path: Path, monkeypatch
) -> None:
    """Canonical preview runs the ordinary pass without loading lock machinery."""
    real_loader = reconciler_main._load_sibling_keyed

    def load_without_advisory(key: str, relpath: str):
        if key == reconciler_main._ADVISORY_LOCK_KEY:
            raise AssertionError("canonical preview must not load advisory-lock machinery")
        return real_loader(key, relpath)

    run_pass = MagicMock(return_value=0)
    monkeypatch.setattr(reconciler_main, "_load_sibling_keyed", load_without_advisory)
    monkeypatch.setattr(reconciler_main, "run_pass", run_pass)

    assert reconciler_main.main(["preview", "--repo-root", str(tmp_path)]) == 0
    kwargs = run_pass.call_args.kwargs
    assert kwargs["route"] == "preview"
    assert kwargs["target_mode"].value == "dry-run"


def test_canonical_uncapped_sync_retains_field_comparable_manifest(
    tmp_path: Path, monkeypatch
) -> None:
    """Canonical LIVE keeps the same auditable entry shape preview exposes."""
    mode_mod = applier._load_mode_module()
    mutations = [
        {
            "direction": "outbound",
            "action": "update",
            "key": "DIG-41",
            "local_id": "local-41",
            "fields": {"summary": "new summary", "priority": "High"},
        },
        {
            "direction": "outbound",
            "action": "create",
            "key": "local-42",
            "local_id": "local-42",
            "fields": {"summary": "new ticket"},
        },
    ]
    legacy_batch = tmp_path / "bridge_state" / "snapshots" / "sync.manifest.json"
    legacy_batch.parent.mkdir(parents=True)

    def fake_batch(*_args, **_kwargs) -> Path:
        legacy_batch.write_text(
            json.dumps(
                {
                    "mutations": [
                        {"key": "DIG-41", "action": "update"},
                        {"key": "local-42", "action": "create"},
                    ]
                }
            )
        )
        return legacy_batch

    monkeypatch.setattr(applier, "_apply_batch", fake_batch)

    manifest_path = applier.apply(
        mutations,
        pass_id="canonical-sync",
        repo_root=tmp_path,
        mode=mode_mod.Mode.LIVE,
        route="sync",
    )

    assert isinstance(manifest_path, Path)
    payload = json.loads(manifest_path.read_text())
    assert payload == {
        "pass_id": "canonical-sync",
        "mode": "live",
        "route": "sync",
        "applied_count": 2,
        "failed_count": 0,
        "deferred_count": 0,
        "plan": [
            {
                "direction": "outbound",
                "action": "create",
                "target": "local-42",
                "local_id": "local-42",
                "fields": {"summary": "new ticket"},
            },
            {
                "direction": "outbound",
                "action": "update",
                "target": "DIG-41",
                "local_id": "local-41",
                "fields": {"summary": "new summary", "priority": "High"},
            },
        ],
        "deferred": [],
    }


def test_canonical_sync_manifest_reports_batch_failures_exactly(
    tmp_path: Path, monkeypatch
) -> None:
    """Canonical tally reflects the outcomes produced by the real batch seam."""
    mode_mod = applier._load_mode_module()
    mutations = [
        {
            "direction": "outbound",
            "action": "update",
            "key": "DIG-42",
            "local_id": "local-42",
            "fields": {"summary": "accepted"},
        },
        {
            "direction": "outbound",
            "action": "update",
            "key": "DIG-41",
            "local_id": "local-41",
            "fields": {"summary": "rejected"},
        },
    ]
    batch_path = tmp_path / "bridge_state" / "snapshots" / "sync.manifest.json"
    batch_path.parent.mkdir(parents=True)

    def fake_batch(*_args, **_kwargs) -> Path:
        batch_path.write_text(
            json.dumps(
                {
                    "mutations": [
                        {"key": "DIG-41", "action": "update", "error": "rejected"},
                        {"key": "DIG-42", "action": "update"},
                    ]
                }
            )
        )
        return batch_path

    monkeypatch.setattr(applier, "_apply_batch", fake_batch)

    manifest_path = applier.apply(
        mutations,
        pass_id="canonical-sync-failure",
        repo_root=tmp_path,
        mode=mode_mod.Mode.LIVE,
        route="sync",
    )

    payload = json.loads(manifest_path.read_text())
    assert payload["applied_count"] == 1
    assert payload["failed_count"] == 1
    assert [entry["target"] for entry in payload["plan"]] == ["DIG-41", "DIG-42"]


def test_canonical_sync_tally_excludes_suppressed_inbound_mutations(
    tmp_path: Path, monkeypatch
) -> None:
    mode_mod = applier._load_mode_module()
    mutation_mod = applier._load_mutation_module()
    mutations = [
        mutation_mod.Mutation(
            mutation_mod.MutationDirection.inbound,
            mutation_mod.MutationAction.update,
            "DIG-1",
            {"fields": {"summary": "first"}},
            {"local_id": "local-1"},
        ),
        mutation_mod.Mutation(
            mutation_mod.MutationDirection.inbound,
            mutation_mod.MutationAction.update,
            "DIG-2",
            {"fields": {"summary": "suppressed"}},
            {"local_id": "local-2"},
        ),
    ]
    manifest_path = tmp_path / "bridge_state" / "snapshots" / "sync.manifest.json"
    manifest_path.parent.mkdir(parents=True)

    def fake_typed(mutation, **_kwargs):
        follow_on = (
            {"kind": "suppress_pair", "local_id": "local-2", "jira_key": "DIG-2"}
            if mutation.target == "DIG-1"
            else None
        )
        return SimpleNamespace(payload={"follow_on": follow_on} if follow_on else {})

    def fake_batch(*_args, **_kwargs) -> Path:
        manifest_path.write_text(json.dumps({"mutations": []}))
        return manifest_path

    monkeypatch.setattr(applier, "_apply_typed", fake_typed)
    monkeypatch.setattr(applier, "_apply_batch", fake_batch)

    result_path = applier.apply(
        mutations,
        pass_id="canonical-sync-suppression",
        repo_root=tmp_path,
        mode=mode_mod.Mode.LIVE,
        route="sync",
        client=object(),
    )

    payload = json.loads(result_path.read_text())
    assert payload["applied_count"] == 1
    assert payload["failed_count"] == 0
    assert [entry["target"] for entry in payload["plan"]] == ["DIG-1", "DIG-2"]


def test_capped_canonical_sync_reports_plan_tally_and_deferred(tmp_path: Path, monkeypatch) -> None:
    mode_mod = applier._load_mode_module()
    mutations = [
        {
            "direction": "outbound",
            "action": "update",
            "key": f"DIG-{number}",
            "local_id": f"local-{number}",
            "fields": {"summary": str(number)},
        }
        for number in (3, 1, 2)
    ]
    manifest_path = tmp_path / "bridge_state" / "snapshots" / "sync.manifest.json"
    manifest_path.parent.mkdir(parents=True)

    def fake_batch(*_args, **_kwargs) -> Path:
        manifest_path.write_text(
            json.dumps(
                {
                    "mutations": [
                        {"key": "DIG-1", "action": "update", "error": "rejected"},
                        {"key": "DIG-2", "action": "update"},
                    ]
                }
            )
        )
        return manifest_path

    monkeypatch.setattr(applier, "_apply_batch", fake_batch)

    result_path = applier.apply(
        mutations,
        pass_id="canonical-sync-capped",
        repo_root=tmp_path,
        mode=mode_mod.Mode.LIVE,
        route="sync",
        max_changes=2,
    )

    payload = json.loads(result_path.read_text())
    assert payload["max_changes"] == 2
    assert payload["applied_count"] == 1
    assert payload["failed_count"] == 1
    assert payload["deferred_count"] == 1
    assert [entry["target"] for entry in payload["plan"]] == ["DIG-1", "DIG-2", "DIG-3"]
    assert payload["deferred"] == [{"direction": "outbound", "action": "update", "target": "DIG-3"}]


@pytest.mark.parametrize("corrupt", [False, True])
def test_canonical_sync_tally_degrades_safely_when_batch_manifest_is_unreadable(
    tmp_path: Path, monkeypatch, corrupt: bool
) -> None:
    mode_mod = applier._load_mode_module()
    manifest_path = tmp_path / "bridge_state" / "snapshots" / "sync.manifest.json"
    manifest_path.parent.mkdir(parents=True)

    def fake_batch(*_args, **_kwargs) -> Path:
        if corrupt:
            manifest_path.write_text("{not-json")
        return manifest_path

    monkeypatch.setattr(applier, "_apply_batch", fake_batch)

    result_path = applier.apply(
        [
            {
                "direction": "outbound",
                "action": "update",
                "key": "DIG-1",
                "local_id": "local-1",
                "fields": {"summary": "one"},
            }
        ],
        pass_id="canonical-sync-unreadable",
        repo_root=tmp_path,
        mode=mode_mod.Mode.LIVE,
        route="sync",
    )

    payload = json.loads(result_path.read_text())
    assert payload["applied_count"] == 1
    assert payload["failed_count"] == 0
