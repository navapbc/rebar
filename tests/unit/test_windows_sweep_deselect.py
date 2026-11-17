"""The advisory Windows sweep runs a maintained subset that is held green.

`sweep (windows)` in ``.github/workflows/test.yml`` passes every pytest argument file under
``.github/windows-sweep-deselect/`` to pytest. Each file holds the tests known to fail on Windows
for one defect class, so a red sweep means a NEW Windows regression rather than standing noise
(bug 6798-606d-37dc-46e7). These tests keep that list honest on the Linux gate: every file is
wired into the command, every line is a well-formed pytest argument, and every line still names a
test module that exists, so a renamed or deleted module cannot leave a dead entry behind.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_TEST_YML = _ROOT / ".github" / "workflows" / "test.yml"
_DESELECT_DIR = _ROOT / ".github" / "windows-sweep-deselect"
_ARGFILE_REF = re.compile(r"'@(\.github/windows-sweep-deselect/[^']+)'")
_LINE = re.compile(r"^--(deselect|ignore)=(tests/[^:\s]+\.py)(::.+)?$")


def _argfiles() -> list[Path]:
    return sorted(_DESELECT_DIR.glob("*.args"))


def _windows_suite_command() -> str:
    workflow = yaml.safe_load(_TEST_YML.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["sweep-windows"]["steps"]
    return next(step["run"] for step in steps if step.get("name") == "Run the default suite")


def test_every_argfile_is_passed_to_the_windows_suite_and_nothing_else() -> None:
    referenced = set(_ARGFILE_REF.findall(_windows_suite_command()))
    on_disk = {path.relative_to(_ROOT).as_posix() for path in _argfiles()}
    assert on_disk, "the deselect directory holds no argument files"
    assert referenced == on_disk, (
        "sweep (windows) must pass exactly the files in .github/windows-sweep-deselect/; "
        f"missing from the command: {sorted(on_disk - referenced)}, "
        f"referenced but absent: {sorted(referenced - on_disk)}"
    )


@pytest.mark.parametrize("argfile", _argfiles(), ids=lambda path: path.name)
def test_argfile_lines_are_well_formed_sorted_and_unique(argfile: Path) -> None:
    raw = argfile.read_bytes()
    assert raw.isascii(), f"{argfile.name} must stay ASCII so any argparse encoding reads it"
    assert b"\r" not in raw, (
        f"{argfile.name} was checked out with CRLF line endings; .gitattributes must pin "
        ".github/windows-sweep-deselect/*.args to eol=lf"
    )
    lines = raw.decode("ascii").split("\n")
    assert lines[-1] == "", f"{argfile.name} must end with one newline"
    lines = lines[:-1]
    assert lines, f"{argfile.name} is empty; delete it and its reference instead"
    malformed = [line for line in lines if not _LINE.match(line)]
    assert not malformed, (
        f"{argfile.name}: every line must be --deselect=<nodeid> or --ignore=<module>, "
        f"with no blanks or comments (argparse reads each line as one argument): {malformed[:5]}"
    )
    ignores = [line for line in lines if line.startswith("--ignore=") and "::" in line]
    assert not ignores, f"{argfile.name}: --ignore takes a module path, not a node id: {ignores}"
    assert lines == sorted(lines), f"{argfile.name} must be sorted"
    assert len(lines) == len(set(lines)), f"{argfile.name} holds duplicate lines"


def test_every_argfile_is_pinned_to_lf_line_endings() -> None:
    """``eol=lf`` keeps a Windows ``core.autocrlf`` checkout from adding ``\\r`` to each line.

    The byte check above proves LF on the platform that runs it; this proves the attribute that
    guarantees it on every platform, including the Linux gate where autocrlf is off.
    """
    paths = [path.relative_to(_ROOT).as_posix() for path in _argfiles()]
    result = subprocess.run(
        ["git", "check-attr", "eol", "--", *paths],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    unpinned = [line for line in result.stdout.splitlines() if not line.endswith(": eol: lf")]
    assert len(result.stdout.splitlines()) == len(paths)
    assert not unpinned, f"these argument files are not pinned to eol=lf: {unpinned}"


def test_no_test_is_listed_in_two_classes() -> None:
    owner: dict[str, str] = {}
    shared: list[str] = []
    for argfile in _argfiles():
        for line in argfile.read_text(encoding="ascii").splitlines():
            if line in owner:
                shared.append(f"{line} ({owner[line]}, {argfile.name})")
            owner[line] = argfile.name
    assert not shared, f"each test belongs to one defect class: {shared[:5]}"


def test_every_listed_module_still_exists() -> None:
    stale = [
        f"{argfile.name}: {line}"
        for argfile in _argfiles()
        for line in argfile.read_text(encoding="ascii").splitlines()
        if (match := _LINE.match(line)) and not (_ROOT / match.group(2)).is_file()
    ]
    assert not stale, (
        "these entries name a test module that no longer exists; remove them so the Windows "
        f"sweep list stays exact: {stale[:10]}"
    )
