"""Happy-path oracle for RP-05 S2b core-command parser factories.

This pins the *shape* of the migration the implementer builds to; the byte-exact
behavioral/parity surface (per-family unknown-token semantics, help text,
diagnostics, exit codes) lives in the held-out interface oracle, not here.

The contract:

* Every registered **core** command Route (everything that is not an advanced
  ``intercept`` family and not the already-migrated ``metrics``) carries a
  ``parser_factory`` reference after S2b — no core Route is left with the
  imperative-only grammar RP-05 exists to remove.
* Each referenced factory is a lean ``callable(*, prog=...)`` that constructs a
  :class:`RebarArgumentParser` bound to ``prog`` (the S2a contract).
* A representative valid argv parses without error through the built parser.
* Constructing the core parser package imports no heavy optional runtime.
* **The command's runtime handler actually parses argv THROUGH its factory** —
  the factory is not dead declaration: a real invocation of the command routes
  through it (the metrics/audit precedent, AC1 de-duplication + AC4 isolation).
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

import rebar
from rebar._cli import _registry
from rebar._cli import main as cli_main
from rebar._cli._parser import ParseError, RebarArgumentParser

# The 49 core commands S2b migrates. ``metrics`` is intentionally excluded — its
# Route already carries an advanced ``parser_factory`` from S2c and re-migrating
# it is forbidden by this ticket.
CORE_COMMANDS = frozenset(
    {
        "show",
        "list",
        "next-batch",
        "deps",
        "ready",
        "search",
        "session-logs",
        "validate",
        "get-file-impact",
        "get-verify-commands",
        "exists",
        "resolve",
        "format",
        "list-descendants",
        "clarity-check",
        "check-ac",
        "quality-check",
        "summary",
        "sign",
        "verify-signature",
        "compact",
        "compact-all",
        "export",
        "import",
        "transition",
        "reopen",
        "claim",
        "create",
        "idea",
        "comment",
        "link",
        "unlink",
        "revert",
        "edit",
        "tag",
        "untag",
        "archive",
        "set-file-impact",
        "set-verify-commands",
        "attach-commits",
        "session-log",
        "init",
        "scratch",
        "delete",
        "fsck",
        "fsck-recover",
        "tracker-maintenance",
        "doctor",
        "grounding-info",
    }
)


def _core_routes() -> list[_registry.Route]:
    return [r for r in _registry.ROUTES if r.name in CORE_COMMANDS]


def _resolve(ref: str):
    module_name, _, attr = ref.partition(":")
    module = importlib.import_module(module_name)
    return getattr(module, attr)


def test_the_core_inventory_is_the_expected_49_commands() -> None:
    """Guards the census: exactly these 49 core commands are S2b's scope."""

    assert len(CORE_COMMANDS) == 49
    present = {r.name for r in _registry.ROUTES}
    assert CORE_COMMANDS <= present, CORE_COMMANDS - present
    # metrics is migrated already and must NOT be in scope here.
    assert "metrics" not in CORE_COMMANDS


def test_every_core_route_carries_a_parser_factory() -> None:
    """AC1 completeness: no core command is left with an imperative-only grammar."""

    unmigrated = sorted(r.name for r in _core_routes() if not r.parser_factory)
    assert unmigrated == [], f"core commands still lacking a parser_factory: {unmigrated}"


def test_core_parser_factories_live_in_the_core_package() -> None:
    """Core factories live under ``rebar._cli._parsers.core`` (not advanced)."""

    for route in _core_routes():
        ref = route.parser_factory
        assert ref, route.name
        module_name = ref.partition(":")[0]
        assert module_name.startswith("rebar._cli._parsers.core"), (route.name, ref)


@pytest.mark.parametrize("route", _core_routes(), ids=lambda r: r.name)
def test_core_factory_builds_prog_bound_parser(route: _registry.Route) -> None:
    """Every core factory builds a ``prog``-bound :class:`RebarArgumentParser`."""

    build = _resolve(route.parser_factory)
    prog = f"rebar {route.name}"
    parser = build(prog=prog)
    assert isinstance(parser, RebarArgumentParser), route.name
    assert parser.prog == prog, route.name


