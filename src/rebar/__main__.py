"""``python -m rebar`` entry point.

Runs the in-process argparse CLI (:func:`rebar._cli.main`) — the same code path
the ``rebar`` console script uses (``rebar.cli:main``). This gives the package an
interpreter-relative invocation that does not depend on the console script being
on ``PATH``, which is useful when ``rebar`` is imported but not installed with its
entry points (e.g. a raw ``PYTHONPATH`` checkout).

The reconciler and ``validate`` read tickets through ``$REBAR_TICKET_CLI`` as a
single executable token, so they target the console script rather than this
``python -m rebar`` form (see :func:`rebar._engine.in_process_cli`); this module
is the import-path-independent fallback and a documented manual entry.
"""

from __future__ import annotations

import sys

from rebar._cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
