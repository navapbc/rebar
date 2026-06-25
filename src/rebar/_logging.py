"""rebar logging substrate — the canonical error-handling / observability convention.

This module's docstring **is** the convention reference (epic ring-gun-jot). Read it
before adding a broad ``except`` or a diagnostic ``print``.

The convention (deliberately boring; mirrors rebar's peers, no novel machinery):

* **Named loggers.** Modules log via ``logger = logging.getLogger(__name__)`` so every
  record resolves under the ``rebar`` root (or the sibling ``rebar_reconciler`` root for
  the reconciler subprocess, which is imported top-level as ``rebar_reconciler.*``).

* **Library hygiene — quiet by default.** ``rebar/__init__.py`` attaches a
  ``logging.NullHandler`` to the ``rebar`` root (and the reconciler package init does the
  same for ``rebar_reconciler``). Importing rebar as a library never configures handlers
  or emits to stderr — the embedding application owns output.

* **Entrypoints install a stderr handler.** The three process entrypoints — the CLI
  ``main`` (:mod:`rebar._cli`), the MCP server ``main`` (:mod:`rebar.mcp_server`), and the
  reconciler subprocess ``main`` (``rebar_reconciler.__main__``) — call
  :func:`install_stderr_handler` so diagnostics become observable. **Never target
  stdout:** CLI *data* ``print(json.dumps(...))`` is a machine contract (the reconciler
  ``json.loads`` it) and MCP-over-stdio reserves stdout for JSON-RPC framing. Only stderr
  diagnostics flow through the logger; stdout data prints stay.

* **Best-effort failures — inline narrow-and-log, no shared helper.** When a failure is
  genuinely non-fatal, narrow the catch and log it::

      try:
          best_effort_thing()
      except SpecificError:
          logger.warning("best-effort X failed; continuing", exc_info=True)

  There is intentionally no shared swallow helper (peers don't have one; it would be novel).

* **The observability floor.** Every broad ``except Exception`` / ``except BaseException``
  must either log with ``exc_info`` **or** carry a justified ``# noqa: BLE001`` with a
  reason string. ``KeyboardInterrupt`` / ``SystemExit`` are never swallowed by a broad
  catch. Narrowing is opportunistic and test-backed; fail-open / body-inspecting /
  public-API sites stay broad-but-logged.

* **MCP surface — raise, don't catch-and-return.** MCP tool handlers ``raise``; the
  framework converts the exception into an ``isError`` tool result and keeps the
  connection alive. Use ``ToolError`` for intentional, clean client-facing messages.

* **CLI surface.** A base ``RebarError`` caught at the boundary maps to an exit code
  (``ConcurrencyError`` -> exit 10).

Enforcement: ruff **BLE001** (blind-except) and **T201** (print; banned in
library/core/MCP, allowed in the CLI presentation layer and the reconciler subprocess'
own stderr) are enabled in CI behind a *shrinking* ``per-file-ignores`` allowlist.
"""

from __future__ import annotations

import logging
import os
import sys

# Marker so repeated entrypoint invocations (e.g. the CLI dispatching another command
# in-process, or tests that call main() many times) never stack duplicate handlers.
_HANDLER_MARKER = "_rebar_stderr_handler"


def install_stderr_handler(root: str = "rebar", *, level: int | None = None) -> None:
    """Install a single stderr ``StreamHandler`` on the ``root`` logger (idempotent).

    Called once per process at each entrypoint. The level honours ``REBAR_LOG_LEVEL``
    (a name like ``DEBUG``/``INFO``/``WARNING`` or a number); the default is ``WARNING``
    so routine runs stay quiet while swallowed failures surface. Targets stderr only —
    stdout is reserved for the CLI data contract and MCP JSON-RPC framing.
    """
    logger = logging.getLogger(root)
    # Idempotent: if we already installed our handler, leave it (don't duplicate).
    for existing in logger.handlers:
        if getattr(existing, _HANDLER_MARKER, False):
            return

    if level is None:
        level = _resolve_level()

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    setattr(handler, _HANDLER_MARKER, True)
    logger.addHandler(handler)
    # Only lower the logger's threshold towards the handler level; never raise it
    # above a level another caller already requested.
    if logger.level == logging.NOTSET or logger.level > level:
        logger.setLevel(level)


def _resolve_level() -> int:
    """Resolve the handler level from ``REBAR_LOG_LEVEL`` (name or number), default WARNING."""
    raw = (os.environ.get("REBAR_LOG_LEVEL") or "").strip()
    if not raw:
        return logging.WARNING
    if raw.isdigit():
        return int(raw)
    resolved = logging.getLevelName(raw.upper())
    # getLevelName returns the int for a known name, else a "Level X" string.
    return resolved if isinstance(resolved, int) else logging.WARNING
