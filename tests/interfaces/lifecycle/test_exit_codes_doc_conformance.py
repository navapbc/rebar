"""Conformance oracle for ticket f0ff — every public dispatcher arm must have a
row in ``docs/exit-codes.md``.

The document declares itself the single source of truth for "each public
dispatcher arm", yet shipped with 41 rows against 75 real arms. The arm set is
derived MECHANICALLY (the union of the two authoritative sources) so a new
command that lacks a row fails this check — the CI guard the ticket adds (AC4).
"""

from __future__ import annotations

import re
from pathlib import Path

_DOC = Path(__file__).resolve().parents[3] / "docs" / "exit-codes.md"

# A per-command table row starts with `| \`<name>\`` where <name> is a lowercase
# subcommand (the numeric code-table rows start with a digit, so are excluded).
_ROW_RE = re.compile(r"^\| `([a-z][a-z0-9-]+)`", re.MULTILINE)


def dispatcher_arms() -> set[str]:
    """The authoritative set of public dispatcher arms: the union of the pinned
    help-text subcommands and the intercepted subcommands."""
    import rebar._cli as cli
    from rebar._cli import _help

    return set(cli._INTERCEPTS) | set(_help.known_subcommands())


def documented_arms(text: str | None = None) -> set[str]:
    doc = text if text is not None else _DOC.read_text(encoding="utf-8")
    return set(_ROW_RE.findall(doc))


def undocumented_arms(arms: set[str], documented: set[str]) -> set[str]:
    """Pure set-diff: arms with no documentation row."""
    return arms - documented


def test_every_dispatcher_arm_has_an_exit_code_row() -> None:
    arms = dispatcher_arms()
    missing = undocumented_arms(arms, documented_arms())
    assert not missing, (
        f"{len(missing)} dispatcher arm(s) have no row in docs/exit-codes.md "
        f"(the single source of truth): {sorted(missing)}"
    )
