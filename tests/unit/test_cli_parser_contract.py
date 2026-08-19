"""Happy-path contract for the RP-05 S2a parser foundation (``rebar._cli._parser``).

Pins the observable byte contract of the shared argparse customization:

* one factory helper builds a parser bound to a deterministic ``prog``,
* help width is fixed and independent of the terminal (COLUMNS),
* help is UTF-8, LF-only, and ends in exactly one trailing newline, and
* the same parser object renders identical help bytes on repeat calls.

Non-terminating parse-error shape and alias/epilog composition live in the
held-out oracle.
"""

from __future__ import annotations

import argparse

import pytest

from rebar._cli import _parser


def _add_demo_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--alpha", help="the alpha option " + "x" * 40)
    parser.add_argument("--beta", help="the beta option " + "y" * 40)
    parser.add_argument("target", help="a positional " + "z" * 40)


def test_factory_builds_parser_bound_to_prog() -> None:
    from rebar._cli import _parser

    parser = _parser.build_argument_parser(prog="rebar demo")
    assert isinstance(parser, argparse.ArgumentParser)
    assert parser.prog == "rebar demo"


def test_help_width_is_fixed_independent_of_terminal(monkeypatch) -> None:
    from rebar._cli import _parser

    monkeypatch.setenv("COLUMNS", "1000")
    wide = _parser.build_argument_parser(prog="rebar demo")
    _add_demo_options(wide)
    wide_help = wide.format_help()

    monkeypatch.setenv("COLUMNS", "20")
    narrow = _parser.build_argument_parser(prog="rebar demo")
    _add_demo_options(narrow)
    narrow_help = narrow.format_help()

    assert wide_help == narrow_help


def test_help_is_lf_only_with_single_trailing_newline() -> None:
    from rebar._cli import _parser

    parser = _parser.build_argument_parser(prog="rebar demo")
    _add_demo_options(parser)
    text = parser.format_help()
    assert "\r" not in text
    text.encode("utf-8")  # must be valid UTF-8
    assert text.endswith("\n")
    assert not text.endswith("\n\n")


def test_help_is_deterministic_across_calls() -> None:
    from rebar._cli import _parser

    parser = _parser.build_argument_parser(prog="rebar demo")
    _add_demo_options(parser)
    assert parser.format_help() == parser.format_help()


def test_factory_protocol_signature_is_keyword_prog() -> None:
    # A conforming factory is build_parser(*, prog: str) -> ArgumentParser. The
    # shared helper is itself such a factory: prog is keyword-only.
    from rebar._cli import _parser

    parser = _parser.build_argument_parser(prog="rebar kw")
    assert parser.prog == "rebar kw"


# --- Held-out oracle (non-terminating ParseError + alias/epilog composition) ---
# Withheld from the S2a implementer; validated post-hoc by the orchestrator.
def _option_strings(parser: argparse.ArgumentParser) -> set[str]:
    out: set[str] = set()
    for action in parser._actions:
        out.update(action.option_strings)
    return out


def test_parse_error_raises_parseerror_not_systemexit() -> None:
    parser = _parser.build_argument_parser(prog="rebar demo")
    parser.add_argument("required_positional")
    # argparse's default would sys.exit(2); the Rebar customization must not terminate.
    with pytest.raises(_parser.ParseError):
        parser.parse_args([])


def test_parse_error_on_unknown_option_does_not_systemexit() -> None:
    parser = _parser.build_argument_parser(prog="rebar demo")
    # A terminating parser would sys.exit(2); the customization must raise
    # ParseError instead. ParseError is not a SystemExit subclass, so a
    # terminating parser would escape this pytest.raises and fail the test.
    with pytest.raises(_parser.ParseError):
        parser.parse_args(["--nope"])


def test_parse_error_carries_prog_and_message() -> None:
    parser = _parser.build_argument_parser(prog="rebar widget")
    parser.add_argument("required_positional")
    with pytest.raises(_parser.ParseError) as excinfo:
        parser.parse_args([])
    err = excinfo.value
    assert getattr(err, "prog", None) == "rebar widget"
    assert str(err)  # a non-empty diagnostic message


def test_successful_parse_still_returns_namespace() -> None:
    parser = _parser.build_argument_parser(prog="rebar demo")
    parser.add_argument("value")
    ns = parser.parse_args(["ok"])
    assert ns.value == "ok"


def test_compose_varies_prog_and_epilog_without_copying_options() -> None:
    def define(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--flag")
        parser.add_argument("--count")
        parser.add_argument("positional")

    canonical = _parser.compose(define, prog="rebar canonical")
    alias = _parser.compose(define, prog="rebar alias", epilog="Deprecated: use 'rebar canonical'.")

    assert _option_strings(canonical) == _option_strings(alias)
    assert canonical.prog == "rebar canonical"
    assert alias.prog == "rebar alias"
    assert "Deprecated: use 'rebar canonical'." in alias.format_help()
    assert "Deprecated" not in canonical.format_help()


def test_composed_alias_parses_identically_to_canonical() -> None:
    def define(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--flag")
        parser.add_argument("positional")

    canonical = _parser.compose(define, prog="rebar canonical")
    alias = _parser.compose(define, prog="rebar alias")
    argv = ["--flag", "v", "pos"]
    assert vars(canonical.parse_args(argv)) == vars(alias.parse_args(argv))
