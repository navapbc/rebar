"""`make lint` must reach the two test-hygiene gates, not defer them to CI.

Story ``73b8-2d79-3046-44ce``. ``scripts/check_raw_git_writes.py`` and
``scripts/check_wall_clock_asserts.py`` ran only as dedicated CI steps of
``_build-and-test.yml``, so the local fast gate returned a clean verdict over a tree CI
rejects — the reachability gap task ``2d9a-78c5-5f87-4a22`` closed for the comment-hygiene
gate. Without this guard the wiring is one silent Makefile edit away from regressing, and the
regression only shows up as a red CI run on someone else's change.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MAKEFILE = _REPO_ROOT / "Makefile"

# The gates this story wired. Each already ran in CI; each must now also be reachable
# locally. Listed explicitly rather than discovered, so ADDING a CI-only script is not
# silently treated as a `make lint` obligation.
_WIRED_GATES = (
    "scripts/check_raw_git_writes.py",
    "scripts/check_wall_clock_asserts.py",
)


def _lint_target_body(makefile_text: str) -> str:
    """Recipe lines of the ``lint`` target only — everything between ``lint:`` and the next
    target header. Mirrors the ``test_comment_hygiene_guard`` idiom: an invocation parked
    under some *other* target must not count as wired."""
    body: list[str] = []
    in_target = False
    for line in makefile_text.splitlines():
        if re.match(r"^lint:", line):
            in_target = True
            continue
        if in_target and re.match(r"^[A-Za-z0-9_.-]+:", line):
            break
        if in_target:
            body.append(line)
    return "\n".join(body)


@pytest.mark.parametrize("script", _WIRED_GATES)
def test_make_lint_invokes_the_hygiene_gate(script: str) -> None:
    body = _lint_target_body(_MAKEFILE.read_text(encoding="utf-8"))
    assert script in body, (
        f"`make lint` does not invoke {script} — the local fast gate would return a clean "
        "verdict over a tree CI rejects (story 73b8-2d79-3046-44ce)."
    )


@pytest.mark.parametrize("script", _WIRED_GATES)
def test_the_wiring_check_has_teeth(script: str) -> None:
    """Stripping the invocation from a synthetic Makefile copy flips the same assertion, and
    an invocation parked outside the ``lint`` target does not satisfy it."""
    mk = _MAKEFILE.read_text(encoding="utf-8")
    stripped = "\n".join(line for line in mk.splitlines() if script not in line)
    assert script not in _lint_target_body(stripped)
    relocated = stripped + f"\nsome-other-target:\n\tpython {script}\n"
    assert script not in _lint_target_body(relocated)
