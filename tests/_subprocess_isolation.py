"""Unit-tier subprocess-isolation predicate — collection-time enforcement.

``tests/conftest.py`` provides the isolation root: a session-scoped sandbox repo
(``_rebar_root_sandbox_repo``) plus an autouse fixture that ``monkeypatch.setenv``s
``REBAR_ROOT`` at it, per xdist worker. It reaches child processes purely by
ENVIRONMENT INHERITANCE — so a test that builds its own ``env=`` mapping from
scratch silently drops ``REBAR_ROOT``, and its child falls back to the git toplevel
of the cwd: the real checkout. Nothing stopped that before this guard.

Contract (the tests are written against these names):

    scan_source(source, path) -> list[dict]
        AST-scan one module's source. Each finding carries at least ``kind``
        ("hazard", which fails collection, or "undecidable", reported only),
        ``lineno`` (1-based) and ``reason`` (an instructive message naming the fix).

    unharnessed_subprocess_reason(items) -> str | None
        The collection-hook predicate: None when the selection is clean, else one
        instructive message. Kept here, not in conftest, so it is testable without
        spawning pytest — mirroring tests/_live_jira_confinement.py.

Detection is AST, never regex, and the callee is resolved through the module's own
``import`` / ``from`` bindings: a naive ``run(`` pattern matches 126 ``asyncio.run``
call sites in the unit tier alone. The classification is deliberately THREE-way — an
``env=`` bound outside the enclosing function cannot be resolved without a def-site
trace, so it is ``undecidable`` and NEVER fails collection: reddening dozens of files
on day one is how a guard gets reverted.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

#: Opt-out marker; a reason string is MANDATORY (``--strict-markers`` checks the NAME only).
MARKER_NAME = "allow_unharnessed_subprocess"

#: Fully-resolved callees that spawn a child process.
_SPAWN_TARGETS = frozenset(
    {
        *(f"subprocess.{n}" for n in ("run", "Popen", "check_output", "check_call", "call")),
        "os.system",
        "os.popen",
        "pytest.main",
    }
)

#: Names that always denote a harness-owned root.
_HARNESS_NAMES = frozenset({"tmp_path", "tmp_path_factory"})
#: Module-level constants that denote the real checkout.
_REPO_ROOT_NAMES = frozenset({"REPO_ROOT", "_REPO_ROOT"})
#: Expressions that carry the ambient (harness-provided) environment forward.
_AMBIENT_ENV_NAMES = frozenset({"environ", "subprocess_env"})
#: Names/attributes whose presence in argv escapes the harness sandbox entirely.
_HOME_ESCAPE_NAMES = frozenset({"home", "mkdtemp", "TemporaryDirectory"})
_ROOT_ENV_KEY = "REBAR_ROOT"
#: Cheap byte pre-filter: only ~35% of test modules can possibly spawn anything.
_PREFILTER_TOKENS = (b"subprocess", b"os.system", b"os.popen", b"pytest.main")

_MARK_HINT = f'@pytest.mark.{MARKER_NAME}("<why this test must escape the harness>")'


# ---------------------------------------------------------------- import-binding resolution


def _import_bindings(tree: ast.AST) -> tuple[dict[str, str], dict[str, str]]:
    """Return ``(module_aliases, function_aliases)`` for *tree*.

    ``module_aliases`` maps a local name to the module it denotes (``sp`` ->
    ``subprocess``); ``function_aliases`` maps a local name to a dotted callee
    (``run`` -> ``subprocess.run``).
    """
    modules: dict[str, str] = {}
    functions: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                modules[local] = alias.name if alias.asname else local
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                functions[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return modules, functions


def _dotted(node: ast.expr) -> str | None:
    """Render an ``a.b.c`` attribute/name chain as a dotted string."""
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


def _resolve_callee(
    func: ast.expr, modules: dict[str, str], functions: dict[str, str]
) -> str | None:
    """Resolve a call's callee to a canonical dotted name, or None."""
    dotted = _dotted(func)
    if dotted is None:
        return None
    if "." not in dotted:
        return functions.get(dotted)
    head, rest = dotted.split(".", 1)
    module = modules.get(head)
    if module is None:
        return None
    return f"{module}.{rest}"


# ---------------------------------------------------------------- expression inspection


def _referenced_names(expr: ast.AST | None) -> set[str]:
    """Every ``Name`` id and ``Attribute`` attr appearing anywhere inside *expr*."""
    if expr is None:
        return set()
    names: set[str] = set()
    for node in ast.walk(expr):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def _mentions(expr: ast.AST | None, wanted: frozenset[str] | set[str]) -> bool:
    return bool(_referenced_names(expr) & set(wanted))


