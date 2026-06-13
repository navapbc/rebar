"""rebar CLI entrypoint.

Thin facade over the in-process argparse CLI (:mod:`rebar._cli`). Tier E (story
adult-oxide-slave) replaced the bash dispatcher delegation here with the native
Python CLI; ``rebar._cli.main`` owns help/overview/error parity, the auto-init
middleware, in-process dispatch for ported commands, and the transitional
subprocess fallback for commands not yet ported. ``reconcile`` is handled inside
``rebar._cli`` (the engine dispatcher never had a reconcile arm).
"""

from __future__ import annotations

import sys

from rebar._cli import main as _main


def main() -> None:
    sys.exit(_main(sys.argv[1:]))


if __name__ == "__main__":
    main()
