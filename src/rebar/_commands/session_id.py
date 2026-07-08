"""Shared session-id resolution (epic crust-fetch-stump, story 6014).

ONE resolver for "which coding-agent session emitted this event", replacing the two
divergent chains that used to live in :mod:`rebar._commands.session_log` and
:mod:`rebar._commands.transition_close`. The ordered var list is data-driven so a new
harness var is a one-line add (story c557 appends the OSS harness vars
``OPENCODE_SESSION_ID`` / ``CODEX_THREAD_ID`` here).

Precedence (first NON-EMPTY wins): the explicit, rebar-owned ``REBAR_SESSION_ID`` (an
operator/hook override is authoritative), then the native harness var
``CLAUDE_CODE_SESSION_ID``, then the ambient ``SESSION_ID``; else ``None``.

Deliberate non-goals:

- This resolver NEVER falls back to git HEAD. A HEAD changes on every commit within one
  session, so it is not a session id — call sites that need a cosmetic non-empty string
  (e.g. the FORCE_CLOSE audit comment) keep that fallback LOCALLY.
- An empty / whitespace-only value is treated as ABSENT (skipped), matching the falsy
  ``or``-chain semantics the old resolvers had.

The value is read as an opaque string and returned verbatim — never interpolated or
executed (consent/provenance sensitivity, epic gotcha).
"""

from __future__ import annotations

import os

# Ordered, data-driven session-id env var list. Explicit rebar var first (authoritative
# override), native harness var next, ambient last. Extend by appending a var name —
# story c557 adds OPENCODE_SESSION_ID / CODEX_THREAD_ID here.
_SESSION_ID_VARS: tuple[str, ...] = (
    "REBAR_SESSION_ID",
    "CLAUDE_CODE_SESSION_ID",
    "SESSION_ID",
)


def resolve_session_id() -> str | None:
    """Return the first non-empty session-id env var in precedence order, else ``None``.

    A value that is empty or whitespace-only is treated as absent (skipped). Never
    returns git HEAD — the var list contains no git call.
    """
    for var in _SESSION_ID_VARS:
        val = os.environ.get(var)
        if val and val.strip():
            return val
    return None
