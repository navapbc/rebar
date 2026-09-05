"""No test may local-clone a bare remote it created without pinning its upkeep.

The defect [rebar:57d2-e356-7eb4-4bf5]: at git's defaults a push into a bare
repository makes ``receive-pack`` spawn ``git maintenance run --auto --quiet
--detach`` -- a child that OUTLIVES the push, so ``subprocess.run([... "push"
...])`` returning is not a happens-before edge for that repository's object
database. ``tests/_git_upkeep.py`` documents that finding and supplies the three
config pins that remove the mutator.

**Why local ``git clone`` is the second amplifier.** Only a WHOLESALE ``objects/``
walker races a pruner; every ordinary git command reads individual object paths and
is untouched. In ``tests/`` there are exactly two such walkers.
``shutil.copytree`` was the first, and bug b394-6198-6010-42f7 guarded it by making
``clone_topology_template`` refuse an unpinned template. A LOCAL clone is the
second and was not guarded: it does not use the transport, it hardlinks or copies
every entry it finds under the source's ``objects/``. Entries the detached
maintenance child prunes between readdir and copy make git die with exit 128 --
``failed to copy file to '<dst>/.git/objects/<sha>'``, ``unable to read sha1
file``, or ``unable to parse commit``, one race with three faces.

Measured on git 2.55, push-then-local-clone in a loop against ONE bare remote
seeded like a ticket store, 8 concurrent workers x 120 iterations on a loaded host:
**unpinned 5/960 clones exited 128; pinned 0/960.**

**Why the coverage is a tree sweep and not a list.** The same reasoning
``test_bare_repository_fixture_portability_heldout.py`` records: coverage that must
be hand-registered is coverage a new file escapes by default. Ticket 57d2 converted
17 creation sites across 16 modules; the sweep is what keeps the eighteenth from
landing green. It reads only each module's own AST, so it needs no CI provider and
runs wherever the suite does.

**Detection.** A name is BARE-BY-HAND when a ``--bare`` git argv in the module binds
it -- the last path operand after the flag, or the directory the call runs in when
the flag takes no operand. ``init_bare_remote`` / ``apply_upkeep_pins`` do not bind
such a name, so a fixture routed through them is invisible here, which is the point.
A VIOLATION is a ``clone`` git argv whose source operand addresses a bare-by-hand
name, directly or through a module-local helper that forwards a parameter into a
clone source.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = "tests"

# ``str``/``Path`` wrappers stand for the path they wrap, and an f-string interpolating
# one (``f"file://{remote}"``) still addresses that name.
_UNWRAPPERS = frozenset({"str", "Path", "PurePath", "fspath"})
# ``clone`` flags that take a separate value operand, so the value is not the source.
_VALUED_CLONE_FLAGS = frozenset(
    {"-b", "--branch", "-o", "--origin", "-c", "--config", "-u", "--upload-pack", "--depth"}
)
_PINNING_FACTORIES = frozenset({"init_bare_remote", "apply_upkeep_pins"})

# This module's own fixture corpus spells the banned construct on purpose, to prove the
# sweep has teeth. It is excluded structurally -- the idiom
# ``test_bare_repository_fixture_portability_heldout.py`` uses -- rather than by a marker
# the rule would then have to define.
EXCLUDED_FILES = (Path(__file__).resolve().relative_to(_REPO_ROOT),)

_TEACHING = """\
A local `git clone` walks the SOURCE repository's objects/ directory wholesale.
At git's defaults a push into a bare repository leaves a detached
`git maintenance run` child repacking and pruning that same directory after the
push has returned, so the clone races a pruner and dies with exit 128.

Create the remote through the shared helper, which pins the upkeep away in the
same call that creates the repository:

    from _git_upkeep import init_bare_remote

    remote = init_bare_remote(tmp_path / "remote.git")
    remote = init_bare_remote(tmp_path / "remote.git", initial_branch="tickets")

