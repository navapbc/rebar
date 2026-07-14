"""Pytest configuration for unit tests.

Adds the engine directory (``src/rebar/_engine``) to ``sys.path`` so engine unit
tests can import the bundled helpers by their on-disk names without each test
file manipulating ``sys.path`` itself. After the ``fare-rant-clasp`` repackage the
old top-level names (``ticket_reducer`` / ``ticket_graph`` / ``ticket_reads`` …)
resolve here to thin compat shims that re-export the real ``rebar.*`` subpackages,
so these imports keep working while exercising the same code the library loads.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = str(_REPO_ROOT / "src" / "rebar" / "_engine")

if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


@pytest.fixture(autouse=True)
def _no_real_session_log_writes(monkeypatch):
    """Unit tests must never perform a REAL session-log store write.

    ``rebar.append_session_log`` commits a ``session_log`` ticket to the shared
    ``tickets`` branch AND writes the ``.rebar/current_session_log`` pointer into the
    repo root. Several best-effort telemetry paths reach it WITHOUT the caller opting
    in — notably the degraded-gate verdicts
    (``llm.workflow.gate_dispatch._degraded_plan_review_verdict`` /
    ``_degraded_code_review_verdict`` -> ``llm.failure.log_degrade`` ->
    ``append_session_log``). So a unit test that merely exercises a degraded verdict
    silently pollutes the shared store and, in a fresh worktree, trips the
    ``_no_repo_root_leaks`` guard on the leaked ``.rebar`` pointer (bug d9aa,
    misty-creatable-mallard).

    Neutralize the write seam for the WHOLE unit tier so no unit test — present or
    future — can leak through any degrade path. This is test-only (production code is
    untouched) and mirrors the tier's existing "never touch the real store" contract.
    A test that specifically exercises the real helper re-monkeypatches it in its body
    (a function-scoped ``monkeypatch.setattr`` applied after this fixture wins), e.g.
    ``test_log_degrade_never_raises``.
    """
    import rebar

    def _noop_append_session_log(*_args, **_kwargs):
        return {"id": None, "alias": None, "created": False}

    monkeypatch.setattr(rebar, "append_session_log", _noop_append_session_log)
