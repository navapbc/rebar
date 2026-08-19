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
    (
        "_logging.py",
        "REBAR_LOG_LEVEL",
        "_logging.py L93: legacy env read of REBAR_LOG_LEVEL.",
    ),
    (
        "_opcert_signing.py",
        "REBAR_OPCERT_ENV_ID",
        "_opcert_signing.py L222: legacy env read of REBAR_OPCERT_ENV_ID.",
    ),
    (
        "_opcert_signing.py",
        "REBAR_OPCERT_KEY_PATH",
        "_opcert_signing.py L188: legacy env read of REBAR_OPCERT_KEY_PATH.",
    ),
    (
        "_operation_config.py",
        "os.environ.get",
        "_operation_config.py L54: legacy dynamic env read (os.environ.get).",
    ),
    (
        "grounding/harness.py",
        "os.environ.get",
        "harness.py L56: legacy dynamic env read (os.environ.get).",
    ),
    (
        "grounding/oracle.py",
        "load_config",
        "oracle.py L170: legacy load_config() call outside a composition root.",
    ),
    (
        "grounding/resolve.py",
        "REBAR_CTAGS_BIN",
        "resolve.py L62: legacy import-time default capture of REBAR_CTAGS_BIN.",
    ),
    (
        "grounding/resolve.py",
        "load_config",
        "resolve.py L543: legacy load_config() call outside a composition root.",
    ),
    (
        "mirror_guard.py",
        "GITHUB_TOKEN",
        "mirror_guard.py L231: legacy env read of GITHUB_TOKEN.",
    ),
    (
        "review_bot/app.py",
        "REVIEW_BOT_PORT",
        "app.py L477: legacy env read of REVIEW_BOT_PORT.",
    ),
    (
        "signing.py",
        "REBAR_SIGNING_KEY",
        "signing.py L98: legacy env read of REBAR_SIGNING_KEY.",
    ),
]

LEGACY_EXCEPTIONS: list[dict] = [
    {"path": path, "symbol": symbol, "rationale": rationale} for path, symbol, rationale in _ROWS
]
