"""Shared infrastructure for Tier B leaf-write commands (history: docs/bash-migration.md §4).

A Tier B command is a small function that (1) validates args, (2) resolves the
ticket id, (3) composes the event JSON in Python, and (4) appends it through ONE
narrow seam — ``append_event`` → ``rebar._store.event_append.write_and_push``
(flock + atomic rename + git commit + best-effort push), the single locked write
path. This module owns the pieces every leaf command shares: tracker/id resolution,
the ghost check, event metadata, and the append call. Tier D retired the bash core,
so all writes route in-process through ``rebar._store`` and invariant I5 (single
locked write path) holds unchanged.
"""

from __future__ import annotations

import contextvars
import logging
import os
import re as _re
import subprocess
import uuid as _uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from rebar import config
from rebar._engine_support.resolver import resolve_ticket_id

logger = logging.getLogger(__name__)

# ── Deferred-commit sink (epic cold-stall-chalk / B2) ─────────────────────────
# The bulk-import batching seam. ``append_event`` is the ONE funnel every write
# (CREATE via composer, EDIT/parent, comments + file-impact/verify via leaf) flows
# through. When ``_batch_sink`` holds a buffer, ``append_event`` composes the event
# and APPENDS ``(ticket_id, event)`` to it instead of committing — the caller then
# flushes the buffer through ``event_append.batch_stage_and_commit`` (one commit per
# chunk). Default ``None`` ⇒ every write commits one-event-per-commit exactly as
# before, so interactive writes keep the per-write durability guarantee and ONLY the
# importer (which sets this contextvar) is batched. State-reading guards in the
# callers run BEFORE ``append_event``, so they are unaffected by buffering.
_batch_sink: contextvars.ContextVar[list[tuple[str, dict]] | None] = contextvars.ContextVar(
    "rebar_batch_sink", default=None
)


@contextmanager
def batch_sink(buffer: list[tuple[str, dict]]) -> Iterator[list[tuple[str, dict]]]:
    """Route ``append_event`` writes into *buffer* (deferred commit) for the block.

    While active, every ``append_event`` call composes its event and appends
    ``(ticket_id, event)`` to *buffer* instead of committing. The caller owns the
    buffer and flushes it via ``batch_stage_and_commit``. Restores the prior sink on
    exit (nestable / contextvar-scoped, so it never leaks across threads or tasks)."""
    token = _batch_sink.set(buffer)
    try:
        yield buffer
    finally:
        _batch_sink.reset(token)


