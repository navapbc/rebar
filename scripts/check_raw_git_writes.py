#!/usr/bin/env python3
"""Raw-git-write lint: keep store writes on the sanctioned seams.

Policy [rebar:d37e-4f64-3265-4f30]: every git mutation against the ticket
store (and its mirrors) must go through one of the two sanctioned write
seams — (1) the store seam: the locked write transactions and event appends
(``rebar.tickets`` / ``rebar._store.event_append`` / ``rebar._commands.txn``),
whose internals legitimately issue raw git; or (2) the reconciler seam:
``rebar_reconciler/git_adapter.py`` and ``_ref_lock.py``. Multiple production
bugs came from write paths that bypassed those seams with raw
``git add/commit/rm`` — including inside workflow YAML.

Two deterministic layers, each matched to what is statically decidable:

- **Layer P** (Python, AST) over ``src/`` and ``scripts/`` ``*.py``:
  - R1: a raw ``subprocess`` invocation whose argv carries ``git`` plus a
    mutation verb at the git SUBCOMMAND position (so ``remote add`` /
    ``worktree add`` never fire). Argv is resolved by LOCAL INTRA-FUNCTION
    TRACKING (inline list literal; a local list variable built from literals
    incl. ``.append``/``.extend``; a constant shell string), and — fail-closed
    — an OPAQUE argv (a bare parameter or non-literal expression) fires as
    ``unresolvable-argv``.
  - R2: a CALL to a shared git wrapper BY NAME (``run_git``, ``run_git_write``,
    ``_run_git``, ``_git``) passing a mutation verb resolved by the same
    tracking. The name is taken from the CALLABLE, which for an attribute call
    is the ATTRIBUTE — so ``core._git(...)``, ``self._git(...)`` and
    ``mod.run_git(...)`` are linted exactly like a bare ``_git(...)``,
    whatever the receiver. That covers the late-binding idiom where a module
    object is passed down as a parameter (``rebar_reconciler/_ref_lock_push.py``)
    to keep ``monkeypatch.setattr(mod, "_git", ...)`` working across a module
    boundary. The verb is resolved by the same tracking:
    a string literal anywhere in the arguments including nested
    list literals, a local list passed plain or splatted, or — fail-closed —
    an opaque argv forwarded to the wrapper (``unresolvable-argv-wrapper``).
    Sibling wrapper names (``_git_ok``/``_git_fetch``/``_git_push``) are NOT
    in the name set: their bodies fire the opaque class at the wrapper
    internal and take a delegation marker, sanctioning callers transitively.
    Any fresh thin wrapper's body fires fail-closed the same way.
  - R3: a COMPOSITION of the reconciler seam's MUTATION PRIMITIVES made from
    outside the write seam itself. R1/R2 enforce the MECHANISM rule ("no raw
    subprocess git"); R3 enforces the PROTOCOL rule ("tracker mutations go
    through the atomic transaction"). The seam's primitives — ``add``,
    ``commit``, ``commit_tree``, ``read_tree``, ``rm_cached``, ``update_ref``
    and ``write_tree`` in ``git_adapter.py`` — are sanctioned exports, so
    before R3 any caller could compose them into a hand-rolled non-atomic
    stage+commit and every individual call passed. R3 binds on the RECEIVER,
    not the bare attribute: ``add``/``commit`` are ubiquitous method names, so
    a call fires only where the receiver resolves to a git-adapter module —
    the module's own name (``git_adapter.add(…)``, ``pkg.git_adapter.add(…)``),
    an ``as``-alias, a name bound by a dynamic module loader
    (``ga = _load("….git_adapter", "git_adapter.py")``), or a primitive
    ``from``-imported out of a git-adapter module. Read/query exports never
    fire, and neither does ``commit_email`` — despite the name it is
    ``git log -1 --format=%ae``, a read.
- **Layer W** (workflows + shell, cwd-aware within-step) over
  ``.github/workflows/*.yml`` run-blocks and ``scripts/*.sh``: a git mutation
  verb fires ONLY when the same step/script block establishes tracker
  context — ``cd`` into a path containing ``.tickets-tracker``,
  ``git -C <tracker-path>``, or a ``working-directory:`` naming it. Ordinary
  repo commits in workflows (docs/artifacts) never fire.

R3's SANCTIONED ENTRY SET is the reconciler write-seam modules themselves —
``git_adapter.py`` (where the primitives are defined), ``_ref_lock.py`` and
``_ref_lock_push.py`` (the ref-lock transaction they are composed under).
They are matched by EXACT REPO-RELATIVE PATH, never by file name: a name-based
exemption would let anyone opt out of this gate by adding a file called
``git_adapter.py`` to an unrelated package, which for a rule whose whole value
is precision is a hole, not a convenience. (The receiver binding above is
name-based on purpose — there, the module's name is evidence that a call is a
git call. Here the name would be a claim of trust, which must be earned by
residency.) RESIDUAL, stated
plainly: this is a per-FILE trust boundary, coarser than the per-FUNCTION
``# raw-git-ok:`` markers inside ``git_adapter.py``, so a non-atomic
composition ADDED inside one of those three files would be sanctioned without
a marker. That is accepted deliberately — those files are the transaction, and
a locked selective-commit helper (see ticket 11a9-b11b-e93d-4832) belongs in
them — but it is a widening, not a free win.

Sanction is a single INLINE MARKER with a MANDATORY reason — no external
allowlist file: ``# raw-git-ok: <reason>`` on the offending line, on the
enclosing function (the line above ``def``, the ``def`` line, or the first
body line), or anywhere in the workflow step / shell block. A marker without
a reason fails the lint. One marker vocabulary covers all three rules, so the
``marker-without-reason`` violation applies to R3 unchanged. This is
deliberately a different marker from ``# tickets-boundary-ok``
(boundary-crossing READ/layout sanction); this one sanctions raw WRITES.

ACCEPTED RESIDUAL: argv construction that crosses function boundaries or is
computed dynamically (beyond the local intra-function tracking) resolves as
the fail-closed opaque-argv class when the WHOLE argv reaches a wrapper name
or a raw subprocess call unresolved; a resolved argv list whose HEAD is a
non-literal expression (``sys.executable``, a path variable) is treated as
non-git — laundering the ``git`` program name itself through a
cross-function variable is accepted residual (local scalar assignments like
``prog = "git"`` ARE tracked); and tracker targeting laundered through a
variable in Layer W remains undetectable statically. The lint
deterministically catches the literal, local-variable/splat, and
opaque-forwarding shapes — which cover every historical bypass and the
codebase's own write idioms.

Bootstrap: ``--report`` prints the full hit inventory (marked hits with
their reasons, unmarked hits as pending) and exits 0 — the authoritative
disposition list for wiring this lint into enforcement.
"""

