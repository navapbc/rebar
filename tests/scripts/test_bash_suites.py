"""Collector: run every standalone ``tests/scripts/test-*.sh`` bash suite under
pytest and fail the run on a non-zero exit.

rebar's engine is bash + python; ~90 behaviours are covered ONLY by standalone
bash suites that historically ran on no trigger. This makes pytest the single
entry point — ``pytest tests/scripts`` (and therefore CI) now fails if any bash
suite fails (ticket coily-conch-tag / Rec 3 of the 2026-06-09 architecture review).

The suites shell out to the ``rebar`` console script, so we prepend the directory
of the running interpreter (where ``pip install -e`` puts the entry point) to PATH
for the subprocess — works both in CI and when pytest is invoked by path.

Network/live-Jira tiers are excluded from the default run: the bash suites here
are all offline (they build throwaway git repos and mock Jira). Any suite that
genuinely needs live credentials must be added to ``_REQUIRES_LIVE`` with a reason
so it is skipped rather than failing CI.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent
_SUITES = sorted(_SCRIPTS_DIR.glob("test-*.sh"))

# Per-suite wall-clock ceiling — generous (some suites spin up git worktrees and
# exercise flock/concurrency), but bounded so a hung suite fails instead of
# stalling CI forever.
_TIMEOUT_S = int(os.environ.get("REBAR_BASH_SUITE_TIMEOUT", "300"))

# Suites that require live network / Jira credentials. Empty today (every suite
# is offline); populate with {name: reason} to skip one out of the default run.
_REQUIRES_LIVE: dict[str, str] = {}


def _suite_env() -> dict[str, str]:
    env = dict(os.environ)
    # Put the interpreter's OWN bin dir first on PATH so the bash suites resolve
    # the `rebar` console script installed next to it by `pip install -e` (the
    # code under test) — not some other `rebar` earlier on the developer's PATH
    # (e.g. a pipx/global install). Use `.parent` WITHOUT `.resolve()`: in a venv,
    # `sys.executable` is a symlink to the base interpreter, so resolving it would
    # point at the base interpreter's bin (which has no venv console scripts) and
    # silently let a stale global `rebar` shadow the editable one.
    bindir = str(Path(sys.executable).parent)
    env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    return env


@pytest.mark.scripts
@pytest.mark.parametrize("suite", _SUITES, ids=[p.name for p in _SUITES])
def test_bash_suite(suite: Path) -> None:
    reason = _REQUIRES_LIVE.get(suite.name)
    if reason:
        pytest.skip(f"requires live network/Jira ({reason}); excluded from default CI")

    proc = subprocess.run(
        ["bash", str(suite)],
        cwd=str(_SCRIPTS_DIR.parent.parent),  # repo root
        env=_suite_env(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_S,
    )
    if proc.returncode != 0:
        tail_out = proc.stdout[-4000:]
        tail_err = proc.stderr[-4000:]
        pytest.fail(
            f"{suite.name} exited {proc.returncode}\n"
            f"--- stdout (tail) ---\n{tail_out}\n"
            f"--- stderr (tail) ---\n{tail_err}",
            pytrace=False,
        )


def test_collector_found_the_suites() -> None:
    # Guard: if the glob silently matches nothing (layout moved), this fails loudly
    # instead of the suite-count quietly dropping to zero.
    assert len(_SUITES) >= 50, f"expected the bash suites under {_SCRIPTS_DIR}, found {len(_SUITES)}"
