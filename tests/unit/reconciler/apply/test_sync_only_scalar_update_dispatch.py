"""A live sync --only selection must dispatch a bound issue’s scalar update, matching legacy
--filter-local-ids. The planner must recognize both selected local ID and bound Jira key;
subprocess setup strips inherited sandbox overrides and protects secret values."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from _subprocess_env import subprocess_env

_HERE = Path(__file__).resolve().parent
_PROBE = _HERE / "_sync_only_dispatch_probe.py"
_EXPECTED = ["update_issue(DIG-1) fields=['description']"]


def _run_probe(route: str, base: Path) -> list[str]:
    # subprocess_env keeps child values while redacting secrets from failure tracebacks.
    env = subprocess_env()
    # Remove inherited fixture sandbox paths so the probe reads its own seeded store.
    for key in ("REBAR_ROOT", "REBAR_CONFIG", "XDG_CONFIG_HOME"):
        env.pop(key, None)
    proc = subprocess.run(
        [sys.executable, str(_PROBE), route, str(base)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(base.parent),
        check=False,
    )
    marker = "PROBE_RESULT "
    for line in proc.stdout.splitlines():
        if line.startswith(marker):
            return json.loads(line[len(marker) :])
    raise AssertionError(
        f"probe {route!r} produced no PROBE_RESULT\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )


def test_sync_only_dispatches_bound_issue_scalar_update(tmp_path: Path) -> None:
    """PRIMARY route: `sync --only=<local_id>` on a bound issue with a description drift MUST
    issue the transport update_issue write. RED before the fix (scope_deferred drop)."""
    updates = _run_probe("only", tmp_path / "repo_only")
    assert updates == _EXPECTED, (
        f"sync --only dropped the bound-issue description write (update_issue calls: {updates!r})"
    )


def test_filter_local_ids_dispatches_bound_issue_scalar_update(tmp_path: Path) -> None:
    """LEGACY route parity guard (and harness sanity): `--filter-local-ids` dispatches the
    same write. GREEN before and after the fix."""
    updates = _run_probe("filter", tmp_path / "repo_filter")
    assert updates == _EXPECTED, (
        f"--filter-local-ids dropped the write (update_issue calls: {updates!r})"
    )
