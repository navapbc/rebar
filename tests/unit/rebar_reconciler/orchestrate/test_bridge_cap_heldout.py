"""Held-out behavioral oracle for explicit canonical sync caps."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from rebar_reconciler import applier


def _make_mutations(n: int) -> list:
    mutation_mod = applier._load_mutation_module()
    directions = mutation_mod.MutationDirection
    actions = mutation_mod.MutationAction
    action_cycle = [actions.create, actions.update, actions.delete]
    return [
        mutation_mod.Mutation(
            direction=directions.inbound if i % 2 == 0 else directions.outbound,
            action=action_cycle[i % len(action_cycle)],
            target=f"ISSUE-{i:05d}",
            payload={"i": i},
            provenance={"src": "bridge-cap-heldout"},
        )
        for i in range(n)
    ]


@pytest.mark.parametrize(
    ("max_changes", "expected_applied", "expected_deferred"),
    [(10, 10, 92), (100, 100, 2), (200, 102, 0)],
)
def test_explicit_live_cap_retains_exact_audit_partition(
    tmp_path: Path,
    max_changes: int,
    expected_applied: int,
    expected_deferred: int,
) -> None:
    """An explicit ceiling always leaves a deterministic retained audit manifest."""
    mutations = _make_mutations(102)
    mode_mod = applier._load_mode_module()
    ordered_targets = [
        mutation.target for mutation in sorted(mutations, key=applier._mode_sort_key)
    ]
    fake_manifest = tmp_path / "bridge_state" / "snapshots" / "cap.manifest.json"
    fake_manifest.parent.mkdir(parents=True)
    fake_manifest.write_text(json.dumps({"mutations": []}))
    applied_targets: list[str] = []

    def fake_typed(mutation, **_kwargs):
        applied_targets.append(mutation.target)
        return None

    def fake_batch(batch, *_args, **_kwargs):
        applied_targets.extend(item["key"] for item in batch)
        return fake_manifest

    with (
        patch.object(applier, "_apply_typed", side_effect=fake_typed),
        patch.object(applier, "_apply_batch", side_effect=fake_batch),
    ):
        manifest_path = applier.apply(
            mutations,
            pass_id=f"cap-{max_changes}",
            repo_root=tmp_path,
            mode=mode_mod.Mode.LIVE,
            max_changes=max_changes,
        )

    assert Path(manifest_path).exists()
    payload = json.loads(Path(manifest_path).read_text())
    assert payload["mode"] == "live"
    assert payload["max_changes"] == max_changes
    assert payload["applied_count"] == expected_applied
    assert payload["deferred_count"] == expected_deferred
    assert len(payload["deferred"]) == expected_deferred
    assert sorted(applied_targets) == sorted(ordered_targets[:expected_applied])
    assert [entry["target"] for entry in payload["deferred"]] == ordered_targets[expected_applied:]
