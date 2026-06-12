"""Tier kill-switches for the bash→Python strangler-fig migration.

Each migration tier (``docs/bash-migration.md``) is guarded by ONE environment
switch selecting the ``bash`` or ``python`` implementation for the commands ported
so far. This module is the single source of truth for parsing those switches; the
bash dispatcher pins its own parse to this one (a parity test asserts identical
resolution), so when a tier's switch is retired both sides go with it.

Parsing follows the established ``REBAR_PUSH`` idiom (ticket-lib.sh): the value is
case-insensitive and whitespace-stripped. An **unrecognized** value falls back to
the tier's default with a one-line stderr warning — never a hard failure, because
an env typo must not take down an agent fleet mid-run. A command that has not yet
been ported ignores the switch entirely (it is simply never routed through here),
so ``REBAR_LEAF_WRITES=python`` mid-tier is safe: it selects Python only where
Python exists.
"""

from __future__ import annotations

import os
import sys

# Tier switches and their defaults. A tier's default stays ``bash`` until its
# parity is green and its dogfood soak passes; then the default flips to
# ``python`` here in one commit (the switch is retained as the rollback lever
# until the tier is retired, when its entry is deleted).
#
# Tier B (``REBAR_LEAF_WRITES``) was retired on 2026-06-11: after the soak
# (full dual-run parity, 240-test interface tier, 77/77 live full-surface probe,
# fsck clean) the default flipped to python and then the switch + bash leaf bodies
# were deleted — Python is now the sole leaf-write implementation. The remaining
# entries gate the tiers still to come.
_TIERS: dict[str, str] = {
    "REBAR_COMPUTE": "bash",  # Tier C — compute-heavy reads
    "REBAR_WRITE_CORE": "bash",  # Tier D — write/sync core
}

_VALID = ("bash", "python")


def resolve(switch: str) -> str:
    """Resolve a tier switch to ``"bash"`` or ``"python"``.

    Case-insensitive and whitespace-stripped (the ``REBAR_PUSH`` idiom). Unset or
    empty resolves to the tier default; an unrecognized non-empty value resolves to
    the default and warns once on stderr. Unknown switch names are a programming
    error and raise ``KeyError``.
    """
    default = _TIERS[switch]
    raw = os.environ.get(switch)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value == "":
        return default
    if value in _VALID:
        return value
    print(
        f"rebar: warning: unrecognized {switch}={raw!r}; "
        f"falling back to {default!r}",
        file=sys.stderr,
    )
    return default


def uses_python(switch: str) -> bool:
    """True when the named tier switch selects the Python implementation."""
    return resolve(switch) == "python"
