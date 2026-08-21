"""Ticket 5304: amending a commit message must not silently drop its ``Change-Id``.

``git commit --amend -F <file>`` (and ``-m``) REPLACES the whole message, so the
``Change-Id`` trailer goes with it. Gerrit's ``commit-msg`` hook then stamps a FRESH one —
it only ever ADDS a Change-Id when none is present — and the next
``git push gerrit HEAD:refs/for/main`` opens a SECOND change instead of adding a patchset
to the existing one. That happened three times in one session (abandoned duplicates
Gerrit 1921, 1926, 1931).

A hook cannot close this: ``git commit --amend -F <file>`` hands
``prepare-commit-msg`` ``source='message'`` with an EMPTY sha — byte-identical to a fresh
``-F`` commit — so nothing downstream can tell "amending, keep the Change-Id" from
"new commit, stamp one". Every large Gerrit project (Go's ``git codereview change``,
OpenStack's ``git-review``, Android's ``repo upload``, Chromium's ``git cl upload``)
therefore makes the failure UNREACHABLE with a wrapper rather than DETECTABLE with a
guard. ``scripts/amend_commit_message.py`` (``make amend-msg FILE=…``) is that wrapper.

The load-bearing test here is the CONTRAST. "the wrapper preserved the Change-Id" passes
vacuously if the wrapper is broken in a way that leaves the message untouched, so the
same fixture, from the same starting commit, must also show the NAIVE form losing it.
"""

from __future__ import annotations

import importlib.util
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from _subprocess_env import subprocess_env

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "amend_commit_message.py"

# A stand-in for Gerrit's real commit-msg hook. It pins the two behaviours this test
# depends on and nothing else, so it stays hermetic and offline:
#   * a message that ALREADY carries a Change-Id is left alone (Gerrit's hook is
#     add-if-absent by design — that is what lets the wrapper work at all);
#   * every stamp it does emit is DISTINCT, so a re-stamp is visible as a changed id
#     rather than hiding behind a coincidentally equal value.
# The counter lives in .git/, outside the work tree, so `git reset --hard` does not
# rewind it: a broken wrapper that let the hook re-stamp yields a THIRD id, not the first.
FAKE_GERRIT_HOOK = """#!/bin/sh
grep -q '^Change-Id:' "$1" && exit 0
counter="$(dirname "$0")/../change-id-counter"
n=$(cat "$counter" 2>/dev/null || echo 0)
n=$((n + 1))
printf '%s\\n' "$n" > "$counter"
printf '\\nChange-Id: I%040d\\n' "$n" >> "$1"
"""

SEED_MESSAGE = "Seed the fixture commit\n"
REWRITTEN_MESSAGE = "Rewrite the message entirely\n\nA fresh body with no trailer at all.\n"

_CHANGE_ID = re.compile(r"^Change-Id:\s*(\S+)\s*$", re.MULTILINE)
_SIGN_OFF = re.compile(r"^Signed-off-by:\s*(.+\S)\s*$", re.MULTILINE)


def _load_script() -> Any:
    """Import the helper by path (``scripts/`` is not an importable package)."""
    spec = importlib.util.spec_from_file_location("amend_commit_message", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["amend_commit_message"] = module
    spec.loader.exec_module(module)
    return module


def _clean_git_env() -> dict[str, str]:
    """Ambient GIT_* pointing at the REAL checkout would redirect the fixture's git."""
    env = subprocess_env()
    for leaked in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"):
        env.pop(leaked, None)
    return env


def _git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=_clean_git_env(),
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.stdout


def _head_message(repo: Path) -> str:
    return _git("log", "-1", "--format=%B", cwd=repo)


def _head_change_id(repo: Path) -> str | None:
    found = _CHANGE_ID.findall(_head_message(repo))
    return found[-1] if found else None


def _head_sign_offs(repo: Path) -> list[str]:
    return _SIGN_OFF.findall(_head_message(repo))


def _make_repo(tmp_path: Path, *, with_gerrit_hook: bool, sign_offs: tuple[str, ...] = ()) -> Path:
    repo = tmp_path / ("stamped" if with_gerrit_hook else "unstamped")
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "fixture@example.com", cwd=repo)
    _git("config", "user.name", "Fixture", cwd=repo)
    _git("config", "commit.gpgsign", "false", cwd=repo)
    if with_gerrit_hook:
        hook = repo / ".git" / "hooks" / "commit-msg"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text(FAKE_GERRIT_HOOK, encoding="utf-8")
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git("add", "seed.txt", cwd=repo)
    seed = SEED_MESSAGE.strip()
    if sign_offs:
        # `git commit -s` would stamp the committer identity; writing the trailer block
        # directly lets the fixture pin ARBITRARY identities and MULTIPLE sign-offs, in
        # a chosen order, exactly as a real multi-author commit carries them. The fake
        # Gerrit hook then appends the Change-Id after them, as the real hook does.
        seed += "\n\n" + "\n".join(f"Signed-off-by: {value}" for value in sign_offs)
    _git("commit", "-q", "-m", seed, cwd=repo)
    return repo


