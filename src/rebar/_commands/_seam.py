"""Shared write-command seam for ticket resolution, event metadata,
screening, authorship, and locked append. Leaf commands compose events here
through rebar._store. The optional batch sink defers commits for import."""

from __future__ import annotations

import argparse
import contextvars
import logging
import os
import re as _re
import subprocess
import sys
import uuid as _uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from rebar import config
from rebar import secret_screen as _secret_screen
from rebar._engine_support.resolver import resolve_ticket_id

logger = logging.getLogger(__name__)

# An active import buffer collects finalized events for chunk commits.
# The default keeps one commit per interactive write. Caller state guards run
# before buffering.
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


class SecretScreenRefused(CommandError):
    """CommandError subtype for a secret-screen policy refusal.
    LLM sidecars can warn about this refusal before returning their usual
    best-effort failure result."""


def _warn_stderr(message: str | None) -> None:
    """Print a shared CLI Warning line only when advisory text exists."""
    if message:
        print(f"Warning: {message}", file=sys.stderr)


def warn_secret_screen_refused(ticket_id: str, record_label: str) -> None:
    """Warn that a ticket's LLM sidecar was blocked by the secret screen and
    was not written. The caller still reports best-effort failure."""
    import sys

    sys.stderr.write(
        f"Warning: {record_label} for {ticket_id} was recorded WITHOUT a durable sidecar "
        "audit record — the write-time secret screen BLOCKED the write because the reviewed "
        "material matched a live credential shape, so the gate's audit-trail record was NOT "
        "written. The gate/close outcome is unchanged (best-effort observability); scrub the "
        "credential from the reviewed material to restore the audit record.\n"
    )


class WriteArgParser(argparse.ArgumentParser):
    """Preserve write-command errors while using argparse.
    Raise CommandError with exit 1 and the established missing-value message.
    Callers disable help and abbreviations and skip unknown tokens through
    parse_known_args."""

    def error(self, message: str) -> None:  # type: ignore[override]
        m = _re.match(r"argument (\S+): expected one argument", message)
        if m:
            raise CommandError(f"Error: {m.group(1)} requires a value", returncode=1)
        raise CommandError(f"Error: {message}", returncode=1)


def write_arg_parser() -> WriteArgParser:
    """Build a :class:`WriteArgParser` with the write-command conventions applied
    (``add_help=False``, ``allow_abbrev=False``). Callers add their value flags and parse with
    ``parse_known_args`` to silently skip unknown tokens."""
    return WriteArgParser(add_help=False, allow_abbrev=False)


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


_author_cache: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "rebar_author_cache", default=None
)


@contextmanager
def author_cache() -> Iterator[None]:
    """Freeze ``author()`` lookups for an explicit bulk-write scope.

    Values are keyed by fallback so a git-config-less import preserves each command's
    historical ``Unknown``/``unknown`` spelling. The scope is context-local, nestable,
    and always restores the prior cache; outside it, ``author()`` remains a live lookup.

    This is deliberately separate from ``_ATTRIBUTION_CACHE`` below: that long-lived,
    repo-keyed cache owns email/current-identity invalidation, while ``user.name`` is
    frozen only when a bulk importer explicitly opens this short-lived scope.
    """
    token = _author_cache.set({})
    try:
        yield
    finally:
        _author_cache.reset(token)


def author(fallback: str = "Unknown") -> str:
    """Commit author name from git config, falling back to ``fallback`` (bash parity).

    The fallback string differs by command: comment / file-impact / verify-commands
    use ``Unknown``; the tag helpers use lowercase ``unknown``. Callers pass the
    value their bash counterpart uses so a git-config-less environment matches.
    """
    cache = _author_cache.get()
    if cache is None:
        return _git_config("user.name", fallback)
    if fallback not in cache:
        # Context copies inherit values by reference. Never mutate the inherited mapping:
        # install a copy in THIS context so a miss in a copied task/context cannot populate
        # its parent's cache (or vice versa).
        value = _git_config("user.name", fallback)
        cache = {**cache, fallback: value}
        _author_cache.set(cache)
    return cache[fallback]


def author_email(repo_root=None) -> str:
    """Commit email from the STORE repo's ``git config user.email`` (``""`` on any
    failure). Reads with ``cwd`` = the resolved repo_root — mirroring
    ``identity._git_email`` — so it reflects the store's committer, not the ambient
    process cwd's git config."""
    return _git_config("user.email", "", cwd=str(repo_root or config.repo_root()))


# Cache email and resolved identity per canonical repository root.
# An identity-pointer change clears this cache.
_ATTRIBUTION_CACHE: dict[str, dict] = {}


def attribution_fields(repo_root=None) -> dict:
    """Return cached event author_email and any resolved author_id.
    Resolve once per canonical repository root. Omit author_id on a miss."""
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
    """Clear cached attribution after an identity change or in tests."""
    _ATTRIBUTION_CACHE.clear()


