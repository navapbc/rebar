"""HELD-OUT oracle (rebar-debug, bug af1b): a scoped `sync --only` LIVE pass MUST dispatch
a bound issue's outbound scalar UPDATE to the transport write, exactly as the legacy
`--filter-local-ids` route does.

Confirmed root cause: run_differs builds the outbound update with ``target = jira_key`` and
hands ``ticket_planner.plan_pass`` a selection whose ``ids`` are the SELECTED LOCAL IDS only;
``_scope_excluded`` compares the jira-key target against those local ids and classifies the
in-scope bound-issue update as ``scope_deferred``, so the live coordinator+fuse reroute drops
the write. The legacy route scopes via ``_build_filter_target_set`` (LOCAL IDS ∪ bound JIRA
KEYS) and applies correctly.

The reconcile pass is driven by ``_sync_only_dispatch_probe.py`` in a CLEAN subprocess: the
probe (re)loads reconciler modules under flat keys, which collides with pytest's package /
conftest module seeding when run in-process. A faithful in-memory transport records the
``update_issue`` calls; the bug is not codec/DC-specific, so an offline transport reproduces
it (matching the live in-CI probe for bug af1b; context: external, GH Actions run 33129851229).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from _subprocess_env import subprocess_env

_HERE = Path(__file__).resolve().parent
_PROBE = _HERE / "_sync_only_dispatch_probe.py"
_ENGINE_DIR = Path(__file__).resolve().parents[4] / "src" / "rebar" / "_engine"
_EXPECTED = ["update_issue(DIG-1) fields=['description']"]


def _run_probe(route: str, base: Path) -> list[str]:
    # subprocess_env() (not dict(os.environ)): pytest renders call args in its default
    # traceback, so a raw environment dict would leak inherited secrets on a subprocess
    # failure — the repr-safe boundary keeps values available to the child while redacting
    # the representation (enforced by test_subprocess_env_repr_security).
    env = subprocess_env()
    # The reconciler unit-test conftests inject REBAR_ROOT / REBAR_CONFIG (empty sandbox
    # repos) and pinned JIRA_* creds as autouse fixtures. The subprocess would inherit them
    # and read an EMPTY ticket store; the probe seeds its OWN repo + env, so drop the
    # inherited overrides and let the probe govern.
    for key in ("REBAR_ROOT", "REBAR_CONFIG", "XDG_CONFIG_HOME"):
        env.pop(key, None)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{_ENGINE_DIR}{os.pathsep}{existing}" if existing else str(_ENGINE_DIR)
    proc = subprocess.run(
        [sys.executable, str(_PROBE), route, str(base)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(_ENGINE_DIR.parents[2]),
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
