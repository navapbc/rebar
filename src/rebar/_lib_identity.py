"""rebar library — identity entities, their key material, and manifest signing.

Split out of ``rebar._lib_writes`` by concern (ticket 4631-5598-7127-4a56). This is
the module's non-ticket surface: nothing here mutates ticket state.

* **Identity & keys** — the ``identity`` entity wrappers, which delegate to
  ``rebar._commands.identity``. Identities carry OpenSSH authorized-keys lines
  (``add_identity_key`` / ``revoke_identity_key``) and ``use_identity`` /
  ``resolve_current_identity`` select the current one.
* **Signing** — ``sign_manifest`` / ``verify_signature``, which bind a manifest to
  the environment key via ``rebar.signing``. At ~50 lines the signing pair cannot
  stand alone under the 100-line module floor, and this is its nearest concern by
  shape: both halves are environment-bound key material.

Every name here is re-exported from ``rebar._lib_writes`` and from the ``rebar``
package facade, so ``rebar.<name>`` and ``rebar._lib_writes.<name>`` keep resolving.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from rebar._errors import RebarError

if TYPE_CHECKING:
    # Schema-derived return types (story 3a10). Import-only under TYPE_CHECKING —
    # ``from __future__ import annotations`` makes every annotation a string, so
    # these names never need to exist at runtime (zero import cost, no cycle).
    from rebar.types import CreateResult, SignResult, VerifySignatureResult


# ── Identity entities and key material ───────────────────────────────────────
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


# ── Cryptographic manifest signing (environment-bound) ────────────────────────
def sign_manifest(ticket_id: str, manifest, *, repo_root=None) -> SignResult:
    """Sign a manifest of verified steps for a ticket with the environment key.

    ``manifest`` is a list of verified-step strings (or a JSON-array string).
    Mints an asymmetric operation certificate (a ``rebar.opcert.v1`` DSSE envelope carrying an
    SSHSIG signature over its PAE bytes, produced with the environment's auto-generated Ed25519
    key at ``<tracker>/.opcert-key``), persists it as a SIGNATURE event, and returns the record
    ``{ticket_id, manifest, algorithm, envelope, principal, material_fingerprint,
    merged_log_commit, signed_at}``.
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
