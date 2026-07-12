"""Authenticated-authorship merge-gate (``rebar verify-authorship``).

The security boundary of the opt-in authenticated-authorship feature (epic
gnu-whale-ichor / 3183). Where the write-time UX gate in ``_seam.append_event`` only
nudges the local writer, THIS gate — run in CI (the Gerrit ``Verified`` leg) — is what
actually enforces authorship: it re-verifies every in-scope mutating event's signature
against the author identity's EPOCH-SCOPED keyring, so a forged / unsigned / wrong-key
event cannot land on ``main`` when the project has opted in.

For each mutating event it emits one classification:

* ``verified``       — an ``author_sig`` is present, binds this event's canonical bytes,
                       and verifies against a key the author identity held VALID at the
                       event's keyring epoch.
* ``unsigned``       — the event carries no ``author_sig``.
* ``unknown-author`` — the event has no ``author_id`` (or it is not an identity ticket),
                       so there is no trust root to verify against.
* ``bad-signature``  — an ``author_sig`` is present but fails (malformed, wrong content,
                       or not signed by a currently-valid key).

Scope is the whole store with ``--all`` (or when no ``--base`` is given); with
``--base <ref>`` it is the event files CHANGED in ``<base>..HEAD`` on the tracker branch
(the commit range CI checks). Exit is NON-ZERO when ``identity.require_authenticated`` is
ON and ANY in-scope event is not ``verified``; when the gate is OFF it is purely advisory
(always exit 0). Mirrors ``verify_commit`` (the commit-ticket gate) in shape.

After a ticket is compacted its raw event files are retired and folded into a SNAPSHOT;
the signed events are preserved in the SNAPSHOT's ``compiled_state['authorship_ledger']``
(built by ``compact.py``), so this gate re-verifies them from the ledger when the raw
files are gone.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from rebar import config

# Classifications (also the human-facing labels; ``verified`` is the only pass).
VERIFIED = "verified"
UNSIGNED = "unsigned"
UNKNOWN_AUTHOR = "unknown-author"
BAD_SIGNATURE = "bad-signature"

# Git's canonical empty-tree object id — the default diff base when none is resolvable, so
# a ``<base>..HEAD`` range degrades to "every event file at HEAD" rather than crashing.
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


class _ScopedEvent:
    """One in-scope event to classify. ``payload`` is the recomputed canonical bytes the
    signature must bind (None for a ledger entry, where the raw event is gone and the
    envelope's own payload is trusted-at-compaction). ``epoch`` is a pre-resolved keyring
    epoch (ledger entries carry it; live events resolve it on demand)."""

    def __init__(
        self,
        ref: str,
        author_id,
        author_sig,
        position: str | None,
        payload: bytes | None,
        epoch: int | None,
    ) -> None:
        self.ref = ref
        self.author_id = author_id
        self.author_sig = author_sig
        self.position = position
        self.payload = payload
        self.epoch = epoch


def _is_identity_author(author_id, tracker: str) -> bool:
    """True iff ``author_id`` names an existing, non-deleted ``identity`` ticket."""
    if not author_id:
        return False
    try:
        from rebar.reducer import reduce_ticket

        d = os.path.join(tracker, str(author_id))
        if not os.path.isdir(d):
            return False
        state = reduce_ticket(d)
    except Exception:  # noqa: BLE001 — an unreadable ticket is simply "not an identity"
        return False
    return (
        isinstance(state, dict)
        and state.get("ticket_type") == "identity"
        and state.get("status") != "deleted"
    )


# Ticket types exempt from authorship enforcement — bootstrap/verbose entities that are
# not "authored work" (mirrors the write-gate's exemption and _GATE_EXEMPT_TYPES). An
# identity's own CREATE is unsigned by construction, so verifying it would make any store
# holding an identity fail the merge-gate — self-defeating.
_GATE_EXEMPT_TYPES = ("session_log", "code_review", "identity")


def _is_gate_exempt_ticket(ticket_dir: str) -> bool:
    """True iff the ticket at ``ticket_dir`` is a gate-exempt type (skip in scanning)."""
    try:
        from rebar.reducer import reduce_ticket

        state = reduce_ticket(ticket_dir)
    except Exception:  # noqa: BLE001 — an unreadable ticket is scanned normally
        return False
    return isinstance(state, dict) and state.get("ticket_type") in _GATE_EXEMPT_TYPES


def _verify_signed(ev: _ScopedEvent, tracker: str, repo_root) -> str:
    """Classify an event that carries an ``author_sig`` — VERIFIED or BAD_SIGNATURE (with
    an UNKNOWN_AUTHOR short-circuit when the author is not an identity)."""
    from rebar.attest import authorship, dsse

    if not _is_identity_author(ev.author_id, tracker):
        return UNKNOWN_AUTHOR
    try:
        envelope = dsse.decode(ev.author_sig if isinstance(ev.author_sig, str) else "")
    except Exception:  # noqa: BLE001 — a malformed envelope is a bad signature, not a crash
        return BAD_SIGNATURE
    # Content binding (live events only): the envelope must sign THIS event's canonical
    # bytes, so a valid envelope over other content cannot be replayed onto this event.
    if ev.payload is not None and envelope.payload != ev.payload:
        return BAD_SIGNATURE
    if ev.epoch is not None:
        epoch = ev.epoch
    elif ev.position is not None:
        epoch = authorship.epoch_for_position(str(ev.author_id), ev.position, repo_root=repo_root)
    else:
        return BAD_SIGNATURE
    verdict = authorship.verify_authorship_at_epoch(
        envelope, str(ev.author_id), epoch, repo_root=repo_root
    )
    return VERIFIED if verdict.verified else BAD_SIGNATURE


def _classify(ev: _ScopedEvent, tracker: str, repo_root) -> str:
    """Classify one in-scope event. Order matters: a missing signature is ``unsigned``
    (even when the author is also unknown), matching the gate's user-facing vocabulary."""
    if not ev.author_sig:
        return UNSIGNED
    return _verify_signed(ev, tracker, repo_root)


# ── scope collection ─────────────────────────────────────────────────────────
def _active_event_files(ticket_dir: str) -> list[str]:
    from rebar.reducer._cache import is_active_event

    try:
        names = os.listdir(ticket_dir)
    except OSError:
        return []
    return sorted(
        n for n in names if n.endswith(".json") and not n.startswith(".") and is_active_event(n)
    )


def _ledger_events(snapshot: dict, ticket_id: str) -> list[_ScopedEvent]:
    """Signed events preserved in a SNAPSHOT's ``authorship_ledger`` (raw files retired)."""
    out: list[_ScopedEvent] = []
    ledger = snapshot.get("data", {}).get("compiled_state", {}).get("authorship_ledger")
    if not isinstance(ledger, list):
        return out
    for entry in ledger:
        if not isinstance(entry, dict):
            continue
        euuid = entry.get("event_uuid")
        out.append(
            _ScopedEvent(
                ref=f"{ticket_id}/{euuid} (ledger)",
                author_id=entry.get("author_id"),
                author_sig=entry.get("author_sig"),
                position=None,
                payload=None,
                epoch=entry.get("epoch") if isinstance(entry.get("epoch"), int) else None,
            )
        )
    return out


def _event_from_file(ticket_id: str, filename: str, path: str) -> list[_ScopedEvent]:
    """Turn one active event file into its scoped-event(s): a SNAPSHOT expands to its
    ledger entries (folded signed events); any other event yields itself. SNAPSHOT is
    never itself classified (it is a fold marker, not a mutating event)."""
    try:
        with open(path, encoding="utf-8") as f:
            event = json.load(f)
    except (OSError, ValueError):
        return []
    if event.get("event_type") == "SNAPSHOT":
        return _ledger_events(event, ticket_id)

    author_sig = event.get("author_sig")
    payload = None
    position = None
    if author_sig:
        from rebar._store.canonical import canonical_str

        payload = canonical_str({k: v for k, v in event.items() if k != "author_sig"}).encode(
            "utf-8"
        )
        position = f"{event.get('timestamp')}-{event.get('uuid')}"
    return [
        _ScopedEvent(
            ref=f"{ticket_id}/{filename}",
            author_id=event.get("author_id"),
            author_sig=author_sig,
            position=position,
            payload=payload,
            epoch=None,
        )
    ]


def _collect_all(tracker: str) -> list[_ScopedEvent]:
    events: list[_ScopedEvent] = []
    try:
        ticket_ids = sorted(
            d
            for d in os.listdir(tracker)
            if not d.startswith(".") and os.path.isdir(os.path.join(tracker, d))
        )
    except OSError:
        return events
    for ticket_id in ticket_ids:
        ticket_dir = os.path.join(tracker, ticket_id)
        if _is_gate_exempt_ticket(ticket_dir):
            continue  # identity/session_log/code_review are bootstrap/verbose, not authored work
        for filename in _active_event_files(ticket_dir):
            events.extend(_event_from_file(ticket_id, filename, os.path.join(ticket_dir, filename)))
    return events


def _collect_range(tracker: str, base: str) -> list[_ScopedEvent]:
    """Scoped events for the event files CHANGED in ``base..HEAD`` on the tracker branch."""
    import subprocess

    from rebar._store.gitutil import run_git

    cp = run_git(tracker, "diff", "--name-only", "--diff-filter=AM", f"{base}..HEAD", check=False)
    if cp.returncode != 0:
        # Base unresolvable (e.g. a shallow CI clone) — fall back to the empty tree so the
        # range degrades to "all event files at HEAD" rather than an error.
        cp = subprocess.run(
            [
                "git",
                "-C",
                tracker,
                "diff",
                "--name-only",
                "--diff-filter=AM",
                f"{_EMPTY_TREE}..HEAD",
            ],
            capture_output=True,
            text=True,
        )
    events: list[_ScopedEvent] = []
    for rel in (cp.stdout or "").splitlines():
        rel = rel.strip()
        if not rel.endswith(".json") or "/" not in rel:
            continue
        ticket_id, filename = rel.split("/", 1)
        if "/" in filename:  # only top-level ticket-dir event files
            continue
        events.extend(_event_from_file(ticket_id, filename, os.path.join(tracker, rel)))
    return events


# ── CLI (pure intercept — owns its own --help; no help/*.txt, no dispatch arm) ────
_USAGE = "rebar verify-authorship [--all | --base <ref>] [--root <path>]"


def cli(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="rebar verify-authorship",
        usage=_USAGE,
        description=(
            "Verify authenticated authorship of the store's mutating events against each "
            "author identity's epoch-scoped keyring (the authorship merge-gate). Advisory "
            "unless identity.require_authenticated is on, in which case any event that is "
            "not `verified` fails the gate (non-zero exit)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    scope = p.add_mutually_exclusive_group()
    scope.add_argument("--all", action="store_true", help="scan the whole store (default)")
    scope.add_argument("--base", help="only events changed in <base>..HEAD on the tracker branch")
    p.add_argument("--root", help="repo root (default: cwd); resolves the ticket store")
    args = p.parse_args(argv)

    try:
        cfg = config.load_config(root=args.root)
    except config.ConfigError as exc:
        print(f"verify-authorship: {exc}", file=sys.stderr)
        return 2
    required = cfg.identity.require_authenticated

    tracker = str(config.tracker_dir(args.root))
    if not os.path.isdir(tracker):
        print(
            f"verify-authorship: ticket store not found at {tracker!r} "
            "(infrastructure issue — the tickets store is not mounted; not an authorship "
            "problem)",
            file=sys.stderr,
        )
        return 2

    if args.base:
        events = _collect_range(tracker, args.base)
    else:
        events = _collect_all(tracker)

    counts = {VERIFIED: 0, UNSIGNED: 0, UNKNOWN_AUTHOR: 0, BAD_SIGNATURE: 0}
    problems: list[tuple[str, str]] = []
    for ev in events:
        cls = _classify(ev, tracker, args.root)
        counts[cls] = counts.get(cls, 0) + 1
        if cls != VERIFIED:
            problems.append((ev.ref, cls))

    for ref, cls in problems:
        print(f"  {cls}: {ref}")
    print(
        "verify-authorship: "
        f"{counts[VERIFIED]} verified, {counts[UNSIGNED]} unsigned, "
        f"{counts[UNKNOWN_AUTHOR]} unknown-author, {counts[BAD_SIGNATURE]} bad-signature "
        f"({len(events)} event(s) in scope)"
    )

    not_verified = len(events) - counts[VERIFIED]
    if not required:
        print(
            "verify-authorship: advisory — identity.require_authenticated is off "
            f"({not_verified} event(s) not verified, not enforced)."
        )
        return 0
    if not_verified:
        print(
            f"verify-authorship: FAIL — {not_verified} in-scope event(s) are not verified "
            "(identity.require_authenticated=true).",
            file=sys.stderr,
        )
        return 1
    print("verify-authorship: OK — every in-scope event is verified.")
    return 0
