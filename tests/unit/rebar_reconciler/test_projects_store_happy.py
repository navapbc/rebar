"""Happy-path oracle for the projects mapping store (story c927).

``resolve_project`` is the pure tri-state rule the inbound/outbound stories consume;
``load_mapping`` reads the committed ``.bridge_state/projects.json`` record beside the
binding store. This file pins the well-formed cases only — the None-sentinel edges,
the absent/malformed record behaviour, and the surface-parity live in the held-out
oracle.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SRC_DIR = Path(__file__).resolve().parents[3] / "src" / "rebar" / "_engine" / "rebar_reconciler"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _SRC_DIR / filename)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


projects_store = _load("_projects_store_happy", "projects_store.py")


def _write_record(repo_root: Path, record: dict) -> None:
    bridge_state = repo_root / ".tickets-tracker" / ".bridge_state"
    bridge_state.mkdir(parents=True, exist_ok=True)
    (bridge_state / "projects.json").write_text(json.dumps(record), encoding="utf-8")


def test_resolve_project_returns_the_set_value_verbatim(tmp_path: Path) -> None:
    """A ticket whose ``bridge_project`` names a project resolves to that key."""
    _write_record(
        tmp_path,
        {"version": 1, "legacy_default": "REB", "projects": {"REB": {"repos": ["rebar"]}}},
    )
    mapping = projects_store.load_mapping(tmp_path)

    assert projects_store.resolve_project({"bridge_project": "REB"}, mapping) == "REB"


def test_resolve_project_absent_field_falls_back_to_legacy_default(tmp_path: Path) -> None:
    """A legacy ticket (no ``bridge_project`` field) resolves to ``legacy_default``."""
    _write_record(
        tmp_path,
        {"version": 1, "legacy_default": "REB", "projects": {"REB": {"repos": ["rebar"]}}},
    )
    mapping = projects_store.load_mapping(tmp_path)

    assert projects_store.resolve_project({}, mapping) == "REB"


def test_write_record_serialized_bytes_are_byte_identical(tmp_path: Path) -> None:
    """``set_project`` persists projects.json with the exact serialized bytes contract:
    ``json.dump(record, indent=2, sort_keys=True)`` plus a single trailing newline.

    Pins the on-disk bytes across the swap from the hand-rolled tempfile+os.replace write
    to the shared ``fsutil.atomic_write`` seam — the conversion must be byte-preserving.
    """
    projects_store.set_project(tmp_path, "REB", ["rebar"])

    written = (tmp_path / ".tickets-tracker" / ".bridge_state" / "projects.json").read_bytes()

    expected = (
        b'{\n  "legacy_default": null,\n  "projects": {\n    "REB": {\n'
        b'      "repos": [\n        "rebar"\n      ]\n    }\n  },\n  "version": 1\n}\n'
    )
    assert written == expected


def test_write_record_first_write_creates_bridge_state_dir(tmp_path: Path) -> None:
    """First write must still create the ``.bridge_state`` parent dir (atomic_write itself
    requires the parent to already exist, so the mkdir is preserved through the swap)."""
    (tmp_path / ".tickets-tracker").mkdir(parents=True, exist_ok=True)

    projects_store.set_project(tmp_path, "REB", ["rebar"])

    assert (tmp_path / ".tickets-tracker" / ".bridge_state" / "projects.json").exists()