See tests/_git_upkeep.py for the measurements behind the three pins."""


@dataclass(frozen=True)
class Finding:
    """One local clone whose source repository was created without upkeep pins."""

    path: Path
    line: int
    name: str
    via: str

    def __str__(self) -> str:
        return (
            f"{self.path}:{self.line}: clones unpinned bare repository '{self.name}' via {self.via}"
        )


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
    """The identifier a git argument addresses, through wrappers and f-strings."""
    while isinstance(node, ast.Call):
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name not in _UNWRAPPERS or not node.args:
            return None
        node = node.args[0]
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.JoinedStr):
        interpolated = [
            _addressed(part.value) for part in node.values if isinstance(part, ast.FormattedValue)
        ]
        named = [name for name in interpolated if name is not None]
        return named[0] if named else None
    return None


def _called_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else None


def _bare_created_by(call: ast.Call) -> str | None:
    """The identifier this call binds to a HAND-BUILT bare repository, if any."""
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


def _clone_source(call: ast.Call) -> str | None:
    """The identifier this call clones FROM, if it is a ``clone`` git argv."""
    argv = _argv(call)
    start = next((i for i, element in enumerate(argv) if _literal(element) == "clone"), None)
    if start is None:
        return None
    index = start + 1
    while index < len(argv):
        flag = _literal(argv[index])
        if flag is not None and flag.startswith("-"):
            index += 2 if flag in _VALUED_CLONE_FLAGS else 1
            continue
        return _addressed(argv[index])
    return None


def _clone_helpers(tree: ast.Module) -> dict[str, int]:
    """Module-local functions that forward a parameter into a clone source.

    ``_clone_tickets(source, destination)`` wrapping the clone is the shape the epoch
    fixture uses; without this the sweep would see only the wrapper's own body and let
    every caller through.
    """
    helpers: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        parameters = [argument.arg for argument in node.args.args]
        sources = {_clone_source(sub) for sub in ast.walk(node) if isinstance(sub, ast.Call)}
        forwarded = [index for index, parameter in enumerate(parameters) if parameter in sources]
        if forwarded:
            helpers[node.name] = forwarded[0]
    return helpers


def _pinned_names(tree: ast.Module) -> set[str]:
    """Names handed to a pinning factory, which are therefore not hand-built."""
    pinned: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _called_name(node) in _PINNING_FACTORIES and node.args:
            name = _addressed(node.args[0])
            if name is not None:
                pinned.add(name)
    return pinned


def scan_module(path: Path, source: str) -> list[Finding]:
    """Every local clone in ``source`` whose bare source repository is unpinned."""
    tree = ast.parse(source)
    bare = {
        name
        for name in (
            _bare_created_by(node) for node in ast.walk(tree) if isinstance(node, ast.Call)
        )
        if name is not None
    } - _pinned_names(tree)
    if not bare:
        return []
    helpers = _clone_helpers(tree)
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        direct = _clone_source(node)
        if direct in bare:
            findings.append(Finding(path, node.lineno, str(direct), "a clone argv"))
            continue
        called = _called_name(node)
        position = helpers.get(called or "")
        if position is None or position >= len(node.args):
            continue
        forwarded = _addressed(node.args[position])
        if forwarded in bare:
            findings.append(Finding(path, node.lineno, str(forwarded), f"{called}()"))
    return sorted(findings, key=lambda finding: finding.line)


def sweep_tests_tree(root: Path = _REPO_ROOT) -> list[Finding]:
    """Sweep every ``tests/**/*.py`` module under ``root``."""
    findings: list[Finding] = []
    for path in sorted((root / TESTS_DIR).rglob("*.py")):
        relative = path.relative_to(root)
        if relative in EXCLUDED_FILES:
            continue
        findings.extend(scan_module(relative, path.read_text(encoding="utf-8")))
    return findings


_OFFENDER = (
    "import subprocess\n"
    "def test_reads_remote(tmp_path):\n"
    '    remote = tmp_path / "remote.git"\n'
    '    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)\n'
    '    subprocess.run(["git", "clone", "-b", "tickets", str(remote), str(tmp_path / "c")])\n'
)

_WRAPPED_OFFENDER = (
    "import subprocess\n"
    "def _clone_tickets(source, destination):\n"
    '    subprocess.run(["git", "clone", "-b", "tickets", str(source), str(destination)])\n'
    "def test_reads_remote(tmp_path):\n"
    '    origin = tmp_path / "origin.git"\n'
    '    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)\n'
    '    _clone_tickets(origin, tmp_path / "c")\n'
)


def test_no_test_clones_an_unpinned_bare_repository() -> None:
    """Coverage follows the tree: every ``tests/**/*.py`` module is in scope."""
    findings = sweep_tests_tree()

    assert not findings, "\n".join(["", *(str(f) for f in findings), "", _TEACHING])


def test_the_sweep_detects_a_reintroduced_unpinned_clone_source() -> None:
    """The guard has teeth: the exact construct ticket 57d2 removed fails again."""
    fixed = _OFFENDER.replace(
        '    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)\n',
        "    remote = init_bare_remote(remote)\n",
    )

    findings = scan_module(Path("tests/test_offender.py"), _OFFENDER)

    assert [(f.line, f.name, f.via) for f in findings] == [(5, "remote", "a clone argv")]
    assert scan_module(Path("tests/test_offender.py"), fixed) == []


def test_the_sweep_follows_a_clone_wrapped_in_a_module_local_helper() -> None:
    """A fixture that hides the clone behind a helper is still covered."""
    findings = scan_module(Path("tests/test_wrapped.py"), _WRAPPED_OFFENDER)

    assert [(f.line, f.name, f.via) for f in findings] == [(7, "origin", "_clone_tickets()")]


def test_the_sweep_ignores_a_clone_of_a_NON_bare_source() -> None:
    """A worktree beside the bare remote is not a bare repository; do not flag it."""
    module = (
        "import subprocess\n"
        "def test_reads_remote(tmp_path):\n"
        '    remote = tmp_path / "remote.git"\n'
        '    work = tmp_path / "work"\n'
        '    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)\n'
        '    subprocess.run(["git", "clone", str(work), str(tmp_path / "c")])\n'
    )

    assert scan_module(Path("tests/test_nonbare.py"), module) == []


def test_the_sweep_reads_a_file_url_clone_source() -> None:
    """``f"file://{remote}"`` addresses ``remote`` exactly as ``str(remote)`` does."""
    module = (
        "import subprocess\n"
        "def test_reads_remote(tmp_path):\n"
        '    remote = tmp_path / "remote.git"\n'
        '    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)\n'
        '    subprocess.run(["git", "clone", "-q", f"file://{remote}", str(tmp_path / "c")])\n'
    )

    findings = scan_module(Path("tests/test_fileurl.py"), module)

    assert [(f.line, f.name) for f in findings] == [(5, "remote")]
