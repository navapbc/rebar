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
from typing import ParamSpec

# A fixed help width, deliberately independent of the terminal / COLUMNS so the
# rendered help bytes are deterministic across environments.
_FIXED_WIDTH = 80

_P = ParamSpec("_P")


class ParseError(Exception):
    """Raised (not exited) when a :class:`RebarArgumentParser` fails to parse.

    Carries the failing parser's ``prog`` so a caller can render usage without
    the parser having terminated the process. ``str(err)`` is the non-empty
    argparse message.
    """

    def __init__(self, message: str, *, prog: str, usage: str | None = None) -> None:
        super().__init__(message)
        self.prog = prog
        # The failing parser's rendered usage block (``usage: ...\n``), captured at
        # raise time so a caller can reproduce argparse's on-error stderr output
        # (usage + ``prog: error: message``) even for a nested subparser it never
        # holds a reference to. ``None`` only for hand-built ParseErrors.
        self.usage = usage


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

    def _format_action_invocation(self, action: argparse.Action) -> str:
        """Render an option invocation in the pre-3.13 style on EVERY Python version.

        Python 3.13 changed argparse to print an optional that takes a value as
        ``-o, --output METAVAR`` (metavar once) instead of the historical
        ``-o METAVAR, --output METAVAR`` (metavar per option string). The generated
        ``help/*.txt`` artifacts are committed bytes checked by
        ``gen_cli_help.py --check`` across the whole CI version matrix, so a
        version-dependent invocation makes ``--check`` report every option-bearing
        command stale on 3.13 while passing on 3.11/3.12. Pin the historical form
        (matching the pinned 3.12 toolchain and 3.11) so generation — and live help —
        are byte-identical on all three. This is the verbatim pre-3.13 stdlib body.
        """
        if not action.option_strings:
            default = self._get_default_metavar_for_positional(action)
            (metavar,) = self._metavar_formatter(action, default)(1)
            return metavar
        if action.nargs == 0:
            return ", ".join(action.option_strings)
        default = self._get_default_metavar_for_optional(action)
        args_string = self._format_args(action, default)
        return ", ".join(
            f"{option_string} {args_string}" for option_string in action.option_strings
        )

    def _format_usage(self, usage, actions, groups, prefix=None):  # type: ignore[no-untyped-def]
        """Wrap a subparsers ``...`` remainder onto its own line on EVERY Python version.

        argparse's usage-line wrapping of a subparsers action's ``{choices} ...`` changed
        repeatedly across CPython 3.x (pre-3.13 split it on whitespace so ``...`` wrapped to
        its own line; 3.13 kept ``{choices} ...`` together; 3.13's private helper was then
        renamed again in a 3.13 patch release — gh-75949). The committed ``help/*.txt`` for
        subparser commands (e.g. ``bridge``) are byte-checked across the whole CI version
        matrix, so chasing those private-method changes is fragile. Instead, post-process the
        RENDERED usage string — a stable seam: split any over-width line ending in a ``...``
        remainder so the ``...`` sits on its own line at the same indent (the historical
        form). This depends only on the output text and the fixed width, not argparse
        internals, so it is stable regardless of which private helper a given CPython uses.
        """
        rendered = super()._format_usage(usage, actions, groups, prefix)
        out_lines: list[str] = []
        for line in rendered.split("\n"):
            if line.endswith(" ...") and len(line) > self._width:
                head = line[:-4]
                indent = head[: len(head) - len(head.lstrip(" "))]
                out_lines.append(head)
                out_lines.append(f"{indent}...")
            else:
                out_lines.append(line)
        return "\n".join(out_lines)


class RebarArgumentParser(argparse.ArgumentParser):
    """An :class:`argparse.ArgumentParser` that raises instead of exiting.

    Uses :class:`RebarHelpFormatter` (fixed width). On a parse failure argparse
    normally calls ``error()`` → ``exit(2)``; here ``error()`` raises
    :class:`ParseError` so a library call never terminates the interpreter. A
    successful parse is unaffected and returns a normal namespace.
    """

    def error(self, message: str) -> None:  # type: ignore[override]
        raise ParseError(message, prog=self.prog, usage=self.format_usage())


