"""Happy-path oracle for RP-05 S2c advanced-command parser factories.

These tests pin the *shape* of the migration: the S2a factory contract is
extended with the optional presentation passthroughs the advanced families
need, every advanced command family exposes a lean ``build(*, prog=...)``
parser factory that constructs a :class:`RebarArgumentParser`, nested families
still select their subcommand, and constructing any advanced parser imports no
heavy optional runtime.

Edge/parity behavior (byte-exact help, the ParseError-vs-SystemExit exit-code
seam, alias option-surface sharing, and full registry factory-reference
readiness) lives in the held-out interface oracle, not here.
"""

from __future__ import annotations

import argparse
import importlib
import subprocess
import sys

import pytest

from rebar._cli import _parser
from rebar._cli._parser import (
    ParseError,
    RebarArgumentParser,
    guard_parse_errors,
    render_parse_error,
)

# --- The parse-error seam: render_parse_error + guard_parse_errors --------
#
# These two functions are the central mechanism the whole migration relies on
# to keep parse-failure behavior byte-identical to argparse's native
# exit-2-with-usage contract. They are exercised indirectly by the held-out
# byte-parity oracle; these pin their observable contract directly so a
# regression that drops the usage line or the exit code can never pass.


def test_render_parse_error_reproduces_argparse_stderr_and_exit_code(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """It writes the usage block then ``<prog>: error: <msg>`` and returns 2."""

    exc = ParseError("argument x: invalid choice", prog="rebar demo", usage="usage: rebar demo x\n")

    code = render_parse_error(exc)

    assert code == 2
    err = capsys.readouterr().err
    assert err == "usage: rebar demo x\nrebar demo: error: argument x: invalid choice\n"


def test_render_parse_error_without_usage_emits_only_the_error_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A hand-built ParseError (``usage=None``) still returns 2 and names the error."""

    code = render_parse_error(ParseError("boom", prog="rebar demo"))

    assert code == 2
    assert capsys.readouterr().err == "rebar demo: error: boom\n"


def test_guard_parse_errors_translates_parse_error_to_systemexit_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A wrapped handler that raises ParseError exits 2 with usage+error on stderr."""

    @guard_parse_errors
    def handler() -> int:
        raise ParseError("bad", prog="rebar demo", usage="usage: rebar demo\n")

    with pytest.raises(SystemExit) as excinfo:
        handler()

    assert excinfo.value.code == 2
    assert capsys.readouterr().err == "usage: rebar demo\nrebar demo: error: bad\n"


def test_guard_parse_errors_lets_help_systemexit_propagate_unchanged() -> None:
    """A ``--help``-style ``SystemExit(0)`` passes through the guard untouched."""

    @guard_parse_errors
    def handler() -> int:
        raise SystemExit(0)

    with pytest.raises(SystemExit) as excinfo:
        handler()

    assert excinfo.value.code == 0


def test_guard_parse_errors_returns_handler_value_on_success() -> None:
    """A successful handler's return value is passed through unchanged."""

    @guard_parse_errors
    def handler() -> int:
        return 7

    assert handler() == 7


# --- Migrated reject path routes through the factory (AC1 de-duplication) --


def test_audit_cli_unknown_subcommand_exits_2_through_the_factory(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The de-duplicated audit handler rejects unknown argv via the shared factory.

    Post-migration, audit's rejected-argv path is the ONE argparse grammar
    (AC1): an unknown subcommand raises ParseError and is rendered as argparse's
    exit-2 usage/error, not a bespoke hand-rolled banner.
    """

    from rebar._cli._audit_commands import audit_cli

    code = audit_cli(["definitely-not-a-subcommand"])

    assert code == 2
    err = capsys.readouterr().err
    assert err.startswith("usage: rebar audit")
    assert "rebar audit: error:" in err


def _resolve(ref: str):
    """Resolve a ``module:attr`` parser-factory reference to the callable."""

    module_name, _, attr = ref.partition(":")
    module = importlib.import_module(module_name)
    return getattr(module, attr)


# --- S2a factory contract extension (finding [3]) -------------------------


def test_build_argument_parser_accepts_presentation_passthroughs() -> None:
    """The lean factory grows optional ``usage``/``add_help``/``formatter_class``.

    Advanced families that carry ``usage=``, ``add_help=False``, or a
    ``RawDescriptionHelpFormatter`` need those preserved. The extension is
    additive: the parser is still a raises-not-exits :class:`RebarArgumentParser`.
    """

    parser = _parser.build_argument_parser(
        prog="rebar demo",
        usage="rebar demo <thing>",
        add_help=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    assert isinstance(parser, RebarArgumentParser)
    assert parser.prog == "rebar demo"
    assert parser.format_usage().startswith("usage: rebar demo <thing>")
    # add_help=False means no -h/--help option was registered.
    assert not any("-h" in action.option_strings for action in parser._actions)


def test_build_argument_parser_defaults_unchanged() -> None:
    """Omitting the new kwargs preserves today's S2a behavior exactly."""

    parser = _parser.build_argument_parser(prog="rebar demo")

    assert isinstance(parser, RebarArgumentParser)
    assert parser.formatter_class is _parser.RebarHelpFormatter
    # add_help defaults True: -h/--help present.
    assert any("-h" in action.option_strings for action in parser._actions)
    # error() still raises instead of exiting.
    try:
        parser.parse_args(["--nonexistent"])
    except ParseError:
        pass
    else:  # pragma: no cover - guards the raises-not-exits contract
        raise AssertionError("expected ParseError")


# --- Advanced family factories build prog-bound RebarArgumentParsers ------


def test_bridge_factory_builds_prog_bound_nested_parser() -> None:
    """The bridge family factory builds its nested parser bound to ``prog``."""

    build = _resolve("rebar._cli._parsers.advanced.bridge:build")
    parser = build(prog="rebar bridge")

    assert isinstance(parser, RebarArgumentParser)
    assert parser.prog == "rebar bridge"
    ns = parser.parse_args(["preview"])
    assert ns.command == "preview"


def test_workflow_factory_nested_selection() -> None:
    """The workflow family factory still selects its nested subcommand."""

    build = _resolve("rebar._cli._parsers.advanced.workflow:build")
    parser = build(prog="rebar workflow")

    assert isinstance(parser, RebarArgumentParser)
    assert parser.prog == "rebar workflow"
    ns = parser.parse_args(["validate", "some-workflow"])
    assert ns.cmd == "validate"


def test_review_plan_factory_builds_prog_bound_parser() -> None:
    """A flat llm-command factory builds a prog-bound parser too."""

    build = _resolve("rebar._cli._parsers.advanced.llm:build_review_plan")
    parser = build(prog="rebar review-plan")

    assert isinstance(parser, RebarArgumentParser)
    assert parser.prog == "rebar review-plan"


# --- AC3: parser construction imports no heavy optional runtime -----------


def test_advanced_parser_package_imports_no_heavy_optional_modules() -> None:
    """Importing + building every advanced parser pulls in no heavy runtime.

    Runs in a FRESH subprocess (never mutating this interpreter's
    ``sys.modules``) with the heavy optional modules poisoned so that *any*
    import of them raises. Building parsers must not touch them; only handler
    execution (out of scope here, and S4 for capability enforcement) may.
    """

    forbidden = [
        "pydantic_ai",
        "fastapi",
        "uvicorn",
        "starlette",
        "jinja2",
        "rebar_reconciler",
    ]
    code = f"""
import sys, importlib
_forbidden = {forbidden!r}
class _Poison:
    def __init__(self, name): self._name = name
    def find_spec(self, name, path=None, target=None):
        base = name.split('.')[0]
        if base in _forbidden or name in _forbidden:
            raise AssertionError('advanced parser construction imported ' + name)
        return None
sys.meta_path.insert(0, _Poison('poison'))
pkg = importlib.import_module('rebar._cli._parsers.advanced')
from rebar._cli import _registry
built = 0
for route in _registry.ROUTES:
    ref = route.parser_factory
    if not ref:
        continue
    mod_name, _, attr = ref.partition(':')
    mod = importlib.import_module(mod_name)
    parser = getattr(mod, attr)(prog='rebar ' + route.name)
    assert parser.prog == 'rebar ' + route.name, route.name
    built += 1
assert built > 0, 'no advanced routes carried a parser_factory'
print('OK', built)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"advanced parser import probe failed:\n{result.stdout}\n{result.stderr}"
    )
    assert result.stdout.startswith("OK "), result.stdout
