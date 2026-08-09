"""Held-out edge oracle for canonical preview and legacy route continuity."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rebar_reconciler import applier, mode, request


def _mutations() -> list[dict]:
    return [
        {
            "direction": "inbound",
            "action": "update",
            "key": "DIG-7",
            "local_id": "local-7",
            "fields": {"description": "remote body", "status": "closed"},
        },
        {
            "direction": "outbound",
            "action": "update",
            "key": "DIG-8",
            "local_id": "local-8",
            "fields": {"summary": "local title"},
        },
    ]


def test_preview_manifest_carries_exact_same_pass_field_drift(tmp_path: Path) -> None:
    rendered = applier.apply(
        _mutations(),
        pass_id="canonical-preview",
        repo_root=tmp_path,
        mode=mode.Mode.DRY_RUN,
        persist=False,
        route="preview",
    )

    assert rendered["route"] == "preview"
    assert rendered["mode"] == "dry-run"
    assert rendered["applied_count"] == 0
    assert rendered["failed_count"] == 0
    assert rendered["deferred_count"] == 2
    assert rendered["plan"] == [
        {
            "direction": "inbound",
            "action": "update",
            "target": "DIG-7",
            "local_id": "local-7",
            "fields": {"description": "remote body", "status": "closed"},
        },
        {
            "direction": "outbound",
            "action": "update",
            "target": "DIG-8",
            "local_id": "local-8",
            "fields": {"summary": "local title"},
        },
    ]


@pytest.mark.parametrize("argv", [["--dry-run"], ["--dry-run-en"]])
def test_prefix_abbreviations_are_invocation_errors(argv: list[str]) -> None:
    with pytest.raises(request.RequestError, match="unrecognized arguments"):
        request.normalize_request(argv, mode)


def test_documented_legacy_dry_run_spelling_remains_valid() -> None:
    normalized = request.normalize_request(["--mode", "dry-run"], mode)
    assert normalized.route == "legacy"
    assert normalized.target_mode is mode.Mode.DRY_RUN


def test_legacy_uncapped_live_keeps_tally_and_removes_manifest(tmp_path: Path, monkeypatch) -> None:
    legacy_batch = tmp_path / "bridge_state" / "snapshots" / "legacy.manifest.json"
    legacy_batch.parent.mkdir(parents=True)

    def fake_batch(*_args, **_kwargs) -> Path:
        legacy_batch.write_text(
            json.dumps(
                {
                    "mutations": [
                        {"key": "DIG-8", "action": "update"},
                        {"key": "DIG-9", "action": "update", "error": "rejected"},
                    ]
                }
            )
        )
        return legacy_batch

    monkeypatch.setattr(applier, "_apply_batch", fake_batch)
    result = applier.apply(
        _mutations(),
        pass_id="legacy-live",
        repo_root=tmp_path,
        mode=mode.Mode.LIVE,
        route="legacy",
    )

    assert result == {"applied_count": 1, "failed_count": 1}
    assert not legacy_batch.exists()


def test_legacy_bootstrap_route_keeps_asymmetric_manifest_shape(tmp_path: Path) -> None:
    rendered = applier.apply(
        _mutations(),
        pass_id="legacy-bootstrap",
        repo_root=tmp_path,
        mode=mode.Mode.BOOTSTRAP_STRICT,
        persist=False,
        route="legacy",
    )

    assert set(rendered) >= {"outbound", "inbound", "applied_count", "deferred_count"}
    assert "route" not in rendered
    assert "plan" not in rendered
