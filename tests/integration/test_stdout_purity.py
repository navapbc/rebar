"""Stdout-purity contract test (epic ring-gun-jot, substrate ticket 8fbd).

The logging substrate routes diagnostics to **stderr** and leaves **stdout** pure:
CLI *data* ``print(json.dumps(...))`` is a machine contract — the reconciler
``json.loads`` a rebar subprocess' stdout at ``reconcile.py``. This test guards that
contract two ways:

1. ``rebar list --full`` stdout parses as a single JSON document (no diagnostic leakage).
2. A forced diagnostic emitted through the named-logger substrate lands on stderr while
   a concurrent stdout data print stays pure JSON.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from rebar import _engine

pytestmark = pytest.mark.integration

_CLI = _engine.in_process_cli()


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _engine_run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_CLI, *args],
        cwd=str(repo),
        env=_engine.engine_env(repo_root=str(repo)),
        text=True,
        capture_output=True,
        check=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """An initialized rebar repo with one ticket."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    r = tmp_path / "a"
    subprocess.run(["git", "clone", "-q", str(remote), str(r)], check=True)
    _git("config", "user.email", "test@example.com", cwd=r)
    _git("config", "user.name", "Test", cwd=r)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=r)
    _engine_run(r, "init")
    _engine_run(r, "create", "task", "purity probe ticket")
    return r


def test_list_full_stdout_is_pure_json(repo: Path) -> None:
    """`rebar list --full` stdout is a single parseable JSON document."""
    proc = _engine_run(repo, "list", "--full")
    # The whole of stdout must parse — any diagnostic leakage would break json.loads.
    tickets = json.loads(proc.stdout)
    assert isinstance(tickets, list)
    assert any(t.get("title") == "purity probe ticket" for t in tickets)


def test_diagnostic_goes_to_stderr_not_stdout() -> None:
    """A forced diagnostic through the substrate lands on stderr; stdout stays pure JSON.

    Simulates an entrypoint: import rebar (NullHandler on the ``rebar`` root), install the
    stderr handler, log a warning with ``exc_info``, then print a JSON data line to stdout.
    The data line must be the only thing on stdout; the diagnostic must be on stderr.
    """
    snippet = textwrap.dedent(
        """
        import json, logging, sys
        import rebar  # attaches NullHandler to the 'rebar' root
        from rebar._logging import install_stderr_handler

        install_stderr_handler("rebar")
        logger = logging.getLogger("rebar.test_probe")
        try:
            raise ValueError("boom")
        except ValueError:
            logger.warning("forced best-effort diagnostic", exc_info=True)

        print(json.dumps({"data": "contract", "n": 1}))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", snippet],
        text=True,
        capture_output=True,
        check=True,
        env={**os.environ, "REBAR_LOG_LEVEL": "WARNING"},
    )

    # stdout is exactly the JSON data line — pure, parseable, nothing else.
    assert json.loads(proc.stdout) == {"data": "contract", "n": 1}

    # The diagnostic (and its traceback) went to stderr, not stdout.
    assert "forced best-effort diagnostic" in proc.stderr
    assert "ValueError: boom" in proc.stderr
    assert "forced best-effort diagnostic" not in proc.stdout
    assert "Traceback" not in proc.stdout


def test_create_non_bug_warns_missing_file_impact_on_stderr(repo: Path) -> None:
    """`create task` nudges about missing file_impact on stderr; stdout stays clean.

    file_impact cannot be set at create time, so the plan-review file-impact-coverage
    gate (P9) would flag any leaf work ticket lacking it. The create command surfaces
    that requirement early as a stderr warning — never on stdout.
    """
    proc = _engine_run(repo, "create", "task", "needs file impact")
    assert "Warning: no file_impact recorded" in proc.stderr
    assert "set-file-impact" in proc.stderr
    # stdout carries only the created-ticket data lines — the warning must not leak there.
    assert "Warning: no file_impact recorded" not in proc.stdout
    assert "Created ticket" in proc.stdout


def test_create_bug_does_not_warn_missing_file_impact(repo: Path) -> None:
    """`create bug`/`session_log` are file-impact gate-exempt → no create-time warning."""
    bug = _engine_run(repo, "create", "bug", "a defect")
    assert "Warning: no file_impact recorded" not in bug.stderr
    log = _engine_run(repo, "create", "session_log", "a log")
    assert "Warning: no file_impact recorded" not in log.stderr