from __future__ import annotations

import argparse
import ast
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

PY_SCAN_TREES = ("src", "scripts")

MUTATION_VERBS = frozenset(
    {"add", "commit", "rm", "mv", "reset", "update-ref", "checkout", "clean"}
)
WRAPPER_NAMES = frozenset({"run_git", "run_git_write", "_run_git", "_git"})
SUBPROC_FUNCS = frozenset({"run", "call", "check_call", "check_output", "Popen"})
TRACKER_DIR_NAME = ".tickets-tracker"

# R3 — the reconciler write seam's composition rule.
# The module whose exports are the seam's primitives; a receiver resolving to it
# is what makes an ``add``/``commit`` call a GIT call rather than an ordinary one.
SEAM_MODULE_NAME = "git_adapter"
# The primitives that MUTATE git state (index, object db, refs). Read/query exports
# are absent by design, and so is ``commit_email`` — despite the name it runs
# ``git log -1 --format=%ae`` (git_adapter.py), a read, not a commit.
SEAM_MUTATION_PRIMITIVES = frozenset(
    {"add", "commit", "commit_tree", "read_tree", "rm_cached", "update_ref", "write_tree"}
)
# Files that ARE the write seam: R3 never fires inside them (see the header's
# RESIDUAL note on this per-file trust boundary). Keyed on the EXACT repo-relative
# path — matching a bare file name would let any package opt out of the gate by
# naming a module ``git_adapter.py``.
_RECONCILER = Path("src/rebar/_engine/rebar_reconciler")
SEAM_INTERNAL_FILES = frozenset(
    {
        _RECONCILER / "git_adapter.py",
        _RECONCILER / "_ref_lock.py",
        _RECONCILER / "_ref_lock_push.py",
    }
)

