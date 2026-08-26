"""The one merge-abort recovery toolkit and its uniqueness guard (story da84-924e-0f49-470a).

``sync.reconverge`` and ``push_recovery``'s non-fast-forward retry both have to survive
the same recoverable ``git merge`` aborts. The parser and the quarantine path arithmetic
used to live in ``sync`` with ``push_recovery`` reaching sideways for them, and the
quarantine MOVER was written TWICE — the two copies having already drifted, since only
push_recovery's verified that a named path is genuinely untracked before relocating it
(bugs ``small-delicious-loris`` / ``sulfuryl-suicidal-osprey``).

These tests pin two things:

1. **the toolkit** — the shared mover's three refusal fences and its move, the parser's
   marker fence, and the CONVERGENCE: the untracked fence now answers on sync's door too,
   which is the one behaviour this consolidation deliberately adds;
2. **the guard** — a second quarantine mover anywhere under ``src/rebar`` cannot merge
   without a reasoned escape marker. Exercised against synthetic sources as well as the
   real tree, so a guard that can no longer fail is itself caught.
"""

from __future__ import annotations

import ast
import subprocess
import textwrap
from pathlib import Path

import pytest
from _tree_scan import parsed_python_files

from rebar._store import merge_recovery, sync

SRC = Path(__file__).resolve().parents[3] / "src" / "rebar"

_LEFTOVER = "aaaa-bbbb-cccc-dddd/0001-SNAPSHOT.json"
_TRACKED = "aaaa-bbbb-cccc-dddd/0002-event.json"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )


def _tickets_repo(tmp_path: Path) -> Path:
    """A minimal tickets-shaped repo with one committed event and one untracked leftover."""
    repo = tmp_path / "tracker"
    (repo / "aaaa-bbbb-cccc-dddd").mkdir(parents=True)
    _git(repo.parent, "init", "-q", "-b", "tickets", str(repo))
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / _TRACKED).write_text('{"e":"committed"}')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    (repo / _LEFTOVER).write_text('{"snapshot":"leftover"}')
    return repo


def _quarantined(repo: Path) -> list[Path]:
    root = repo / ".git" / "reconverge-quarantine"
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


def _abort(stderr: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["git", "merge"], 2, "", stderr)


# ── The shared mover ──────────────────────────────────────────────────────────────────


def test_the_mover_relocates_an_untracked_leftover_and_preserves_its_bytes(
    tmp_path: Path,
) -> None:
    repo = _tickets_repo(tmp_path)

    assert merge_recovery.quarantine_untracked(_git, str(repo), [_LEFTOVER]) is True

    assert not (repo / _LEFTOVER).exists(), "the colliding path must be out of the way"
    preserved = [p for p in _quarantined(repo) if p.read_text() == '{"snapshot":"leftover"}']
    assert preserved, "the leftover's bytes must survive in quarantine (moved, never deleted)"


def test_the_mover_refuses_a_tracked_path_and_moves_nothing(tmp_path: Path) -> None:
    """The untracked (``??``) fence: a mis-parse must never relocate TRACKED data, so one
    non-untracked path in the batch refuses the WHOLE recovery — including the leftover
    that would otherwise have moved."""
    repo = _tickets_repo(tmp_path)

    assert merge_recovery.quarantine_untracked(_git, str(repo), [_LEFTOVER, _TRACKED]) is False

    assert (repo / _TRACKED).read_text() == '{"e":"committed"}'
    assert (repo / _LEFTOVER).exists(), "the fence is checked for ALL paths before any move"
    assert _quarantined(repo) == []


def test_syncs_door_now_carries_the_untracked_fence(tmp_path: Path) -> None:
    """The CONVERGENCE this consolidation adds: ``sync._quarantine_untracked`` — the door
    reconverge and ``doctor --repair`` both reach for — used to move whatever it was
    handed. Sharing one mover gives it the fence push_recovery's copy already had."""
    repo = _tickets_repo(tmp_path)

    assert sync._quarantine_untracked(str(repo), [_TRACKED]) is False
    assert (repo / _TRACKED).read_text() == '{"e":"committed"}'


def test_the_mover_refuses_an_unresolvable_common_dir(tmp_path: Path) -> None:
    """A quarantine path computed from an empty ``--git-common-dir`` would land INSIDE
    the working tree, so the recovery is refused outright."""
    (tmp_path / "leftover.json").write_text("x")

    def blank_common(_path: str, *args: str) -> subprocess.CompletedProcess:
        out = "" if args[:2] == ("rev-parse", "--git-common-dir") else "?? leftover.json\n"
        return subprocess.CompletedProcess(["git", *args], 0, out, "")

    assert merge_recovery.quarantine_untracked(blank_common, str(tmp_path), ["leftover.json"]) is (
        False
    )
    assert (tmp_path / "leftover.json").read_text() == "x", "nothing may move on refusal"


def test_the_mover_refuses_a_path_that_vanished(tmp_path: Path) -> None:
    """A path that disappeared between the status check and the move (a concurrent
    writer) answers False rather than raising, so the caller keeps its abort net."""
    gitdir = tmp_path / ".git"
    gitdir.mkdir()

    def scripted(_path: str, *args: str) -> subprocess.CompletedProcess:
        out = str(gitdir) if args[:2] == ("rev-parse", "--git-common-dir") else "?? gone.json\n"
        return subprocess.CompletedProcess(["git", *args], 0, out, "")

    assert merge_recovery.quarantine_untracked(scripted, str(tmp_path), ["gone.json"]) is False


# ── The shared abort parser ───────────────────────────────────────────────────────────


