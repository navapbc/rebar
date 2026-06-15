"""In-process ``get-file-impact`` / ``get-verify-commands`` (Tier E E2).

Ports the two field-read commands the bash dispatcher reached via
``_ticketlib_dispatch ticket_get_file_impact`` / ``ticket_get_verify_commands``
(``ticket-lib-api.sh``). Both resolve an id and read one array off the reduced
ticket — reusing the single reducer (bug f026), the shared resolver, and the
canonical ``--output`` / error-envelope layer — so the library and the argparse
CLI share one implementation.

Byte-parity with the bash arms (verified empirically against the dispatcher):

* ``get-file-impact`` — spaced ``json.dumps`` (default separators), reducer
  insertion order; **silent ``[]`` on a resolve/dir miss**, exit 0; arity →
  ``Usage:`` (exit 1); empty id → ``Error: ticket_id must be non-empty`` (exit 1).
* ``get-verify-commands`` — compact (``jq -c``) output with the canonical sorted
  keys; honors ``--output`` (``report`` profile: text default, json allowed); a
  miss prints ``Error: ticket '<id>' not found`` to **stderr always**, plus the
  schema error envelope to **stdout in json mode**, exit 1.
"""

from __future__ import annotations

import json
import os
import sys

from rebar._engine_support.output import (
    OutputFormatError,
    error_envelope,
    parse_output,
)
from rebar._engine_support.reads import ReadError, show_state
from rebar._engine_support.resolver import resolve_ticket_id
from rebar.reducer import reduce_ticket


# ── library-facing pure helpers ───────────────────────────────────────────────
def file_impact(ticket_id: str, tracker: str) -> list:
    """File-impact array for ``ticket_id``; ``[]`` on a resolve/dir miss."""
    resolved = resolve_ticket_id(ticket_id, tracker)
    if resolved is None:
        return []
    path = os.path.join(tracker, resolved)
    if not os.path.isdir(path):
        return []
    state = reduce_ticket(path)
    return (state or {}).get("file_impact") or []


def verify_commands(ticket_id: str, tracker: str) -> list:
    """Verify-commands array for ``ticket_id``; raises :class:`ReadError` on a miss."""
    state = show_state(ticket_id, tracker)
    return state.get("verify_commands") or []


# ── CLI arms (byte-parity with the dispatcher) ────────────────────────────────
def file_impact_cli(argv: list[str], tracker: str) -> int:
    if len(argv) < 1:
        sys.stderr.write("Usage: ticket get-file-impact <ticket_id>\n")
        return 1
    ticket_id = argv[0]
    if not ticket_id:
        sys.stderr.write("Error: ticket_id must be non-empty\n")
        return 1
    sys.stdout.write(json.dumps(file_impact(ticket_id, tracker), ensure_ascii=False) + "\n")
    return 0


def verify_commands_cli(argv: list[str], tracker: str) -> int:
    try:
        fmt, rest = parse_output(argv, "report")
    except OutputFormatError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    if len(rest) < 1:
        sys.stderr.write("Usage: ticket get-verify-commands <ticket_id>\n")
        return 1
    ticket_id = rest[0]
    if not ticket_id:
        sys.stderr.write("Error: ticket_id must be non-empty\n")
        return 1
    try:
        vc = verify_commands(ticket_id, tracker)
    except ReadError:
        # Miss: the schema envelope goes to stdout in json mode; the text error
        # goes to stderr in BOTH modes (matches the dispatcher's _emit_error_envelope).
        if fmt == "json":
            env = error_envelope(
                "ticket_not_found", ticket_id, f"Ticket '{ticket_id}' not found", 1
            )
            sys.stdout.write(json.dumps(env, ensure_ascii=False) + "\n")
        sys.stderr.write(f"Error: ticket '{ticket_id}' not found\n")
        return 1
    sys.stdout.write(json.dumps(vc, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0
