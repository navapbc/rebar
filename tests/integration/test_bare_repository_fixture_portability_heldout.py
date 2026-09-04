"""Held-out oracle for bare-repository fixture portability, and the sweep it runs.

Policy [rebar:3476-4472-3407-4246, rebar:740d-187c-53a2-4b7d]: git refuses to
*discover* a bare repository through ``-C``/cwd when a developer's config sets
``safe.bareRepository = explicit`` (git 2.38.0+). It accepts the same repository
named by a top-level ``--git-dir``. A fixture that reads a bare remote with
``git -C <bare>`` therefore dies with exit 128 -- a refusal to open the
repository at all, *before* any ref lookup -- for every such developer, while CI
(which uses the ``all`` default) stays green. The supported test suite must run
both locally and in CI, so this is a defect in the test, not environmental
noise.

**Why the coverage is a tree sweep and not a list.** Ticket 3476 fixed 13 files
and left this file behind as the guard, pinning a HAND-MAINTAINED allowlist of
three test node ids that it re-ran under a strict git config. Four days later
``tests/interfaces/lifecycle/test_atomic_completion_close.py`` landed,
reintroduced the construct at eight sites, and nothing failed: the allowlist
named three files that were ALREADY FIXED. Coverage that must be hand-registered
is coverage a new file escapes by default. So the allowlist is gone and
:func:`sweep_tests_tree` derives the covered set from the tree instead -- every
``tests/**/*.py`` module, with a new bare-repository fixture in scope the moment
it is written.

**Why the sweep lives HERE and not in a ``scripts/check_*.py``.** That is this
repository's usual shape for a static sweep, and it was the intended home. It
cannot be: ``scripts/_mechanism_delta/detect_ci.py`` names every
``scripts/check_*.py`` as a ``ci_gate`` BY ITS PATH, so any new gate script is a
new mechanism -- and while an inline ``# mechanism-ok:`` marker admits one at
``check_mechanism_delta.py --check``, ``tests/unit/test_mechanism_delta.py``
additionally asserts ``new=0`` and ``set(baseline) == live`` REGARDLESS of
markers, which only an operator running ``--lock`` can satisfy. Repairing the
guard that already failed, in place, adds no mechanism at all, and is the
narrower change besides: this file exists to enforce exactly this rule, and the
suite CI runs is a portable trigger that needs no CI provider of its own.

Detection, per module, using only that module's own AST.

**Bare names** -- an identifier a ``--bare`` invocation binds:

- the last path operand after the flag. ``git init --bare <dir>`` and
  ``git clone --bare <src> <dst>`` both bind the repository they CREATE, never
  an earlier source;
- the directory the call runs in when the flag takes no operand, the
  ``_git(remote, "init", "-q", "--bare")`` shape the already-fixed integration
  fixtures use;
- a name bound by a bare-repository FACTORY: a ``bare``-named creator taking
  only paths (``init_bare_remote(remote)``), or a same-module helper that
  creates a bare repository AND RETURNS IT. Returning it is the load-bearing
  half -- a fixture helper that creates a bare remote and returns the WORKTREE
  that pushes to it is the common shape, and marking its result bare would be
  plainly wrong. The return is read POSITIONALLY (``return remote, writer``
  marks only the first element) and the mark lands on the CALLER's binding, so
  a rename (``origin, scribe = _ticket_remote(...)``) is still covered;
- the argument passed at a PARAMETER POSITION the callee marks bare in its own
  body, which covers ``_publish_ticket_store(repo, remote)`` by dataflow rather
  than by the caller happening to spell the name the same way.

Marking is otherwise module-scoped by identifier, which covers the same flows a
second way when the caller reuses the helper's own name.

**Discovery helpers** -- a function that forwards its first parameter to a
``-C`` flag, the shape every one of these test modules uses:
``def _git(repo, *args): subprocess.run(["git", "-C", str(repo), *args])``.

**Violation** -- a ``-C`` flag applied to a bare name, directly or through a
discovery helper. Two calls are exempt: one carrying an inline
``-c safe.bareRepository=all``, the deliberate opt-in ticket 3476 preserved in
``tests/scripts/test_reconcile_bridge_push_retry.py``; and the CREATING call,
since ``git -C <dir> init --bare`` runs before ``<dir>`` is a repository and so
has nothing for git to refuse to discover.
"""

from __future__ import annotations

import ast
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = "tests"

# ``str``/``Path`` wrappers stand for the path they wrap, so unwrap them before
# deciding which identifier a git argument addresses.
_UNWRAPPERS = frozenset({"str", "Path", "PurePath", "fspath"})
_CREATION_VERBS = ("init", "create", "make", "new", "build")
_ESCAPE = "safe.bareRepository=all"

