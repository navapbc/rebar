"""The nested-pytest launch lives in exactly one module.

A source grep cannot police this construct: Black splits an argv list one element per line, so
``"-m", "pytest"`` never appears adjacently in the tree and a grep-based guard passes vacuously
whether or not a second copy exists.  The scan below is therefore structural, over ANY list or
tuple literal — not only one written inline as ``subprocess.run``'s first argument, because a
caller may bind the argv to a local name first.
"""

from __future__ import annotations

import ast
from pathlib import Path

from _tree_scan import parsed_python_files

_TESTS_DIR = Path(__file__).resolve().parents[1]
_OWNER = _TESTS_DIR / "_nested_pytest.py"
_ESCAPE_MARKER = "# nested-pytest-ok:"
#: Stands for the ``sys.executable`` attribute, never for the string of the same name.
_SYS_EXECUTABLE = object()
#: Stands for the ``pytest`` console script beside the running interpreter.
_CONSOLE_SCRIPT = object()
#: The option that keeps a child out of the SHARED numbered temp root.
_BASETEMP_FLAG = "--basetemp"
#: Wrappers that turn a path object into an argv element without changing what it names.
_PATH_WRAPPERS = frozenset({"str", "fspath"})


def _static_text(node: ast.expr) -> str | None:
    """Render a string expression that needs no name resolution, else ``None``.

    An f-string contributes only its literal parts.  That is enough: the question asked of
    a ``python -c`` payload is whether it names pytest at all, and a payload that reaches
    pytest through an interpolated value alone is not a shape this tree contains.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        return "".join(
            piece.value
            for piece in node.values
            if isinstance(piece, ast.Constant) and isinstance(piece.value, str)
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _static_text(node.left), _static_text(node.right)
        return None if left is None or right is None else left + right
    return None


def _string_bindings(tree: ast.AST) -> dict[str, str]:
    """Every ``name = "<text>"`` in the module, so a payload bound before use resolves.

    Flow-insensitive on purpose: the real violator assigned its ``-c`` payload to a local
    first, and a guard that only reads argv literals inline cannot see that.
    """
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        text = _static_text(node.value)
        if isinstance(target, ast.Name) and text is not None:
            bindings[target.id] = text
    return bindings


def _is_console_script(node: ast.expr) -> bool:
    """``<anything> / "pytest"`` — the console script beside the running interpreter."""
    return (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Div)
        and isinstance(node.right, ast.Constant)
        and node.right.value == "pytest"
    )


def _console_script_names(tree: ast.AST) -> set[str]:
    """Locals bound to the console-script path, which argv then spells as ``str(name)``."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_console_script(node.value):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def _unwrap(node: ast.expr) -> ast.expr:
    """Strip a ``str(...)`` / ``os.fspath(...)`` wrapper from a path expression."""
    if isinstance(node, ast.Call) and len(node.args) == 1:
        func = node.func
        called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if called in _PATH_WRAPPERS:
            return node.args[0]
    return node


def _atom(node: ast.expr, scripts: set[str], texts: dict[str, str]) -> object:
    """What one argv element denotes: an entry point, its literal text, or nothing known."""
    node = _unwrap(node)
    if (
        isinstance(node, ast.Attribute)
        and node.attr == "executable"
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
    ):
        return _SYS_EXECUTABLE
    if _is_console_script(node):
        return _CONSOLE_SCRIPT
    if isinstance(node, ast.Name):
        return _CONSOLE_SCRIPT if node.id in scripts else texts.get(node.id)
    return _static_text(node)


def _shape(atoms: list[object]) -> str | None:
    """Which nested-pytest launch, if any, this argv literal spells.

    Three spellings of one operation.  ``module`` and ``dash-c`` both re-decide everything
    the helper decides, so both belong in the helper; ``console-script`` cannot use the
    helper at all (``python -m pytest`` puts the repository root on the child's
    ``sys.path``, destroying what such tests reproduce) and so must own its basetemp itself.
    """
    if atoms and atoms[0] is _CONSOLE_SCRIPT:
        return "console-script"
    window = list(zip(atoms, atoms[1:], atoms[2:], strict=False))
    if (_SYS_EXECUTABLE, "-m", "pytest") in window:
        return "module"
    if any(
        head is _SYS_EXECUTABLE
        and flag == "-c"
        and isinstance(payload, str)
        and "pytest" in payload
        for head, flag, payload in window
    ):
        return "dash-c"
    return None