def _run_wrapper(repo: Path, message_file: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(message_file)],
        cwd=repo,
        env=_clean_git_env(),
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.fixture
def stamped_repo(tmp_path: Path) -> Path:
    """A throwaway repo whose HEAD carries a Change-Id, exactly as Gerrit would stamp it."""
    return _make_repo(tmp_path, with_gerrit_hook=True)


def test_wrapper_preserves_change_id_where_naive_amend_loses_it(
    stamped_repo: Path, tmp_path: Path
) -> None:
    """The wrapper carries the Change-Id forward; ``--amend -F`` on the SAME commit does not.

    Both halves run from the identical starting commit in the identical repo, so the
    contrast is the evidence: without it, "the wrapper preserved the Change-Id" would
    also pass for a wrapper that never rewrote the message.
    """
    original = _head_change_id(stamped_repo)
    assert original is not None, "fixture precondition: the seed commit must carry a Change-Id"
    seed_sha = _git("rev-parse", "HEAD", cwd=stamped_repo).strip()

    message_file = tmp_path / "message.txt"
    message_file.write_text(REWRITTEN_MESSAGE, encoding="utf-8")

    # NEGATIVE CONTROL — the naive form the wrapper exists to replace.
    _git("commit", "--amend", "-q", "-F", str(message_file), cwd=stamped_repo)
    naive = _head_change_id(stamped_repo)
    assert naive != original, (
        "negative control is inert: `git commit --amend -F` kept the Change-Id, so this "
        "fixture cannot distinguish a working wrapper from a no-op one"
    )
    assert REWRITTEN_MESSAGE.splitlines()[0] in _head_message(stamped_repo)

    # Rewind to the very same starting commit and take the wrapper's path instead.
    _git("reset", "--hard", "-q", seed_sha, cwd=stamped_repo)
    assert _head_change_id(stamped_repo) == original

    result = _run_wrapper(stamped_repo, message_file)
    assert result.returncode == 0, result.stderr

    amended = _head_message(stamped_repo)
    assert _head_change_id(stamped_repo) == original
    # The message really was REWRITTEN — a wrapper that silently did nothing would
    # satisfy the Change-Id assertion above all on its own.
    assert amended.startswith(REWRITTEN_MESSAGE.splitlines()[0])
    assert "A fresh body with no trailer at all." in amended
    assert SEED_MESSAGE.strip() not in amended
    assert _git("rev-parse", "HEAD", cwd=stamped_repo).strip() != seed_sha


def test_supplied_file_change_id_is_replaced_by_heads(stamped_repo: Path, tmp_path: Path) -> None:
    """A stray Change-Id in FILE must not survive: Gerrit rejects two of them."""
    original = _head_change_id(stamped_repo)
    message_file = tmp_path / "message.txt"
    message_file.write_text(
        "Rewrite with a stray trailer\n\nBody.\n\n"
        "Change-Id: Ideadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n",
        encoding="utf-8",
    )

    result = _run_wrapper(stamped_repo, message_file)
    assert result.returncode == 0, result.stderr

    amended = _head_message(stamped_repo)
    assert _CHANGE_ID.findall(amended) == [original]


def test_wrapper_preserves_sign_offs_where_naive_amend_loses_them(tmp_path: Path) -> None:
    """Ticket ebfa-3804: HEAD's ``Signed-off-by`` trailers survive the amend, in order.

    Same contrast structure as the Change-Id test: the naive ``--amend -F`` from the
    identical starting commit must LOSE the sign-offs, or the fixture cannot tell a
    working wrapper from a no-op one.
    """
    sign_offs = ("Alice Dev <alice@example.com>", "Bob Dev <bob@example.com>")
    repo = _make_repo(tmp_path, with_gerrit_hook=True, sign_offs=sign_offs)
    assert _head_sign_offs(repo) == list(sign_offs), (
        "fixture precondition: the seed commit must carry both sign-offs, in order"
    )
    original_change_id = _head_change_id(repo)
    assert original_change_id is not None
    seed_sha = _git("rev-parse", "HEAD", cwd=repo).strip()

    message_file = tmp_path / "message.txt"
    message_file.write_text(REWRITTEN_MESSAGE, encoding="utf-8")

    # NEGATIVE CONTROL — the naive form loses every sign-off.
    _git("commit", "--amend", "-q", "-F", str(message_file), cwd=repo)
    assert _head_sign_offs(repo) == [], (
        "negative control is inert: `git commit --amend -F` kept the sign-offs, so this "
        "fixture cannot distinguish a working wrapper from a no-op one"
    )

    # Rewind to the very same starting commit and take the wrapper's path instead.
    _git("reset", "--hard", "-q", seed_sha, cwd=repo)
    assert _head_sign_offs(repo) == list(sign_offs)

    result = _run_wrapper(repo, message_file)
    assert result.returncode == 0, result.stderr

    amended = _head_message(repo)
    assert _head_sign_offs(repo) == list(sign_offs)
    assert _head_change_id(repo) == original_change_id
    assert amended.startswith(REWRITTEN_MESSAGE.splitlines()[0])
    assert SEED_MESSAGE.strip() not in amended


