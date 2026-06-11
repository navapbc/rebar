"""I1 per-file commit: retirement-only changes ARE committed (bug 1e08).

_commit_binding_store_snapshot must stage BOTH bindings.json AND
bindings-retired.json, and its idempotency guard must be PER-FILE. The prior
substring test (``"bindings.json" not in status.stdout``) would not match the
distinct ``bindings-retired.json`` file, silently skipping a retirement-only
commit (a soft-deleted binding lost on the next merge origin/tickets).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
RECONCILE_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "reconcile.py"
)


def _load_module(name: str, path: Path):
    if name in sys.modules:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture
def reconcile_mod():
    mod = _load_module("_test_reconcile_retired", RECONCILE_PATH)
    yield mod
    sys.modules.pop("_test_reconcile_retired", None)


def _init_tickets_git_repo(tracker_dir: Path) -> None:
    tracker_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "tickets", str(tracker_dir)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tracker_dir), "config", "user.email", "t@t.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tracker_dir), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(tracker_dir),
            "commit",
            "--allow-empty",
            "-m",
            "init",
            "--no-verify",
        ],
        check=True,
        capture_output=True,
    )


def _file_in_head(tracker_dir: Path, rel: str) -> str | None:
    r = subprocess.run(
        ["git", "-C", str(tracker_dir), "show", f"HEAD:{rel}"],
        capture_output=True,
        text=True,
    )
    return r.stdout if r.returncode == 0 else None


class _Stub:
    """Minimal stand-in: _commit_binding_store_snapshot only touches git."""


def test_retirement_only_change_is_committed(tmp_path, reconcile_mod):
    tracker_dir = tmp_path / ".tickets-tracker"
    _init_tickets_git_repo(tracker_dir)
    bridge = tracker_dir / ".bridge_state"
    bridge.mkdir(parents=True)

    # Commit an initial bindings.json so the live file is already up-to-date
    # and ONLY bindings-retired.json will differ on the retirement pass.
    (bridge / "bindings.json").write_text(json.dumps({"bindings": {}, "reverse": {}}))
    subprocess.run(
        ["git", "-C", str(tracker_dir), "add", ".bridge_state/bindings.json"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tracker_dir), "commit", "--no-verify", "-m", "init bindings"],
        check=True,
        capture_output=True,
    )

    # Now write a retirement-only change: bindings-retired.json (new file),
    # bindings.json unchanged.
    (bridge / "bindings-retired.json").write_text(
        json.dumps({"version": 1, "retired": {"DIG-DEAD": {"local_id": "loc-1"}}})
    )

    ok = reconcile_mod._commit_binding_store_snapshot(_Stub(), tmp_path, "pass-retire")
    assert ok is True

    committed = _file_in_head(tracker_dir, ".bridge_state/bindings-retired.json")
    assert committed is not None, (
        "retirement-only change must be committed; the per-file idempotency "
        "guard must NOT skip a change confined to bindings-retired.json"
    )
    assert "DIG-DEAD" in committed


def test_no_change_is_noop(tmp_path, reconcile_mod):
    tracker_dir = tmp_path / ".tickets-tracker"
    _init_tickets_git_repo(tracker_dir)
    bridge = tracker_dir / ".bridge_state"
    bridge.mkdir(parents=True)
    (bridge / "bindings.json").write_text(json.dumps({"bindings": {}, "reverse": {}}))
    subprocess.run(
        ["git", "-C", str(tracker_dir), "add", ".bridge_state/bindings.json"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tracker_dir), "commit", "--no-verify", "-m", "init"],
        check=True,
        capture_output=True,
    )
    head_before = subprocess.run(
        ["git", "-C", str(tracker_dir), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout

    # No change to either file → fast-path no-op (no new commit).
    ok = reconcile_mod._commit_binding_store_snapshot(_Stub(), tmp_path, "pass-noop")
    assert ok is True
    head_after = subprocess.run(
        ["git", "-C", str(tracker_dir), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout
    assert head_before == head_after, "no-change pass must not create a commit"


def test_both_files_committed_together(tmp_path, reconcile_mod):
    tracker_dir = tmp_path / ".tickets-tracker"
    _init_tickets_git_repo(tracker_dir)
    bridge = tracker_dir / ".bridge_state"
    bridge.mkdir(parents=True)
    (bridge / "bindings.json").write_text(
        json.dumps(
            {
                "bindings": {"loc-1": {"jira_key": "DIG-9", "state": "confirmed"}},
                "reverse": {"DIG-9": "loc-1"},
            }
        )
    )
    (bridge / "bindings-retired.json").write_text(
        json.dumps({"version": 1, "retired": {"DIG-OLD": {"local_id": "loc-x"}}})
    )
    ok = reconcile_mod._commit_binding_store_snapshot(_Stub(), tmp_path, "pass-both")
    assert ok is True
    assert _file_in_head(tracker_dir, ".bridge_state/bindings.json") is not None
    assert _file_in_head(tracker_dir, ".bridge_state/bindings-retired.json") is not None