def _launch_spans(tree: ast.AST) -> list[tuple[int, int]]:
    """First and last line of every argv literal that launches a nested pytest."""
    scripts, texts = _console_script_names(tree), _string_bindings(tree)
    spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.List | ast.Tuple):
            continue
        atoms = [_atom(element, scripts, texts) for element in node.elts]
        shape = _shape(atoms)
        if shape is None or (shape == "console-script" and _BASETEMP_FLAG in atoms):
            continue
        spans.append((node.lineno, node.end_lineno or node.lineno))
    return spans


def _launch_lines(tree: ast.AST) -> list[int]:
    """The first line of every launch literal."""
    return [start for start, _ in _launch_spans(tree)]


def _unmarked_launches(path: Path) -> list[int]:
    source = path.read_text()
    body = source.splitlines()
    marked = {
        number
        for number, line in enumerate(body, start=1)
        if _ESCAPE_MARKER in line and line.split(_ESCAPE_MARKER, 1)[1].strip()
    }
    # Black splits an argv list one element per line, so the marker may sit on ANY line of the
    # literal — most naturally beside the element that names pytest, not beside the opening
    # bracket.  Excuse a launch when a reasoned marker falls anywhere in its span.
    return [
        start
        for start, end in _launch_spans(ast.parse(source))
        if not marked.intersection(range(start, end + 1))
    ]


def test_the_launch_appears_only_in_the_helper() -> None:
    offenders = {
        str(path.relative_to(_TESTS_DIR)): lines
        for path in sorted(_TESTS_DIR.rglob("*.py"))
        if path != _OWNER and (lines := _unmarked_launches(path))
    }

    assert not offenders, (
        "nested pytest is launched outside tests/_nested_pytest.py; route it through "
        f"run_nested_pytest() or annotate the line '{_ESCAPE_MARKER} <reason>':\n{offenders}"
    )


def test_the_helper_itself_carries_the_only_launch() -> None:
    assert len(_launch_lines(ast.parse(_OWNER.read_text()))) == 1


def test_the_helper_is_defined_exactly_once() -> None:
    definitions = [
        module.path
        for module in parsed_python_files(_TESTS_DIR)
        if any(line.startswith("def run_nested_pytest") for line in module.source.splitlines())
    ]

    assert definitions == [_OWNER]


def test_a_marker_inside_a_black_split_launch_excuses_it(tmp_path: Path) -> None:
    """Real launches are split one element per line; the marker need not sit on the bracket."""
    offender = tmp_path / "test_split.py"
    launch = (
        "import subprocess, sys\n"
        "subprocess.run(\n"
        "    [\n"
        "        sys.executable,\n"
        "        '-m',\n"
        "        'pytest',{marker}\n"
        "    ]\n"
        ")\n"
    )
    offender.write_text(launch.format(marker=""))
    assert _unmarked_launches(offender) == [3]

    offender.write_text(launch.format(marker=f"  {_ESCAPE_MARKER} reproduces bug 291e"))
    assert _unmarked_launches(offender) == []


def test_an_escape_marker_without_a_reason_does_not_excuse_a_launch(tmp_path: Path) -> None:
    offender = tmp_path / "test_offender.py"
    offender.write_text(
        "import subprocess, sys\n"
        f"subprocess.run([sys.executable, '-m', 'pytest'])  {_ESCAPE_MARKER}\n"
    )

    assert _unmarked_launches(offender) == [2]

    offender.write_text(
        "import subprocess, sys\n"
        f"subprocess.run([sys.executable, '-m', 'pytest'])  {_ESCAPE_MARKER} reproduces bug 291e\n"
    )

    assert _unmarked_launches(offender) == []


# --------------------------------------------------------------------------------------
# The evasion spellings (bug 16e1-237d).  A launch spelled any way other than the
# ``python -m pytest`` argv triple used to pass this guard vacuously; the file that
# revealed it was measured allocating four children into the SHARED numbered temp root
# and deleting four other sessions' roots.  Each case below SEEDS the defect on disk so
# the guard is shown to distinguish the broken form from the fixed one.
# --------------------------------------------------------------------------------------

