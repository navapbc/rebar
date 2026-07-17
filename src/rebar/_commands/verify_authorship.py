"""Authenticated-authorship merge-gate (``rebar verify-authorship``).

The security boundary of the opt-in authenticated-authorship feature (epic
gnu-whale-ichor / 3183). Where the write-time UX gate in ``_seam.append_event`` only
nudges the local writer, THIS gate — run in CI (the Gerrit ``Verified`` leg) — is what
actually enforces authorship: it re-verifies every in-scope mutating event's signature
against the author identity's COMMIT-ANCESTRY-SCOPED keyring, so a forged / unsigned /
wrong-key event cannot land on ``main`` when the project has opted in.

For each mutating event it emits one classification:

* ``verified``            — an ``author_sig`` is present, binds this event's canonical bytes
                            (via its in-toto Statement subject), and verifies against a key
                            the author identity held VALID at the event's commit.
* ``unsigned``            — the event carries no ``author_sig``.
* ``unknown-author``      — the event has no ``author_id`` (or it is not an identity ticket),
                            so there is no trust root to verify against.
* ``bad-signature``       — an ``author_sig`` is present but fails (malformed, wrong content,
                            or not signed by ANY key this identity has ever held).
* ``key_not_valid_at_era`` — an ``author_sig`` verifies against a REAL key of the author
                            identity, but that key was not valid at the event's commit
                            (not yet added, or already revoked) — a real key, wrong era.

Display grouping: ``verified`` (the only pass) / ``unverified``
(= ``bad-signature`` | ``key_not_valid_at_era`` | ``unknown-author``) / ``unsigned``.
Under ``identity.require_authenticated`` every non-``verified`` classification fails
the gate.

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
    """One in-scope event to classify.

    Two provenances. A LIVE event (raw ``.json`` on disk) carries its full ``event`` dict
    (so the content binding is recomputed) and its ``ticket_dir`` (so its commit is resolved
    on demand); ``commit_sha`` / ``content_hash`` / ``signer_pubkey`` are ``None``. A LEDGER
    entry (folded into a SNAPSHOT, raw file retired) carries the recorded ``content_hash``,
    ``signer_pubkey``, ``event_uuid`` and a pre-resolved ``commit_sha`` (from its recorded
    ``position``), with ``event`` / ``ticket_dir`` ``None``. ``position`` is the event's
    ``{timestamp}-{uuid}`` string (for the intra-commit ordering refinement)."""

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


def _verify_signed(ev: _ScopedEvent, tracker: str, repo_root) -> str:
    """Classify an event that carries an ``author_sig``: VERIFIED / KEY_NOT_VALID_AT_ERA /
    BAD_SIGNATURE (with an UNKNOWN_AUTHOR short-circuit when the author is not an identity).

    The in-toto content binding is checked first (the envelope's Statement subject must name
    THIS event's uuid + content hash), then the commit-ancestry era verify; a failed era
    verify that STILL passes an any-key verify is a real-but-wrong-era key
    (``key_not_valid_at_era``) rather than a forgery (``bad-signature``)."""
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

    # Content binding: the DSSE payload must be an in-toto Statement whose single subject
    # binds THIS event's uuid + content hash, so a valid envelope over other content cannot
    # be replayed onto this event. LIVE events recompute the hash from the raw dict; LEDGER
    # entries compare against the recorded content_hash / event_uuid.
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
        commit_sha = authorship.resolve_event_commit(
            ev.position or "", ev.ticket_dir or "", repo_root=repo_root
        )
    else:
        commit_sha = ev.commit_sha

    if commit_sha is not None:
        v = authorship.verify_authorship_at_commit(
            envelope, str(author_id), commit_sha, ev.position, repo_root=repo_root
        )
        if v.verified:
            return VERIFIED
    # Era verify failed (or the commit was unresolvable) — distinguish a real-but-wrong-era
    # key from a forgery.
    any_v = authorship.verify_authorship_any_key(envelope, str(author_id), repo_root=repo_root)
    if any_v.verified:
        return KEY_NOT_VALID_AT_ERA
    return BAD_SIGNATURE


def _classify(ev: _ScopedEvent, tracker: str, repo_root) -> str:
    """Classify one in-scope event. Order matters: a missing signature is ``unsigned``
    (even when the author is also unknown), matching the gate's user-facing vocabulary."""
    if not ev.author_sig:
        return UNSIGNED
    return _verify_signed(ev, tracker, repo_root)


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
    """The event's introducing tracker-branch commit SHA, or ``None`` if unresolvable. LEDGER
    entries carry a pre-resolved commit; a LIVE event is resolved via ``commit_map`` (the batched
    single-pass lookup keyed by tracker-relative path, ``ev.ref`` for a file event) and only
    falls back to the per-event :func:`resolve_event_commit` when the map lacks the path (empty
    map / a merge-introduced file). Never raises (:func:`resolve_event_commit` is fail-closed)."""
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
    """Whether an event is ENFORCED by the gate. With no ``since_ref`` every event is enforced.
    Otherwise an event is enforced iff its introducing ``commit_sha`` is ``since_ref`` or a
    descendant of it (``git merge-base --is-ancestor <since_ref> <commit>`` exits 0). An
    unresolvable commit is enforced (fail-closed); any git error also fails closed."""
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

    # Reducer-IGNORED observability sidecars (COMPLETION_VERDICT, REVIEW_RESULT,
    # TICKET_DIGEST, ENQUEUE_ENRICH, the plan-review/digest sidecars, …) are NOT in
    # ``rebar.reducer.KNOWN_EVENT_TYPES``: they carry no ticket state, are not "authored
    # work", and are emitted by best-effort seams that may have no signing key. Classifying
    # them would false-fail the authorship gate under enforcement (an unsigned sidecar reads
    # as ``unsigned``), so they are OUT of scope — skipped exactly as the reducer's
    # forward-compat path preserves-and-ignores them.
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
_USAGE = (
    "rebar verify-identity [--all | --base <ref>] [--require-authenticated] "
    "[--since <ref>] [--format {text,json}] [--root <path>]"
)


def cli(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="rebar verify-identity",
        usage=_USAGE,
        description=(
            "Verify authenticated authorship of the store's mutating events against each "
            "author identity's epoch-scoped keyring (the authorship merge-gate; also available "
            "under the back-compat alias `rebar verify-authorship`). Advisory unless "
            "identity.require_authenticated (or --require-authenticated) is on, in which case "
            "any ENFORCED event that is not `verified` fails the gate (non-zero exit). Events "
            "whose introducing commit predates --since / identity.enforce_since are "
            "grandfathered: reported but never fail the gate."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    scope = p.add_mutually_exclusive_group()
    scope.add_argument("--all", action="store_true", help="scan the whole store (default)")
    scope.add_argument("--base", help="only events changed in <base>..HEAD on the tracker branch")
    p.add_argument(
        "--require-authenticated",
        action="store_true",
        help="force enforcement on regardless of identity.require_authenticated config",
    )
    p.add_argument(
        "--since",
        help="grandfather boundary: only enforce events at/descending this ref "
        "(default: identity.enforce_since)",
    )
    p.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="output format (default: text). json prints only a report array to stdout",
    )
    p.add_argument("--root", help="repo root (default: cwd); resolves the ticket store")
    args = p.parse_args(argv)

    try:
        cfg = config.load_config(root=args.root)
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

    # Resolve every event's introducing commit in ONE git-log pass instead of one subprocess
    # per event (bug 1cc0). _resolve_commit looks each event up here and only falls back to the
    # per-event resolver for a path the map lacks (fail-closed).
    from rebar.attest import authorship

    commit_map = authorship.build_introducing_commit_map(repo_root=args.root)

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
        # A compacted LEDGER entry can carry a null commit_sha when compaction could not
        # resolve the introducing commit at fold time (bug B). Re-resolve it here from the
        # recorded ``position`` (which resolves to the real introducing commit) so BOTH the
        # era classification (_verify_signed's ledger branch reads ``ev.commit_sha``) and the
        # enforcement decision (_resolve_commit returns ``ev.commit_sha``) use the real
        # commit — otherwise a validly-signed event fail-closes to ``key_not_valid_at_era``.
        if ev.event is None and ev.commit_sha is None and ev.position:
            ev.commit_sha = authorship.resolve_position_commit(
                ev.position, tracker, repo_root=args.root
            )
        cls = _classify(ev, tracker, args.root)
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
        print(json.dumps(report))
        for ref, cls, gf in problems:
            print(f"  {cls}{' [grandfathered]' if gf else ''}: {ref}", file=sys.stderr)
        print(summary, file=sys.stderr)
    else:
        for ref, cls, gf in problems:
            print(f"  {cls}{' [grandfathered]' if gf else ''}: {ref}")
        print(summary)

    out = sys.stderr if as_json else sys.stdout
    if not required:
        not_verified = len(events) - counts[VERIFIED]
        print(
            "verify-identity: advisory — enforcement is off "
            f"({not_verified} event(s) not verified, not enforced).",
            file=out,
        )
        return 0
    if enforced_not_verified:
        print(
            f"verify-identity: FAIL — {enforced_not_verified} enforced in-scope event(s) "
            "are not verified (enforcement on).",
            file=sys.stderr,
        )
        return 1
    print("verify-identity: OK — every enforced in-scope event is verified.", file=out)
    return 0