def test_the_parser_names_only_the_paths_under_its_own_marker() -> None:
    """Each variant reads its own marker's indented block and stops at the trailer, so
    an abort naming BOTH classes never cross-contaminates the two recovery routes."""
    merge = _abort(
        "error: The following untracked working tree files would be overwritten by merge:\n"
        f"\t{_LEFTOVER}\n"
        "Please move or remove them before you merge.\n"
        "error: Your local changes to the following files would be overwritten by merge:\n"
        f"\t{_TRACKED}\n"
        "Please commit your changes or stash them before you merge.\n"
        "Aborting\n"
    )

    assert merge_recovery.untracked_overwrite_paths(merge) == [_LEFTOVER]
    assert merge_recovery.local_change_paths(merge) == [_TRACKED]


def test_the_parser_answers_empty_when_its_marker_is_absent() -> None:
    """The fence that keeps recovery scoped to exactly the recoverable classes: an
    ordinary merge CONFLICT names no marker, so nothing is proposed for relocation."""
    conflict = _abort("CONFLICT (content): Merge conflict in a/b.json\nAutomatic merge failed;\n")

    assert merge_recovery.untracked_overwrite_paths(conflict) == []
    assert merge_recovery.local_change_paths(conflict) == []


# ── Guard: the quarantine mover is written ONCE ───────────────────────────────────────

QUARANTINE_OWNER = "_store/merge_recovery.py"
QUARANTINE_ESCAPE = "# quarantine-move-ok:"
_MOVE_ATTRS = ("move", "copy2")
_QUARANTINE_ATOM = "quarantine"


def _escaped_lines(source: str, escape: str) -> set[int]:
    """1-based line numbers sanctioned by *escape* with a MANDATORY reason: the escape
    line itself and the line after it, so a move can be tagged in place or above."""
    lines: set[int] = set()
    for i, line in enumerate(source.splitlines(), start=1):
        if escape not in line:
            continue
        if not line.split(escape, 1)[1].strip():
            raise AssertionError(f"{escape} on line {i} carries no reason")
        lines.update({i, i + 1})
    return lines


def _names_quarantine(fn: ast.AST) -> bool:
    """Whether any IDENTIFIER inside *fn* names the quarantine. Identifiers only — prose
    in a docstring is not a mover, and this is the second half of the conjunction."""
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and _QUARANTINE_ATOM in node.id.lower():
            return True
        if isinstance(node, ast.Attribute) and _QUARANTINE_ATOM in node.attr.lower():
            return True
        if isinstance(node, ast.arg) and _QUARANTINE_ATOM in node.arg.lower():
            return True
    return False


def quarantine_move_violations(source: str) -> list[tuple[int, str]]:
    """Function scopes carrying BOTH atoms of the construct — a ``shutil.move`` /
    ``shutil.copy2`` call AND an identifier naming the quarantine — minus escaped lines.
    The CONJUNCTION is what discriminates: a bare file move is not a quarantine mover,
    and a function that merely names the quarantine relocates nothing."""
    tree = ast.parse(source)
    escaped = _escaped_lines(source, QUARANTINE_ESCAPE)
    out: list[tuple[int, str]] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        moves = [
            node
            for node in ast.walk(fn)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _MOVE_ATTRS
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "shutil"
        ]
        if not moves or not _names_quarantine(fn):
            continue
        out.extend((node.lineno, fn.name) for node in moves if node.lineno not in escaped)
    return sorted(out)


def test_the_quarantine_mover_lives_only_in_the_owner_module() -> None:
    """The guard. Every quarantine relocation under ``src/rebar`` belongs to
    ``merge_recovery.py``; a second copy needs a reasoned escape marker."""
    offenders = {}
    for module in parsed_python_files(SRC):
        if module.path.relative_to(SRC).as_posix() == QUARANTINE_OWNER:
            continue
        hits = quarantine_move_violations(module.source)
        if hits:
            offenders[module.path.relative_to(SRC).as_posix()] = hits
    assert not offenders, f"quarantine movers outside {QUARANTINE_OWNER}: {offenders}"


def test_the_guard_catches_a_reintroduced_second_mover() -> None:
    """The guard can FAIL — a forked mover coming back is caught at its move line."""
    src = textwrap.dedent("""
        def _quarantine_untracked_paths(base, paths):
            quarantine = _dir(base)
            for rel in paths:
                shutil.move(str(base / rel), str(quarantine / rel))
    """)
    assert quarantine_move_violations(src) == [(5, "_quarantine_untracked_paths")]


def test_the_guard_needs_both_atoms() -> None:
    """Neither atom alone is the construct: an ordinary file move is not a quarantine
    mover, and naming the quarantine without relocating anything is not either."""
    plain_move = "def stage(a, b):\n    shutil.move(a, b)\n"
    assert quarantine_move_violations(plain_move) == []

    names_only = "def report(quarantine):\n    return str(quarantine)\n"
    assert quarantine_move_violations(names_only) == []

    prose = '''
def stage(a, b):
    """Not the quarantine mover, whatever this docstring says about quarantine."""
    shutil.move(a, b)
'''
    assert quarantine_move_violations(prose) == []


def test_the_guard_escape_requires_a_reason() -> None:
    """An escape marker with no reason is itself a failure — the exception has to be
    visible in review, not silently absent."""
    body = "def spill(paths, quarantine):\n    shutil.move(paths[0], quarantine)  {marker}\n"
    assert (
        quarantine_move_violations(body.format(marker=f"{QUARANTINE_ESCAPE} audited one-off")) == []
    )
    with pytest.raises(AssertionError):
        quarantine_move_violations(body.format(marker=QUARANTINE_ESCAPE))
