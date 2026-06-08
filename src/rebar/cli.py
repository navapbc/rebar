"""rebar CLI.

Delegates to the bundled bash dispatcher for all ticket subcommands, and
intercepts ``rebar reconcile`` to route it to ``python -m dso_reconciler``
(the engine dispatcher itself has no reconcile arm).
"""

from __future__ import annotations

import os
import subprocess
import sys

from rebar import config
from rebar._engine import dispatcher, engine_env


def _reconcile(argv: list[str]) -> int:
    """rebar reconcile [--mode MODE] [--repo-root ROOT] [extra dso_reconciler args]."""
    root = str(config.repo_root())
    args = list(argv)
    if not any(a == "--repo-root" or a.startswith("--repo-root=") for a in args):
        args += ["--repo-root", root]
    if not any(a == "--mode" or a.startswith("--mode=") for a in args):
        args += ["--mode", "dry-run"]
    return subprocess.call(
        ["python3", "-m", "dso_reconciler", *args],
        env=engine_env(root),
    )


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] == "reconcile":
        sys.exit(_reconcile(argv[1:]))
    env = engine_env()
    # Run the dispatcher inside the repo root so its cwd-relative git operations
    # resolve the right repository even when invoked from elsewhere.
    cwd = env.get("REBAR_ROOT") or env.get("PROJECT_ROOT")
    if not (cwd and os.path.isdir(cwd)):
        cwd = None
    sys.exit(
        subprocess.call(["bash", str(dispatcher()), *argv], env=env, cwd=cwd)
    )


if __name__ == "__main__":
    main()
