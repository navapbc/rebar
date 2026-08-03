"""Held-out edge and compatibility oracle for GET rotation."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
RECONCILER_DIR = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler"


def _load(name: str, filename: str):
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, RECONCILER_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _binding_doc(stamps: dict[str, str]) -> dict:
    bindings = {
        f"loc-{suffix}": {
            "jira_key": f"DIG-{suffix}",
            "state": "confirmed",
            "last_get_pass": stamp,
        }
        for suffix, stamp in stamps.items()
    }
    return {
        "version": 2,
        "bindings": bindings,
        "reverse": {entry["jira_key"]: local_id for local_id, entry in bindings.items()},
    }


def _write_store(root: Path, *, inline: dict[str, str], sidecar: dict[str, str] | None) -> None:
    bridge = root / ".tickets-tracker" / ".bridge_state"
    bridge.mkdir(parents=True)
    (bridge / "bindings.json").write_text(json.dumps(_binding_doc(inline)))
    if sidecar is not None:
        (bridge / "get_rotation.json").write_text(
            json.dumps({"version": 1, "last_get_pass": sidecar})
        )


def _selected(outbound, store, monkeypatch: pytest.MonkeyPatch) -> set[str]:
    monkeypatch.setenv("RECONCILER_ABSENT_GET_BUDGET", "1")
    tickets = [
        {"ticket_id": f"loc-{suffix}", "status": "open", "ticket_type": "task"}
        for suffix in ("A", "B", "C")
    ]
    return outbound._compute_outbound_select_absent_gets(
        tickets,
        {},
        store,
        set(),
        set(),
        object(),
    )


def test_interleaved_old_new_state_selects_same_next_key_as_all_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An old-only inline advance must beat a stale sidecar value on the next read."""
    mixed_root = tmp_path / "mixed"
    all_new_root = tmp_path / "all-new"
    inline = {
        "A": "2026-07-01T00-00-02",
        "B": "2026-07-01T00-00-03",
        "C": "2026-07-01T00-00-01",
    }
    _write_store(
        mixed_root,
        inline=inline,
        sidecar={
            "DIG-A": "2026-07-01T00-00-02",
            "DIG-B": "2026-07-01T00-00-00",
            "DIG-C": "2026-07-01T00-00-01",
        },
    )
    _write_store(
        all_new_root,
        inline=inline,
        sidecar={
            "DIG-A": "2026-07-01T00-00-02",
            "DIG-B": "2026-07-01T00-00-03",
            "DIG-C": "2026-07-01T00-00-01",
        },
    )

    binding_store = _load("_heldout_rotation_binding_store", "binding_store.py")
    outbound = _load("_heldout_rotation_outbound", "outbound_differ.py")
    mixed = binding_store.BindingStore(mixed_root / ".tickets-tracker")
    all_new = binding_store.BindingStore(all_new_root / ".tickets-tracker")

    # Prove the mixed-version precondition: the old binary advanced only inline.
    assert mixed.last_get_pass("DIG-B") == "2026-07-01T00-00-03"
    assert _selected(outbound, mixed, monkeypatch) == {"DIG-C"}
    assert _selected(outbound, mixed, monkeypatch) == _selected(outbound, all_new, monkeypatch)

    # The next new-binary write must preserve the mixed-version guarantee by
    # advancing both representations, not merely continue the legacy behavior.
    mixed.set_last_get("DIG-C", "2026-07-01T00-00-04")
    mixed.save()
    mixed_rotation = json.loads(
        (mixed_root / ".tickets-tracker" / ".bridge_state" / "get_rotation.json").read_text()
    )
    assert mixed_rotation["last_get_pass"]["DIG-C"] == "2026-07-01T00-00-04"