_TAG_CTRL_RE = _re.compile(r"[\x00-\x1f\x7f]")


def validate_tag_name(raw: str) -> str:
    """Return a trimmed tag or raise CommandError for empty or control text.
    All tag write paths share this guard."""
    t = str(raw).strip()
    if not t:
        raise CommandError("Error: tag name must be non-empty / non-whitespace")
    if _TAG_CTRL_RE.search(t):
        raise CommandError(f"Error: invalid tag name {raw!r} (contains control characters)")
    return t


def current_tags(ticket_id: str, tracker: Path) -> list[str]:
    """Return reducer-compiled ticket tags, or an empty list if the ticket
    cannot be reduced."""
    from rebar.reducer import reduce_ticket

    try:
        state = reduce_ticket(str(tracker / ticket_id))
        return list((state or {}).get("tags") or [])
    except Exception:  # noqa: BLE001 — bash tag helpers swallow show failures too; fall open to no observed tags
        return []


# Keep identity, session logs, and code reviews writable before an
# authenticated identity exists.
_AUTHORSHIP_GATE_EXEMPT_TYPES = ("session_log", "code_review", "identity")


def _event_ticket_type(ticket_id, event_type, data, tracker) -> str | None:
    """Read type from CREATE data or reduce an existing ticket for signing-gate
    exemptions. Unknown types remain non-exempt."""
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
    """Sign canonical event bytes when identity and key are available.
    Log missing material or signing failures and write unsigned unless the
    authenticated write gate refuses a non-exempt type with missing material.
    CI verifies signatures at merge."""
    try:
        cfg = config.compose_config(repo_root)
        require_auth = cfg.identity.require_authenticated
        signing_key = cfg.identity.signing_key
    except Exception:  # noqa: BLE001 — a malformed config must never break an unrelated write
        return

    # Skip signing and gating when neither is configured.
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
        except Exception:
            logger.warning(
                "authorship: could not sign event %s for %s — writing unsigned",
                event.get("uuid"),
                ticket_id,
                exc_info=True,
            )
        # A signing attempt ends here even if it logged a failure.
        # The local write gate only checks missing identity or key.
        return

    # Log missing identity or key when signing is configured or required.
    # Without a required gate, the event is still written unsigned.
    logger.warning(
        "authorship: signing SKIPPED for event %s on %s — %s; writing unsigned",
        event.get("uuid"),
        ticket_id,
        "no resolvable identity (author_id) for this writer"
        if not author_id
        else "no identity.signing_key configured",
    )

    if require_auth:
        ttype = _event_ticket_type(ticket_id, event_type, data, tracker)
        if ttype not in _AUTHORSHIP_GATE_EXEMPT_TYPES:
            raise CommandError(
                "Error: identity.require_authenticated is on but this event cannot be "
                "signed. Configure your identity (`rebar identity use <id>`) and "
                "identity.signing_key (path to your SSH signing key), then retry."
            )


def finalize_event(
    event: dict, ticket_id: str, event_type: str, data: dict, tracker, repo_root
) -> None:
    """Screen and finalize an event in place before its caller commits.
    Apply secret screening, refuse an unpublishable store outside a batch,
    stamp attribution, and run optional authorship signing or the write gate.
    This shared path covers leaf, transaction, delete, importer, and reconciler
    writes. It neither commits nor pushes."""
    screen_event(data)
    if _batch_sink.get() is None:
        _refuse_unpublishable_store(tracker)
    event.update(attribution_fields(repo_root))
    _apply_authorship(event, ticket_id, event_type, data, tracker, repo_root)


def _refuse_unpublishable_store(tracker) -> None:
    """Fail before committing an event to a tracker with an unrecoverable split history."""
    from rebar._store.freshness import write_blocking_divergence

    stale = write_blocking_divergence(tracker)
    if stale is None:
        return
    remote_ref = stale.get("remote_ref", "the shared store")
    raise CommandError(
        "Error: ticket write refused: "
        f"{stale.get('reason', 'the local ticket store cannot publish')}. "
        f"Run `rebar store fsck` to confirm DIVERGED against {remote_ref}; recover with "
        "`rebar store fsck-recover` for stale rebase/merge dangling commits, or use "
        "`rebar store maintenance` from an isolated operator window before writing "
        "again.",
        returncode=1,
    )


_secret_override: contextvars.ContextVar[str] = contextvars.ContextVar(
    "rebar_secret_override", default=""
)


