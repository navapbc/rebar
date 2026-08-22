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

_TESTS_DIR = Path(__file__).resolve().parents[1]
_OWNER = _TESTS_DIR / "_nested_pytest.py"
_ESCAPE_MARKER = "# nested-pytest-ok:"
#: Stands for the ``sys.executable`` attribute, never for the string of the same name.
_SYS_EXECUTABLE = object()


def _launch_spans(tree: ast.AST) -> list[tuple[int, int]]:
    """First and last line of every literal spelling ``sys.executable``, ``-m``, ``pytest``."""
    spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.List | ast.Tuple):
            continue
        atoms: list[object] = []
        for element in node.elts:
            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                atoms.append(element.value)
            elif (
                isinstance(element, ast.Attribute)
                and element.attr == "executable"
                and isinstance(element.value, ast.Name)
                and element.value.id == "sys"
            ):
                atoms.append(_SYS_EXECUTABLE)
            else:
                atoms.append(None)
        window = list(zip(atoms, atoms[1:], atoms[2:], strict=False))
        if (_SYS_EXECUTABLE, "-m", "pytest") in window:
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
        path
        for path in sorted(_TESTS_DIR.rglob("*.py"))
        if any(line.startswith("def run_nested_pytest") for line in path.read_text().splitlines())
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