_MARKER_RE = re.compile(r"#\s*raw-git-ok:(.*)$")
_CD_TRACKER_RE = re.compile(r"\bcd\s+[^\n;|&]*" + re.escape(TRACKER_DIR_NAME))
_DASH_C_TRACKER_RE = re.compile(r"\bgit\s+(?:-\S+\s+)*-C\s+\S*" + re.escape(TRACKER_DIR_NAME))

# The lint's own fixture corpus (tests drive it against synthetic trees, but the
# test module itself contains raw-write shapes as string fixtures) and the lint
# itself are structurally excluded — the check_criteria_vocabulary.py idiom.
EXCLUDED_FILES = (
    Path("scripts/check_raw_git_writes.py"),
    Path("tests/unit/test_raw_git_write_lint.py"),
)


class _Opaque:
    """Sentinel type: argv not resolvable by local tracking.

    A distinct class (rather than a bare ``object()``) so the resolver return
    unions stay narrowable — ``isinstance(x, _Opaque)`` splits the union, where
    an ``is _OPAQUE`` check against an ``object`` instance does not.
    """


_OPAQUE = _Opaque()


@dataclass
class Hit:
    path: Path
    line: int
    kind: str
    detail: str
    sanction: str | None  # marker reason if sanctioned


@dataclass
class Violation:
    path: Path
    line: int
    kind: str
    detail: str


_TEACHING = (
    "Store writes go through the event-append/transaction APIs (rebar.tickets /\n"
    "rebar._store.event_append / rebar._commands.txn) or the reconciler seam\n"
    "(rebar_reconciler/git_adapter.py, _ref_lock.py). If this raw write is\n"
    "genuinely seam-internal (or targets a disposable sandbox repo, not the\n"
    "tracker), add an inline marker with a reason:  # raw-git-ok: <reason>\n"
    "A [seam-composition] hit means the git-adapter's mutation primitives were\n"
    "composed by hand outside the write seam: run the mutation through the\n"
    "atomic locked transaction instead of stacking add/commit/update-ref."
)


# ---------------------------------------------------------------------------
# markers
# ---------------------------------------------------------------------------


def _collect_markers(lines: list[str]) -> tuple[dict[int, str], list[int]]:
    """(line -> non-empty reason) plus the lines carrying an EMPTY reason."""
    reasons: dict[int, str] = {}
    empty: list[int] = []
    for i, line in enumerate(lines, start=1):
        m = _MARKER_RE.search(line)
        if not m:
            continue
        reason = m.group(1).strip()
        if reason:
            reasons[i] = reason
        else:
            empty.append(i)
    return reasons, empty


def _function_marker(
    func: ast.FunctionDef | ast.AsyncFunctionDef, reasons: dict[int, str]
) -> str | None:
    """A function-level marker: line above the def/decorators, the def line,
    or the first body line."""
    first_line = min([func.lineno] + [d.lineno for d in func.decorator_list])
    candidates = [first_line - 1, func.lineno]
    if func.body:
        candidates.append(func.body[0].lineno)
    for ln in candidates:
        if ln in reasons:
            return reasons[ln]
    return None


