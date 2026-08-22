"""Standalone contract for the reconcile helper's git-adapter seam.

Test contract card::

    authoritative_contract: >
      reconcile.py and reconcile_helpers.py explicitly support by-file standalone
      loading; _commit_binding_store_snapshot returns True for no state/success and
      False with a diagnostic for operational failure; _loader.py preserves an exact
      sys.modules key supplied by the caller.
    trigger_preconditions: >
      an isolated child interpreter cannot import a rebar_reconciler parent package
      and loads reconcile.py with spec_from_file_location.
    production_path: >
      reconcile.py -> by-path reconcile_helpers.py ->
      _commit_binding_store_snapshot -> rebar._store.push.commit_tickets_branch
      (the locked store commit seam; the git-adapter module is still loaded for
      the tracker/state-file path constants).
    test_tier: >
      unit test driving a real isolated Python subprocess; an in-process unit is
      insufficient because prior imports can leak package/sys.modules context.
    observable_postcondition: >
      no-state and successful state calls return True; adapter failure returns False
      and emits the documented diagnostic; a pre-seeded canonical adapter is reused.
    negative_control: >
      the same standalone route is exercised with no state, successful state, and
      an adapter failure so the oracle distinguishes all documented outcomes.
    ci_gate: make test
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
RECONCILE_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "reconcile.py"
GIT_ADAPTER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "git_adapter.py"

_STANDALONE_PROBE = r"""
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

reconcile_path = Path(sys.argv[1]).resolve()
repo_root = Path(sys.argv[2]).resolve()
scenario = sys.argv[3]

# Prove the package-free precondition before seeding any exact child key.
parent_importable = importlib.util.find_spec("rebar_reconciler") is not None
assert not parent_importable
assert "rebar_reconciler" not in sys.modules

adapter_key = "rebar_reconciler.git_adapter"
seeded_adapter = None
if scenario != "no_state":
    # The helper reads the tracker/state-file constants from this module; the
    # commit itself now goes through rebar._store.push.commit_tickets_branch
    # (ticket 11a9-b11b), so no add/diff/commit callables are seeded — the
    # observable is the real tracker repo (success) or the diagnostic (failure).
    seeded_adapter = ModuleType(adapter_key)
    seeded_adapter.TRACKER_DIR = ".tickets-tracker"
    seeded_adapter.BINDINGS_FILE = ".bridge_state/bindings.json"
    seeded_adapter.BINDINGS_RETIRED_FILE = ".bridge_state/bindings-retired.json"
    seeded_adapter.GET_ROTATION_FILE = ".bridge_state/get_rotation.json"
    seeded_adapter.IMPOSSIBLE_LINKS_FILE = ".bridge_state/impossible-links.json"
    seeded_adapter.PEER_CONFIRMATIONS_FILE = ".bridge_state/peer-confirmations.json"
    sys.modules[adapter_key] = seeded_adapter

spec = importlib.util.spec_from_file_location("_standalone_reconcile_contract", reconcile_path)
assert spec is not None and spec.loader is not None
reconcile = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = reconcile
spec.loader.exec_module(reconcile)

result = reconcile._commit_binding_store_snapshot(object(), repo_root, "standalone-contract")
resolved_adapter = sys.modules.get(adapter_key)
head_subject = None
if scenario == "success":
    import subprocess

    head = subprocess.run(
        ["git", "-C", str(repo_root / ".tickets-tracker"), "log", "-1", "--format=%s"],
        capture_output=True,
        text=True,
        check=False,
    )
    head_subject = head.stdout.strip() if head.returncode == 0 else None
print(
    json.dumps(
        {
            "result": result,
            "parent_importable": parent_importable,
            "adapter_name": getattr(resolved_adapter, "__name__", None),
            "adapter_file": getattr(resolved_adapter, "__file__", None),
            "same_adapter": seeded_adapter is None or resolved_adapter is seeded_adapter,
            "head_subject": head_subject,
        }
    )
)
"""


def _run_standalone(tmp_path: Path, scenario: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            _STANDALONE_PROBE,
            str(RECONCILE_PATH),
            str(tmp_path),
            scenario,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )


def _result(proc: subprocess.CompletedProcess[str]) -> dict:
    assert proc.returncode == 0, (
        f"standalone reconcile probe failed:\n--- stdout ---\n{proc.stdout}"
        f"--- stderr ---\n{proc.stderr}"
    )
    return json.loads(proc.stdout)


def _write_binding_state(tmp_path: Path, *, git_repo: bool) -> None:
    tracker = tmp_path / ".tickets-tracker"
    binding_file = tracker / ".bridge_state" / "bindings.json"
    binding_file.parent.mkdir(parents=True)
    binding_file.write_text("{}")
    if git_repo:
        for argv in (
            ["git", "init", "-q", str(tracker)],
            ["git", "-C", str(tracker), "config", "user.name", "standalone-contract"],
            ["git", "-C", str(tracker), "config", "user.email", "standalone@contract.test"],
        ):
            subprocess.run(argv, check=True, capture_output=True)


def test_package_free_no_state_returns_true_and_loads_canonical_adapter(tmp_path: Path) -> None:
    result = _result(_run_standalone(tmp_path, "no_state"))

    assert result["parent_importable"] is False
    assert result["result"] is True
    assert result["adapter_name"] == "rebar_reconciler.git_adapter"
    assert Path(result["adapter_file"]).resolve() == GIT_ADAPTER_PATH.resolve()


def test_package_free_state_reuses_preseeded_adapter_and_reaches_git_ops(tmp_path: Path) -> None:
    _write_binding_state(tmp_path, git_repo=True)
    result = _result(_run_standalone(tmp_path, "success"))

    assert result["result"] is True
    assert result["same_adapter"] is True
    # The locked store seam really committed the snapshot to the tracker repo.
    assert result["head_subject"] == (
        "reconciler: persist binding-store snapshot [pass standalone-contract]"
    )


def test_package_free_adapter_failure_returns_false_with_diagnostic(tmp_path: Path) -> None:
    # The tracker directory exists with state but is NOT a git repository, so the
    # strict locked commit seam raises and the helper degrades to False + stderr.
    _write_binding_state(tmp_path, git_repo=False)
    proc = _run_standalone(tmp_path, "failure")
    result = _result(proc)

    assert result["result"] is False
    assert result["same_adapter"] is True
    assert "binding-store commit to tickets branch failed" in proc.stderr
    assert "PushDeliveryError" in proc.stderr
