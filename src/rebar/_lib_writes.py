"""rebar library — write path (ticket lifecycle, mutations, and signing).

The wrapper bodies for the public write/mutation surface, split out of the
``rebar`` package facade (``__init__.py``) so that facade stays a thin re-export
namespace (ticket S3 / 4532). ``rebar.<name>`` re-exports every public function
here; the private ``_python_leaf`` helper (the Tier B leaf-write adapter) lives
here too and is re-exported as ``rebar._python_leaf``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast, overload

from rebar import config
from rebar._errors import ConcurrencyError, RebarError

if TYPE_CHECKING:
    # Schema-derived return types (story 3a10). Import-only under TYPE_CHECKING —
    # ``from __future__ import annotations`` makes every annotation a string, so
    # these names never need to exist at runtime (zero import cost, no cycle).
    from rebar.types import (
        ClaimResult,
        CreateResult,
        SignResult,
        TransitionResult,
        VerifySignatureResult,
    )


# ── Initialization ───────────────────────────────────────────────────────────
def init_repo(*, repo_root=None) -> None:
    """Initialize the ticket system (orphan ``tickets`` branch + worktree).

    This is the explicit library init path (Tier E E4, in-process): it always
    bootstraps and never prompts. Other library calls do NOT auto-init — they
    require this to have run first (or ``rebar init`` interactively)."""
    from rebar._commands import init as _init_cmd

    rc = _init_cmd.init_core(repo_root, silent=True)
    if rc != 0:
        raise RebarError(f"rebar init failed (exit {rc})", returncode=rc)


# ── Write path (subprocess → dispatcher) ─────────────────────────────────────
@overload
def create_ticket(
    ticket_type: str,
    title: str,
    *,
    parent: str | None = ...,
    priority: int | None = ...,
    assignee: str | None = ...,
    description: str | None = ...,
    tags: list[str] | None = ...,
    source: dict | None = ...,
    return_alias: Literal[False] = ...,
    repo_root=...,
    _creation_channel: str = ...,
) -> str: ...


@overload
def create_ticket(
    ticket_type: str,
    title: str,
    *,
    parent: str | None = ...,
    priority: int | None = ...,
    assignee: str | None = ...,
    description: str | None = ...,
    tags: list[str] | None = ...,
    source: dict | None = ...,
    return_alias: Literal[True],
    repo_root=...,
    _creation_channel: str = ...,
) -> CreateResult: ...


def create_ticket(
    ticket_type: str,
    title: str,
    *,
    parent: str | None = None,
    priority: int | None = None,
    assignee: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    source: dict | None = None,
    return_alias: bool = False,
    repo_root=None,
    _creation_channel: str = "python",
) -> str | CreateResult:
    """Create a ticket.

    Returns the canonical 16-hex ticket id (default). With ``return_alias=True``,
    returns ``{"id": <16-hex>, "alias": <human alias>}`` so agents don't need a
    second ``show`` to learn the alias (WS5e).

    ``source`` (P1.2 import): optional provenance dict — keys ``source_id``,
    ``source_created_at``, ``source_author``, ``source_env`` are recorded on the
    CREATE event and surfaced in compiled state, so an imported ticket preserves
    where it came from while still getting a fresh local id + HLC timestamp.

    ``_creation_channel`` is INTERNAL (leading underscore; not part of the documented
    public signature): a direct library call defaults to ``"python"``, and the MCP
    adapter passes ``"mcp"`` through it so a genesis CREATE records its interface. A
    later import story overrides it via the ``source=`` path.
    """
    # Composed in-process via the shared create_core (validation/alias/CREATE
    # event); the bash create path was retired with the Tier B cutover.
    from rebar._commands import composer
    from rebar._commands._seam import CommandError

    try:
        res = composer.create_core(
            ticket_type,
            title,
            parent=parent,
            priority=priority,
            assignee=assignee,
            description=description,
            tags=tags,
            source=source,
            repo_root=repo_root,
            creation_channel=_creation_channel,
        )
    except CommandError as exc:
        raise RebarError(
            f"rebar create failed (exit {exc.returncode}): {exc.message}",
            returncode=exc.returncode,
            stderr=exc.message,
        ) from None
    if not return_alias:
        return res["id"]
    return {"id": res["id"], "alias": res["alias"] or ""}


def idea(
    title: str,
    *,
    description: str | None = None,
    return_alias: bool = False,
    repo_root=None,
    _creation_channel: str = "python",
) -> str | CreateResult:
    """Capture an undesigned idea: create an ``epic`` in status ``idea`` atomically.

    The idea is born in status ``idea`` via a single CREATE event (no intervening
    STATUS event), so it is never momentarily ``open``/claimable. It is excluded from
    ``ready``/``next-batch``, and ``idea -> closed`` (reject) skips the completion
    gates. Promote a kept idea with ``transition(id, "idea", "open")``.

    Returns the canonical 16-hex ticket id (default), or ``{"id", "alias"}`` with
    ``return_alias=True`` — same shape as :func:`create_ticket`.

    ``_creation_channel`` is INTERNAL (see :func:`create_ticket`): defaults to
    ``"python"``; the MCP adapter passes ``"mcp"``.
    """
    from rebar._commands import composer
    from rebar._commands._seam import CommandError

    try:
        res = composer.create_core(
            "epic",
            title,
            description=description,
            status="idea",
            repo_root=repo_root,
            creation_channel=_creation_channel,
        )
    except CommandError as exc:
        raise RebarError(
            f"rebar idea failed (exit {exc.returncode}): {exc.message}",
            returncode=exc.returncode,
            stderr=exc.message,
        ) from None
    if not return_alias:
        return res["id"]
    return {"id": res["id"], "alias": res["alias"] or ""}


def create_identity(
    name: str,
    email: str,
    mappings: list[dict] | None = None,
    keys: list[str] | None = None,
    *,
    tags: list[str] | None = None,
    repo_root=None,
    return_alias: bool = False,
    _creation_channel: str = "python",
) -> str | CreateResult:
    """Mint an ``identity`` entity ticket in one CREATE event; return its id.

    ``name`` becomes the title; ``email`` / ``mappings`` (``{provider, external_id}``)
    / ``keys`` (OpenSSH authorized-keys lines) ride the CREATE payload and surface in
    compiled state. ``tags`` (e.g. ``["placeholder"]`` for a ghost) ride the SAME CREATE
    event atomically. Returns the canonical 16-hex id (default), or ``{"id", "alias"}``
    with ``return_alias=True`` — same shape as :func:`create_ticket`.

    ``_creation_channel`` is INTERNAL (see :func:`create_ticket`): defaults to
    ``"python"``; the MCP adapter passes ``"mcp"``.
    """
    from rebar._commands import identity as _identity
    from rebar._commands._seam import CommandError

    try:
        res = _identity.create_identity_core(
            name,
            email,
            mappings=mappings,
            keys=keys,
            tags=tags,
            repo_root=repo_root,
            creation_channel=_creation_channel,
        )
    except CommandError as exc:
        raise RebarError(
            f"rebar identity create failed (exit {exc.returncode}): {exc.message}",
            returncode=exc.returncode,
            stderr=exc.message,
        ) from None
    if not return_alias:
        return res["id"]
    return {"id": res["id"], "alias": res["alias"] or ""}


def ensure_identity_for(
    provider: str,
    external_id: str,
    display_name: str,
    *,
    repo_root=None,
    creation_channel: str = "python",
) -> str:
    """Resolve-or-mint the identity for an inbound ``(provider, external_id)`` user; return
    its id (2f13). Idempotent: reuses an existing mapping (upgrading a placeholder's title
    in place when it is still a ghost), else mints a ``placeholder`` identity. Never raises
    on a lookup problem — see :func:`rebar._commands.identity.ensure_identity_for`.

    ``creation_channel`` (story e622) is threaded to a minted placeholder's genesis CREATE;
    it defaults to ``"python"`` and the Jira inbound path passes ``"jira"``."""
    from rebar._commands import identity as _identity

    return _identity.ensure_identity_for(
        provider,
        external_id,
        display_name,
        repo_root=repo_root,
        creation_channel=creation_channel,
    )


def create_placeholder(
    provider: str,
    external_id: str,
    display_name: str,
    *,
    repo_root=None,
) -> str:
    """Resolve-or-mint the placeholder identity for ``(provider, external_id)``; return its id
    (117b). A thin alias for :func:`ensure_identity_for` — see
    :func:`rebar._commands.identity.create_placeholder`."""
    from rebar._commands import identity as _identity

    return _identity.create_placeholder(provider, external_id, display_name, repo_root=repo_root)


def add_identity_key(identity_id, public_key, *, signature=None, repo_root=None) -> None:
    """Add ``public_key`` to an identity's epoch-scoped keyring (epic gnu-whale-ichor).

    GENESIS/TOFU: the first key on a keyless identity is added trust-on-first-use (no
    signature). NON-GENESIS: ``signature`` (a :class:`~rebar.attest.dsse.Envelope` over
    ``authorship.keyop_payload("KEY_ADD", identity_id, public_key)``) is REQUIRED and must
    verify against a currently-valid key, or the rotation is refused (``RebarError``)."""
    from rebar._commands import identity as _identity
    from rebar._commands._seam import CommandError

    try:
        _identity.add_identity_key(
            identity_id, public_key, signature=signature, repo_root=repo_root
        )
    except CommandError as exc:
        raise RebarError(
            f"rebar identity key add failed (exit {exc.returncode}): {exc.message}",
            returncode=exc.returncode,
            stderr=exc.message,
        ) from None


def revoke_identity_key(identity_id, public_key, *, signature, repo_root=None) -> None:
    """Revoke ``public_key`` from an identity's keyring (epic gnu-whale-ichor).

    Always signed: ``signature`` (a :class:`~rebar.attest.dsse.Envelope` over
    ``authorship.keyop_payload("KEY_REVOKE", identity_id, public_key)``) is REQUIRED and
    must verify against a currently-valid key, or the revoke is refused (``RebarError``)."""
    from rebar._commands import identity as _identity
    from rebar._commands._seam import CommandError

    try:
        _identity.revoke_identity_key(
            identity_id, public_key, signature=signature, repo_root=repo_root
        )
    except CommandError as exc:
        raise RebarError(
            f"rebar identity key revoke failed (exit {exc.returncode}): {exc.message}",
            returncode=exc.returncode,
            stderr=exc.message,
        ) from None


def use_identity(identity_id: str, *, repo_root=None) -> None:
    """Point ``.rebar/current_identity`` at ``identity_id`` (a local, git-ignored
    pointer — never propagated across machines)."""
    from rebar._commands import identity as _identity

    _identity.use_identity(identity_id, repo_root=repo_root)


def resolve_current_identity(*, repo_root=None) -> str | None:
    """Resolve the current self-identity (opt-in; returns ``None`` on any miss, never
    raises). Prefers the ``.rebar/current_identity`` pointer, else a case-insensitive
    ``git config user.email`` match against identity tickets."""
    from rebar._commands import identity as _identity

    return _identity.resolve_current_identity(repo_root=repo_root)


def transition(
    ticket_id: str,
    current_status: str,
    target_status: str,
    *,
    force: bool = False,
    reason: str = "",
    force_close: str | None = None,
    repo_root=None,
) -> TransitionResult:
    """Transition a ticket's status with optimistic concurrency.

    Raises :class:`ConcurrencyError` if the ticket's actual status no longer
    matches ``current_status`` (engine exit code 10), and :class:`RebarError`
    for other failures.

    ``open -> in_progress`` is a start-work transition gated by the plan-review
    gate (``verify.require_plan_review_for_claim``) exactly like :func:`claim`;
    pass ``force=True`` (optionally with a ``reason`` recorded in the audit
    comment) to bypass it. ``force`` also waives the unresolved-children guard
    when closing.

    ``force_close="<reason>"`` is the library counterpart of the CLI
    ``--force-close``: when closing a work ticket under the completion-verification
    close gate (``verify.require_completion_verification_for_close``), it closes
    WITHOUT running the verifier or signing, leaving the ticket
    closed-without-signature (the durable "validation did not pass" signal). It is
    threaded to the same command-layer seam the CLI uses; ``None`` means "not a
    forced close" (the verifier runs normally).
    """
    # In-process (Tier E E3): resolve the id, then run the shared transition core
    # (ticket-transition.sh was retired from this path). The structured result
    # {ticket_id, from, to, newly_unblocked[]} is the single source of truth.
    from rebar._commands import transition as _transition
    from rebar._commands._seam import CommandError
    from rebar._commands.txn import ConcurrencyMismatch
    from rebar._engine_support.resolver import resolve_ticket_id

    tracker = str(config.tracker_dir(repo_root))
    resolved = resolve_ticket_id(ticket_id, tracker)
    if resolved is None:
        raise RebarError(
            f"rebar transition failed (exit 1): Error: ticket '{ticket_id}' not found",
            returncode=1,
            stderr=f"Error: ticket '{ticket_id}' not found\n",
        )
    try:
        result = _transition.transition_compute(
            resolved,
            current_status,
            target_status,
            force=force,
            reason=reason,
            force_close=force_close or "",
            repo_root=repo_root,
        )
    except ConcurrencyMismatch as exc:
        raise ConcurrencyError(
            f"transition rejected: {ticket_id} is no longer '{current_status}'. {exc.message}",
            returncode=10,
            stderr=exc.message,
        ) from None
    except CommandError as exc:
        raise RebarError(
            f"rebar transition failed (exit {exc.returncode}): {exc.message}",
            returncode=exc.returncode,
            stderr=exc.message,
        ) from None
    return {
        "ticket_id": result["ticket_id"],
        "from": result["from"],
        "to": result["to"],
        "newly_unblocked": result["newly_unblocked"],
    }


def claim(ticket_id: str, *, assignee=None, force=None, repo_root=None) -> ClaimResult:
    """Atomically claim an OPEN ticket: move it to ``in_progress`` and set its
    assignee in one locked critical section.

    Raises :class:`ConcurrencyError` (engine exit code 10) if the ticket is not
    ``open`` — i.e. someone else already claimed it — and :class:`RebarError` for
    other failures. This is the optimistic-concurrency primitive parallel agents
    use to grab work without double-assignment.

    When the plan-review claim gate is enabled
    (``verify.require_plan_review_for_claim``), a non-bug/non-session_log claim
    requires a fresh certified plan-review attestation; pass ``force="<reason>"``
    to bypass the gate with an audit comment.
    """
    # In-process (Tier E E3): resolve the id, then run the shared claim core
    # (ticket-claim.sh was retired from this path). Returns the structured result
    # {ticket_id, status, assignee}.
    from rebar._commands import transition as _transition
    from rebar._commands._seam import CommandError
    from rebar._commands.txn import ConcurrencyMismatch
    from rebar._engine_support.resolver import resolve_ticket_id

    tracker = str(config.tracker_dir(repo_root))
    resolved = resolve_ticket_id(ticket_id, tracker)
    if resolved is None:
        raise RebarError(
            f"rebar claim failed (exit 1): Error: ticket '{ticket_id}' not found",
            returncode=1,
            stderr=f"Error: ticket '{ticket_id}' not found\n",
        )
    try:
        # Pass assignee THROUGH (don't coerce None→""): None is the "unspecified"
        # sentinel that triggers the ticket.default_assignee fallback in claim_compute,
        # while an explicit "" clears the assignee without falling back (story c36c).
        return cast(
            "ClaimResult",
            _transition.claim_compute(
                resolved, assignee=assignee, force_plan_review=force or "", repo_root=repo_root
            ),
        )
    except ConcurrencyMismatch as exc:
        raise ConcurrencyError(
            f"claim rejected: {ticket_id} is not open (already claimed). {exc.message}",
            returncode=10,
            stderr=exc.message,
        ) from None
    except CommandError as exc:
        raise RebarError(
            f"rebar claim failed (exit {exc.returncode}): {exc.message}",
            returncode=exc.returncode,
            stderr=exc.message,
        ) from None


def reopen(ticket_id: str, *, repo_root=None) -> TransitionResult:
    """Reopen a closed ticket (closed -> open) — a thin convenience over
    :func:`transition`, still optimistic-concurrency (raises ConcurrencyError if
    the ticket is not currently ``closed``)."""
    return transition(ticket_id, "closed", "open", repo_root=repo_root)


def _python_leaf(fn, *args, repo_root, what: str, **kwargs) -> None:
    """Run a Tier B leaf write in-process — the sole path since the cutover.

    Tier B retired its kill-switch after the soak (docs/bash-migration.md §4); the
    library/MCP write surface now calls ``rebar._commands`` directly. A command
    failure is mapped onto RebarError so the exit-code contract is unchanged.
    Extra keyword arguments are forwarded verbatim to ``fn`` (e.g. ``source=`` for
    comment provenance).
    """
    from rebar._commands._seam import CommandError

    try:
        fn(*args, repo_root=repo_root, **kwargs)
    except CommandError as exc:
        raise RebarError(
            f"rebar {what} failed (exit {exc.returncode}): {exc.message}",
            returncode=exc.returncode,
            stderr=exc.message,
        ) from None


def comment(ticket_id: str, body: str, *, source: dict | None = None, repo_root=None) -> None:
    """Append a comment. ``source`` (P1.2 import): optional per-comment provenance
    (``source_author``/``source_created_at``) preserved on the imported comment."""
    from rebar._commands import leaf

    _python_leaf(leaf.comment, ticket_id, body, source=source, repo_root=repo_root, what="comment")


def append_session_log(
    entry: str,
    *,
    summary=None,
    relates_to=None,
    discovered_from=None,
    repo_root=None,
    _creation_channel: str = "python",
) -> dict:
    """Append ``entry`` to the current session_log, creating one on first use.

    A convenience over ``create`` + ``comment``: the first call creates a
    ``session_log`` (titled ``summary`` or a default) and records it as the
    current log via a local pointer; subsequent calls append to that same log.
    Optional ``relates_to`` / ``discovered_from`` link the log to the work it
    documents (blocking links remain refused). Returns
    ``{"id", "alias", "created"}``.

    ``_creation_channel`` is INTERNAL (see :func:`create_ticket`): defaults to
    ``"python"``; the MCP adapter passes ``"mcp"`` — it stamps the session_log's genesis
    CREATE when this call creates one."""
    from rebar._commands import session_log
    from rebar._commands._seam import CommandError

    try:
        return session_log.append(
            entry,
            summary=summary,
            relates_to=relates_to,
            discovered_from=discovered_from,
            repo_root=repo_root,
            creation_channel=_creation_channel,
        )
    except CommandError as exc:
        raise RebarError(exc.message, returncode=exc.returncode, stderr=exc.message) from None


def start_session_log(
    *,
    summary=None,
    relates_to=None,
    discovered_from=None,
    repo_root=None,
    _creation_channel: str = "python",
) -> dict:
    """Explicitly create a NEW session_log and make it the current one (rotating
    away from any prior log). Returns ``{"id", "alias"}``.

    ``_creation_channel`` is INTERNAL (see :func:`create_ticket`): defaults to
    ``"python"``; the MCP adapter passes ``"mcp"``."""
    from rebar._commands import session_log
    from rebar._commands._seam import CommandError

    try:
        return session_log.start(
            summary=summary,
            relates_to=relates_to,
            discovered_from=discovered_from,
            repo_root=repo_root,
            creation_channel=_creation_channel,
        )
    except CommandError as exc:
        raise RebarError(exc.message, returncode=exc.returncode, stderr=exc.message) from None


def edit_ticket(ticket_id: str, *, repo_root=None, **fields) -> None:
    """Edit ticket fields: title, priority, assignee, ticket_type, description.

    Tags (P2.3): use ``add_tags``/``remove_tags``/``set_tags`` (lists or CSV) to
    mutate via convergent TAG_DELTA deltas. (The ``tags=`` set-alias was removed
    pre-1.0 — DE7; it is now rejected as an unknown field.)
    """
    tag_add = fields.pop("add_tags", None)
    tag_remove = fields.pop("remove_tags", None)
    tag_set = fields.pop("set_tags", None)
    normalized = {}
    for key, value in fields.items():
        if value is None:
            continue
        normalized[key] = str(value)
    from rebar._commands import composer

    _python_leaf(
        composer.edit_core,
        ticket_id,
        normalized,
        repo_root=repo_root,
        what="edit",
        tag_add=tag_add,
        tag_remove=tag_remove,
        tag_set=tag_set,
    )


def link(id1: str, id2: str, relation: str, *, repo_root=None) -> None:
    """Link two tickets.

    ``relation`` must be one of the six canonical relations: blocks, depends_on,
    relates_to, duplicates, supersedes, discovered_from.
    """
    from rebar._commands import composer

    def _link(i, j, rel, *, repo_root):
        composer.link_core(i, j, rel, repo_root=repo_root, quiet=True)

    _python_leaf(_link, id1, id2, relation, repo_root=repo_root, what="link")


def unlink(id1: str, id2: str, *, repo_root=None) -> None:
    from rebar._commands import unlink as _unlink_cmd

    _python_leaf(_unlink_cmd.unlink_core, id1, id2, repo_root=repo_root, what="unlink")


def tag(ticket_id: str, tag: str, *, repo_root=None) -> None:
    from rebar._commands import leaf

    _python_leaf(leaf.tag, ticket_id, tag, repo_root=repo_root, what="tag")


def untag(ticket_id: str, tag: str, *, repo_root=None) -> None:
    from rebar._commands import leaf

    _python_leaf(leaf.untag, ticket_id, tag, repo_root=repo_root, what="untag")


def archive(ticket_id: str, *, repo_root=None) -> None:
    from rebar._commands import leaf

    _python_leaf(leaf.archive, ticket_id, repo_root=repo_root, what="archive")


def compact(ticket_id: str | None = None, *, repo_root=None) -> None:
    # In-process (Tier E E3): compact-on-id via the shared compaction core
    # (ticket-compact.sh retired from this path). Output is captured (the bash
    # library wrapper captured it too); failures raise RebarError.
    import contextlib
    import io

    from rebar._commands import compact as _compact

    out, err = io.StringIO(), io.StringIO()
    argv = [ticket_id] if ticket_id else []
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = _compact.compact_cli(argv, repo_root=repo_root)
    if rc != 0:
        raise RebarError(
            f"rebar compact failed (exit {rc}): {err.getvalue().strip()}",
            returncode=rc,
            stderr=err.getvalue(),
        )


def attach_commits(ticket_id: str, commits, *, repo_root=None) -> dict:
    """Attach commit SHAs to a ticket as a durable, union-merged ``commits`` list
    (epic a88f / WS-H). ``commits`` is a list of SHA strings or {sha, message?,
    author?, …} records. Convergent (union by sha) and NOT synced to Jira. Returns
    ``{ticket_id, attached}``."""
    from rebar._commands import _seam
    from rebar._commands._seam import CommandError

    tracker = _seam.tracker_dir(repo_root)
    tid = _seam.require_id(ticket_id, tracker)
    _seam.require_not_ghost(tid, tracker)
    records = []
    for c in commits:
        if isinstance(c, str) and c:
            records.append({"sha": c})
        elif isinstance(c, dict) and c.get("sha"):
            records.append(c)
        else:
            raise RebarError(f"invalid commit entry {c!r}: need a sha string or {{sha, …}} dict")
    try:
        _seam.append_event(tid, "COMMITS", {"commits": records}, tracker, repo_root=repo_root)
    except CommandError as exc:
        raise RebarError(
            f"rebar attach-commits failed (exit {exc.returncode}): {exc.message}",
            returncode=exc.returncode,
            stderr=exc.message,
        ) from None
    return {"ticket_id": tid, "attached": len(records)}


# ── Cryptographic manifest signing (environment-bound) ────────────────────────
def sign_manifest(ticket_id: str, manifest, *, repo_root=None) -> SignResult:
    """Sign a manifest of verified steps for a ticket with the environment key.

    ``manifest`` is a list of verified-step strings (or a JSON-array string).
    Computes an HMAC-SHA256 signature with the environment-specific signing key
    (``REBAR_SIGNING_KEY`` or the gitignored ``.signing-key``), persists it as a
    SIGNATURE event, and returns the record
    ``{ticket_id, manifest, algorithm, signature, key_id, head_sha, signed_at}``.
    """
    from rebar import signing
    from rebar.signing import SigningError

    try:
        return cast("SignResult", signing.sign_manifest(ticket_id, manifest, repo_root=repo_root))
    except SigningError as exc:
        raise RebarError(
            f"rebar sign failed (exit {exc.returncode}): {exc.message}",
            returncode=exc.returncode,
            stderr=exc.message,
        ) from None


def verify_signature(
    ticket_id: str, *, kind: str | None = None, repo_root=None
) -> VerifySignatureResult:
    """Certify a ticket's recorded verified steps against its signature.

    Returns a verdict dict ``{ticket_id, verified, verdict, reason, manifest,
    ...}``. ``verdict`` is ``certified`` (steps match the signature under this
    environment's key), ``mismatch`` (steps altered / signature invalid),
    ``foreign_key`` (signed by a different environment), or ``unsigned``. Raises
    :class:`RebarError` only when the ticket id cannot be resolved.

    ``kind`` selects which attestation to verify (epic dark-acme-lumen): ``None`` (default)
    verifies the most-recent signature (back-compatible); an explicit kind (e.g.
    ``"completion-verifier"``) verifies that kind strictly from the kind-keyed map.
    """
    from rebar import signing
    from rebar.signing import SigningError

    try:
        return cast(
            "VerifySignatureResult",
            signing.verify_signature(ticket_id, kind=kind, repo_root=repo_root),
        )
    except SigningError as exc:
        raise RebarError(
            f"rebar verify-signature failed (exit {exc.returncode}): {exc.message}",
            returncode=exc.returncode,
            stderr=exc.message,
        ) from None
