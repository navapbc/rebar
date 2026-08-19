"""Recorded legacy exceptions for the config-ownership gate (RP-04 S7.1, ticket 29a9).

Each row suppresses ONE genuine current below-seam ambient read that predates the gate:
an exact ``path`` (relative to ``src/rebar``), the exact ``symbol`` (env-name / callee) the
gate reports, and a specific ``rationale`` naming the reading module, line, and access. This
is the ONLY sanctioned way to make the real tree pass — product code is never annotated to
silence the gate, and detection is never weakened. Retire a row by moving its read behind the
relevant composition-root or provider-credential seam.

The whitelist is now EMPTY: every legacy below-seam read has been cut to an owned
configuration seam (RP-04 C3 subsystem cutovers). New rows should not be added — route a
new read through the appropriate seam instead.
"""

from __future__ import annotations

# (path, symbol, rationale) — expanded into the dict contract below.
_ROWS: list[tuple[str, str, str]] = []

LEGACY_EXCEPTIONS: list[dict] = [
    {"path": path, "symbol": symbol, "rationale": rationale} for path, symbol, rationale in _ROWS
]
