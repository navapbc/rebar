"""rebar MCP authentication seam (S2) — the provider-agnostic token-verifier layer.

This module is the SINGLE audience/fail-closed choke point for MCP-over-HTTP auth.
It builds a :class:`CompositeTokenVerifier` — an ordered chain of provider-specific
verifiers — and re-checks the RFC 8707 resource (audience) on every accepted token
INDEPENDENTLY of the sub-verifier, so a mis-scoped token can never slip through. Auth
is OFF by default; when enabled, ``rebar.mcp_server.build_server`` wires the composite
into FastMCP's Resource-Server support (the SDK then serves RFC 9728 PRM, emits the
401/``WWW-Authenticate`` challenge, and enforces ``required_scopes``).

S2 ships ONLY the dependency-free ``static`` bearer verifier. The factory knows the
CLOSED strategy vocabulary ({static, jwt, introspection, proxy, custom}) but the other
four are implemented by later stories — asking for one is a hard ``AuthConfigError``,
not a silent no-op, so the vocabulary stays honest.

All verifiers expose ``async def verify_token(self, token) -> AccessToken | None``:
an ``AccessToken`` accepts, ``None`` rejects. No verifier ever raises to signal
rejection — a raised exception is treated as NON-acceptance by the composite (never
swallowed-to-accept). Every error/log line is passed through :func:`redact` so no
token or secret substring is ever emitted.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from pathlib import Path

from mcp.server.auth.provider import AccessToken

logger = logging.getLogger("rebar.mcp.auth")

__all__ = [
    "AuthConfigError",
    "CompositeTokenVerifier",
    "StaticBearerVerifier",
    "build_composite_verifier",
    "redact",
]

# The CLOSED strategy vocabulary. Adding a name here (and a builder branch below) is how
# a later story lands a new verifier; an entry outside this set is a hard startup error.
_STRATEGIES: frozenset[str] = frozenset({"static", "jwt", "introspection", "proxy", "custom"})


class AuthConfigError(RuntimeError):
    """Raised for a fail-closed auth startup/config problem — an unknown or
    not-yet-implemented strategy, an enabled-but-empty composite, or a malformed
    static-token secrets record. Propagates out of ``build_server`` so a misconfigured
    authenticated server refuses to start rather than booting wide open."""


def redact(message: str, *secrets: str) -> str:
    """Return ``message`` with every non-empty ``secret`` substring replaced by
    ``"<redacted>"``. Use on EVERY auth error/log line so a token or secret can never
    leak into logs or an exception message. Longest secrets are replaced first so a
    secret that contains a shorter secret is fully masked."""
    out = message
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        out = out.replace(secret, "<redacted>")
    return out


class StaticBearerVerifier:
    """A dependency-free bearer-token verifier backed by a static secrets file.

    Loads the secrets file at init and stores ONLY SHA-256 hex digests (never the
    plaintext tokens) mapped to their ``{client_id, scopes}`` record. ``verify_token``
    hashes the presented token and compares it against each stored digest in
    constant time (:func:`hmac.compare_digest`)."""

    def __init__(self, *, tokens_file: str, resource: str) -> None:
        self._resource = resource
        # digest(hex) -> {"client_id": str, "scopes": list[str]}. Plaintext tokens are
        # NEVER retained on the instance — only their digests.
        self._by_digest: dict[str, dict] = _load_static_tokens(tokens_file)

    async def verify_token(self, token: str) -> AccessToken | None:
        digest = hashlib.sha256(token.encode()).hexdigest()
        for stored_digest, record in self._by_digest.items():
            if hmac.compare_digest(digest, stored_digest):
                return AccessToken(
                    token=token,
                    client_id=record["client_id"],
                    scopes=list(record["scopes"]),
                    resource=self._resource,
                    expires_at=None,  # static tokens are non-expiring — NEVER 0
                )
        return None


class CompositeTokenVerifier:
    """An ordered chain of verifiers that is the SINGLE audience/fail-closed choke point.

    ``verify_token`` tries each verifier IN ORDER. A verifier that raises is logged
    (redacted) at debug and treated as non-acceptance — never swallowed-to-accept. A
    result is accepted only when it is an ``AccessToken`` AND its ``resource`` equals
    this composite's resource (the independent RFC 8707 audience re-check — the SDK's
    bearer backend never checks ``.resource``, so this is where audience is enforced).
    The first accepted token short-circuits; if none is accepted, returns ``None``."""

    def __init__(self, verifiers: list, *, resource: str) -> None:
        self._verifiers = list(verifiers)
        self._resource = resource

    async def verify_token(self, token: str) -> AccessToken | None:
        for verifier in self._verifiers:
            try:
                result = await verifier.verify_token(token)
            except Exception as exc:  # noqa: BLE001 — a raising verifier is non-acceptance
                logger.debug(
                    "token verifier %s raised: %s",
                    type(verifier).__name__,
                    redact(str(exc), token),
                )
                continue
            if result is not None and result.resource == self._resource:
                return result
        return None


def build_composite_verifier(mcp_cfg) -> CompositeTokenVerifier:
    """Build the composite verifier from ``mcp_cfg.auth_strategies`` (ordered).

    Each strategy MUST be in the closed vocabulary ``_STRATEGIES``; anything else is a
    hard :class:`AuthConfigError`. In S2 only ``static`` is implemented — the other
    recognized-but-unimplemented strategies raise ``AuthConfigError`` (later stories add
    them). An empty resulting verifier list (e.g. ``auth_strategies`` empty) is also a
    hard error: an enabled-but-empty composite would fail open."""
    verifiers: list = []
    for strategy in mcp_cfg.auth_strategies:
        if strategy not in _STRATEGIES:
            raise AuthConfigError(
                f"unknown auth strategy {strategy!r}: expected one of {sorted(_STRATEGIES)}"
            )
        if strategy == "static":
            verifiers.append(
                StaticBearerVerifier(
                    tokens_file=mcp_cfg.auth_static_tokens_file,
                    resource=mcp_cfg.auth_resource_server_url,
                )
            )
        else:
            # Recognized vocabulary, but the verifier ships in a later story.
            raise AuthConfigError(f"strategy {strategy!r} is not implemented yet")

    if not verifiers:
        raise AuthConfigError(
            "auth is enabled but no usable token verifier was configured "
            "(mcp.auth_strategies is empty)"
        )
    return CompositeTokenVerifier(verifiers, resource=mcp_cfg.auth_resource_server_url)


# ── static-token secrets file ────────────────────────────────────────────────
def _load_static_tokens(tokens_file: str) -> dict[str, dict]:
    """Parse the static-token secrets file into ``{digest: {client_id, scopes}}``.

    Format: JSON ``{"tokens": [ <record>, ... ]}``. Each record has ``name`` (label),
    ``client_id``, ``scopes`` (list, may be empty), and EXACTLY ONE of ``token_sha256``
    (a 64-char hex digest) or ``token_env`` (the NAME of an env var holding the token,
    read + hashed at load). A record with both / neither, a plaintext ``token`` literal,
    a malformed digest, or a ``token_env`` naming an unset/empty var → ``AuthConfigError``.
    Only digests are returned — plaintext never leaves this function."""
    if not tokens_file:
        raise AuthConfigError("static bearer auth requires mcp.auth_static_tokens_file to be set")
    path = Path(tokens_file)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AuthConfigError(f"cannot read static tokens file {tokens_file!r}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AuthConfigError(
            f"static tokens file {tokens_file!r} is not valid JSON: {exc}"
        ) from exc

    if not isinstance(data, dict) or not isinstance(data.get("tokens"), list):
        raise AuthConfigError(
            f"static tokens file {tokens_file!r} must be a JSON object with a 'tokens' array"
        )

    by_digest: dict[str, dict] = {}
    for index, record in enumerate(data["tokens"]):
        digest, entry = _parse_static_record(record, index)
        by_digest[digest] = entry
    if not by_digest:
        raise AuthConfigError(f"static tokens file {tokens_file!r} defines no tokens")
    return by_digest


def _parse_static_record(record, index: int) -> tuple[str, dict]:
    """Validate a single static-token record → ``(digest_hex, {client_id, scopes})``.

    Raises :class:`AuthConfigError` on any structural problem. All error text is
    redaction-safe: it never echoes a token or digest value."""
    where = f"static tokens file record #{index}"
    if not isinstance(record, dict):
        raise AuthConfigError(f"{where} must be a JSON object")
    if "token" in record:
        # A plaintext token literal must never appear in the secrets file.
        raise AuthConfigError(
            f"{where} carries a plaintext 'token' field — use 'token_sha256' or 'token_env'"
        )
    client_id = record.get("client_id")
    if not isinstance(client_id, str) or not client_id:
        raise AuthConfigError(f"{where} is missing a non-empty string 'client_id'")
    scopes = record.get("scopes", [])
    if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
        raise AuthConfigError(f"{where} 'scopes' must be a list of strings")

    has_sha = "token_sha256" in record
    has_env = "token_env" in record
    if has_sha == has_env:  # both or neither
        raise AuthConfigError(f"{where} must have EXACTLY ONE of 'token_sha256' or 'token_env'")

    if has_sha:
        digest = record["token_sha256"]
        if not isinstance(digest, str) or len(digest) != 64 or not _is_hex(digest):
            raise AuthConfigError(f"{where} 'token_sha256' must be a 64-char hex SHA-256 digest")
        digest = digest.lower()
    else:
        env_name = record["token_env"]
        if not isinstance(env_name, str) or not env_name:
            raise AuthConfigError(f"{where} 'token_env' must be a non-empty env var name")
        secret = os.environ.get(env_name, "")
        if not secret:
            raise AuthConfigError(
                f"{where} 'token_env' names env var {env_name!r} which is unset or empty"
            )
        digest = hashlib.sha256(secret.encode()).hexdigest()

    return digest, {"client_id": client_id, "scopes": list(scopes)}


def _is_hex(s: str) -> bool:
    try:
        int(s, 16)
        return True
    except ValueError:
        return False
