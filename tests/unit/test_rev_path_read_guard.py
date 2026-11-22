"""No git revision read may pass ``<rev>:<path>`` without ``--`` (bug comic-heavyset-jerboa).

For ``show``, ``log``, ``diff`` and ``rev-list``, git checks every revision argument for
ambiguity with a file of the same name unless ``--`` ends the revisions. That check stats
``<cwd>/<rev>:<path>``. On Windows the stat fails with "Filename too long" once the joined
string reaches MAX_PATH, and git exits 128 even though the object exists. ``git cat-file blob
<rev>:<path>`` resolves the object name and never stats it, so file content at a revision is
read that way (bug cold-censorable-armedcrab fixed ``fsck_restore`` first).

This module scans ``src/rebar`` and ``scripts`` and fails on either shape:

* a revision verb with no ``--`` given a ``<rev>:<path>`` argument: a string literal, an
  f-string with a literal ``:``, or a name bound to one of those in the same function;
* a ``git show`` with no ``--`` and no commit-display flag. That is a content read whose
  argument the scan cannot see, such as a path passed in as a parameter.
"""

from __future__ import annotations

import ast
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_SCANNED = ("src/rebar", "scripts")

#: Verbs that parse revisions through git's setup_revisions and so run the ambiguity stat.
_REVISION_VERBS = frozenset({"show", "log", "diff", "rev-list"})
#: ``git show`` flags that print commit metadata or a file list, never a blob's content.
_COMMIT_DISPLAY = frozenset({"-s", "--no-patch", "--stat", "--name-only", "--name-status"})
_COMMIT_DISPLAY_PREFIXES = ("--format", "--pretty")
#: git's own options that take a separate value before the verb.
_GIT_VALUE_OPTIONS = frozenset({"-C", "-c"})
_REMEDY = "read the blob with `git cat-file blob <rev>:<path>`, or end the revisions with `--`"


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    why: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.why}; {_REMEDY}"


def _literal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_rev_path(node: ast.AST, bindings: dict[str, ast.AST]) -> bool:
    """Whether *node* evaluates to a ``<rev>:<path>`` string, as far as the AST shows."""
    if isinstance(node, ast.Name) and node.id in bindings:
        return _is_rev_path(bindings[node.id], {})
    text = _literal(node)
    if text is not None:
        return ":" in text and not text.startswith("-")
    if isinstance(node, ast.JoinedStr):
        head = _literal(node.values[0]) if node.values else None
        if head is not None and head.startswith("-"):
            return False
        return any(":" in (_literal(part) or "") for part in node.values)
    return False


def _argv(call: ast.Call) -> list[ast.AST] | None:
    """The git argv tokens of *call* after ``git``'s own options, starting at the verb."""
    tokens: list[ast.AST]
    if call.args and isinstance(call.args[0], (ast.List, ast.Tuple)):
        tokens = list(call.args[0].elts)
        if not tokens or _literal(tokens[0]) != "git":
            return None
        tokens = tokens[1:]
    else:
        func = call.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if "git" not in name.lower().split("_"):
            return None
        tokens = list(call.args)
    for index, token in enumerate(tokens):
        text = _literal(token)
        if text is None:
            continue
        if text in _GIT_VALUE_OPTIONS:
            continue
        previous = _literal(tokens[index - 1]) if index else None
        if previous in _GIT_VALUE_OPTIONS:
            continue
        return tokens[index:]
    return None


def _check_call(call: ast.Call, bindings: dict[str, ast.AST]) -> str | None:
    argv = _argv(call)
    if not argv:
        return None
    verb = _literal(argv[0])
    if verb not in _REVISION_VERBS:
        return None
    rest = argv[1:]
    words = [_literal(token) for token in rest]
    if "--" in words:
        return None
    if any(_is_rev_path(token, bindings) for token in rest):
        return f"`git {verb}` is given a <rev>:<path> argument with no `--`"
    if verb != "show":
        return None
    display = any(
        word is not None and (word in _COMMIT_DISPLAY or word.startswith(_COMMIT_DISPLAY_PREFIXES))
        for word in words
    )
    if not display:
        return "`git show` with no `--` and no commit-display flag reads object content"
    return None


def _scope_bindings(scope: ast.AST) -> dict[str, ast.AST]:
    """Simple ``name = <expr>`` bindings made directly in *scope* (not in nested scopes)."""
    bindings: dict[str, ast.AST] = {}
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                bindings[target.id] = node.value
    return bindings


def scan_source(source: str, path: str) -> list[Finding]:
    tree = ast.parse(source, filename=path)
    findings: list[Finding] = []
    scopes: list[ast.AST] = [tree]
    scopes += [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    seen: set[int] = set()
    for scope in reversed(scopes):  # innermost function first, so its bindings win
        bindings = _scope_bindings(scope)
        for node in ast.walk(scope):
            if isinstance(node, ast.Call) and id(node) not in seen:
                seen.add(id(node))
                why = _check_call(node, bindings)
                if why:
                    findings.append(Finding(path, node.lineno, why))
    return sorted(findings, key=lambda f: (f.path, f.line))


def scan_tree(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for top in _SCANNED:
        for path in sorted((root / top).rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            findings += scan_source(path.read_text(encoding="utf-8"), rel)
    return findings


def test_no_revision_read_passes_a_rev_path_without_double_dash() -> None:
    findings = scan_tree(_ROOT)
    assert not findings, "\n".join(str(f) for f in findings)


def _scan(body: str) -> list[str]:
    return [f.why for f in scan_source(textwrap.dedent(body), "probe.py")]


@pytest.mark.parametrize(
    "body",
    [
        'run_git(t, "show", f"{rev}:{path}")',
        'run_git(t, "show", "tickets:.gitignore")',
        'subprocess.run(["git", "-C", str(r), "show", f"HEAD:{path}"])',
        'subprocess.run(["git", "-c", "core.x=1", "log", f"{a}:{b}"])',
        'def f(t):\n    source = f"{ref}:{name}"\n    return run_git(t, "show", source)',
        'def f(root, spec):\n    return _git(root, "show", spec)',
        '_run_scan_git(t, "diff", f"{a}:{p}", f"{b}:{p}", operation="x")',
    ],
)
def test_the_scan_reports_each_unsafe_shape(body: str) -> None:
    assert _scan(body), body


@pytest.mark.parametrize(
    "body",
    [
        'run_git(t, "cat-file", "blob", f"{rev}:{path}")',
        'run_git(t, "cat-file", "-e", f"{rev}:{path}")',
        'run_git(t, "rev-parse", f"{head}:{path}")',
        'run_git(t, "show", f"{rev}:{path}", "--")',
        'run_git(t, "show", "-s", "--format=%ct", sha)',
        'subprocess.run(["git", "show", "--stat", "--format=%H%n%s%n", sha])',
        '_git("show", sha, "--format=")',
        'run_git(t, "log", "--format=%H:%s", "HEAD")',
        'parser.add_parser("show", help="x")',
        'subprocess.run(["rebar", "show", tid])',
    ],
)
def test_the_scan_passes_each_safe_shape(body: str) -> None:
    assert not _scan(body), body
