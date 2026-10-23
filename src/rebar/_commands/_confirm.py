"""CLI mutation confirmations from command outcomes, without store rereads.
Global quiet and output flags set a per-invocation context. Text output
respects quiet. JSON uses the common mutation envelope unless a verb has an
existing JSON shape. Link redirect JSON remains byte-identical."""

from __future__ import annotations

from contextlib import contextmanager

from rebar._engine_support.output import OutputFormatError, parse_output
from rebar._mcp_errors import js_safe_dumps

# Modes the global --output extraction accepts for the in-scope mutating verbs
# (matches the "report" profile the legacy-JSON verbs already validate against).
_ALLOWED = ("text", "json")

# Per-invocation output context, installed by the CLI router. Single-invocation
# process state (the CLI is one dispatch per process; tests reset via the
# context manager's finally).
_state: dict = {"quiet": False, "fmt": None}


@contextmanager
def confirmation_context(*, quiet: bool, fmt: str | None):
    """Install the extracted global output flags for the current dispatch."""
    prev = dict(_state)
    _state.update({"quiet": quiet, "fmt": fmt})
    try:
        yield
    finally:
        _state.update(prev)


def json_mode() -> bool:
    """True when the router extracted ``--output json`` for this invocation."""
    return _state["fmt"] == "json"


def extract_global_flags(argv: list[str]) -> tuple[list[str], bool, str]:
    """Return remaining args, quiet, and output mode.
    Parse flags only before bare --, preserving later literal data. Output uses
    the shared parser and defaults to text."""
    head = argv[: argv.index("--")] if "--" in argv else list(argv)
    tail = argv[len(head) :]
    quiet = False
    kept: list[str] = []
    for tok in head:
        if tok in ("--quiet", "-q"):
            quiet = True
        else:
            kept.append(tok)
    fmt, rest = parse_output(kept, allowed=_ALLOWED, default="text")
    return rest + tail, quiet, fmt


def emit_text(line: str) -> None:
    """Print a text confirmation line, honouring the output context.

    Suppressed by ``--quiet`` and in JSON mode (where stdout is exclusively the
    JSON document). Verbs that own a pre-existing ``--output json`` shape print
    that shape themselves and route only their TEXT confirmation through here.
    """
    if _state["quiet"] or _state["fmt"] == "json":
        return
    print(line)


def emit(outcome: str, subject: str, detail: str, line: str, *, extra: dict | None = None) -> None:
    """Print a confirmation on the active channel (envelope verbs).

    Text mode: ``line`` (suppressed by ``--quiet``). ``--output json``: the uniform
    pre-1.0-UNSTABLE mutation envelope ``{"outcome", "subject", "detail"}`` —
    ``--quiet`` never suppresses JSON (it governs the text channel only). ``extra``
    merges additional structured fields (e.g. ``link``'s nested redirect record).
    """
    if _state["fmt"] == "json":
        doc: dict = {"outcome": outcome, "subject": subject, "detail": detail}
        if extra:
            doc.update(extra)
        print(js_safe_dumps(doc, ensure_ascii=False))
        return
    if not _state["quiet"]:
        print(line)


def noop(subject: str, reason: str) -> None:
    """Print the idempotent no-op confirmation: ``no change: <reason>``."""
    emit("noop", subject, reason, f"no change: {reason}")


def confirm_created(noun: str, res: dict) -> None:
    """Normalized ``create``/``idea`` text confirmation (legacy JSON untouched).

    ``created [<noun> ]<alias> (<id>): <title>`` — one line carrying every datum
    the two pre-normalization lines did (alias, canonical id, title; the trailing
    bare-id line is folded into the parenthesis). ``noun`` is ``""`` for ``create``
    and ``"idea "`` for ``idea`` (preserving the old line's idea marker).
    """
    alias, tid = res["alias"], res["id"]
    who = f"{alias} ({tid})" if alias and alias != tid else tid
    emit_text(f"created {noun}{who}: {res['title']}")


def leaf_confirm(command: str, out) -> None:
    """Map a ``_REGISTRY`` leaf command's outcome return to its confirmation.

    ``out`` is what the leaf function returned: the resolved id for ``comment`` /
    ``set-verify-commands`` (a ``(id, count)`` pair for the latter), and a small
    ``{"wrote", "id", ...}`` outcome dict for the no-op-capable ``archive``. Unknown commands
    (a future registry entry without a formatter)
    stay silent rather than guessing a line.
    """
    if command == "comment":
        emit("commented", out, "comment added", f"comment added to {out}")
    elif command == "archive":
        if out["wrote"]:
            emit("archived", out["id"], "archived", f"archived {out['id']}")
        else:
            noop(out["id"], f"{out['id']} already archived")
    elif command == "set-verify-commands":
        tid, count = out
        emit(
            "verify-commands-set",
            tid,
            f"{count} command(s)",
            f"verify-commands set on {tid}: {count}",
        )


__all__ = [
    "OutputFormatError",
    "confirmation_context",
    "emit",
    "emit_text",
    "extract_global_flags",
    "json_mode",
    "leaf_confirm",
    "noop",
]