def _carries_ambient_env(expr: ast.AST | None) -> bool:
    """True when *expr* forwards the ambient environment (``os.environ`` / helper)."""
    return _mentions(expr, _AMBIENT_ENV_NAMES)


# ---------------------------------------------------------------- the scanner


class _Scope:
    """One function frame: its parameter names and its local single-name assignments."""

    def __init__(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.name = node.name
        args = node.args
        params = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
        if args.vararg is not None:
            params.add(args.vararg.arg)
        if args.kwarg is not None:
            params.add(args.kwarg.arg)
        self.params = params
        self.assignments: dict[str, list[ast.expr]] = {}
        for child in ast.walk(node):
            targets: list[ast.expr] = []
            if isinstance(child, ast.Assign):
                targets = list(child.targets)
            elif isinstance(child, ast.AnnAssign) and child.value is not None:
                targets = [child.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    self.assignments.setdefault(target.id, []).append(
                        child.value  # type: ignore[union-attr]
                    )


class _Scanner(ast.NodeVisitor):
    """Walk one module, classifying every resolved spawn site."""

    def __init__(self, path: str, modules: dict[str, str], functions: dict[str, str]) -> None:
        self.path = path
        self._modules = modules
        self._functions = functions
        self._scopes: list[_Scope] = []
        self.findings: list[dict] = []

    # -- scope bookkeeping ---------------------------------------------------------

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._scopes.append(_Scope(node))
        self.generic_visit(node)
        self._scopes.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    @property
    def _param_names(self) -> set[str]:
        return set().union(*(scope.params for scope in self._scopes)) if self._scopes else set()

    @property
    def _enclosing(self) -> str | None:
        return self._scopes[0].name if self._scopes else None

    def _resolve_local(self, name: str) -> ast.expr | None:
        """One-hop local resolution of *name* within the enclosing function frames."""
        for scope in reversed(self._scopes):
            bound = scope.assignments.get(name)
            if bound is not None and len(bound) == 1:
                return bound[0]
        return None

    # -- call classification -------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        callee = _resolve_callee(node.func, self._modules, self._functions)
        if callee in _SPAWN_TARGETS:
            assert callee is not None
            self._classify(node, callee)
        self.generic_visit(node)

    def _classify(self, node: ast.Call, callee: str) -> None:
        keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        argv = _argv_elements(node)
        harness = _HARNESS_NAMES | self._param_names

        self._check_environment(node, callee, keywords.get("env"))
        self._check_root(node, callee, keywords.get("cwd"), argv, harness)
        self._check_home_escape(node, callee, argv)

    def _check_environment(self, node: ast.Call, callee: str, env: ast.expr | None) -> None:
        if env is None:
            return
        verdict = self._environment_verdict(env, depth=0)
        if verdict == "ok":
            return
        if verdict == "hazard":
            self._record(
                node,
                callee,
                "hazard",
                f"`{callee}(env=...)` is a dict literal with no `**`-spread of an ambient "
                f"mapping and no explicit {_ROOT_ENV_KEY!r} key (an `os.environ[...]` in a "
                f"VALUE position cherry-picks one variable and still drops the root). The "
                f"tier's isolation root reaches children ONLY by inheritance, so this child "
                f"falls back to the git toplevel of its cwd — the real checkout. Fix: spread "
                f"`**subprocess_env()` into the mapping, or set {_ROOT_ENV_KEY!r} "
                f"explicitly; if the test must escape the harness, mark it {_MARK_HINT}.",
            )
            return
        self._record(
            node,
            callee,
            "undecidable",
            f"`{callee}(env=...)` is built outside this function, so the guard cannot tell "
            f"whether {_ROOT_ENV_KEY!r} survives into the child. Reported only — never fails "
            f"collection. If you are editing this call, build it from `subprocess_env()`.",
        )

    def _environment_verdict(self, env: ast.expr, depth: int) -> str:
        # A DICT LITERAL is judged on its own terms FIRST: `os.environ` in a VALUE
        # position (`{"PATH": os.environ["PATH"]}`) cherry-picks one variable and drops
        # the root, so it must not accept. Only a `**`-spread of an ambient mapping, or
        # an explicit REBAR_ROOT key, forwards the root. A non-literal expression
        # (`env=os.environ`, `env=subprocess_env(...)`) forwards the whole mapping.
        if isinstance(env, ast.Dict):
            return _dict_env_verdict(env)
        if _carries_ambient_env(env):
            return "ok"
        if isinstance(env, ast.Name) and depth == 0:
            bound = self._resolve_local(env.id)
            if bound is not None:
                return self._environment_verdict(bound, depth + 1)
        return "undecidable"

    def _check_root(
        self,
        node: ast.Call,
        callee: str,
        cwd: ast.expr | None,
        argv: list[ast.expr],
        harness: set[str],
    ) -> None:
        # An EXPLICIT root wins: when the call names its own cwd (or `git -C <path>`), a
        # `tmp_path` mentioned elsewhere in argv does not relocate the child, so it must
        # not excuse the site. The argv accept path is for calls that name no root at all
        # — `git init str(tmp_path)`, `git -C str(repo) …` — where the root IS a positional.
        explicit = cwd if cwd is not None else _dash_c_target(argv)
        if explicit is None:
            return
        if _mentions(explicit, harness) or not _mentions(explicit, _REPO_ROOT_NAMES):
            return
        where = "cwd=" if explicit is cwd else "a `-C` argument"
        self._record(
            node,
            callee,
            "hazard",
            f"`{callee}` roots its child at the real checkout: {where} resolves to "
            f"REPO_ROOT and nothing in the call carries a harness-owned root (`tmp_path` or "
            f"a fixture parameter), so the child resolves {_ROOT_ENV_KEY!r} to this working "
            f"tree and can mutate it. Fix: root it at `tmp_path` or a fixture-provided repo; "
            f"route a nested pytest through `tests/_nested_pytest.run_nested_pytest`; or, if "
            f"the test genuinely needs the checkout, mark it {_MARK_HINT}.",
        )

    def _check_home_escape(self, node: ast.Call, callee: str, argv: list[ast.expr]) -> None:
        for element in argv:
            names = _referenced_names(element)
            literals = {n.value for n in ast.walk(element) if isinstance(n, ast.Constant)}
            if not (names & _HOME_ESCAPE_NAMES or ("environ" in names and "HOME" in literals)):
                continue
            self._record(
                node,
                callee,
                "hazard",
                f"`{callee}` derives a child path from the real HOME or an ad-hoc temp dir "
                f"(`Path.home()`, `os.environ['HOME']`, `mkdtemp`, `TemporaryDirectory`) "
                f"rather than from the harness; those roots outlive the test and are shared "
                f"across xdist workers. Fix: use `tmp_path`, else mark it {_MARK_HINT}.",
            )
            return

    def _record(self, node: ast.Call, callee: str, kind: str, reason: str) -> None:
        enclosing = self._enclosing
        self.findings.append(
            {
                "kind": kind,
                "lineno": node.lineno,
                "col_offset": node.col_offset,
                "path": self.path,
                "callee": callee,
                "function": enclosing,
                # Attribution: a finding inside a `test_*` function belongs to THAT test and
                # is scoped to runs that collect it. Anything else (a module-level helper)
                # belongs to the file.
                "is_test": bool(enclosing and enclosing.startswith("test")),
                "reason": f"{self.path}:{node.lineno}: {reason}",
            }
        )


def _dict_env_verdict(env: ast.Dict) -> str:
    """Classify a dict-literal ``env=``: ok / hazard / undecidable."""
    for key in env.keys:
        if isinstance(key, ast.Constant) and key.value == _ROOT_ENV_KEY:
            return "ok"
    spreads = [value for key, value in zip(env.keys, env.values, strict=True) if key is None]
    if any(_carries_ambient_env(value) for value in spreads):
        return "ok"
    if spreads:
        return "undecidable"
    return "hazard"


def _argv_elements(node: ast.Call) -> list[ast.expr]:
    """The positional command elements of a spawn call, when they are a literal sequence."""
    if not node.args:
        return []
    first = node.args[0]
    if isinstance(first, (ast.List, ast.Tuple)):
        return list(first.elts)
    return [first]


def _dash_c_target(argv: list[ast.expr]) -> ast.expr | None:
    """The expression following a literal ``-C`` element (git's repository selector)."""
    for index, element in enumerate(argv[:-1]):
        if isinstance(element, ast.Constant) and element.value == "-C":
            return argv[index + 1]
    return None


# ---------------------------------------------------------------- public API


def scan_source(source: str, path: str) -> list[dict]:
    """AST-scan one module's *source*, returning hazard/undecidable findings."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    modules, functions = _import_bindings(tree)
    scanner = _Scanner(path, modules, functions)
    scanner.visit(tree)
    return scanner.findings


def _might_spawn(raw: bytes) -> bool:
    """Fast byte pre-filter — skips ~65% of test modules without parsing them."""
    return any(token in raw for token in _PREFILTER_TOKENS)


_SCAN_CACHE: dict[str, list[dict]] = {}


def scan_path(path: str) -> list[dict]:
    """Scan one file on disk, pre-filtered and memoized for the whole session."""
    cached = _SCAN_CACHE.get(path)
    if cached is not None:
        return cached
    try:
        raw = Path(path).read_bytes()
    except OSError:
        findings: list[dict] = []
    else:
        findings = scan_source(raw.decode("utf-8", "replace"), path) if _might_spawn(raw) else []
    _SCAN_CACHE[path] = findings
    return findings


def _item_path(item: Any) -> str | None:
    candidate = getattr(item, "path", None) or getattr(item, "fspath", None)
    try:
        return str(Path(str(candidate)).resolve()) if candidate else None
    except (OSError, ValueError):  # pragma: no cover — exotic path
        return None


def _item_function_name(item: Any) -> str:
    name = getattr(item, "originalname", None) or getattr(item, "name", "") or ""
    return str(name).split("[", 1)[0]


def _marker_reason_problem(item: Any, marker: Any) -> str | None:
    """Return a message when an opt-out marker carries no usable reason."""
    args = getattr(marker, "args", ()) or ()
    reason = args[0] if args else None
    if not isinstance(reason, str) or not reason.strip():
        return (
            f"{getattr(item, 'nodeid', item)}: `@pytest.mark.{MARKER_NAME}` requires a "
            f"non-empty reason naming why this test must escape the unit tier's isolation "
            f"root, e.g. {_MARK_HINT}."
        )
    return None


class _Selection:
    """What this run actually collected, and which of it opted out."""

    def __init__(self) -> None:
        self.problems: list[str] = []
        self.collected: dict[str, set[str]] = {}
        self.exempt_functions: dict[str, set[str]] = {}
        self.exempt_files: set[str] = set()
        self.paths: list[str] = []

    def absorb(self, item: Any) -> None:
        path = _item_path(item)
        if path is None:
            return
        if path not in self.collected:
            self.paths.append(path)
            self.collected[path] = set()
        name = _item_function_name(item)
        self.collected[path].add(name)
        for marker in _iter_markers(item):
            problem = _marker_reason_problem(item, marker)
            if problem is not None:
                self.problems.append(problem)
                continue
            self.exempt_functions.setdefault(path, set()).add(name)
            self.exempt_files.add(path)

    def fails(self, path: str, finding: dict) -> bool:
        """Does *finding* fail THIS run? Scope first, then the opt-out."""
        function = finding.get("function")
        if finding.get("is_test"):
            # Attributable to one test. A run that does not collect that test — the
            # everyday `pytest <file>::<one_test>` — is not affected by it at all, so the
            # finding is OUT OF SCOPE rather than merely unexcused. Scoping this on the
            # collected selection also keeps the marker reachable: markers arrive per
            # collected item, so an uncollected test's opt-out is never seen.
            if function not in self.collected.get(path, set()):
                return False
            return function not in self.exempt_functions.get(path, set())
        # A module-level helper carries no test name of its own: it belongs to the file,
        # is in scope whenever anything in the file is collected, and a file-level opt-out
        # (typically a module `pytestmark`) covers it.
        return path not in self.exempt_files


def _iter_markers(item: Any) -> list[Any]:
    try:
        return list(item.iter_markers(MARKER_NAME))
    except AttributeError:  # pragma: no cover — not a real pytest Item
        return []


def unharnessed_subprocess_reason(items: list[Any]) -> str | None:
    """Return a collection-failure message for unharnessed spawns, else None.

    Only ``hazard`` findings fail, and only those in scope for THIS selection — a
    hazard belonging to a test the run did not collect is irrelevant to it.
    ``undecidable`` findings never fail: an ``env=`` bound outside the enclosing
    function cannot be resolved without a def-site trace, and failing those would
    redden dozens of files at introduction — what gets a guard reverted.
    """
    selection = _Selection()
    for item in items:
        selection.absorb(item)

    hazards: list[str] = []
    for path in selection.paths:
        for finding in scan_path(path):
            if finding["kind"] == "hazard" and selection.fails(path, finding):
                hazards.append(finding["reason"])

    if not hazards and not selection.problems:
        return None

    sections = [
        "Unit-tier subprocess-isolation violation: the unit tier hands children its "
        f"isolation root ({_ROOT_ENV_KEY}) by ENVIRONMENT INHERITANCE only, so a spawn "
        "that rebuilds `env=` from scratch, or roots itself at the real checkout, escapes "
        "the harness and runs against this working tree."
    ]
    for title, entries in (
        ("Unharnessed spawn site(s)", hazards),
        ("Opt-out marker(s) without a reason", selection.problems),
    ):
        if entries:
            sections.append(f"{title} [{len(entries)}]:\n  " + "\n  ".join(entries))
    return "\n".join(sections)
