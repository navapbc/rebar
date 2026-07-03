"""Commit-message ticket-reference gate (see ``docs/commit-ticket-trailer.md``).

Every commit to ``main`` must reference a rebar ticket that RESOLVES in the store.
This module extracts the reference from a commit message and resolves it via the
shared resolver (:func:`resolve_ticket_id`) against the config-resolved tracker dir,
so the same id forms work everywhere: alias, full id, short id, and Jira key.

Canonical location is the ``rebar-ticket: <id>`` trailer; a leading ``<id>:`` subject
token is also accepted. Enforced in CI (the Gerrit ``Verified`` leg —
``.github/workflows/gerrit-verify.yaml``); self-gates on
``verify.require_ticket_for_commit``.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

from rebar import config
from rebar._engine_support.resolver import resolve_ticket_id

# A single SAFE token: no path separators, no ``..``, no whitespace/control. This is a
# PATH-SAFETY guard — an extracted candidate feeds ``resolve_ticket_id``'s
# ``os.path.isdir(tracker/<cand>)`` fast path, so a crafted ``rebar-ticket: ../../x``
# must never reach it. ID-SHAPE matching (full/short/alias/Jira/prefix) stays in
# ``resolve_ticket_id`` — defined once there, so this guard can't drift from it.
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TRAILER = re.compile(r"^\s*rebar-ticket:\s*(.+?)\s*$", re.IGNORECASE)

# THE single source of truth for the expected-format wording. The CLI error, the CLI
# ``--help``, and the docs (``docs/commit-ticket-trailer.md``, pinned by a drift test)
# all quote THIS constant, so they cannot diverge as the mechanism is reused.
EXPECTED_FORMAT = """\
Every commit to `main` must reference a rebar ticket. Add a trailer to the commit
message (preferred), or start the subject with the ticket id:

    rebar-ticket: <id>        e.g.  rebar-ticket: blank-guild-koi
  - or a subject prefix -
    <id>: <summary>           e.g.  blank-guild-koi: fix the widget