def test_corrupt_sidecar_fails_open_to_legacy_rotation(tmp_path: Path) -> None:
    _write_store(
        tmp_path,
        inline={"A": "2026-07-01T00-00-02", "B": "", "C": ""},
        sidecar={},
    )
    sidecar = tmp_path / ".tickets-tracker" / ".bridge_state" / "get_rotation.json"
    sidecar.write_text("{ not-json ")

    binding_store = _load("_heldout_corrupt_rotation_binding_store", "binding_store.py")
    store = binding_store.BindingStore(tmp_path / ".tickets-tracker")
    assert store.last_get_pass("DIG-A") == "2026-07-01T00-00-02"
    store.set_last_get("DIG-A", "2026-07-01T00-00-03")
    store.save()
    repaired = json.loads(sidecar.read_text())
    assert repaired["last_get_pass"]["DIG-A"] == "2026-07-01T00-00-03"


def test_legacy_only_store_materializes_equivalent_sidecar_on_save(tmp_path: Path) -> None:
    inline = {
        "A": "2026-07-01T00-00-02",
        "B": "2026-07-01T00-00-03",
        "C": "2026-07-01T00-00-01",
    }
    _write_store(tmp_path, inline=inline, sidecar=None)
    binding_store = _load("_heldout_legacy_rotation_binding_store", "binding_store.py")
    store = binding_store.BindingStore(tmp_path / ".tickets-tracker")
    before = {key: store.last_get_pass(key) for key in ("DIG-A", "DIG-B", "DIG-C")}

    store.save()

    rotation_path = tmp_path / ".tickets-tracker" / ".bridge_state" / "get_rotation.json"
    assert rotation_path.is_file(), "the first persisted new-binary save materializes the sidecar"
    rotation = json.loads(rotation_path.read_text())
    assert rotation["last_get_pass"] == {f"DIG-{key}": value for key, value in inline.items()}
    reloaded = binding_store.BindingStore(tmp_path / ".tickets-tracker")
    assert {key: reloaded.last_get_pass(key) for key in before} == before


def test_sidecar_tempfile_failure_does_not_abort_legacy_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rotation persistence is fail-open even when its tempfile cannot be created."""
    _write_store(
        tmp_path,
        inline={"A": "2026-07-01T00-00-01", "B": "", "C": ""},
        sidecar=None,
    )
    binding_store = _load("_heldout_rotation_save_failure", "binding_store.py")
    from rebar_reconciler import get_rotation

    store = binding_store.BindingStore(tmp_path / ".tickets-tracker")
    store.set_last_get("DIG-A", "2026-07-01T00-00-02")

    real_mkstemp = get_rotation.tempfile.mkstemp

    def fail_rotation_tempfile(*args, prefix: str = "", **kwargs):
        if prefix == "get_rotation_":
            raise OSError("rotation sidecar is temporarily unwritable")
        return real_mkstemp(*args, prefix=prefix, **kwargs)

    monkeypatch.setattr(get_rotation.tempfile, "mkstemp", fail_rotation_tempfile)

    # The legacy binding save is the compatibility floor and must still succeed.
    store.save()

    bindings_path = tmp_path / ".tickets-tracker" / ".bridge_state" / "bindings.json"
    bindings = json.loads(bindings_path.read_text())
    assert bindings["bindings"]["loc-A"]["last_get_pass"] == "2026-07-01T00-00-02"


def test_rotation_paths_and_regenerable_documentation_are_pinned() -> None:
    from rebar._store import push
    from rebar_reconciler import git_adapter

    assert getattr(git_adapter, "GET_ROTATION_FILE", None) == ".bridge_state/get_rotation.json"
    assert "get_rotation.json" in (push._resolve_conflicted_pop.__doc__ or "")


def test_rotation_extraction_respects_module_size_policy() -> None:
    rotation = RECONCILER_DIR / "get_rotation.py"
    assert rotation.is_file(), "A2-1 must add the cohesive GET-rotation module"
    line_counts = {
        path.name: len(path.read_text().splitlines())
        for path in (
            RECONCILER_DIR / "binding_store.py",
            RECONCILER_DIR / "outbound_differ.py",
            rotation,
        )
    }
    assert 100 <= line_counts["get_rotation.py"] <= 500
    assert line_counts["binding_store.py"] <= 800
    assert line_counts["outbound_differ.py"] <= 800