# This module's own git-behaviour pin deliberately runs ``git -C <bare>`` to
# PROVE git refuses it, so its fixture corpus is a live instance of the banned
# construct. It is excluded structurally -- the check_wall_clock_asserts.py /
# check_comment_hygiene.py idiom -- rather than by a marker the rule would then
# have to define.
EXCLUDED_FILES = (Path(__file__).resolve().relative_to(_REPO_ROOT),)

_TEACHING = """\
git refuses to DISCOVER a bare repository via -C when a developer sets
safe.bareRepository=explicit, so these call sites fail with exit 128 locally
while CI (which uses the 'all' default) stays green. Address the bare
repository explicitly instead:

    def _bare_git(repo: Path, *args: str) -> str:
        # a bare repo must be NAMED by --git-dir: git refuses to discover one
        # via -C under safe.bareRepository=explicit
        proc = subprocess.run(["git", "--git-dir", str(repo), *args], ...)

and route only the bare-repository call sites through it -- the -C helper is
still correct for the non-bare worktrees beside them."""


@dataclass(frozen=True)
class Finding:
    """One ``-C`` invocation aimed at a bare repository."""

    path: Path
    line: int
    name: str
    via: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: bare repository '{self.name}' addressed via {self.via}"


def _argv(call: ast.Call) -> list[ast.expr]:
    """The call's arguments, with list/tuple argv literals spliced in."""
    flat: list[ast.expr] = []
    for arg in call.args:
        if isinstance(arg, ast.List | ast.Tuple):
            flat.extend(arg.elts)
        else:
            flat.append(arg)
    return flat


def _literal(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _addressed(node: ast.expr) -> str | None:
    """The identifier a git argument addresses, unwrapping ``str``/``Path``."""
    while isinstance(node, ast.Call):
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name not in _UNWRAPPERS or not node.args:
            return None
        node = node.args[0]
    return node.id if isinstance(node, ast.Name) else None


def _called_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else None


def _flag_target(call: ast.Call, flag: str) -> str | None:
    """The identifier ``flag`` addresses in this call, if any."""
    argv = _argv(call)
    for index, element in enumerate(argv[:-1]):
        if _literal(element) == flag:
            return _addressed(argv[index + 1])
    return None


def _is_escaped(call: ast.Call) -> bool:
    """The call opts in deliberately with ``-c safe.bareRepository=all``."""
    return any(_literal(element) == _ESCAPE for element in _argv(call))


def _bare_created_by(call: ast.Call) -> str | None:
    """The identifier this call binds to a bare repository, if it creates one."""
    argv = _argv(call)
    flag = next((i for i, element in enumerate(argv) if _literal(element) == "--bare"), None)
    if flag is None:
        return None
    named = [_addressed(element) for element in argv[flag + 1 :]]
    named = [name for name in named if name is not None]
    if named:
        return named[-1]
    # No operand, so the bare repository is the directory the call runs in.
    return _addressed(argv[0]) if argv else None


def _creates_bare(call: ast.Call) -> bool:
    """A ``bare``-named creator taking only paths -- ``init_bare_remote(remote)``.

    A bare-repository RUNNER (``_bare_git(remote, "rev-parse", ...)``) always
    carries a git subcommand string, and an INSPECTOR
    (``unpinned_bare_repositories(template)``) never names a creation verb, so
    neither is mistaken for a factory.
    """
    called = (_called_name(call) or "").lower()
    if "bare" not in called or not any(verb in called for verb in _CREATION_VERBS):
        return False
    return all(_literal(argument) is None for argument in _argv(call))


def _created_in(node: ast.AST) -> set[str]:
    """Every identifier a ``--bare`` invocation binds inside this subtree."""
    created = {_bare_created_by(sub) for sub in ast.walk(node) if isinstance(sub, ast.Call)}
    created.discard(None)
    return {name for name in created if name is not None}


def _returned_positions(node: ast.FunctionDef | ast.AsyncFunctionDef) -> frozenset[int]:
    """Which values this function returns are bare repositories it created.

    ``-1`` marks a scalar return; ``0``, ``1``, ... mark tuple positions.
    """
    created = _created_in(node)
    positions: set[int] = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Return) or sub.value is None:
            continue
        if isinstance(sub.value, ast.Tuple):
            positions |= {
                index
                for index, element in enumerate(sub.value.elts)
                if _addressed(element) in created
            }
        elif _addressed(sub.value) in created:
            positions.add(-1)
    return frozenset(positions)