# ---------------------------------------------------------------------------
# Layer P — AST
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SeamBindings:
    """Per-file R3 bindings: what, in THIS module, refers to the git adapter.

    ``receivers`` are extra names that hold the git-adapter module (``as``-aliases
    and dynamic-loader assignments); the module's own name always counts and is
    not listed. ``primitives`` are mutation primitives ``from``-imported out of a
    git-adapter module, so a BARE call to them is a seam call. ``enabled`` is
    False inside the write seam's own modules, switching R3 off wholesale — the
    module NAME alone binds a receiver, so an empty binding set is NOT enough to
    turn the rule off.
    """

    receivers: frozenset[str]
    primitives: frozenset[str]
    enabled: bool = True


_NO_SEAM = _SeamBindings(frozenset(), frozenset(), enabled=False)


def _names_seam_module(text: str) -> bool:
    """Does *text* name the git-adapter module? Accepts a dotted module path
    (``rebar_reconciler.git_adapter``) or a file name (``git_adapter.py``)."""
    stem = text[:-3] if text.endswith(".py") else text
    return stem.rsplit("/", 1)[-1].rsplit(".", 1)[-1] == SEAM_MODULE_NAME


def _seam_import_bindings(node: ast.Import | ast.ImportFrom) -> tuple[set[str], set[str]]:
    """(receiver aliases, bare primitive names) contributed by one import statement."""
    receivers: set[str] = set()
    primitives: set[str] = set()
    if isinstance(node, ast.Import):
        for a in node.names:
            if a.asname and _names_seam_module(a.name):
                receivers.add(a.asname)
        return receivers, primitives
    from_seam = node.module is not None and _names_seam_module(node.module)
    for a in node.names:
        if a.asname and a.name == SEAM_MODULE_NAME:
            receivers.add(a.asname)
        elif from_seam and a.name in SEAM_MUTATION_PRIMITIVES:
            primitives.add(a.asname or a.name)
    return receivers, primitives


def _seam_loader_receivers(node: ast.Assign) -> set[str]:
    """Names bound by a dynamic module load of the git adapter, e.g.
    ``ga = _load("rebar_reconciler.git_adapter", "git_adapter.py")``."""
    if not isinstance(node.value, ast.Call):
        return set()
    named = any(
        isinstance(arg, ast.Constant)
        and isinstance(arg.value, str)
        and _names_seam_module(arg.value)
        for arg in node.value.args
    )
    if not named:
        return set()
    return {t.id for t in node.targets if isinstance(t, ast.Name)}


def _seam_bindings(tree: ast.Module) -> _SeamBindings:
    """Resolve the whole module's git-adapter bindings once, up front.

    Module-wide (not scope-local) on purpose: an import at module level must bind
    for calls inside every function, and the loader idiom appears inside function
    bodies. Over-binding a name is harmless — the primitive-name check still gates
    the emission — while under-binding would silently drop a real composition.
    """
    receivers: set[str] = set()
    primitives: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            r, p = _seam_import_bindings(node)
            receivers |= r
            primitives |= p
        elif isinstance(node, ast.Assign):
            receivers |= _seam_loader_receivers(node)
    return _SeamBindings(frozenset(receivers), frozenset(primitives))


def _git_subcommand(tokens: list[str | None]) -> str | _Opaque | None:
    """The git subcommand from resolved argv tokens, skipping global options.

    Returns the subcommand string, None when there is none, or _OPAQUE when an
    unresolved token blocks the decision.
    """
    i = 1
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok is None:
            return _OPAQUE
        if tok in ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"):
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        return tok
    return None