# A few representative valid argvs parse cleanly through the built parser. These
# assert only that a well-formed invocation is *accepted* (no ParseError) — the
# byte-exact accepted/rejected matrices are the held-out oracle's job, so this
# stays free of internal dest-name coupling.
_ACCEPTS = [
    ("show", ["some-ticket"]),
    ("list", []),
    ("exists", ["some-ticket"]),
    ("resolve", ["some-ticket"]),
    ("comment", ["some-ticket", "a note"]),
    ("create", ["task", "a title"]),
    ("tag", ["some-ticket", "atag"]),
]


@pytest.mark.parametrize("name,argv", _ACCEPTS, ids=[a[0] for a in _ACCEPTS])
def test_representative_valid_argv_is_accepted(name: str, argv: list[str]) -> None:
    """A canonical well-formed invocation parses without raising ParseError."""

    route = next(r for r in _registry.ROUTES if r.name == name)
    parser = _resolve(route.parser_factory)(prog=f"rebar {name}")
    try:
        parser.parse_args(argv)
    except ParseError as exc:  # pragma: no cover - failure path
        raise AssertionError(f"{name} rejected valid argv {argv}: {exc}") from exc


def test_core_parser_package_imports_no_heavy_optional_modules() -> None:
    """Importing + building every core parser pulls in no heavy runtime.

    Runs in a fresh subprocess with heavy optional modules poisoned so any
    import of them raises. Building parsers must not touch them; only handler
    execution (out of scope here) may.
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
    def find_spec(self, name, path=None, target=None):
        base = name.split('.')[0]
        if base in _forbidden or name in _forbidden:
            raise AssertionError('core parser construction imported ' + name)
        return None
sys.meta_path.insert(0, _Poison())
importlib.import_module('rebar._cli._parsers.core')
from rebar._cli import _registry
core = {sorted(CORE_COMMANDS)!r}
built = 0
for route in _registry.ROUTES:
    if route.name not in core:
        continue
    ref = route.parser_factory
    assert ref, route.name
    mod_name, _, attr = ref.partition(':')
    mod = importlib.import_module(mod_name)
    parser = getattr(mod, attr)(prog='rebar ' + route.name)
    assert parser.prog == 'rebar ' + route.name, route.name
    built += 1
assert built == 49, built
print('OK', built)
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, (
        f"core parser import probe failed:\n{result.stdout}\n{result.stderr}"
    )
    assert result.stdout.startswith("OK "), result.stdout


# --- AC1/AC4: the handler actually parses THROUGH its factory --------------
#
# The whole point of the migration is that each core command's runtime parses
# via its S2a factory (the metrics/audit precedent: the handler does
# ``build(prog=...).parse_args(argv)``). A factory that merely exists as a
# ``Route.parser_factory`` declaration while the handler keeps parsing argv
# imperatively is EXACTLY the duplicate grammar RP-05 exists to remove. These
# spy on the declared factory and prove a real command invocation calls it.


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real initialized rebar store bound as ``REBAR_ROOT`` for in-process main()."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "t@t.co"),
        ("config", "user.name", "t"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return repo


def _spy_factory(monkeypatch: pytest.MonkeyPatch, name: str) -> list[str]:
    """Patch command ``name``'s declared factory with a recording delegate.

    The handler resolves the factory by a function-local ``from <mod> import
    <attr>`` (the metrics/audit pattern), so patching the defining module's
    attribute is observed at call time. Returns the list the spy appends ``prog``
    to on each construction.
    """
    route = next(r for r in _registry.ROUTES if r.name == name)
    assert route.parser_factory, name
    mod_name, _, attr = route.parser_factory.partition(":")
    module = importlib.import_module(mod_name)
    original = getattr(module, attr)
    seen: list[str] = []

    def spy(*, prog: str):
        seen.append(prog)
        return original(prog=prog)

    monkeypatch.setattr(module, attr, spy)
    return seen


def test_list_runtime_parses_through_its_factory(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _spy_factory(monkeypatch, "list")
    cli_main(["list"])
    assert seen == ["rebar list"], "rebar list did not parse through its core factory"


def test_show_runtime_parses_through_its_factory(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = rebar.create_ticket("task", "T", repo_root=str(store))
    seen = _spy_factory(monkeypatch, "show")
    cli_main(["show", tid])
    assert seen == ["rebar show"], "rebar show did not parse through its core factory"


def test_comment_runtime_parses_through_its_factory(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = rebar.create_ticket("task", "T", repo_root=str(store))
    seen = _spy_factory(monkeypatch, "comment")
    cli_main(["comment", tid, "a note"])
    assert seen == ["rebar comment"], "rebar comment did not parse through its core factory"


def test_transition_runtime_parses_through_its_factory(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = rebar.create_ticket("task", "T", repo_root=str(store))
    seen = _spy_factory(monkeypatch, "transition")
    cli_main(["transition", tid, "open", "in_progress"])
    assert seen == ["rebar transition"], "rebar transition did not parse through its core factory"


def test_summary_runtime_parses_through_its_factory(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = rebar.create_ticket("task", "T", repo_root=str(store))
    seen = _spy_factory(monkeypatch, "summary")
    cli_main(["summary", tid])
    assert seen == ["rebar summary"], "rebar summary did not parse through its core factory"


# --- AC1 governance: the factory GOVERNS, it is not called-and-discarded -----
#
# A handler that runs its factory purely to satisfy the isolation spy but then
# parses argv imperatively (discarding the factory's namespace) leaves the exact
# parallel imperative grammar RP-05 removes. To prove the factory is the parser
# of record, replace it with one that REJECTS the command's otherwise-valid argv
# and assert the invocation now fails: if the factory governs, the rejection
# propagates; if it is discarded, the command still succeeds (RED = theater).


def _rejecting_parser(prog: str) -> RebarArgumentParser:
    from rebar._cli._parser import build_argument_parser

    parser = build_argument_parser(prog=prog, add_help=False, allow_abbrev=False)
    parser.add_argument("--REJECT-EVERYTHING", required=True)
    return parser


def _install_rejecting_factory(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    route = next(r for r in _registry.ROUTES if r.name == name)
    mod_name, _, attr = route.parser_factory.partition(":")
    module = importlib.import_module(mod_name)
    monkeypatch.setattr(module, attr, lambda *, prog: _rejecting_parser(prog))


def _invoke_rc(argv: list[str]) -> int:
    try:
        return cli_main(argv)
    except SystemExit as exc:  # help/parse contract may exit
        return exc.code if isinstance(exc.code, int) else 1


_GOVERNANCE = [
    ("export", ["export"]),
    ("compact-all", ["compact-all"]),
    ("create", ["create", "task", "T"]),
    ("fsck", ["fsck"]),
    ("session-log", ["session-log", "append", "hi"]),
]


@pytest.mark.parametrize("name,argv", _GOVERNANCE, ids=[g[0] for g in _GOVERNANCE])
def test_factory_governs_execution_not_discarded(
    name: str, argv: list[str], store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a rejecting factory injected, the command must fail — proving the
    factory's parse governs execution rather than being run-and-discarded."""

    baseline = _invoke_rc(argv)
    assert baseline == 0, f"{name} valid argv should succeed pre-mutation, got {baseline}"

    _install_rejecting_factory(monkeypatch, name)
    mutated = _invoke_rc(argv)
    assert mutated != 0, (
        f"{name}: injecting a rejecting parser_factory did not change the outcome "
        f"(rc still {mutated}) — the factory is discarded, not the parser of record"
    )
