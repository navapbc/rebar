#!/usr/bin/env python3
# mechanism-ok: ci_gate scripts/check_bare_repository_discovery.py — rebar:740d-187c-53a2-4b7d:
# the SECOND occurrence of this defect class. Ticket 3476 fixed 13 files and left a
# hand-registered 3-node allowlist behind; a file added four days later reintroduced the
# construct and nothing failed. Only a tree-wide sweep closes that, and this repository's
# sweep shape is a scripts/check_*.py wired into `make lint`.
"""Bare-repository discovery lint over ``tests/**``.

Policy [rebar:3476-4472-3407-4246, rebar:740d-187c-53a2-4b7d]: git refuses to
*discover* a bare repository through ``-C``/cwd when a developer's config sets
``safe.bareRepository = explicit`` (git 2.38.0+). It accepts the same repository
named by a top-level ``--git-dir``. A fixture that reads a bare remote with
``git -C <bare>`` therefore dies with exit 128 -- a refusal to open the
repository at all, *before* any ref lookup -- for every such developer, while CI
(which uses the ``all`` default) stays green. The supported test suite must run
both locally and in CI, so this is a defect in the test, not environmental
noise.

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

The remedy is always the same, and is already the house shape: a sibling
``_bare_git(repo, *args)`` running ``git --git-dir <repo> ...``, with the
``-C`` helper kept for the non-bare worktrees beside it.
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

TESTS_DIR = "tests"

# This lint's own oracle deliberately runs ``git -C <bare>`` to PROVE git refuses
# it, so its fixture corpus is a live instance of the banned construct. It is
# excluded structurally, the check_wall_clock_asserts.py / check_comment_hygiene.py
# idiom, rather than by a marker the rule would then have to define.
EXCLUDED_FILES = (Path("tests/integration/test_bare_repository_fixture_portability_heldout.py"),)

# ``str``/``Path`` wrappers stand for the path they wrap, so unwrap them before
# deciding which identifier a git argument addresses.
_UNWRAPPERS = frozenset({"str", "Path", "PurePath", "fspath"})
_CREATION_VERBS = ("init", "create", "make", "new", "build")
_ESCAPE = "safe.bareRepository=all"

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


def check(root: Path) -> list[Finding]:
    """Sweep every ``tests/**/*.py`` module under ``root``."""
    findings: list[Finding] = []
    for path in sorted((root / TESTS_DIR).rglob("*.py")):
        if path.relative_to(root) in EXCLUDED_FILES:
            continue
        findings.extend(scan_module(path.relative_to(root), path.read_text(encoding="utf-8")))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    options = parser.parse_args(argv)

    findings = check(options.root)
    if not findings:
        return 0
    for finding in findings:
        print(finding, file=sys.stderr)
    print(f"\n{_TEACHING}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