class _ScopeLinter:
    """Lints one lexical scope (module or function body) in statement order,
    tracking local list variables built from literals."""

    def __init__(
        self,
        rel_path: Path,
        reasons: dict[int, str],
        subproc_aliases: set[str],
        func_sanction: str | None,
        hits: list[Hit],
        seam: _SeamBindings = _NO_SEAM,
    ) -> None:
        self.rel_path = rel_path
        self.reasons = reasons
        self.subproc_aliases = subproc_aliases
        self.func_sanction = func_sanction
        self.hits = hits
        self.seam = seam
        self.local_lists: dict[str, list[str | None] | None] = {}
        self.local_strs: dict[str, str | None] = {}

    # -- local list tracking ------------------------------------------------

    def _resolve_elements(self, node: ast.AST) -> list[str | None] | None:
        if isinstance(node, (ast.List, ast.Tuple)):
            out: list[str | None] = []
            for elt in node.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    out.append(elt.value)
                elif isinstance(elt, ast.Name) and self.local_strs.get(elt.id) is not None:
                    out.append(self.local_strs[elt.id])
                else:
                    out.append(None)
            return out
        return None

    def _track(self, stmt: ast.stmt) -> None:
        if isinstance(stmt, ast.Assign):
            elements = self._resolve_elements(stmt.value)
            scalar = (
                stmt.value.value
                if isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str)
                else None
            )
            for tgt in stmt.targets:
                if isinstance(tgt, ast.Name):
                    self.local_lists[tgt.id] = elements  # None => untracked
                    self.local_strs[tgt.id] = scalar
        elif isinstance(stmt, ast.AugAssign) and isinstance(stmt.target, ast.Name):
            add = self._resolve_elements(stmt.value)
            cur = self.local_lists.get(stmt.target.id)
            if cur is not None and add is not None:
                cur.extend(add)
            else:
                self.local_lists[stmt.target.id] = None
        elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            call = stmt.value
            f = call.func
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                cur = self.local_lists.get(f.value.id)
                if cur is None:
                    return
                if f.attr == "append" and len(call.args) == 1:
                    a = call.args[0]
                    cur.append(
                        a.value
                        if isinstance(a, ast.Constant) and isinstance(a.value, str)
                        else None
                    )
                elif f.attr == "extend" and len(call.args) == 1:
                    add = self._resolve_elements(call.args[0])
                    if add is not None:
                        cur.extend(add)
                    else:
                        self.local_lists[f.value.id] = None

    # -- argv resolution ----------------------------------------------------

    def _resolve_argv(self, node: ast.AST) -> list[str | None] | _Opaque:
        elements = self._resolve_elements(node)
        if elements is not None:
            return elements
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                return list(shlex.split(node.value))
            except ValueError:
                return _OPAQUE
        if isinstance(node, ast.Name):
            tracked = self.local_lists.get(node.id)
            if tracked is not None:
                return list(tracked)
            return _OPAQUE
        return _OPAQUE

    # -- sanction + emission --------------------------------------------------

    def _emit(self, node: ast.stmt | ast.expr, kind: str, detail: str) -> None:
        line = node.lineno
        sanction = self.reasons.get(line) or self.func_sanction
        self.hits.append(Hit(self.rel_path, line, kind, detail, sanction))

    # -- rules ----------------------------------------------------------------

    def _is_raw_subprocess(self, func: ast.AST) -> bool:
        if isinstance(func, ast.Attribute) and func.attr in SUBPROC_FUNCS:
            return isinstance(func.value, ast.Name) and func.value.id in self.subproc_aliases
        if isinstance(func, ast.Name):
            return func.id in self.subproc_aliases and func.id in SUBPROC_FUNCS
        return False

    def _wrapper_name(self, func: ast.AST) -> str | None:
        if isinstance(func, ast.Name) and func.id in WRAPPER_NAMES:
            return func.id
        if isinstance(func, ast.Attribute) and func.attr in WRAPPER_NAMES:
            return func.attr
        return None

    def _seam_primitive(self, func: ast.AST) -> str | None:
        """The git-adapter mutation primitive this callable names, if any (R3)."""
        if not self.seam.enabled:
            return None
        if isinstance(func, ast.Name):
            return func.id if func.id in self.seam.primitives else None
        if not isinstance(func, ast.Attribute) or func.attr not in SEAM_MUTATION_PRIMITIVES:
            return None
        recv = func.value
        # The receiver's OWN trailing name is the discriminator, so both
        # ``git_adapter.add(…)`` and ``pkg.git_adapter.add(…)`` bind, while
        # ``session.commit()`` / ``seen.add(x)`` do not.
        if isinstance(recv, ast.Name):
            name = recv.id
        elif isinstance(recv, ast.Attribute):
            name = recv.attr
        else:
            return None
        if name == SEAM_MODULE_NAME or name in self.seam.receivers:
            return func.attr
        return None

    def _check_call(self, call: ast.Call) -> None:
        primitive = self._seam_primitive(call.func)
        if primitive is not None:
            self._emit(
                call,
                "seam-composition",
                f"git-adapter mutation primitive {primitive}() composed outside the write seam",
            )
            return
        if self._is_raw_subprocess(call.func):
            self._check_raw_subprocess(call)
            return
        name = self._wrapper_name(call.func)
        if name is not None:
            self._check_wrapper_call(call, name)

    def _check_raw_subprocess(self, call: ast.Call) -> None:
        if not call.args:
            return
        argv = self._resolve_argv(call.args[0])
        if isinstance(argv, _Opaque):
            self._emit(
                call,
                "unresolvable-argv",
                "raw subprocess call with argv not resolvable by local tracking (fail-closed)",
            )
            return
        tokens = argv
        if not tokens:
            return
        head = tokens[0]
        if head is None:
            # Resolved list with a non-literal head (sys.executable, a path
            # variable): treated as non-git. Accepted residual documented in
            # the module header; local `prog = "git"` scalars ARE tracked.
            return
        if head != "git" and not head.endswith("/git"):
            return
        sub = _git_subcommand(tokens)
        if isinstance(sub, _Opaque):
            self._emit(call, "unresolvable-argv", "git argv subcommand position is not a literal")
        elif isinstance(sub, str) and sub in MUTATION_VERBS:
            self._emit(call, "raw-git-mutation", f"raw subprocess git mutation: git {sub}")

    def _check_wrapper_call(self, call: ast.Call, name: str) -> None:
        tokens: list[str] = []
        starred_untracked = False
        untracked_names = 0
        for arg in call.args:
            if isinstance(arg, ast.Starred):
                inner = self._resolve_argv(arg.value)
                if isinstance(inner, _Opaque):
                    starred_untracked = True
                else:
                    tokens.extend(t for t in inner if t is not None)
            elif isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                tokens.append(arg.value)
            elif isinstance(arg, (ast.List, ast.Tuple)):
                tokens.extend(t for t in (self._resolve_elements(arg) or []) if t is not None)
            elif isinstance(arg, ast.Name):
                tracked = self.local_lists.get(arg.id)
                if tracked is not None:
                    tokens.extend(t for t in tracked if t is not None)
                else:
                    untracked_names += 1
            # other expressions (str(x), f-strings, attributes) are operands
        verb = next((t for t in tokens if t in MUTATION_VERBS), None)
        if verb is not None:
            self._emit(
                call, "wrapper-git-mutation", f"git wrapper {name}() passed mutation verb {verb!r}"
            )
        elif starred_untracked:
            self._emit(
                call,
                "unresolvable-argv-wrapper",
                f"opaque argv forwarded to git wrapper {name}() (fail-closed)",
            )
        elif not tokens and untracked_names:
            self._emit(
                call,
                "unresolvable-argv-wrapper",
                f"unresolvable argv to git wrapper {name}() (fail-closed)",
            )

    # -- traversal ------------------------------------------------------------

    def run(self, body: list[ast.stmt]) -> None:
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                sanction = _function_marker(stmt, self.reasons) or self.func_sanction
                sub = _ScopeLinter(
                    self.rel_path,
                    self.reasons,
                    self.subproc_aliases,
                    sanction,
                    self.hits,
                    self.seam,
                )
                sub.run(stmt.body)
                continue
            if isinstance(stmt, ast.ClassDef):
                sub = _ScopeLinter(
                    self.rel_path,
                    self.reasons,
                    self.subproc_aliases,
                    self.func_sanction,
                    self.hits,
                    self.seam,
                )
                sub.run(stmt.body)
                continue
            for call in self._calls_in(stmt):
                self._check_call(call)
            self._track(stmt)
            for inner in self._nested_bodies(stmt):
                self.run(inner)

    @staticmethod
    def _nested_bodies(stmt: ast.stmt) -> list[list[ast.stmt]]:
        bodies: list[list[ast.stmt]] = []
        for field in ("body", "orelse", "finalbody"):
            val = getattr(stmt, field, None)
            if isinstance(val, list) and val and isinstance(val[0], ast.stmt):
                bodies.append(val)
        for handler in getattr(stmt, "handlers", []) or []:
            bodies.append(handler.body)
        return bodies

    @staticmethod
    def _calls_in(stmt: ast.stmt) -> list[ast.Call]:
        calls: list[ast.Call] = []

        class _V(ast.NodeVisitor):
            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                return  # nested scopes handled separately

            visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                return

            def visit_Call(self, node: ast.Call) -> None:
                calls.append(node)
                self.generic_visit(node)

        v = _V()
        # visit expression/statement parts but not nested definition bodies
        for field, value in ast.iter_fields(stmt):
            if field in ("body", "orelse", "finalbody", "handlers"):
                continue
            if isinstance(value, ast.AST):
                v.visit(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, ast.AST):
                        v.visit(item)
        return calls