def _parameter_positions(node: ast.FunctionDef | ast.AsyncFunctionDef) -> frozenset[int]:
    """Which of this function's parameters its own body creates a bare repository at."""
    created = _created_in(node)
    return frozenset(
        index for index, parameter in enumerate(node.args.args) if parameter.arg in created
    )


@dataclass(frozen=True)
class _Helper:
    """What calling a helper binds to a bare repository."""

    returns: frozenset[int]
    parameters: frozenset[int]


def _helpers(tree: ast.Module) -> dict[str, _Helper]:
    """Functions whose call binds a bare repository, by return and parameter position."""
    helpers: dict[str, _Helper] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        helper = _Helper(_returned_positions(node), _parameter_positions(node))
        if helper.returns or helper.parameters:
            helpers[node.name] = helper
    return helpers


def _bound_at(parent: ast.Assign | None, positions: frozenset[int]) -> set[str]:
    """The caller-side names an assignment binds to the given return positions.

    Read from the ASSIGNMENT, not from the helper's own identifier, so a caller
    may bind the result under any name and still be covered.
    """
    if parent is None:
        return set()
    names: set[str] = set()
    for target in parent.targets:
        if isinstance(target, ast.Name) and -1 in positions:
            names.add(target.id)
        elif isinstance(target, ast.Tuple):
            names |= {
                element.id
                for index, element in enumerate(target.elts)
                if isinstance(element, ast.Name) and index in positions
            }
    return names


def _passed_at(call: ast.Call, positions: frozenset[int]) -> set[str]:
    """The identifiers this call passes at the given parameter positions."""
    addressed = (
        _addressed(argument) for index, argument in enumerate(call.args) if index in positions
    )
    return {name for name in addressed if name is not None}


def _factory_names(
    node: ast.Call, parent: ast.Assign | None, helpers: dict[str, _Helper]
) -> set[str]:
    """Names a bare-repository factory call binds."""
    helper = helpers.get(_called_name(node) or "")
    if helper is not None:
        return _bound_at(parent, helper.returns) | _passed_at(node, helper.parameters)
    if not _creates_bare(node):
        return set()
    addressed = _addressed(node.args[0]) if node.args else None
    return _bound_at(parent, frozenset({-1})) | ({addressed} if addressed else set())


def _bare_names(tree: ast.Module, helpers: dict[str, _Helper]) -> set[str]:
    """Identifiers bound to a bare repository anywhere in this module."""
    assigned = {
        id(node.value): node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
    }
    bare: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        created = _bare_created_by(node)
        if created is not None:
            bare.add(created)
        bare |= _factory_names(node, assigned.get(id(node)), helpers)
    return bare


def _discovery_helpers(tree: ast.Module) -> set[str]:
    """Functions that forward their first parameter to a ``-C`` flag."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not node.args.args:
            continue
        first = node.args.args[0].arg
        if any(
            isinstance(sub, ast.Call) and _flag_target(sub, "-C") == first for sub in ast.walk(node)
        ):
            names.add(node.name)
    return names


def scan_module(path: Path, source: str) -> list[Finding]:
    """Every ``-C`` invocation in ``source`` that targets a bare repository."""
    tree = ast.parse(source)
    helpers = _helpers(tree)
    bare = _bare_names(tree, helpers)
    if not bare:
        return []
    discovery = _discovery_helpers(tree)
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _is_escaped(node) or _bare_created_by(node) is not None:
            continue
        if _flag_target(node, "-C") in bare:
            findings.append(Finding(path, node.lineno, str(_flag_target(node, "-C")), "a -C flag"))
            continue
        called = _called_name(node)
        addressed = _addressed(node.args[0]) if node.args else None
        if called in discovery and addressed in bare:
            findings.append(Finding(path, node.lineno, str(addressed), f"{called}()"))
    return sorted(findings, key=lambda finding: finding.line)


_OFFENDER = (
    "import subprocess\n"
    "def _git(repo, *args):\n"
    '    return subprocess.run(["git", "-C", str(repo), *args], check=True)\n'
    "def test_reads_remote(tmp_path):\n"
    '    remote = tmp_path / "remote.git"\n'
    '    _git(tmp_path, "init", "--bare", str(remote))\n'
    '    _git(remote, "show-ref")\n'
    '    _git(remote.parent, "status")\n'
)


def sweep_tests_tree(root: Path = _REPO_ROOT) -> list[Finding]:
    """Sweep every ``tests/**/*.py`` module under ``root``."""
    findings: list[Finding] = []
    for path in sorted((root / TESTS_DIR).rglob("*.py")):
        relative = path.relative_to(root)
        if relative in EXCLUDED_FILES:
            continue
        findings.extend(scan_module(relative, path.read_text(encoding="utf-8")))
    return findings


def test_no_test_addresses_a_bare_repository_by_discovery() -> None:
    """Coverage follows the tree: every ``tests/**/*.py`` module is in scope."""
    findings = sweep_tests_tree()

    assert not findings, "\n".join(["", *(str(f) for f in findings), "", _TEACHING])


def test_the_sweep_detects_a_reintroduced_discovery_site() -> None:
    """The guard has teeth: a fresh bare-fixture module in the banned shape fails."""
    fixed = _OFFENDER.replace('_git(remote, "show-ref")', '_bare_git(remote, "show-ref")')

    findings = scan_module(Path("tests/test_offender.py"), _OFFENDER)

    assert [(f.line, f.name, f.via) for f in findings] == [(7, "remote", "_git()")]
    assert scan_module(Path("tests/test_offender.py"), fixed) == []


def test_the_sweep_follows_a_returned_bare_remote_through_a_rename() -> None:
    """A caller may bind a fixture's bare remote under any name and still be caught."""
    module = (
        "import subprocess\n"
        "def _git(repo, *args):\n"
        '    return subprocess.run(["git", "-C", str(repo), *args], check=True)\n'
        "def _ticket_remote(tmp_path):\n"
        '    remote = tmp_path / "remote.git"\n'
        '    writer = tmp_path / "writer"\n'
        '    _git(tmp_path, "init", "--bare", str(remote))\n'
        "    return remote, writer\n"
        "def test_reads_remote(tmp_path):\n"
        "    origin, scribe = _ticket_remote(tmp_path)\n"
        '    _git(origin, "show-ref")\n'
        '    _git(scribe, "status")\n'
    )

    findings = scan_module(Path("tests/test_renamed.py"), module)

    # Only the returned BARE element is flagged; the worktree beside it is not.
    assert [(f.line, f.name) for f in findings] == [(11, "origin")]


