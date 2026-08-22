"""A library read path may not terminate its host process (bug 176d).

``_engine_support/reads.tracker_dir()`` printed to stderr and called ``sys.exit(1)``
when the repo root was not a git worktree. That function is reached from the
library gate surface (``_lib_gates``) and thence from MCP (``_mcp_reads``).

``SystemExit`` derives from ``BaseException``, and FastMCP's tool wrapper catches
``Exception`` -- so the exit sails past it and kills the whole ``anyio`` run. The
MCP server *process* terminates instead of the tool returning an error. A leaf
helper does not get to decide process lifetime for every embedding surface; the
library contract is to raise ``RebarError``.

The CLI's observable behaviour must not change: same stderr line, same exit 1.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from rebar._errors import RebarError

_SRC = Path(__file__).resolve().parents[3] / "src" / "rebar"

_MESSAGE = "not inside a git repository (set REBAR_ROOT or run inside the repo)"
_EXPECTED_STDERR = f"Error: {_MESSAGE}"


# ── the library contract: raise, do not exit ────────────────────────────────


def test_tracker_dir_raises_instead_of_exiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-repo cwd with REBAR_ROOT unset must raise a rebar error, not SystemExit."""
    from rebar._engine_support import reads

    monkeypatch.delenv("REBAR_ROOT", raising=False)
    monkeypatch.delenv("REBAR_TRACKER_DIR", raising=False)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(RebarError) as excinfo:
        reads.tracker_dir()

    assert "not inside a git repository" in str(excinfo.value)


def test_tracker_dir_raises_for_a_non_repo_explicit_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The supplied-root branch (REBAR_ROOT / explicit arg pointing at a non-worktree)
    is the second ``sys.exit`` site and must raise too."""
    from rebar._engine_support import reads

    monkeypatch.delenv("REBAR_TRACKER_DIR", raising=False)

    with pytest.raises(RebarError):
        reads.tracker_dir(str(tmp_path))


def test_tracker_dir_does_not_raise_systemexit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pinned separately from the RebarError assertion: SystemExit is a
    BaseException, so a `pytest.raises(RebarError)` alone would not catch a
    regression back to `sys.exit` -- it would error out instead of failing
    informatively. This makes the process-lifetime claim explicit."""
    from rebar._engine_support import reads

    monkeypatch.delenv("REBAR_ROOT", raising=False)
    monkeypatch.delenv("REBAR_TRACKER_DIR", raising=False)
    monkeypatch.chdir(tmp_path)

    try:
        reads.tracker_dir()
    except RebarError:
        pass
    except SystemExit:  # pragma: no cover - the regression this test exists for
        pytest.fail("tracker_dir raised SystemExit; a library read must not exit the host")


# ── the CLI boundary keeps its observable contract ──────────────────────────


def test_cli_maps_the_error_to_exit_1_with_the_same_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI boundary owns the exit decision now that the read core raises.

    Driven by making dispatch raise, rather than by an uninitialized cwd: the
    auto-init middleware (`_cli/_init.py`) refuses a non-repo cwd BEFORE dispatch,
    so an end-to-end invocation never reaches this handler and would pass whether
    or not the mapping existed. (It did -- an earlier version of this test was
    vacuous for exactly that reason.) The residual real triggers are an explicit
    non-repo root and the repo vanishing mid-session.
    """
    import rebar._cli as cli
    from rebar._errors import TrackerRootError

    def _boom(argv: list[str]) -> int:
        raise TrackerRootError(_MESSAGE)

    monkeypatch.setattr(cli, "_main_dispatch", _boom)

    rc = cli.main(["list"])

    assert rc == 1
    assert capsys.readouterr().err == f"Error: {_MESSAGE}\n"


def test_cli_contract_for_an_uninitialized_cwd_is_unchanged(tmp_path: Path) -> None:
    """Regression cover for the user-visible contract as a whole: exit 1 and the
    exact stderr line. This path is served by the auto-init middleware, not by the
    read core -- it is pinned here so the refactor cannot disturb it."""
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    cp = subprocess.run(
        [sys.executable, "-m", "rebar.cli", "list"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=env,
        check=False,
    )
    assert cp.returncode == 1, f"expected exit 1, got {cp.returncode}\n{cp.stderr}"
    assert _EXPECTED_STDERR in cp.stderr, cp.stderr


# ── no sys.exit reachable from the library / MCP surfaces ───────────────────


def _is_main_guard(node: ast.AST) -> bool:
    """True for an ``if __name__ == "__main__":`` block."""
    if not isinstance(node, ast.If):
        return False
    t = node.test
    return (
        isinstance(t, ast.Compare)
        and isinstance(t.left, ast.Name)
        and t.left.id == "__name__"
        and any(isinstance(c, ast.Constant) and c.value == "__main__" for c in t.comparators)
    )


def _calls_sys_exit(path: Path) -> list[int]:
    """Line numbers of `sys.exit(...)` / `exit(...)` calls in ``path``.

    A ``sys.exit`` under ``if __name__ == "__main__":`` is excluded: that is the
    script entrypoint deciding its own process's status, which is exactly where the
    exit decision belongs. Only exits reachable when the module is IMPORTED matter
    here.
    """
    tree = ast.parse(path.read_text())
    skip: set[int] = set()
    for node in ast.walk(tree):
        if _is_main_guard(node):
            for inner in ast.walk(node):
                skip.add(id(inner))
    hits: list[int] = []
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr == "exit":
            if isinstance(f.value, ast.Name) and f.value.id == "sys":
                hits.append(node.lineno)
        elif isinstance(f, ast.Name) and f.id == "exit":
            hits.append(node.lineno)
    return hits


@pytest.mark.parametrize(
    "relpath",
    ["_engine_support/reads.py", "_lib_gates.py", "_lib_reads.py", "_mcp_reads.py"],
)
def test_no_sys_exit_on_the_library_read_path(relpath: str) -> None:
    """The embedding surfaces must not contain a process-terminating call. This is a
    source scan rather than a behavioural probe because the failure mode it guards
    is *unreachable code becoming reachable* -- a new `sys.exit` on any of these
    modules would only surface as a dead MCP server in production."""
    path = _SRC / relpath
    hits = _calls_sys_exit(path)
    assert not hits, f"{relpath} calls sys.exit at line(s) {hits}; raise RebarError instead"
