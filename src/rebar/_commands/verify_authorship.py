"""Authenticated-authorship merge gate (``rebar verify-identity``).

For each in-scope mutating event, verify canonical content and the author's commit-era
keyring, classifying it as ``verified``, ``unsigned``, ``unknown-author``,
``bad-signature``, or ``key_not_valid_at_era``. Only ``verified`` passes when
``identity.require_authenticated`` is enabled. Otherwise reporting is advisory.

``--base`` scopes to event files changed in ``base..HEAD``. ``--all`` or no base scans the
store. Compacted events are reverified from the snapshot authorship ledger.
"""

from __future__ import annotations

import json
import os
import sys

from rebar import config
from rebar._cli._parser import guard_parse_errors
from rebar._cli._parsers.advanced.verify import build_identity
from rebar._mcp_errors import js_safe_dumps
from rebar.reducer import KNOWN_EVENT_TYPES

# Classifications (also the human-facing labels; ``verified`` is the only pass).
VERIFIED = "verified"
UNSIGNED = "unsigned"
UNKNOWN_AUTHOR = "unknown-author"
BAD_SIGNATURE = "bad-signature"
KEY_NOT_VALID_AT_ERA = "key_not_valid_at_era"

# Git's canonical empty-tree object id — the default diff base when none is resolvable, so
# a ``<base>..HEAD`` range degrades to "every event file at HEAD" rather than crashing.
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


class _ScopedEvent:
    """An event plus the provenance needed for authorship classification.

    Raw event records carry the event and ticket directory so content and introducing commit
    are resolved from the file and repository history. Snapshot-ledger records instead carry
    the saved hash, signer key, UUID, position, and commit."""

    def __init__(
        self,
        ref: str,
        author_id,
        author_sig,
        position: str | None,
        commit_sha: str | None,
        *,
        event: dict | None = None,
        ticket_dir: str | None = None,
        event_uuid=None,
        content_hash: str | None = None,
        signer_pubkey: str | None = None,
        ticket_id: str | None = None,
    ) -> None:
        self.ref = ref
        self.author_id = author_id
        self.author_sig = author_sig
        self.position = position
        self.commit_sha = commit_sha
        self.event = event
        self.ticket_dir = ticket_dir
        self.event_uuid = event_uuid
        self.content_hash = content_hash
        self.signer_pubkey = signer_pubkey
        self.ticket_id = ticket_id


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


def _is_archived_ticket(ticket_dir: str) -> bool:
    """Return whether a ticket is net archived and therefore outside gate scope.

    Use the reducer's ARCHIVED/REVERT calculation. Lookup failures return false so the ticket
    is scanned."""
    try:
        from rebar.reducer._api import _is_net_archived

        return _is_net_archived(ticket_dir)
    except Exception:  # noqa: BLE001 — an unreadable ticket is scanned normally
        return False