def test_the_sweep_follows_a_bare_repository_passed_into_a_fixture() -> None:
    """A parameter the callee creates a bare repository at marks the caller's argument."""
    module = (
        "import subprocess\n"
        "def _git(repo, *args):\n"
        '    return subprocess.run(["git", "-C", str(repo), *args], check=True)\n'
        "def _publish(repo, target):\n"
        '    _git(target.parent, "init", "--bare", str(target))\n'
        "def test_reads_remote(tmp_path, project):\n"
        '    somewhere = tmp_path / "somewhere.git"\n'
        "    _publish(project, somewhere)\n"
        '    _git(somewhere, "show-ref")\n'
    )

    findings = scan_module(Path("tests/test_passed.py"), module)

    assert [(f.line, f.name) for f in findings] == [(9, "somewhere")]


def test_the_sweep_honours_the_inline_safe_bare_repository_opt_in() -> None:
    """An explicit ``-c safe.bareRepository=all`` is a deliberate escape, not a defect."""
    module = (
        "import subprocess\n"
        "def _git(repo, *args):\n"
        '    return subprocess.run(["git", "-C", str(repo), *args], check=True)\n'
        "def test_reads_remote(tmp_path):\n"
        '    remote = tmp_path / "remote.git"\n'
        '    _git(tmp_path, "init", "--bare", str(remote))\n'
        '    _git(remote, "-c", "safe.bareRepository=all", "rev-parse", "HEAD")\n'
    )

    assert scan_module(Path("tests/test_escaped.py"), module) == []


def test_git_refuses_bare_discovery_but_accepts_an_explicit_git_dir(tmp_path: Path) -> None:
    """Pin the git behaviour the sweep encodes, so the rule cannot rot silently."""
    strict_config = tmp_path / "strict-gitconfig"
    subprocess.run(
        ["git", "config", "--file", str(strict_config), "safe.bareRepository", "explicit"],
        check=True,
        capture_output=True,
        text=True,
    )
    environment = subprocess_env(
        GIT_CONFIG_GLOBAL=str(strict_config),
        GIT_CONFIG_NOSYSTEM="1",
    )
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "--quiet", str(bare)],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    def run(*argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *argv], env=environment, capture_output=True, text=True, check=False
        )

    discovered = run("-C", str(bare), "rev-parse", "--is-bare-repository")
    explicit = run("--git-dir", str(bare), "rev-parse", "--is-bare-repository")
    missing = run("--git-dir", str(tmp_path / "missing.git"), "rev-parse", "HEAD")

    assert discovered.returncode == 128
    assert "safe.bareRepository" in discovered.stderr
    assert explicit.returncode == 0
    assert explicit.stdout.strip() == "true"
    assert missing.returncode == 128
    assert "not a git repository" in missing.stderr