Accepted <id> forms (resolved against the ticket store):
    alias       blank-guild-koi
    full id     fc9e-8c2e-cb2f-465f
    short id    fc9e-8c2e
    Jira key    REB-310            (project prefix from jira.project)"""


def _is_safe(token: str) -> bool:
    return ".." not in token and bool(_SAFE_TOKEN.match(token))


def extract_ticket_refs(message: str) -> list[str]:
    """Ordered, de-duplicated SAFE candidate ids from a commit message.

    Sources, in order: every ``rebar-ticket:`` trailer line (value split on whitespace,
    each token stripped of surrounding ``()``/``,``), then the leading ``<id>:`` token of
    the subject line. Only single safe tokens survive (:func:`_is_safe`) — a candidate
    with ``/``, ``..``, whitespace, or control chars is dropped BEFORE any store lookup.
    """
    out: list[str] = []
    seen: set[str] = set()

    def _add(tok: str) -> None:
        tok = tok.strip().strip("(),")
        if tok and tok not in seen and _is_safe(tok):
            seen.add(tok)
            out.append(tok)

    lines = message.splitlines()
    for line in lines:  # trailer(s) — anywhere, for robustness
        m = _TRAILER.match(line)
        if m:
            for tok in m.group(1).split():
                _add(tok)
    for line in lines:  # leading `<id>:` token of the subject (first non-empty line)
        if line.strip():
            _add(line.split(":", 1)[0])
            break
    return out


class VerifyResult:
    """Outcome of :func:`verify_commit_message`."""

    def __init__(self, ok: bool, resolved: str | None, tried: list[str]) -> None:
        self.ok = ok
        self.resolved = resolved
        self.tried = tried


def verify_commit_message(message: str, *, root: str | None = None) -> VerifyResult:
    """Resolve any extracted candidate against the config-resolved ticket store.

    ``ok`` is true when ANY safe candidate resolves via :func:`resolve_ticket_id`
    (which handles full/short/alias/Jira/prefix). A normal not-found is ``ok=False``
    (the caller renders the diagnostic); this never raises for that case. The store
    location follows the standard config precedence (``config.tracker_dir`` →
    ``REBAR_TRACKER_DIR`` / ``tracker.dir``).
    """
    tracker = str(config.tracker_dir(root))
    tried = extract_ticket_refs(message)
    for cand in tried:
        resolved = resolve_ticket_id(cand, tracker)
        if resolved is not None:
            return VerifyResult(True, resolved, tried)
    return VerifyResult(False, None, tried)


def render_missing(tried: list[str]) -> str:
    """The exit-1 diagnostic: EXPECTED_FORMAT + what was tried + the fix."""
    triedstr = ", ".join(tried) if tried else "(none present)"
    return (
        "commit has no resolvable rebar ticket reference.\n\n"
        + EXPECTED_FORMAT
        + f"\n\nTried: {triedstr} - none resolved in the ticket store.\n"
        + "Fix: add the trailer, then `git commit --amend` and re-push (keep the Change-Id)."
    )


# ── CLI (pure intercept — owns its own --help; no help/*.txt, no dispatch arm) ────
_USAGE = "rebar verify-commit-ticket [--rev <ref> | --message-file <path> | --message <text>]"


def _read_message(args: argparse.Namespace) -> tuple[str, bool]:
    """Return ``(message, is_merge)``. Raises :class:`_InfraError` on an I/O failure
    (bad rev, missing file) so the caller can exit with a DISTINGUISHABLE code — never
    conflated with a genuine missing-ticket."""
    if args.message is not None:
        return args.message, False
    if args.message_file is not None:
        try:
            with open(args.message_file, encoding="utf-8") as fh:
                return fh.read(), False
        except OSError as exc:
            raise _InfraError(f"cannot read --message-file {args.message_file!r}: {exc}") from exc
    rev = args.rev or "HEAD"
    msg = subprocess.run(["git", "show", "-s", "--format=%B", rev], capture_output=True, text=True)
    if msg.returncode != 0:
        raise _InfraError(f"git could not read commit {rev!r}: {(msg.stderr or '').strip()}")
    parents = subprocess.run(
        ["git", "show", "-s", "--format=%P", rev], capture_output=True, text=True
    )
    is_merge = parents.returncode == 0 and len(parents.stdout.split()) > 1
    return msg.stdout, is_merge


class _InfraError(Exception):
    """An I/O / environment failure (exit 2) — told apart from a missing ticket (exit 1)."""


def cli(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="rebar verify-commit-ticket",
        usage=_USAGE,
        description=(
            "Verify a commit message references a rebar ticket that resolves in the store.\n\n"
            + EXPECTED_FORMAT
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument("--rev", help="git revision to read the message from (default: HEAD)")
    src.add_argument("--message-file", help="read the commit message from this file")
    src.add_argument("--message", help="the commit message text (inline)")
    p.add_argument("--root", help="repo root (default: cwd); resolves the ticket store")
    args = p.parse_args(argv)

    cfg = config.load_config(root=args.root)
    if not cfg.verify.require_ticket_for_commit:
        # Staged-rollout / override: the gate is off, so this is a no-op pass.
        print("verify-commit-ticket: gate disabled (verify.require_ticket_for_commit=false)")
        return 0

    try:
        message, is_merge = _read_message(args)
    except _InfraError as exc:
        print(f"verify-commit-ticket: {exc}", file=sys.stderr)
        return 2
    if is_merge:
        print("verify-commit-ticket: merge commit - exempt.")
        return 0

    tracker = str(config.tracker_dir(args.root))
    if not os.path.isdir(tracker):
        # Distinguishable infra error (exit 2): the store isn't mounted/available — NOT a
        # missing-ticket. In CI this means the tickets-branch fetch/mount step failed.
        print(
            f"verify-commit-ticket: ticket store not found at {tracker!r} "
            "(infrastructure issue - the tickets store is not mounted; not a commit problem)",
            file=sys.stderr,
        )
        return 2

    result = verify_commit_message(message, root=args.root)
    if result.ok:
        print(f"verify-commit-ticket: OK - resolved {result.resolved}")
        return 0
    print(f"verify-commit-ticket: {render_missing(result.tried)}", file=sys.stderr)
    return 1
