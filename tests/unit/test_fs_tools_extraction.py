"""WS-A: the shared fs/repo safety cluster in llm/fs_tools.py.

Pins the structural contract: the runner-agnostic primitives live in fs_tools and
are consumed by pai_tools to build the agent's file tools, and fs_tools stays
import-light (no heavy `agents`-extra import at module load)."""

from __future__ import annotations

import subprocess
import sys


def test_fs_tools_cluster_is_reused_by_identity_and_drives_discovery(tmp_path) -> None:
    from rebar.llm import fs_tools, pai_tools

    # (1) pai_tools builds its file tools on the SAME primitive objects — reuse by
    # IDENTITY (`is`), not a re-implemented copy. A name-existence probe would pass
    # even if pai_tools forked its own path/discovery hardening; identity would not.
    assert pai_tools._safe_path is fs_tools._safe_path
    assert pai_tools._within_root is fs_tools._within_root
    assert pai_tools._discovery_filter is fs_tools._discovery_filter
    assert pai_tools._SCAN_MAX_FILES is fs_tools._SCAN_MAX_FILES

    # (2) The noise constants (_NOISE_DIRS / _NOISE_SUFFIXES) and _git_tracked are
    # exercised through their EFFECT on the seam that consumes them, _discovery_filter,
    # rather than asserted as literal frozenset contents.
    skip_dir, skip_file = fs_tools._discovery_filter(str(tmp_path))
    assert skip_dir("node_modules") is True  # a _NOISE_DIRS member is hidden
    assert skip_dir("src") is False  # an ordinary source dir is kept
    assert skip_file(str(tmp_path / "x.min.js"), "x.min.js") is True  # a _NOISE_SUFFIXES file


def test_pai_tools_consume_the_shared_cluster() -> None:
    # pai_tools (the only file-tool builder post-cutover) reuses fs_tools' helpers
    # rather than re-implementing the path/discovery hardening.
    from rebar.llm import fs_tools, pai_tools

    assert pai_tools._safe_path is fs_tools._safe_path
    assert pai_tools._discovery_filter is fs_tools._discovery_filter
    assert pai_tools._within_root is fs_tools._within_root
    assert pai_tools._SCAN_MAX_FILES is fs_tools._SCAN_MAX_FILES


def test_importing_fs_tools_does_not_pull_the_agent_runtime() -> None:
    # The optionality invariant: importing the module must not import the heavy
    # agents extra (the agent runtime is imported lazily inside the runner). Run in a
    # CLEAN subprocess — mutating this process's sys.modules (deleting pydantic_ai)
    # would break `isinstance(_, Model)` for FunctionModel in other tests via a
    # duplicate-module-object leak.
    code = (
        "import sys, rebar.llm.fs_tools;"
        "print('LEAK' if any(m.startswith('pydantic_ai') for m in sys.modules) else 'CLEAN')"
    )
    cp = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": "src", "PATH": __import__("os").environ.get("PATH", "")},
    )
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.strip() == "CLEAN", "importing rebar.llm.fs_tools pulled in the agents extra"
