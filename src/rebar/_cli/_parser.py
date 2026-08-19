"""Stdlib-only argparse foundation for the rebar CLI (RP-05 S2a scaffolding).

This module ships a deterministic argparse customization that shadows the
existing router without cutting any routing over to it. It imports ONLY the
standard library so it stays cheap and free of optional-dependency coupling:

* :class:`RebarHelpFormatter` renders help at a FIXED width (80 columns),
  independent of the terminal size or the ``COLUMNS`` environment variable, so
  help bytes are reproducible across machines and CI.
* :class:`RebarArgumentParser` turns a parse failure into a raised
  :class:`ParseError` instead of terminating the process, giving library callers
  a non-terminating parse shape. A successful parse still returns a normal
  :class:`argparse.Namespace`.
* :func:`build_argument_parser` is the conforming factory (``prog`` is
  keyword-only), and :func:`compose` lets an alias/compat spelling vary
  ``prog``/``epilog`` while reusing ONE shared argument-definition function.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable

# A fixed help width, deliberately independent of the terminal / COLUMNS so the
# rendered help bytes are deterministic across environments.
_FIXED_WIDTH = 80


class ParseError(Exception):
    """Raised (not exited) when a :class:`RebarArgumentParser` fails to parse.

    Carries the failing parser's ``prog`` so a caller can render usage without
    the parser having terminated the process. ``str(err)`` is the non-empty
    argparse message.
    """

    def __init__(self, message: str, *, prog: str) -> None:
        super().__init__(message)
        self.prog = prog


class RebarHelpFormatter(argparse.HelpFormatter):
    """Deterministic help formatter with a fixed width.

    The width is hardcoded to :data:`_FIXED_WIDTH`; argparse's default reads
    ``shutil.get_terminal_size()`` (honouring ``COLUMNS``) when ``width`` is
    ``None``, which makes help non-reproducible. Add-order is preserved by
    argparse, so no ordering customization is needed.
    """

    def __init__(
        self,
        prog: str,
        indent_increment: int = 2,
        max_help_position: int = 24,
        width: int | None = None,
    ) -> None:
        super().__init__(
            prog,
            indent_increment=indent_increment,
            max_help_position=max_help_position,
            width=_FIXED_WIDTH,
        )


class RebarArgumentParser(argparse.ArgumentParser):
    """An :class:`argparse.ArgumentParser` that raises instead of exiting.

    Uses :class:`RebarHelpFormatter` (fixed width). On a parse failure argparse
    normally calls ``error()`` → ``exit(2)``; here ``error()`` raises
    :class:`ParseError` so a library call never terminates the interpreter. A
    successful parse is unaffected and returns a normal namespace.
    """

    def error(self, message: str) -> None:  # type: ignore[override]
        raise ParseError(message, prog=self.prog)


def build_argument_parser(
    *,
    prog: str,
    description: str | None = None,
    epilog: str | None = None,
) -> argparse.ArgumentParser:
    """Build a :class:`RebarArgumentParser` bound to ``prog``.

    This is itself a conforming factory: ``prog`` is keyword-only, matching the
    ``build_parser(*, prog: str) -> ArgumentParser`` protocol shape.
    """

    return RebarArgumentParser(
        prog=prog,
        description=description,
        epilog=epilog,
        formatter_class=RebarHelpFormatter,
    )


def compose(
    define: Callable[[argparse.ArgumentParser], None],
    *,
    prog: str,
    epilog: str | None = None,
) -> argparse.ArgumentParser:
    """Compose a parser from a shared argument-definition function.

    ``define`` receives a fresh parser and adds the shared arguments. Varying
    ``prog``/``epilog`` while reusing ONE ``define`` lets a canonical spelling
    and an alias/compat spelling share an identical option surface with no
    option copying — the composed parsers parse argv identically.
    """

    parser = build_argument_parser(prog=prog, epilog=epilog)
    define(parser)
    return parser