def _verify_signed(ev: _ScopedEvent, tracker: str, repo_root, position_resolver=None) -> str:
    """Classify a signed event as ``verified``, ``key_not_valid_at_era``,
    ``bad-signature``, or ``unknown-author``.

    Validate the first in-toto subject's event UUID and content hash before commit-era key
    checks. After failed era verification, test every key recorded for the identity. A match
    yields ``key_not_valid_at_era`` when a commit was resolved. An unresolved ledger record
    instead yields ``verified``, while an unresolved raw event remains nonverified."""
    from rebar.attest import authorship, dsse

    is_live = ev.event is not None

    # LIVE: the author is known upfront — an unknown author short-circuits (matching the
    # gate's vocabulary) even before we look at the signature.
    if is_live and not _is_identity_author(ev.author_id, tracker):
        return UNKNOWN_AUTHOR

    # LEDGER: a recorded null signer means no key matched at compaction → forged/foreign.
    if not is_live and ev.signer_pubkey is None:
        return BAD_SIGNATURE

    try:
        envelope = dsse.decode(ev.author_sig if isinstance(ev.author_sig, str) else "")
    except Exception:  # noqa: BLE001 — a malformed envelope is a bad signature, not a crash
        return BAD_SIGNATURE

    # Author identity: LIVE carries it on the event; LEDGER recovers it from the DSSE
    # envelope's keyid (the signing principal == the author identity id).
    if is_live:
        author_id = ev.author_id
    else:
        author_id = envelope.signatures[0].keyid if envelope.signatures else None
        if not _is_identity_author(author_id, tracker):
            return UNKNOWN_AUTHOR

    # Bind the first entry in the nonempty in-toto subject list to this event's UUID and hash.
    # Recompute hashes for raw events. Ledger entries use their stored hash and UUID.
    try:
        statement = json.loads(envelope.payload.decode("utf-8"))
        subject = statement["subject"]
        if not isinstance(subject, list) or not subject:
            raise ValueError("empty or non-list subject")
        subject_name = subject[0]["name"]
        subject_hash = subject[0]["digest"]["sha256"]
    except Exception:  # noqa: BLE001 — a non-Statement / malformed payload is a bad signature
        return BAD_SIGNATURE

    expected_uuid: str | None
    expected_hash: str | None
    if ev.event is not None:
        expected_uuid = ev.event.get("uuid")
        expected_hash = authorship.authorship_content_hash(ev.event)
    else:
        expected_uuid = ev.event_uuid
        expected_hash = ev.content_hash
    if subject_name != expected_uuid or subject_hash != expected_hash:
        return BAD_SIGNATURE

    # Determine the event's introducing commit: LIVE resolves it on demand from the raw
    # file's position; LEDGER carries the pre-resolved commit. An unresolvable commit
    # (None) makes the era verify below fail closed (non-verified).
    if ev.event is not None:
        # A batched position→commit resolver (the whole-store gate's per-run map) turns this into
        # an O(1) lookup; without one, fall back to the per-event git log (ticket a2c7).
        if position_resolver is not None:
            commit_sha = position_resolver(ev.position or "")
        else:
            commit_sha = authorship.resolve_event_commit(
                ev.position or "", ev.ticket_dir or "", repo_root=repo_root
            )
    else:
        commit_sha = ev.commit_sha

    if commit_sha is not None:
        v = authorship.verify_authorship_at_commit(
            envelope,
            str(author_id),
            commit_sha,
            ev.position,
            repo_root=repo_root,
            position_resolver=position_resolver,
        )
        if v.verified:
            return VERIFIED

    # Era verify failed OR the commit was unresolvable. An any-key verify (against EVERY key the
    # identity has ever held) decides authenticity: a signature by no such key is a forgery.
    any_v = authorship.verify_authorship_any_key(envelope, str(author_id), repo_root=repo_root)
    if not any_v.verified:
        return BAD_SIGNATURE
    # A signature from an identity key is wrong-era when a commit was resolved. An unresolved
    # ledger entry cannot be era-scoped, so accept a signature verified against the identity's
    # recorded keys. Raw events remain fail-closed because their files are present at HEAD.
    if commit_sha is None and not is_live:
        return VERIFIED
    return KEY_NOT_VALID_AT_ERA


def _classify(ev: _ScopedEvent, tracker: str, repo_root, position_resolver=None) -> str:
    """Classify one in-scope event. Order matters: a missing signature is ``unsigned``
    (even when the author is also unknown), matching the gate's user-facing vocabulary."""
    if not ev.author_sig:
        return UNSIGNED
    return _verify_signed(ev, tracker, repo_root, position_resolver=position_resolver)


def _display_group(verdict: str) -> str:
    """The three-way display grouping for a verdict (matches the module docstring +
    verify_signature schema): ``unsigned`` / ``verified`` / ``unverified`` (everything else)."""
    if verdict == UNSIGNED:
        return "unsigned"
    if verdict == VERIFIED:
        return "verified"
    return "unverified"


def _resolve_commit(
    ev: _ScopedEvent, repo_root, commit_map: dict[str, str] | None = None
) -> str | None:
    """Resolve an event's introducing commit, or ``None`` on failure.

    Ledger entries use their recorded commit. Raw entries use the batched path map and fall
    back to per-event history lookup when the map misses, such as after a merge."""
    if ev.commit_sha is not None:
        return ev.commit_sha
    if commit_map is not None:
        mapped = commit_map.get(ev.ref)
        if mapped is not None:
            return mapped
    if ev.position and ev.ticket_dir:
        from rebar.attest import authorship

        return authorship.resolve_event_commit(ev.position, ev.ticket_dir, repo_root=repo_root)
    return None