def render_parse_error(exc: ParseError) -> int:
    """Reproduce argparse's on-error stderr output for a caught :class:`ParseError`.

    A migrated handler holds a :class:`RebarArgumentParser` whose ``error()`` raises
    instead of terminating; catching that at the handler boundary and calling this
    reproduces the EXACT bytes argparse writes on a parse failure — the parser's
    usage block followed by ``<prog>: error: <message>`` — and returns the argparse
    convention exit code ``2``. No traceback escapes.
    """
    import sys

    if exc.usage:
        sys.stderr.write(exc.usage)
    sys.stderr.write(f"{exc.prog}: error: {exc}\n")
    return 2


def guard_parse_errors(func: Callable[_P, int]) -> Callable[_P, int]:
    """Wrap a CLI handler so a :class:`ParseError` reproduces argparse's exit contract.

    A migrated handler builds a :class:`RebarArgumentParser`; both an argparse-internal
    parse failure and an explicit ``parser.error(...)`` now RAISE :class:`ParseError`
    instead of terminating. Handlers that previously relied on argparse's native
    ``error()`` — which prints usage/message to stderr and then ``SystemExit(2)`` —
    let that ``SystemExit`` propagate through ``main()`` to the process boundary. This
    decorator restores that EXACT observable contract: it renders the usage/message to
    stderr (no traceback) and re-raises ``SystemExit(2)``. A ``--help`` action already
    raises :class:`SystemExit`, which propagates unchanged.
    """
    import functools

    @functools.wraps(func)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> int:
        try:
            return func(*args, **kwargs)
        except ParseError as exc:
            raise SystemExit(render_parse_error(exc)) from None

    return wrapper


def build_argument_parser(
    *,
    prog: str,
    description: str | None = None,
    epilog: str | None = None,
    usage: str | None = None,
    add_help: bool = True,
    formatter_class: type[argparse.HelpFormatter] | None = None,
    allow_abbrev: bool = True,
) -> argparse.ArgumentParser:
    """Build a :class:`RebarArgumentParser` bound to ``prog``.

    This is itself a conforming factory: ``prog`` is keyword-only, matching the
    ``build_parser(*, prog: str) -> ArgumentParser`` protocol shape.

    The presentation passthroughs are additive (S2c). ``usage``/``add_help``/
    ``allow_abbrev`` are forwarded to argparse unchanged; ``formatter_class`` defaults
    to :class:`RebarHelpFormatter` when ``None`` and is otherwise passed through so a
    family that carries a ``RawDescriptionHelpFormatter`` (or argparse's default
    :class:`argparse.HelpFormatter`) keeps its exact help rendering.
    """

    return RebarArgumentParser(
        prog=prog,
        description=description,
        epilog=epilog,
        usage=usage,
        add_help=add_help,
        allow_abbrev=allow_abbrev,
        formatter_class=formatter_class or RebarHelpFormatter,
    )


def compose(
    define: Callable[[argparse.ArgumentParser], None],
    *,
    prog: str,
    description: str | None = None,
    epilog: str | None = None,
    usage: str | None = None,
    add_help: bool = True,
    formatter_class: type[argparse.HelpFormatter] | None = None,
) -> argparse.ArgumentParser:
    """Compose a parser from a shared argument-definition function.

    ``define`` receives a fresh parser and adds the shared arguments. Varying
    ``prog``/``epilog`` (and the S2c presentation passthroughs) while reusing ONE
    ``define`` lets a canonical spelling and an alias/compat spelling share an
    identical option surface with no option copying — the composed parsers parse
    argv identically.
    """

    parser = build_argument_parser(
        prog=prog,
        description=description,
        epilog=epilog,
        usage=usage,
        add_help=add_help,
        formatter_class=formatter_class,
    )
    define(parser)
    return parser