#: The pre-fix body of ``tests/unit/test_anthropic_lazy_import_isolation_heldout.py``, kept
#: verbatim so reverting the fix is a defect the guard is permanently shown to catch (AC3).
_PRE_FIX_VIOLATOR = """\
import subprocess
import sys


def test_isolated(target: str) -> None:
    node = f"tests/unit/test_rp04_s4_llm_runtime_heldout.py::{target}"
    script = (
        "import sys; "
        "assert 'pydantic_ai.providers.anthropic' not in sys.modules; "
        "import pytest; "
        f"raise SystemExit(pytest.main(['-q', '-p', 'no:cacheprovider', {node!r}]))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
"""


def test_the_pre_fix_violator_is_a_detected_launch(tmp_path: Path) -> None:
    """AC2/AC3. The exact form that evaded this guard is now seen, and only when broken."""
    offender = tmp_path / "test_pre_fix.py"
    offender.write_text(_PRE_FIX_VIOLATOR)

    assert _unmarked_launches(offender) == [14], (
        "the python -c spelling of a nested pytest launch is invisible to this guard again"
    )


def test_a_dash_c_payload_naming_pytest_is_a_launch(tmp_path: Path) -> None:
    """``python -c '<script that runs pytest>'`` is ``python -m pytest`` in disguise."""
    offender = tmp_path / "test_inline.py"
    launch = (
        "import subprocess, sys\n"
        "subprocess.run(\n"
        "    [\n"
        "        sys.executable,\n"
        "        '-c',\n"
        "        'import pytest; raise SystemExit(pytest.main([\"-q\"]))',{marker}\n"
        "    ]\n"
        ")\n"
    )
    offender.write_text(launch.format(marker=""))
    assert _unmarked_launches(offender) == [3]

    offender.write_text(launch.format(marker=f"  {_ESCAPE_MARKER} reproduces bug 16e1"))
    assert _unmarked_launches(offender) == []


def test_a_dash_c_payload_that_never_names_pytest_is_not_a_launch(tmp_path: Path) -> None:
    """The many ``python -c`` children under tests/ that are not pytest stay unflagged."""
    innocent = tmp_path / "test_innocent.py"
    innocent.write_text(
        "import subprocess, sys\n"
        "subprocess.run([sys.executable, '-c', 'import json; print(json.dumps([1]))'])\n"
    )

    assert _unmarked_launches(innocent) == []


def test_a_bare_console_script_launch_without_a_basetemp_is_a_launch(tmp_path: Path) -> None:
    """The second evasion spelling: the ``pytest`` console script next to the interpreter.

    This one is admitted by an explicit ``--basetemp`` rather than by the helper, because
    routing it through ``python -m pytest`` would put the repository root on the child's
    ``sys.path`` and destroy the very thing such tests reproduce.
    """
    offender = tmp_path / "test_console.py"
    launch = (
        "import subprocess, sys\n"
        "from pathlib import Path\n"
        "console_script = Path(sys.executable).parent / 'pytest'\n"
        "subprocess.run(\n"
        "    [\n"
        "        str(console_script),\n"
        "        '-q',{basetemp}\n"
        "    ]\n"
        ")\n"
    )
    offender.write_text(launch.format(basetemp=""))
    assert _unmarked_launches(offender) == [5]

    offender.write_text(launch.format(basetemp="\n        '--basetemp',\n        'x',"))
    assert _unmarked_launches(offender) == []


def test_a_console_script_on_a_path_that_is_not_argv0_is_not_a_launch(tmp_path: Path) -> None:
    """A stub ``pytest`` written to a directory is a fixture, not a launch."""
    innocent = tmp_path / "test_stub.py"
    innocent.write_text(
        "import subprocess\n"
        "from pathlib import Path\n"
        "stub = Path('bin') / 'pytest'\n"
        "stub.write_text('#!/bin/sh\\nexit 5\\n')\n"
        "subprocess.run(['bash', '-c', 'pytest -q'])\n"
    )

    assert _unmarked_launches(innocent) == []
