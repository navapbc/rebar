"""Enumeration-closure DET lint (bug c9c2-c569-f5cb-426f): an advisory member of the P6 family.

A certified plan can enumerate a file set smaller than the one its own work compels: a
required-empty grep AC whose hit set includes a path the plan never names, a removed symbol
still cited elsewhere, or a deleted module whose test file loses every test rather than the
one the plan names. Reasoning over the plan does not find these; sweeping the tree does.

The lint derives *sweeps* from the plan — required-empty grep commands in AC items, and
backticked identifiers on removal sentences — runs them over the repo's files, and reports
every hit path outside the plan's *enumerated set* (backticked paths plus ``file_impact``).
It also parses test modules whose module-level imports bind a module the plan deletes and
names the tests the plan leaves unmentioned.

A grep command is parsed with :mod:`shlex` and translated to a Python regex; it is never
executed. Every failure abstains (counted in coverage) and the lint never blocks: it rides
:func:`det_advisory.p6_ac_quality`, so ``DET_CHECKS`` stays P1–P11.
"""

from __future__ import annotations

import ast
import fnmatch
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .det_floor import PlanContext

_MAX_FILE_BYTES = 1 << 20
_MAX_FILES = 50_000
_MAX_REPORTED = 10
_NOISE_DIRS = frozenset(
    {".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", ".mypy_cache"}
)

_SPAN_RE = re.compile(r"`([^`\n]+)`")
_PATH_RE = re.compile(r"[\w.\-]+(?:/[\w.\-]*)*")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")
# Imperative / present forms only: "deleted module" and "removal" are prose, not plan steps.
_REMOVAL_RE = re.compile(
    r"\b(?:remov|delet|renam|retir)(?:e|es|ing)\b|\bdrop(?:s|ping)?\b", re.IGNORECASE
)
_NEW_NAME_RE = re.compile(r"\b(?:to|with|into)\b", re.IGNORECASE)
_SEGMENT_RE = re.compile(r"(?<=[.;])\s+|\n")
_DELETED_PATH_RE = re.compile(
    r"\b(?:(?:remov|delet|retir)(?:e|es|ing)|drop(?:s|ping)?)\s+(?:the\s+)?(?:(?:whole|entire)\s+)?"
    r"(?:(?:module|file)\s+)?((?:`[^`\n]+`(?:,\s*|\s+and\s+)?)+)",
    re.IGNORECASE,
)
_REQUIRED_EMPTY_RE = re.compile(
    r"\b(?:no\s+(?:hits?|match(?:es)?|results?|output|lines?)|returns?\s+nothing|"
    r"is\s+empty|returns?\s+empty|empty\s+(?:output|result)|zero\s+(?:hits?|match(?:es)?))\b",
    re.IGNORECASE,
)
_CHECKBOX_RE = re.compile(r"^\s*-\s*\[[ xX]?\]")
_SHELL_OPERATORS = frozenset({";", "|", "||", "&&", "&", ">", ">>", "<"})
_VALUE_FLAGS = frozenset({"-A", "-B", "-C", "-m", "--max-count", "--context"})


@dataclass(frozen=True)
class _Sweep:
    """One regex sweep: what to search, where, and the plan text it came from."""

    regex: re.Pattern[str]
    source: str
    includes: tuple[str, ...] = ()
    excludes: tuple[str, ...] = ()


@dataclass
class _GrepArgs:
    patterns: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    flags: set[str] = field(default_factory=set)


# ── plan parsing ──────────────────────────────────────────────────────────────
def _span_path(span: str) -> str | None:
    """A backticked span as a repo path (``:line`` / ``::test`` suffix dropped), else None."""
    token = span.strip().split("::")[0].split(":")[0]
    if not _PATH_RE.fullmatch(token) or token.startswith("-"):
        return None
    if "/" in token or re.search(r"\.[A-Za-z0-9]+$", token):
        return token[2:] if token.startswith("./") else token
    return None


def _enumerated_paths(ctx: PlanContext) -> set[str]:
    out = {p for s in _SPAN_RE.findall(ctx.plan_text) if (p := _span_path(s))}
    for fi in ctx.state.get("file_impact") or []:
        p = fi.get("path") if isinstance(fi, dict) else fi
        if isinstance(p, str) and p:
            out.add(p)
    return out