def _subproc_aliases(tree: ast.Module) -> set[str]:
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "subprocess":
                    aliases.add(a.asname or "subprocess")
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for a in node.names:
                if a.name in SUBPROC_FUNCS:
                    aliases.add(a.asname or a.name)
    return aliases


def _scan_python_file(path: Path, rel: Path) -> tuple[list[Hit], list[Violation]]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    reasons, empty_markers = _collect_markers(lines)
    violations = [
        Violation(rel, ln, "marker-without-reason", "raw-git-ok marker requires a reason")
        for ln in empty_markers
    ]
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [], violations
    hits: list[Hit] = []
    # R3 is off inside the write seam itself — those modules ARE the transaction.
    seam = _NO_SEAM if rel in SEAM_INTERNAL_FILES else _seam_bindings(tree)
    linter = _ScopeLinter(rel, reasons, _subproc_aliases(tree), None, hits, seam)
    linter.run(tree.body)
    return hits, violations


# ---------------------------------------------------------------------------
# Layer W — workflows + shell
# ---------------------------------------------------------------------------


def _block_has_tracker_context(block: str, working_directory: str) -> bool:
    if TRACKER_DIR_NAME in working_directory:
        return True
    return bool(_CD_TRACKER_RE.search(block) or _DASH_C_TRACKER_RE.search(block))