def _is_enforced(commit_sha: str | None, since_ref: str | None, tracker: str) -> bool:
    """Return whether the event is enforced relative to ``since_ref``.

    Without a boundary, every event is enforced. Missing commits and exceptions while invoking
    Git also enforce the event. Otherwise enforcement follows Git's ancestry exit status.
    Nonzero exits place the event outside the boundary."""
    if not since_ref:
        return True
    if commit_sha is None:
        return True
    try:
        import subprocess

        proc = subprocess.run(
            ["git", "-C", tracker, "merge-base", "--is-ancestor", since_ref, commit_sha],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return proc.returncode == 0
    except Exception:  # noqa: BLE001 — ANY git failure → enforce (fail-closed), never raise
        return True


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
        pos = entry.get("position")
        pos = pos if isinstance(pos, dict) else {}
        out.append(
            _ScopedEvent(
                ref=f"{ticket_id}/{euuid} (ledger)",
                # The ledger no longer records author_id; it is recovered at verify time
                # from the DSSE envelope's keyid (the signing principal == the author
                # identity id — see sshsig signing).
                author_id=None,
                author_sig=entry.get("signature"),
                position=pos.get("position"),
                commit_sha=pos.get("commit_sha"),
                event_uuid=euuid,
                content_hash=entry.get("content_hash"),
                signer_pubkey=entry.get("signer_pubkey"),
                ticket_id=ticket_id,
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

    # Ignore reducer-unknown observability sidecars because they do not mutate ticket state.
    # Best-effort producers may omit signatures, so scanning sidecars would produce false
    # enforcement failures.
    if event.get("event_type") not in KNOWN_EVENT_TYPES:
        return []

    author_sig = event.get("author_sig")
    # The position ({timestamp}-{uuid}) is always computed so an event's introducing commit
    # can be resolved for grandfathering even when it is unsigned (the classification path
    # only consults it for signed events).
    position = f"{event.get('timestamp')}-{event.get('uuid')}"
    return [
        _ScopedEvent(
            ref=f"{ticket_id}/{filename}",
            author_id=event.get("author_id"),
            author_sig=author_sig,
            position=position,
            commit_sha=None,
            event=event,
            ticket_dir=os.path.dirname(path),
            event_uuid=event.get("uuid"),
            ticket_id=ticket_id,
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
        if _is_gate_exempt_ticket(ticket_dir) or _is_archived_ticket(ticket_dir):
            # identity/session_log/code_review are bootstrap/verbose, and a net-ARCHIVED ticket
            # is retired work — neither is authored work the gate enforces.
            continue
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
            check=False,
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


# Registry-routed CLI handler with top-level help served from committed bytes.


@guard_parse_errors
def cli(argv: list[str]) -> int:
    p = build_identity(prog="rebar verify-identity")
    args = p.parse_args(argv)

    try:
        cfg = config.compose_config(root=args.root)
    except config.ConfigError as exc:
        print(f"verify-identity: {exc}", file=sys.stderr)
        return 2
    required = args.require_authenticated or cfg.identity.require_authenticated
    since_ref = args.since if args.since is not None else cfg.identity.enforce_since
    as_json = args.format == "json"

    tracker = str(config.tracker_dir(args.root))
    if not os.path.isdir(tracker):
        print(
            f"verify-identity: ticket store not found at {tracker!r} "
            "(infrastructure issue — the tickets store is not mounted; not an authorship "
            "problem)",
            file=sys.stderr,
        )
        return 2

    if args.base:
        events = _collect_range(tracker, args.base)
    else:
        events = _collect_all(tracker)

    if not required:
        # Advisory mode never fails, so avoid era verification and commit maps. Report only
        # the signature-presence split, keeping the scan O(events) with exit 0.
        signed = sum(1 for ev in events if ev.author_sig)
        out = sys.stderr if as_json else sys.stdout
        if as_json:
            # The report array holds one entry per NON-verified ENFORCED event; advisory enforces
            # none, so it is empty (and no per-event verdicts are computed in advisory mode).
            print(js_safe_dumps([]))
        print(
            "verify-identity: advisory — enforcement is off "
            f"({signed} signed (not era-verified in advisory mode), "
            f"{len(events) - signed} unsigned; {len(events)} event(s) in scope).",
            file=out,
        )
        return 0

    # Resolve every event's introducing commit in ONE git-log pass instead of one subprocess
    # per event (bug 1cc0). _resolve_commit looks each event up here and only falls back to the
    # per-event resolver for a path the map lacks (fail-closed).
    from rebar.attest import authorship

    commit_map = authorship.build_introducing_commit_map(repo_root=args.root)
    # Resolve ledger positions in one full-history pass. Map hits avoid per-entry history
    # walks and survive topology changes. Misses use the fallback below.
    position_map = authorship.build_position_commit_map(repo_root=args.root)

    def _resolve_position(position: str) -> str | None:
        """Resolve a position through the batched map, then one history lookup.

        Empty or unresolved positions return ``None``. An unresolved raw event remains
        nonverified. A snapshot-ledger event can still pass when its signature matches a key
        recorded for the identity."""
        if not position:
            return None
        hit = position_map.get(position)
        if hit is not None:
            return hit
        return authorship.resolve_position_commit(position, tracker, repo_root=args.root)

    counts = {
        VERIFIED: 0,
        UNSIGNED: 0,
        UNKNOWN_AUTHOR: 0,
        BAD_SIGNATURE: 0,
        KEY_NOT_VALID_AT_ERA: 0,
    }
    problems: list[tuple[str, str, bool]] = []  # (ref, verdict, grandfathered)
    report: list[dict] = []  # one entry per NON-verified in-scope event
    enforced_not_verified = 0
    for ev in events:
        # Treat a ledger commit as a cache. Re-resolve its position against current history to
        # repair rewritten or formerly unresolved ancestry. Retain the stored SHA only when
        # both batched and per-position resolution fail.
        if ev.event is None and ev.position:
            resolved_sha = _resolve_position(ev.position)
            if resolved_sha is not None:
                ev.commit_sha = resolved_sha
        cls = _classify(ev, tracker, args.root, position_resolver=_resolve_position)
        counts[cls] = counts.get(cls, 0) + 1
        if cls == VERIFIED:
            continue
        commit_sha = _resolve_commit(ev, args.root, commit_map)
        enforced = _is_enforced(commit_sha, since_ref, tracker)
        grandfathered = not enforced
        if enforced:
            enforced_not_verified += 1
        problems.append((ev.ref, cls, grandfathered))
        report.append(
            {
                "event_uuid": ev.event_uuid,
                "ticket_id": ev.ticket_id,
                "commit": commit_sha,
                "author": ev.author_id,
                "verdict": cls,
                "display": _display_group(cls),
                "grandfathered": grandfathered,
            }
        )

    summary = (
        "verify-identity: "
        f"{counts[VERIFIED]} verified, {counts[UNSIGNED]} unsigned, "
        f"{counts[UNKNOWN_AUTHOR]} unknown-author, {counts[BAD_SIGNATURE]} bad-signature, "
        f"{counts[KEY_NOT_VALID_AT_ERA]} key-not-valid-at-era "
        f"({len(events)} event(s) in scope)"
    )

    if as_json:
        # JSON mode: ONLY the report array on stdout; any human text goes to stderr.
        print(js_safe_dumps(report))
        for ref, cls, gf in problems:
            print(f"  {cls}{' [grandfathered]' if gf else ''}: {ref}", file=sys.stderr)
        print(summary, file=sys.stderr)
    else:
        for ref, cls, gf in problems:
            print(f"  {cls}{' [grandfathered]' if gf else ''}: {ref}")
        print(summary)

    # Enforcement is ON here (the advisory/report-only path returned early, above, without
    # running any per-event era-verify — ticket a2c7).
    out = sys.stderr if as_json else sys.stdout
    if enforced_not_verified:
        print(
            f"verify-identity: FAIL — {enforced_not_verified} enforced in-scope event(s) "
            "are not verified (enforcement on).",
            file=sys.stderr,
        )
        return 1
    print("verify-identity: OK — every enforced in-scope event is verified.", file=out)
    return 0