def _covered(path: str, enumerated: set[str]) -> bool:
    return any(path == e or path.startswith(e.rstrip("/") + "/") for e in enumerated)


def _segments(text: str) -> list[str]:
    return [s for s in _SEGMENT_RE.split(text) if s.strip()]


def _removed_symbols(text: str) -> list[str]:
    """Backticked identifiers a removal verb takes as its object: after the verb, and before
    any to/with/into that introduces the new name."""
    out: list[str] = []
    for seg in _segments(text):
        verb = _REMOVAL_RE.search(seg)
        if not verb:
            continue
        cut = _NEW_NAME_RE.search(seg, verb.end())
        scope = seg[verb.end() : None if cut is None else cut.start()]
        for span in _SPAN_RE.findall(scope):
            name = span.strip()
            distinctive = "_" in name.strip("_") or re.search(r"[a-z][A-Z]", name)
            if _IDENT_RE.fullmatch(name) and distinctive and name not in out:
                out.append(name)
    return out


def _deleted_paths(text: str) -> set[str]:
    """Paths a removal verb applies to directly ("Delete `src/x.py`, `tests/y.py`")."""
    out: set[str] = set()
    for m in _DELETED_PATH_RE.finditer(text):
        out.update(p for s in _SPAN_RE.findall(m.group(1)) if (p := _span_path(s)))
    return out


def _ac_items(text: str) -> list[str]:
    """AC checklist items with their continuation lines joined."""
    items: list[str] = []
    in_ac = False
    for line in text.split("\n"):
        if line.lower().startswith("## acceptance criteria"):
            in_ac = True
        elif line.startswith("## "):
            in_ac = False
        elif in_ac and _CHECKBOX_RE.match(line):
            items.append(line)
        elif in_ac and items and line.strip():
            items[-1] += " " + line.strip()
    return items


# ── grep command translation (parsed, never executed) ──────────────────────────
def _split_command(cmd: str) -> list[str] | None:
    lexer = shlex.shlex(cmd, posix=True, punctuation_chars=";|&<>")
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    for i, tok in enumerate(tokens):
        if tok in _SHELL_OPERATORS:
            return tokens[:i]
    return tokens


def _grep_argv(tokens: list[str]) -> list[str] | None:
    """The argument list after the grep verb, or None for a non-recursive / non-grep command."""
    if tokens[:2] == ["git", "grep"]:
        return tokens[2:]
    if tokens and tokens[0] == "rg":
        return tokens[1:]
    if tokens and tokens[0] in ("grep", "egrep"):
        recursive = any(
            t in ("--recursive", "--dereference-recursive")
            or (t.startswith("-") and not t.startswith("--") and set("rR") & set(t))
            for t in tokens[1:]
        )
        return tokens[1:] if recursive else None
    return None


def _parse_grep_args(argv: list[str]) -> _GrepArgs:
    args = _GrepArgs()
    positional: list[str] = []
    it = iter(argv)
    for tok in it:
        if tok == "--":
            args.paths.extend(it)
            break
        if tok in ("-e", "--regexp"):
            args.patterns.append(next(it, ""))
        elif tok.startswith("--regexp="):
            args.patterns.append(tok.split("=", 1)[1])
        elif tok in _VALUE_FLAGS:
            next(it, None)
        elif tok.startswith("--"):
            args.flags.add(tok)
        elif tok.startswith("-") and len(tok) > 1:
            if tok.startswith("-e") and len(tok) > 2:
                args.patterns.append(tok[2:])
            else:
                args.flags.update(tok[1:])
        else:
            positional.append(tok)
    if not args.patterns and positional:
        args.patterns.append(positional.pop(0))
    args.paths = positional + args.paths
    return args