def _shell_line_git_mutation(line: str) -> str | None:
    code = line.split("#", 1)[0]
    try:
        tokens = shlex.split(code)
    except ValueError:
        tokens = code.split()
    for i, tok in enumerate(tokens):
        if tok != "git" and not tok.endswith("/git"):
            continue
        sub = _git_subcommand([t if t else None for t in tokens[i:]])
        if isinstance(sub, str) and sub in MUTATION_VERBS:
            return sub
    return None


def _scan_shell_block(
    rel: Path, block: str, working_directory: str, base_line: int
) -> tuple[list[Hit], list[Violation]]:
    lines = block.splitlines()
    reasons, empty_markers = _collect_markers(lines)
    violations = [
        Violation(rel, base_line, "marker-without-reason", "raw-git-ok marker requires a reason")
        for _ in empty_markers
    ]
    if not _block_has_tracker_context(block, working_directory):
        return [], violations
    sanction = next(iter(reasons.values()), None)  # any reasoned marker sanctions the block
    for offset, line in enumerate(lines):
        verb = _shell_line_git_mutation(line)
        if verb is not None:
            hit = Hit(
                rel,
                base_line + offset,
                "workflow-tracker-git-mutation",
                f"git {verb} in a {TRACKER_DIR_NAME} context block",
                sanction,
            )
            return [hit], violations  # one hit per block: the first offending line
    return [], violations