@contextmanager
def forced_secret_write(reason: str) -> Iterator[None]:
    """Scope in which the secret screen is overridden with *reason* (bug e7a9).

    Set by the CLI dispatcher for the duration of one command, so EVERY write verb —
    ``create``/``edit``/``session-log`` as well as ``comment`` — can honour
    ``--allow-secret-pattern=<reason>`` without each one threading a kwarg down to the
    seam. An empty reason is not an override: the audit record would have no ``why``.
    """
    token = _secret_override.set(reason or "")
    try:
        yield
    finally:
        _secret_override.reset(token)


def screen_event(data: dict) -> None:
    """Refuse a payload carrying a live credential shape, or record a forced write (e7a9).

    REFUSE, never redact (operator decision 2026-08-07): rewriting the caller's payload
    would make the stored event differ from what the caller believes it wrote, which an
    append-only store cannot undo. The refusal names the family/field/line and NEVER the
    value, so reporting the leak does not become a second leak.
    """
    if not isinstance(data, dict) or data.get("secret_override"):
        return  # already stamped as forced upstream (append_event's kwarg)
    findings = _secret_screen.screen_event_data(data)
    if not findings:
        return
    reason = _secret_override.get()
    if not reason:
        raise SecretScreenRefused(
            _secret_screen.refusal_message(findings, override_flag="--allow-secret-pattern")
        )
    data["secret_override"] = _secret_screen.override_record(findings, reason)
    logger.warning(
        "secret screen FORCED: bypassed %s (reason recorded on the event)",
        ", ".join(data["secret_override"]["families"]),
    )


def append_event(
    ticket_id: str,
    event_type: str,
    data: dict,
    tracker: Path,
    *,
    repo_root=None,
    author_fallback: str = "Unknown",
    under_lock_check=None,
    allow_secret_pattern: str = "",
) -> int | None:
    """Compose an event and write it through the locked store path.
    An allow_secret_pattern reason records a screened-content override in
    the event. Raise CommandError with the write failure's exit code."""
    # Require initialized store provenance before any event append.
    if not (Path(tracker) / ".env-id").is_file():
        raise CommandError("Error: ticket system not initialized. Run 'ticket init' first.")

    # Bind one operation snapshot, reusing the caller's if already present.
    from rebar._operation_config import compose_and_bind_operation_snapshot

    with compose_and_bind_operation_snapshot(repo_root=repo_root):
        return _append_event_body(
            ticket_id,
            event_type,
            data,
            tracker,
            repo_root=repo_root,
            author_fallback=author_fallback,
            under_lock_check=under_lock_check,
            allow_secret_pattern=allow_secret_pattern,
        )


def _append_event_body(
    ticket_id: str,
    event_type: str,
    data: dict,
    tracker: Path,
    *,
    repo_root,
    author_fallback: str,
    under_lock_check,
    allow_secret_pattern: str,
) -> int | None:
    """Compose, finalize, and commit within the bound operation snapshot."""
    from rebar._store import event_append as _store_append
    from rebar._store import hlc
    from rebar._store.compat import StoreIncompatibleError
    from rebar._store.event_append import StoreError
    from rebar._store.lock import LockTimeout, RebaseGuard

    # Stamp the override reason and bypassed families before screening.
    # Event attribution records who performed the write.
    if allow_secret_pattern:
        findings = _secret_screen.screen_event_data(data)
        if findings:
            data = dict(data)
            data["secret_override"] = _secret_screen.override_record(findings, allow_secret_pattern)
            logger.warning(
                "secret screen FORCED on %s %s: bypassed %s (reason recorded on the event)",
                ticket_id,
                event_type,
                ", ".join(data["secret_override"]["families"]),
            )

    timestamp, uuid_str = hlc.next_tick(str(tracker), ticket_id), str(_uuid.uuid4())
    event = {
        "timestamp": timestamp,
        "uuid": uuid_str,
        "event_type": event_type,
        "env_id": env_id(tracker),
        "author": author(author_fallback),
        "data": data,
    }
    # Finalize before buffering so batch and direct writes carry the same
    # attribution and signing data.
    finalize_event(event, ticket_id, event_type, data, tracker, repo_root)
    # Buffer finalized events for import. Otherwise commit and push now.
    sink = _batch_sink.get()
    if sink is not None:
        sink.append((ticket_id, event))
        return int(timestamp)
    try:
        if under_lock_check is None:
            _store_append.write_and_push(str(tracker), ticket_id, event)
        else:
            _store_append.write_and_push(
                str(tracker), ticket_id, event, under_lock_check=under_lock_check
            )
        return int(timestamp)
    except (StoreError, RebaseGuard, LockTimeout, StoreIncompatibleError) as exc:
        if under_lock_check is not None and isinstance(exc, LockTimeout):
            raise
        # StoreIncompatibleError (story 21dd): the write-lock gate fails closed on a
        # store this rebar cannot interpret — surface it as a non-zero CommandError.
        raise CommandError(str(exc), returncode=getattr(exc, "returncode", 1)) from None
