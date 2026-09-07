"""Resolve the session identifier shared by event-producing commands.

Precedence:

1. ``REBAR_SESSION_ID``
2. ``CLAUDE_CODE_SESSION_ID``
3. ``OPENCODE_SESSION_ID``
4. ``SESSION_ID``

The first non-empty value wins. Whitespace-only values are absent.

The selected value is:

- opaque;
- returned verbatim;
- ``None`` when every variable is absent.

It is never:

- interpolated or executed;
- derived from git ``HEAD``.

Callers that need a display fallback provide it locally.
"""

from __future__ import annotations

import os

# Ordered, data-driven session-id env var list. Explicit rebar var first (authoritative
# override), native harness vars next in popularity order, ambient last. OpenCode ships a
# readable OPENCODE_SESSION_ID (epic OSS survey). Codex is deliberately NOT listed here: it
# exposes no supported readable session env var (see story 7656), so it is covered by its
# SessionStart shim exporting REBAR_SESSION_ID instead — not a native var. An absent /
# unrecognised var simply falls through (skipped), so it degrades to a lower-precedence var or
# None — never an error.
_SESSION_ID_VARS: tuple[str, ...] = (
    "REBAR_SESSION_ID",
    "CLAUDE_CODE_SESSION_ID",
    "OPENCODE_SESSION_ID",
    "SESSION_ID",
)

# rebar's harness-provenance convention var: an opaque tag naming the harness that produced
# the claim (base name "claude-code" / "opencode" / "codex" / "cursor", optionally
# "_<version>"-suffixed), populated by the
# same per-harness shims as REBAR_SESSION_ID (stories ec5c / 7656). Read verbatim.
_HARNESS_VAR = "AI_AGENT"

# Secondary Claude Code remote-session id, captured independently of the primary session id.
_REMOTE_SESSION_VAR = "CLAUDE_CODE_REMOTE_SESSION_ID"


def _first_nonempty(*varnames: str) -> str | None:
    """Return the first env var (in order) whose value is non-empty after strip, else None."""
    for var in varnames:
        val = os.environ.get(var)  # read-via: harness-protocol-env
        if val and val.strip():
            return val
    return None


def resolve_session_id() -> str | None:
    """Return the first non-empty session-id env var in precedence order, else ``None``.

    A value that is empty or whitespace-only is treated as absent (skipped). Never
    returns git HEAD — the var list contains no git call.
    """
    return _first_nonempty(*_SESSION_ID_VARS)


def resolve_harness() -> str | None:
    """Return the opaque harness-provenance tag (``AI_AGENT``), or ``None`` if unset/blank.

    Read verbatim — never interpolated or executed (provenance sensitivity).
    """
    return _first_nonempty(_HARNESS_VAR)


def resolve_remote_session() -> str | None:
    """Return the secondary Claude Code remote-session id, or ``None`` if unset/blank."""
    return _first_nonempty(_REMOTE_SESSION_VAR)