class CommandError(Exception):
    """A leaf-command failure with a stderr message and process exit code.

    The CLI entrypoint prints ``message`` to stderr and exits ``returncode``; the
    library facade maps it onto ``RebarError`` so the exit-1 contract is unchanged.
    ``error_code``/``input_str`` are set when the bash counterpart also emits a
    ``--output json`` error envelope (e.g. invalid_ticket_type), so the CLI path can
    reproduce that envelope before the stderr prose.
    """

    def __init__(
        self,
        message: str,
        returncode: int = 1,
        *,
        error_code: str | None = None,
        input_str: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.returncode = returncode
        self.error_code = error_code
        self.input_str = input_str


def tracker_dir(repo_root=None) -> Path:
    """Resolve the tracker dir: the REBAR_TRACKER_DIR override, then repo-root."""
    return config.tracker_dir(repo_root)


def require_id(ticket_id: str, tracker: Path) -> str:
    """Resolve any id form (full/short/alias/prefix) to the canonical dir name.

    Raises :class:`CommandError` (exit 1) when the id is empty or unresolvable —
    mirroring the bash ``_ticketlib_resolve_id`` contract (the resolver prints its
    own ambiguity/not-found diagnostics to stderr).
    """
    if not ticket_id:
        raise CommandError("Error: ticket id must be non-empty")
    resolved = resolve_ticket_id(ticket_id, str(tracker))
    if resolved is None:
        raise CommandError(f"Error: ticket '{ticket_id}' not found")
    return resolved


def require_not_ghost(ticket_id: str, tracker: Path) -> None:
    """Ghost check: the ticket must have a CREATE or SNAPSHOT event (else exit 1).

    Mirrors the bash ``find ... -name '*-CREATE.json' -o -name '*-SNAPSHOT.json'``
    guard that prevents writing an event onto a ticket that was never created.
    """
    tdir = tracker / ticket_id
    if tdir.is_dir():
        for entry in os.listdir(tdir):
            if entry.startswith("."):
                continue
            if entry.endswith("-CREATE.json") or entry.endswith("-SNAPSHOT.json"):
                return
    raise CommandError(f"Error: ticket {ticket_id} has no CREATE or SNAPSHOT event")


def env_id(tracker: Path) -> str:
    """The store's environment id (``.env-id``); empty string if absent."""
    try:
        return (tracker / ".env-id").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _git_config(key: str, fallback: str = "", *, cwd=None) -> str:
    """A single ``git config <key>`` read, degrading to ``fallback`` on ANY failure.

    Shells ``git config <key>`` (optionally under ``cwd`` so it reads a specific repo's
    config, as ``identity._git_email`` does) and returns the stripped value; an unset
    key, a missing/failed ``git`` invocation, a non-zero exit, or a timeout all fall back
    to ``fallback``. NEVER raises (attribution reads must not break a write). ``timeout=5``
    bounds a hung git.
    """
    try:
        out = subprocess.run(
            ["git", "config", key],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return fallback
    if out.returncode != 0:
        return fallback
    value = (out.stdout or "").strip()
    return value or fallback


def author(fallback: str = "Unknown") -> str:
    """Commit author name from git config, falling back to ``fallback`` (bash parity).

    The fallback string differs by command: comment / file-impact / verify-commands
    use ``Unknown``; the tag helpers use lowercase ``unknown``. Callers pass the
    value their bash counterpart uses so a git-config-less environment matches.
    """
    return _git_config("user.name", fallback)


def author_email(repo_root=None) -> str:
    """Commit email from the STORE repo's ``git config user.email`` (``""`` on any
    failure). Reads with ``cwd`` = the resolved repo_root — mirroring
    ``identity._git_email`` — so it reflects the store's committer, not the ambient
    process cwd's git config."""
    return _git_config("user.email", "", cwd=str(repo_root or config.repo_root()))


# Denormalized author-attribution stamped onto every locally-written event envelope
# (epic gnu-whale-ichor). Cached per canonical repo_root so a batch of writes performs
# ONE git-config read + ONE identity resolution. The identity pointer changing
# (``use_identity``) invalidates this via ``_reset_attribution_cache``.
_ATTRIBUTION_CACHE: dict[str, dict] = {}


def attribution_fields(repo_root=None) -> dict:
    """Denormalized author attribution for an event envelope: ``{"author_email": ...}``
    plus ``{"author_id": <id>}`` ONLY when a current identity resolves (omitted on a
    miss). Cached per canonical repo_root — the first call per key does exactly ONE
    ``author_email()`` and ONE ``resolve_current_identity()``; later calls return the
    cached dict. ``identity`` is imported lazily (it imports from this module)."""
    from rebar._commands import identity

    key = os.path.realpath(str(repo_root or config.repo_root()))
    cached = _ATTRIBUTION_CACHE.get(key)
    if cached is not None:
        return cached
    fields: dict = {"author_email": author_email(repo_root)}
    ident = identity.resolve_current_identity(repo_root=repo_root)
    if ident is not None:
        fields["author_id"] = ident
    _ATTRIBUTION_CACHE[key] = fields
    return fields


def _reset_attribution_cache() -> None:
    """Clear the per-repo attribution cache (tests; and on an identity-pointer change)."""
    _ATTRIBUTION_CACHE.clear()


_TAG_CTRL_RE = _re.compile(r"[\x00-\x1f\x7f]")


def validate_tag_name(raw: str) -> str:
    """Trim a tag name and reject empty/whitespace-only/control-char values (P2.3).

    The single tag-name guard shared by every write path (leaf tag/untag and the
    edit add/remove/set deltas) so ``rebar.tag``/MCP ``tag_ticket`` can't bypass it.
    Returns the trimmed name; raises :class:`CommandError` on an invalid one.
    """
    t = str(raw).strip()
    if not t:
        raise CommandError("Error: tag name must be non-empty / non-whitespace")
    if _TAG_CTRL_RE.search(t):
        raise CommandError(f"Error: invalid tag name {raw!r} (contains control characters)")
    return t


def current_tags(ticket_id: str, tracker: Path) -> list[str]:
    """The compiled ``tags`` list for a ticket via the shared reducer (single source).

    Mirrors the bash tag helpers, which reduce the ticket (``ticket_show``) to read
    current tags before composing the next EDIT. Returns ``[]`` when the ticket has
    no tags or cannot be reduced (the bash helpers swallow show failures too).
    """
    from rebar.reducer import reduce_ticket

    try:
        state = reduce_ticket(str(tracker / ticket_id))
        return list((state or {}).get("tags") or [])
    except Exception:  # noqa: BLE001 — bash tag helpers swallow show failures too; fall open to no observed tags
        return []


# Gate-exempt ticket types for the identity write-gate (mirrors the file-impact /
# lifecycle exemptions in composer/transition): these are gate-/graph-exempt entities, so
# requiring an authenticated signature on them would make bootstrapping an identity — or
# writing a session_log / code_review artifact — impossible under the gate.
_AUTHORSHIP_GATE_EXEMPT_TYPES = ("session_log", "code_review", "identity")


def _event_ticket_type(ticket_id, event_type, data, tracker) -> str | None:
    """Best-effort ticket type for the write-gate exemption check. A CREATE carries the
    type in its data; for a later event we reduce the existing ticket. Any failure yields
    ``None`` (the gate then treats it as non-exempt — fail toward enforcement). Only reached
    on the cannot-sign path under an opted-in gate, so the reduce is off the hot path."""
    if event_type == "CREATE":
        t = data.get("ticket_type")
        return t if isinstance(t, str) else None
    try:
        from rebar.reducer import reduce_ticket

        state = reduce_ticket(os.path.join(str(tracker), ticket_id))
        t = state.get("ticket_type") if isinstance(state, dict) else None
        return t if isinstance(t, str) else None
    except Exception:  # noqa: BLE001 — an unreadable ticket is simply "type unknown" (non-exempt)
        return None


def _apply_authorship(event: dict, ticket_id, event_type, data, tracker, repo_root) -> None:
    """Optional write-time authorship signing + the opt-in UX write-gate (epic
    gnu-whale-ichor / 3183).

    Signing is BEST-EFFORT: when a current identity resolves (``author_id`` is on the
    envelope) AND ``identity.signing_key`` is configured, sign the event's CANONICAL bytes
    (the envelope WITHOUT ``author_sig``) as an authorship attestation and store the DSSE
    envelope under ``author_sig``. No identity / no key ⇒ the event is written UNSIGNED; a
    signing FAILURE is logged and the event is written unsigned too — signing NEVER breaks a
    write.

    The ONE exception is the UX WRITE-GATE: when ``identity.require_authenticated`` is on and
    the event CANNOT be signed (no resolvable identity or no signing key), a non-exempt ticket
    type is REFUSED with a clear message. This gate is a CONVENIENCE (fast local feedback), NOT
    the security boundary — the real enforcement is the merge-gate ``rebar verify-authorship``,
    which re-verifies signatures against the epoch-scoped keyring in CI. A determined writer can
    bypass this local gate, but cannot forge a signature the merge-gate will accept.
    """
    try:
        cfg = config.load_config(repo_root)
        require_auth = cfg.identity.require_authenticated
        signing_key = cfg.identity.signing_key
    except Exception:  # noqa: BLE001 — a malformed config must never break an unrelated write
        return

    # Fast path: nothing configured ⇒ no signing, no gate (the overwhelming common case).
    if not require_auth and not signing_key:
        return

    author_id = event.get("author_id")
    if author_id and signing_key:
        try:
            from rebar.attest import authorship, dsse

            envelope = authorship.sign_event_authorship(
                event, signing_key, principal=str(author_id)
            )
            event["author_sig"] = dsse.encode(
                envelope.payload_type,
                envelope.payload,
                [{"keyid": s.keyid, "sig": s.sig} for s in envelope.signatures],
            )
        except Exception:  # noqa: BLE001 — a signing failure never breaks the write; the merge-gate flags it
            logger.warning(
                "authorship: could not sign event %s for %s — writing unsigned",
                event.get("uuid"),
                ticket_id,
                exc_info=True,
            )
        # Signed (or a failure logged + written unsigned). The write-gate below fires only on
        # a MISSING identity/key, not on a signing failure when both are present, so return.
        return

    # Cannot sign: no resolvable identity or no signing_key configured.
    if require_auth:
        ttype = _event_ticket_type(ticket_id, event_type, data, tracker)
        if ttype not in _AUTHORSHIP_GATE_EXEMPT_TYPES:
            raise CommandError(
                "Error: identity.require_authenticated is on but this event cannot be "
                "signed. Configure your identity (`rebar identity use <id>`) and "
                "identity.signing_key (path to your SSH signing key), then retry."
            )


def append_event(
    ticket_id: str,
    event_type: str,
    data: dict,
    tracker: Path,
    *,
    repo_root=None,
    author_fallback: str = "Unknown",
) -> None:
    """Compose an event and append it through the single locked write path.

    Builds the canonical event envelope (``{timestamp, uuid, event_type, env_id,
    author, data}``) and commits + pushes IN-PROCESS via
    ``rebar._store.event_append.write_and_push`` (the canonical committer owns
    serialisation; it never re-derives the envelope fields composed here). Raises
    :class:`CommandError` carrying the exit code on failure (e.g. 75 = rebase/merge
    guard). (Tier D retired the bash seam; ``rebar._store`` is the sole write core.)
    """
    from rebar._store import event_append as _store_append
    from rebar._store import hlc
    from rebar._store.compat import StoreIncompatibleError
    from rebar._store.event_append import StoreError
    from rebar._store.lock import LockTimeout, RebaseGuard

    # Init gate at the single write seam (bug roar-nurse-stomp): every write —
    # create/edit/link AND the leaf appends (comment/tag/set_*/sign/...) — flows
    # through here, so enforcing `.env-id` once keeps the precondition consistent
    # across ALL write commands and guarantees no event is ever appended without an
    # env_id provenance stamp. (composer/transition keep their own pre-checks for an
    # early, identical message; this is the backstop none can bypass.)
    if not (Path(tracker) / ".env-id").is_file():
        raise CommandError("Error: ticket system not initialized. Run 'ticket init' first.")

    timestamp, uuid_str = hlc.next_tick(str(tracker), ticket_id), str(_uuid.uuid4())
    event = {
        "timestamp": timestamp,
        "uuid": uuid_str,
        "event_type": event_type,
        "env_id": env_id(tracker),
        "author": author(author_fallback),
        "data": data,
    }
    # Denormalized author attribution (epic gnu-whale-ichor): stamp author_email
    # (always) + author_id (when a current identity resolves) alongside `author`.
    # Merged BEFORE the batch-sink branch so a buffered event is byte-identical to a
    # directly-committed one. Canonical serialization sorts keys, so envelope order is
    # irrelevant to the on-disk bytes.
    event.update(attribution_fields(repo_root))
    # Optional write-time authorship signing + the opt-in UX write-gate (epic
    # gnu-whale-ichor / 3183). Runs AFTER attribution (so `author_id` is available to sign
    # with) and BEFORE the batch-sink branch (so a buffered event is byte-identical to a
    # directly-committed one). Best-effort signing; may raise CommandError ONLY when the
    # write-gate is on and the event cannot be signed for a non-exempt type.
    _apply_authorship(event, ticket_id, event_type, data, tracker, repo_root)
    # Deferred-commit sink (B2): when a batch buffer is active, hand the composed
    # event to it instead of committing. The caller flushes the buffer via
    # batch_stage_and_commit. Guards above (init gate, env_id/author/hlc) have run,
    # so the buffered event is byte-identical to what write_and_push would commit.
    sink = _batch_sink.get()
    if sink is not None:
        sink.append((ticket_id, event))
        return
    try:
        _store_append.write_and_push(str(tracker), ticket_id, event)
    except (StoreError, RebaseGuard, LockTimeout, StoreIncompatibleError) as exc:
        # StoreIncompatibleError (story 21dd): the write-lock gate fails closed on a
        # store this rebar cannot interpret — surface it as a non-zero CommandError.
        raise CommandError(str(exc), returncode=getattr(exc, "returncode", 1)) from None
