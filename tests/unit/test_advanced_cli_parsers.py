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

from rebar._cli import _parser
from rebar._cli._parser import ParseError, RebarArgumentParser


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
