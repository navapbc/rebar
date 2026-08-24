"""Behavior checks for the server manifest environment contract."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.scripts

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKER_PATH = REPO_ROOT / "scripts" / "check_server_manifest.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_server_manifest", CHECKER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


def _canonical_records() -> list[dict[str, object]]:
    return [
        {
            "name": item["name"],
            "description": item["description"],
            "isRequired": False,
        }
        for item in checker.MCP_ENV_VARS
    ]


def _run_with_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    records: list[dict[str, object]],
) -> int:
    manifest = tmp_path / "server.json"
    manifest.write_text(
        json.dumps({"packages": [{"environmentVariables": records}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(checker, "SERVER_JSON", manifest)
    return checker.main()


def test_committed_manifest_matches_complete_canonical_records():
    assert checker.main() == 0


def test_description_drift_names_the_affected_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    records = _canonical_records()
    records[0]["description"] = "changed description"

    assert _run_with_manifest(tmp_path, monkeypatch, records) == 1
    output = capsys.readouterr().out
    assert "REBAR_ROOT" in output
    assert "description" in output


def test_requiredness_drift_names_the_affected_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    records = _canonical_records()
    records[0]["isRequired"] = True

    assert _run_with_manifest(tmp_path, monkeypatch, records) == 1
    output = capsys.readouterr().out
    assert "REBAR_ROOT" in output
    assert "isRequired" in output


def test_duplicate_manifest_name_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    records = _canonical_records()
    records.append(dict(records[0]))

    assert _run_with_manifest(tmp_path, monkeypatch, records) == 1
    output = capsys.readouterr().out
    assert "DUPLICATE" in output
    assert "REBAR_ROOT" in output


def test_duplicate_canonical_name_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    records = _canonical_records()
    monkeypatch.setattr(
        checker,
        "MCP_ENV_VARS",
        (*checker.MCP_ENV_VARS, dict(checker.MCP_ENV_VARS[0])),
    )

    assert _run_with_manifest(tmp_path, monkeypatch, records) == 1
    output = capsys.readouterr().out
    assert "DUPLICATE" in output
    assert "REBAR_ROOT" in output


def test_missing_manifest_name_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    records = _canonical_records()[1:]

    assert _run_with_manifest(tmp_path, monkeypatch, records) == 1
    output = capsys.readouterr().out
    assert "MISSING" in output
    assert "REBAR_ROOT" in output


def test_extra_manifest_name_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    records = _canonical_records()
    records.append(
        {
            "name": "REBAR_UNKNOWN",
            "description": "Unknown setting.",
            "isRequired": False,
        }
    )

    assert _run_with_manifest(tmp_path, monkeypatch, records) == 1
    output = capsys.readouterr().out
    assert "EXTRA" in output
    assert "REBAR_UNKNOWN" in output