def _iter_step_blocks(doc: object):
    """Yield (step_name, run_text, working_directory) for every workflow step."""
    if not isinstance(doc, dict):
        return
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        job_wd = ""
        defaults = job.get("defaults")
        if isinstance(defaults, dict) and isinstance(defaults.get("run"), dict):
            job_wd = str(defaults["run"].get("working-directory") or "")
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict):
                continue
            run = step.get("run")
            if not isinstance(run, str):
                continue
            wd = str(step.get("working-directory") or "") or job_wd
            yield str(step.get("name") or "unnamed step"), run, wd


def _first_line_of(text_lines: list[str], needle: str) -> int:
    target = needle.strip()
    if target:
        for i, line in enumerate(text_lines, start=1):
            if target in line:
                return i
    return 1


def _scan_workflow_file(path: Path, rel: Path) -> tuple[list[Hit], list[Violation]]:
    text = path.read_text(encoding="utf-8")
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return [], []
    file_lines = text.splitlines()
    hits: list[Hit] = []
    violations: list[Violation] = []
    for step_name, run, wd in _iter_step_blocks(doc):
        base_line = _first_line_of(file_lines, run.splitlines()[0] if run.splitlines() else "")
        step_hits, step_violations = _scan_shell_block(rel, run, wd, base_line)
        for h in step_hits:
            h.detail = f"step {step_name!r}: {h.detail}"
        hits.extend(step_hits)
        violations.extend(step_violations)
    return hits, violations


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------


def _iter_files(root: Path, subdir: str, suffixes: tuple[str, ...]) -> list[Path]:
    base = root / subdir
    if not base.is_dir():
        return []
    out = []
    for path in sorted(base.rglob("*")):
        if path.is_file() and path.suffix in suffixes:
            rel = path.relative_to(root)
            if rel in EXCLUDED_FILES:
                continue
            if any(part in (".venv", "__pycache__", "node_modules") for part in rel.parts):
                continue
            out.append(path)
    return out


def _scan_all(root: Path) -> tuple[list[Hit], list[Violation]]:
    hits: list[Hit] = []
    violations: list[Violation] = []
    for tree_name in PY_SCAN_TREES:
        for path in _iter_files(root, tree_name, (".py",)):
            h, v = _scan_python_file(path, path.relative_to(root))
            hits.extend(h)
            violations.extend(v)
    for path in _iter_files(root, ".github/workflows", (".yml", ".yaml")):
        h, v = _scan_workflow_file(path, path.relative_to(root))
        hits.extend(h)
        violations.extend(v)
    for path in _iter_files(root, "scripts", (".sh",)):
        text = path.read_text(encoding="utf-8")
        h, v = _scan_shell_block(path.relative_to(root), text, "", 1)
        hits.extend(h)
        violations.extend(v)
    return hits, violations


def check(root: Path) -> list[Violation]:
    hits, violations = _scan_all(root)
    for hit in hits:
        if hit.sanction is None:
            violations.append(Violation(hit.path, hit.line, hit.kind, hit.detail))
    violations.sort(key=lambda v: (str(v.path), v.line))
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument(
        "--report",
        action="store_true",
        help="print the full hit inventory (marked + unmarked) and exit 0",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()

    if args.report:
        hits, violations = _scan_all(root)
        print(f"raw-git-writes report: {len(hits)} hit(s)\n")
        for hit in sorted(hits, key=lambda h: (str(h.path), h.line)):
            status = f"marked: {hit.sanction}" if hit.sanction else "UNMARKED"
            print(f"  {hit.path}:{hit.line}  [{hit.kind}]  {hit.detail}  ({status})")
        for v in violations:
            print(f"  {v.path}:{v.line}  [{v.kind}]  {v.detail}")
        return 0

    violations = check(root)
    if not violations:
        return 0
    print(f"raw-git-writes: {len(violations)} unsanctioned raw git write(s) found\n")
    for v in violations:
        print(f"  {v.path}:{v.line}  [{v.kind}]  {v.detail}")
    print(f"\n{_TEACHING}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