def _translate_pattern(pattern: str, *, basic: bool) -> str:
    """POSIX grep syntax to Python ``re``: BRE swaps escaped and bare ``|(){}+?``."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern):
            n = pattern[i + 1]
            if n in "<>":
                out.append(r"\b")
            elif basic and n in "|(){}+?":
                out.append(n)
            else:
                out.append(c + n)
            i += 2
            continue
        out.append("\\" + c if basic and c in "|(){}+?" else c)
        i += 1
    return "".join(out)


def _grep_sweep(cmd: str) -> _Sweep | None:
    tokens = _split_command(cmd)
    if not tokens:
        return None
    argv = _grep_argv(tokens)
    if argv is None:
        return None
    args = _parse_grep_args(argv)
    if not args.patterns:
        return None
    fixed = "F" in args.flags or "--fixed-strings" in args.flags
    basic = tokens[0] != "rg" and tokens[0] != "egrep" and not ({"E", "P"} & args.flags)
    parts = [re.escape(p) if fixed else _translate_pattern(p, basic=basic) for p in args.patterns]
    body = "|".join(f"(?:{p})" for p in parts)
    if "w" in args.flags or "--word-regexp" in args.flags:
        body = rf"(?<!\w)(?:{body})(?!\w)"
    ignore_case = "i" in args.flags or "--ignore-case" in args.flags
    regex = re.compile(body, re.MULTILINE | (re.IGNORECASE if ignore_case else 0))
    excluded = [_pathspec_exclusion(p) for p in args.paths]
    excludes = tuple(e for e in excluded if e)
    includes = tuple(p for p, e in zip(args.paths, excluded, strict=True) if not e and p != ".")
    return _Sweep(regex, f"AC grep `{cmd[:80]}`", includes, excludes)


def _pathspec_exclusion(spec: str) -> str | None:
    for prefix in (":(exclude)", ":!", ":^"):
        if spec.startswith(prefix):
            return spec[len(prefix) :]
    return None


def _sweeps(ctx: PlanContext) -> tuple[list[_Sweep], int]:
    """Every sweep the plan implies, plus the count of grep commands that could not compile."""
    sweeps: list[_Sweep] = []
    abstained = 0
    for item in _ac_items(ctx.plan_text):
        if not _REQUIRED_EMPTY_RE.search(item):
            continue
        for span in _SPAN_RE.findall(item):
            try:
                sweep = _grep_sweep(span)
            except re.error:
                abstained += 1
                continue
            if sweep is not None:
                sweeps.append(sweep)
    for name in _removed_symbols(ctx.plan_text):
        sweeps.append(_Sweep(re.compile(rf"\b{name}\b"), f"removed symbol `{name}`"))
    return sweeps, abstained


# ── the tree ──────────────────────────────────────────────────────────────────
def _list_files(root: str) -> list[str]:
    """Repo-relative files: git's view in a checkout, else a walk (snapshots are plain trees)."""
    try:
        proc = subprocess.run(
            ["git", "-C", root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        proc = None
    if proc is not None and proc.returncode == 0:
        names = proc.stdout.decode("utf-8", "replace").split("\0")
        return sorted(n for n in names if n)[:_MAX_FILES]
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _NOISE_DIRS)
        rel_dir = os.path.relpath(dirpath, root)
        for fn in sorted(filenames):
            out.append(fn if rel_dir == "." else f"{rel_dir}/{fn}".replace(os.sep, "/"))
            if len(out) >= _MAX_FILES:
                return out
    return out


def _read_text(root: str, rel: str) -> str | None:
    path = os.path.join(root, rel)
    try:
        if os.path.getsize(path) > _MAX_FILE_BYTES:
            return None
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    if b"\0" in data:
        return None
    return data.decode("utf-8", "replace")


def _spec_matches(rel: str, spec: str) -> bool:
    spec = spec.rstrip("/")
    return rel == spec or rel.startswith(spec + "/") or fnmatch.fnmatch(rel, spec)


def _in_scope(rel: str, sweep: _Sweep) -> bool:
    if sweep.includes and not any(_spec_matches(rel, s) for s in sweep.includes):
        return False
    return not any(_spec_matches(rel, s) for s in sweep.excludes)


def _sweep_tree(
    root: str, files: list[str], sweeps: list[_Sweep], enumerated: set[str]
) -> dict[str, tuple[int, str, str]]:
    """``{path: (line, matched text, source)}`` for the first hit in each un-enumerated path."""
    gaps: dict[str, tuple[int, str, str]] = {}
    for rel in files:
        if _covered(rel, enumerated):
            continue
        scoped = [s for s in sweeps if _in_scope(rel, s)]
        text = _read_text(root, rel) if scoped else None
        if text is None:
            continue
        for sweep in scoped:
            m = sweep.regex.search(text)
            if m:
                line = text.count("\n", 0, m.start()) + 1
                gaps[rel] = (line, _enclosing_token(text, m.start(), m.end()), sweep.source)
                break
    return gaps


def _enclosing_token(text: str, start: int, end: int) -> str:
    """The match widened to whole identifier characters, so an over-match shows the real name
    (``_ALIASES\\b`` hitting ``_ARCH_ALIASES`` reports ``_ARCH_ALIASES``)."""
    while start > 0 and (text[start - 1].isalnum() or text[start - 1] == "_"):
        start -= 1
    while end < len(text) and (text[end].isalnum() or text[end] == "_"):
        end += 1
    return text[start:end].split("\n", 1)[0][:60]


# ── deleted modules and the test files they break ──────────────────────────────
def _module_name(path: str) -> str | None:
    if not path.endswith(".py"):
        return None
    parts = path[:-3].split("/")
    if parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or None


def _binds(node: ast.stmt, module: str) -> bool:
    def hit(name: str) -> bool:
        return name == module or name.startswith(module + ".")

    if isinstance(node, ast.Import):
        return any(hit(a.name) for a in node.names)
    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        return hit(node.module) or any(hit(f"{node.module}.{a.name}") for a in node.names)
    return False


def _test_names(tree: ast.Module) -> list[tuple[str, str]]:
    """``(display name, bare name)`` for each top-level test and ``Test*`` method."""
    out: list[tuple[str, str]] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if node.name.startswith("test"):
                out.append((node.name, node.name))
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            out.extend(
                (f"{node.name}.{m.name}", m.name)
                for m in node.body
                if isinstance(m, ast.FunctionDef | ast.AsyncFunctionDef)
                and m.name.startswith("test")
            )
    return out


def _is_test_file(rel: str) -> bool:
    base = rel.rsplit("/", 1)[-1]
    return base.endswith(".py") and (base.startswith("test_") or base.endswith("_test.py"))


def _orphaned_tests(root: str, files: list[str], plan_text: str) -> list[str]:
    deleted = _deleted_paths(plan_text)
    modules = sorted({m for p in deleted if (m := _module_name(p))})
    issues: list[str] = []
    for rel in files:
        if not modules or not _is_test_file(rel) or _covered(rel, deleted):
            continue
        text = _read_text(root, rel)
        bound = [m for m in modules if text and m.rsplit(".", 1)[-1] in text]
        if not bound or text is None:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        mod = next((m for m in bound if any(_binds(n, m) for n in tree.body)), None)
        if mod is None:
            continue
        unnamed = [d for d, bare in _test_names(tree) if not re.search(rf"\b{bare}\b", plan_text)]
        if unnamed:
            issues.append(
                f"enumeration closure: `{rel}` imports deleted module `{mod}` at module level, "
                f"so its tests the plan does not name also break: {', '.join(unnamed[:10])}"
            )
    return issues


# ── entry point ───────────────────────────────────────────────────────────────
def closure_issues(ctx: PlanContext) -> tuple[list[str], dict[str, int]]:
    """P6 evidence lines for paths the plan's own work compels but does not enumerate.

    Returns ``(issues, coverage)``. Fail-open: an unreadable tree or an uncompilable grep
    pattern abstains (``closure_abstained``) rather than accusing the plan."""
    cov = {"closure_sources": 0, "closure_gaps": 0, "closure_abstained": 0}
    if not ctx.repo_root:
        return [], cov
    sweeps, abstained = _sweeps(ctx)
    cov["closure_sources"] = len(sweeps)
    cov["closure_abstained"] = abstained
    if not sweeps and not _deleted_paths(ctx.plan_text):
        return [], cov
    files = _list_files(ctx.repo_root)
    gaps = _sweep_tree(ctx.repo_root, files, sweeps, _enumerated_paths(ctx))
    issues = [
        f"enumeration closure: `{rel}:{line}` matches `{text}` for {source} but is not "
        "enumerated in the plan or file_impact"
        for rel, (line, text, source) in list(gaps.items())[:_MAX_REPORTED]
    ]
    if len(gaps) > _MAX_REPORTED:
        issues.append(
            f"enumeration closure: {len(gaps) - _MAX_REPORTED} more un-enumerated path(s) match"
        )
    issues.extend(_orphaned_tests(ctx.repo_root, files, ctx.plan_text))
    cov["closure_gaps"] = len(issues)
    return issues, cov
