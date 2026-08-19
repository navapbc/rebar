"""Recorded legacy exceptions for the config-ownership gate (RP-04 S7.1, ticket 29a9).

Each row suppresses ONE genuine current below-seam ambient read that predates the gate:
an exact ``path`` (relative to ``src/rebar``), the exact ``symbol`` (env-name / callee) the
gate reports, and a specific ``rationale`` naming the reading module, line, and access. This
is the ONLY sanctioned way to make the real tree pass — product code is never annotated to
silence the gate, and detection is never weakened. Retire a row by moving its read behind the
relevant composition-root or provider-credential seam.
"""

from __future__ import annotations

# (path, symbol, rationale) — expanded into the dict contract below.
_ROWS: list[tuple[str, str, str]] = [
    (
        "_engine/rebar_reconciler/binding_lifecycle.py",
        "RECONCILER_ABSENT_RETIRE_GRACE",
        "binding_lifecycle.py L314: legacy env read of RECONCILER_ABSENT_RETIRE_GRACE, MOVED "
        "here from binding_store.py with the note_absent absence policy it parameterizes "
        "(RP-02 S2 T2). Still ambient and byte-identical to the pre-move read — no behaviour, "
        "default or clamp changed; only the owning module did. Cutting it to the configuration "
        "seam remains RP-04 S7.3.a's slice (ticket best-kingly-monkey tracks the retarget).",
    ),
    (
        "_engine/rebar_reconciler/binding_lifecycle.py",
        "os.environ.get",
        "binding_lifecycle.py L69: legacy dynamic env read (os.environ.get) inside the "
        "_env_int defensive parser, MOVED here from binding_store.py with the absence policy "
        "it serves (RP-02 S2 T2). Still ambient and byte-identical to the pre-move read. "
        "Cutting it to the configuration seam remains RP-04 S7.3.a's slice (ticket "
        "best-kingly-monkey tracks the retarget).",
    ),
]

LEGACY_EXCEPTIONS: list[dict] = [
    {"path": path, "symbol": symbol, "rationale": rationale} for path, symbol, rationale in _ROWS
]