def test_supplied_file_sign_off_is_respected_and_not_duplicated(tmp_path: Path) -> None:
    """A FILE that carries its own sign-off wins: nothing is carried, nothing doubled.

    The carry-forward exists only for a file that OMITS the trailer. When the author
    supplies one — even HEAD's very own — the file's trailer block is authoritative, so
    HEAD's sign-offs must not be appended next to (or on top of) it.
    """
    repo = _make_repo(tmp_path, with_gerrit_hook=True, sign_offs=("Alice Dev <alice@example.com>",))

    message_file = tmp_path / "message.txt"
    message_file.write_text(
        "Rewrite with its own sign-off\n\nBody.\n\nSigned-off-by: Alice Dev <alice@example.com>\n",
        encoding="utf-8",
    )

    result = _run_wrapper(repo, message_file)
    assert result.returncode == 0, result.stderr
    assert _head_sign_offs(repo) == ["Alice Dev <alice@example.com>"]

    # And a DIFFERENT identity in FILE is equally authoritative: HEAD's is not re-added.
    message_file.write_text(
        "Rewrite signed by someone else\n\nBody.\n\nSigned-off-by: Carol Dev <carol@example.com>\n",
        encoding="utf-8",
    )
    result = _run_wrapper(repo, message_file)
    assert result.returncode == 0, result.stderr
    assert _head_sign_offs(repo) == ["Carol Dev <carol@example.com>"]


def test_no_sign_off_on_head_invents_none(stamped_repo: Path, tmp_path: Path) -> None:
    """A HEAD without a sign-off stays without one — the wrapper never fabricates a DCO."""
    assert _head_sign_offs(stamped_repo) == [], (
        "fixture precondition: the seed commit must carry no sign-off"
    )
    message_file = tmp_path / "message.txt"
    message_file.write_text(REWRITTEN_MESSAGE, encoding="utf-8")

    result = _run_wrapper(stamped_repo, message_file)
    assert result.returncode == 0, result.stderr
    assert _head_sign_offs(stamped_repo) == []
    assert _head_change_id(stamped_repo) is not None


def test_refuses_to_amend_when_head_carries_no_change_id(
    tmp_path: Path,
) -> None:
    """No Change-Id on HEAD means the commit-msg hook is missing — fail, do not amend."""
    repo = _make_repo(tmp_path, with_gerrit_hook=False)
    assert _head_change_id(repo) is None, "fixture precondition: HEAD must have no Change-Id"
    before = _git("rev-parse", "HEAD", cwd=repo).strip()

    message_file = tmp_path / "message.txt"
    message_file.write_text(REWRITTEN_MESSAGE, encoding="utf-8")

    result = _run_wrapper(repo, message_file)

    assert result.returncode != 0
    assert "Change-Id" in result.stderr
    assert "make hooks" in result.stderr
    # Loudly refusing means HEAD is untouched — not amended into a commit the hook will
    # then stamp with a fresh id, which is the very failure being prevented.
    assert _git("rev-parse", "HEAD", cwd=repo).strip() == before
    assert SEED_MESSAGE.strip() in _head_message(repo)


def test_missing_message_file_is_a_loud_failure(stamped_repo: Path, tmp_path: Path) -> None:
    """A typo'd FILE must not amend HEAD to an empty message."""
    before = _git("rev-parse", "HEAD", cwd=stamped_repo).strip()

    result = _run_wrapper(stamped_repo, tmp_path / "does-not-exist.txt")

    assert result.returncode != 0
    assert _git("rev-parse", "HEAD", cwd=stamped_repo).strip() == before


def test_strip_change_id_lines_removes_every_variant() -> None:
    """The pure part of the composition, exercised without touching git."""
    module = _load_script()
    stripped = module.strip_change_id_lines(
        "Subject\n\nBody\n\nSigned-off-by: A <a@example.com>\n"
        "Change-Id: I1111111111111111111111111111111111111111\n"
        "change-id: I2222222222222222222222222222222222222222\n"
    )
    assert "Change-Id" not in stripped
    assert "change-id" not in stripped
    assert "Signed-off-by: A <a@example.com>" in stripped
    assert stripped.startswith("Subject\n\nBody\n")


def test_extract_change_id_takes_the_last_trailer() -> None:
    module = _load_script()
    assert module.extract_change_id("Subject\n\nBody\n") is None
    assert (
        module.extract_change_id(
            "Subject\n\nChange-Id: I1111111111111111111111111111111111111111\n"
        )
        == "I1111111111111111111111111111111111111111"
    )


def test_make_target_delegates_to_the_script() -> None:
    """`make amend-msg FILE=…` is the documented entry point, so keep it wired."""
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "amend-msg:" in makefile
    assert "scripts/amend_commit_message.py" in makefile
    phony = next(line for line in makefile.splitlines() if line.startswith(".PHONY:"))
    assert "amend-msg" in phony.split()
