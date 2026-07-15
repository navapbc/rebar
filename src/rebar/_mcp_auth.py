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

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlparse

from mcp.server.auth.provider import AccessToken

logger = logging.getLogger("rebar.mcp.auth")

__all__ = [
    "AuthConfigError",
    "CompositeTokenVerifier",
    "IntrospectionError",
    "IntrospectionTokenVerifier",
    "JWKSTokenVerifier",
    "StaticBearerVerifier",
    "build_composite_verifier",
    "redact",
]

# Symmetric (HMAC) JWS algorithms — forbidden on a JWKS source. Configuring one
# alongside a JWKS URI is the RS256↔HS256 key-confusion attack surface (an attacker
# signs HS256 using the published RSA public key as the HMAC secret), so we refuse
# start. Closed, explicitly-enumerated family; case-insensitive match.
_SYMMETRIC_ALGS: frozenset[str] = frozenset({"HS256", "HS384", "HS512"})

# The CLOSED strategy vocabulary. Adding a name here (and a builder branch below) is how
# a later story lands a new verifier; an entry outside this set is a hard startup error.
_STRATEGIES: frozenset[str] = frozenset({"static", "jwt", "introspection", "proxy", "custom"})


class AuthConfigError(RuntimeError):
    """Raised for a fail-closed auth startup/config problem — an unknown or
    not-yet-implemented strategy, an enabled-but-empty composite, or a malformed
    static-token secrets record. Propagates out of ``build_server`` so a misconfigured
    authenticated server refuses to start rather than booting wide open."""


class IntrospectionError(RuntimeError):
    """Raised by :class:`IntrospectionTokenVerifier` for a fail-closed transport/parse
    failure — a non-200 status, a timeout/connection error, or a malformed body (not
    valid JSON, or JSON lacking a boolean ``active`` field). The composite treats a
    raised exception as non-acceptance → deny, so introspection never yields acceptance
    on a failure. Message text is passed through :func:`redact` so no token/secret leaks."""


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


class JWKSTokenVerifier:
    """A provider-generic OIDC/JWT verifier backed by a JWKS endpoint (S3).

    Validates a JWT's signature against keys fetched from ``jwks_uri`` (via PyJWT's
    ``PyJWKClient``) under a PINNED, asymmetric-only algorithm allowlist, plus the
    ``iss``/``aud``/``exp``/``nbf`` claims and (optionally) the header ``typ``. It never
    derives the algorithm from the token header (RFC 8725), so ``alg:none`` and
    RS256↔HS256 confusion are structurally impossible.

    Construction is fail-closed: a symmetric algorithm alongside a JWKS source, a
    non-HTTPS URI, or a private/link-local/loopback literal-IP host (unless
    ``allow_private_jwks_host``) each raise :class:`AuthConfigError`.

    The blocking JWKS HTTP fetch is offloaded via :func:`asyncio.to_thread` so it never
    blocks the event loop, and an unknown-``kid`` refetch cooldown — a check-and-update of
    the last-refetch timestamp made atomic under an ``asyncio.Lock`` — bounds a
    random-``kid`` flood to at most one fetch per cooldown window even under concurrency."""

    def __init__(
        self,
        *,
        jwks_uri: str,
        issuer: str,
        resource: str,
        algorithms,
        leeway: int,
        refetch_cooldown: int,
        timeout: int,
        expected_typ: str,
        allow_private_jwks_host: bool,
    ) -> None:
        import jwt  # lazy: pyjwt ships in the optional [mcp] extra

        self._algorithms = tuple(algorithms)
        # Algorithm pinning: refuse any symmetric family member on a JWKS source.
        for alg in self._algorithms:
            if alg.upper() in _SYMMETRIC_ALGS:
                raise AuthConfigError(
                    f"symmetric algorithm {alg!r} is not allowed on a JWKS source "
                    f"(asymmetric-only: RS*/PS*/ES*/EdDSA) — RS256/HS256 confusion guard"
                )
        if not self._algorithms:
            raise AuthConfigError("mcp.auth_jwt_algorithms must list at least one algorithm")

        # SSRF guards on the JWKS URI (construction-time; DNS is NOT resolved here).
        parsed = urlparse(jwks_uri)
        if parsed.scheme != "https":
            raise AuthConfigError(
                f"mcp.auth_jwt_jwks_uri must be an https:// URL (got scheme {parsed.scheme!r})"
            )
        host = parsed.hostname or ""
        if not allow_private_jwks_host:
            try:
                ip = ipaddress.ip_address(host)
            except ValueError:
                ip = None  # a DNS name (not a literal IP) is allowed; not resolved here
            if ip is not None and (ip.is_loopback or ip.is_private or ip.is_link_local):
                raise AuthConfigError(
                    f"mcp.auth_jwt_jwks_uri host {host!r} is private/link-local/loopback; "
                    f"set mcp.auth_jwt_allow_private_jwks_host=true to permit it"
                )

        self._jwks_uri = jwks_uri
        self._issuer = issuer
        self._resource = resource
        self._leeway = leeway
        self._refetch_cooldown = refetch_cooldown
        self._expected_typ = expected_typ
        # Pass timeout as a KEYWORD so PyJWKClient's fetch is bounded (a test asserts this).
        self._client = jwt.PyJWKClient(jwks_uri, timeout=timeout)

        # Concurrency-safe unknown-kid refetch cooldown state.
        self._lock = asyncio.Lock()
        self._known_kids: set[str] = set()
        self._last_refetch: float | None = None

    async def verify_token(self, token: str) -> AccessToken | None:
        import jwt

        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as exc:
            logger.debug("jwks verifier: bad header: %s", redact(str(exc), token))
            return None

        # Cross-JWT-confusion typ check (RFC 8725 §3.11), only when configured.
        if self._expected_typ and header.get("typ") != self._expected_typ:
            return None

        kid = header.get("kid") or ""
        signing_key = await self._resolve_signing_key(token, kid)
        if signing_key is None:
            return None

        try:
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=list(self._algorithms),
                audience=self._resource,
                issuer=self._issuer,
                leeway=self._leeway,
                options={"require": ["exp"]},
            )
        except jwt.InvalidTokenError as exc:
            logger.debug("jwks verifier: invalid token: %s", redact(str(exc), token))
            return None

        scopes = _split_scopes(claims)
        return AccessToken(
            token=token,
            client_id=claims.get("sub", ""),
            scopes=scopes,
            resource=self._resource,
            expires_at=int(claims["exp"]),
        )

    async def _resolve_signing_key(self, token: str, kid: str):
        """Resolve the JWKS signing key, cooldown-gating fetches for unknown kids.

        Returns the signing key, or ``None`` when the kid is unknown-and-cooldown-gated
        or the JWKS fetch fails (a fetch failure surfaces as deny to the composite). The
        check-and-update of the last-refetch timestamp is atomic under a lock so a burst
        of distinct unknown kids triggers at most one fetch per cooldown window."""
        import jwt

        async with self._lock:
            if kid not in self._known_kids:
                now = time.monotonic()
                if (
                    self._last_refetch is not None
                    and self._refetch_cooldown > 0
                    and now - self._last_refetch < self._refetch_cooldown
                ):
                    # Cooldown window still open for a previously-attempted fetch: deny
                    # without fetching (flood guard).
                    return None
                # Timestamp on every ATTEMPT (success OR failure) so a failing endpoint
                # cannot be used to bypass the flood guard.
                self._last_refetch = now
                try:
                    signing_key = await asyncio.to_thread(
                        self._client.get_signing_key_from_jwt, token
                    )
                except (jwt.InvalidTokenError, jwt.exceptions.PyJWKClientError, OSError) as exc:
                    logger.debug("jwks verifier: key fetch failed: %s", redact(str(exc), token))
                    return None
                self._known_kids.add(kid)
                return signing_key

        # Known kid: no cooldown gate. The client caches keys, so this does not refetch.
        try:
            return await asyncio.to_thread(self._client.get_signing_key_from_jwt, token)
        except (jwt.InvalidTokenError, jwt.exceptions.PyJWKClientError, OSError) as exc:
            logger.debug("jwks verifier: key resolve failed: %s", redact(str(exc), token))
            return None


class IntrospectionTokenVerifier:
    """An RFC 7662 OAuth 2.0 token-introspection verifier (S4).

    On every ``verify_token`` it POSTs the opaque token to a configured Authorization
    Server ``/introspect`` endpoint, authenticating with ``client_secret_basic`` (HTTP
    Basic ``client_id:secret``, the secret sourced at construction from the env var NAMED
    by ``client_secret_env`` — never from config). It accepts only ``active: true`` with a
    matching RFC 8707 audience, and maps the response into an ``AccessToken``.

    Fail-closed by construction: the client secret env var must be present + non-empty, the
    endpoint must be ``https``, and a private/link-local/loopback literal-IP host is refused
    unless ``allow_private_host``. Fail-closed at request time: a non-200 status, a transport
    error (timeout/connection), a non-JSON body, or a body lacking a boolean ``active`` field
    each **raise** :class:`IntrospectionError` (the composite treats that as deny). There is
    NO caching in v1 — every call re-introspects, giving instant revocation."""

    def __init__(
        self,
        *,
        endpoint: str,
        client_id: str,
        client_secret_env: str,
        resource: str,
        allow_private_host: bool = False,
        allow_missing_aud: bool = False,
        connect_timeout: float = 5.0,
        read_timeout: float = 10.0,
        transport=None,
    ) -> None:
        import httpx  # ships in the base deps

        # The client secret is read from the env var NAMED by client_secret_env (fail-closed
        # if unset/empty) and kept only in memory — never logged; route logging through redact.
        secret = os.environ.get(client_secret_env) or ""
        if not secret:
            raise AuthConfigError(
                f"mcp.auth_introspection_client_secret_env names env var "
                f"{client_secret_env!r} which is unset or empty"
            )

        # SSRF guards (construction-time; DNS is NOT resolved here).
        parsed = urlparse(endpoint)
        if parsed.scheme != "https":
            raise AuthConfigError(
                f"mcp.auth_introspection_endpoint must be an https:// URL "
                f"(got scheme {parsed.scheme!r})"
            )
        host = parsed.hostname or ""
        if not allow_private_host:
            try:
                ip = ipaddress.ip_address(host)
            except ValueError:
                ip = None  # a DNS name (not a literal IP) is allowed; not resolved here
            if ip is not None and (ip.is_loopback or ip.is_private or ip.is_link_local):
                raise AuthConfigError(
                    f"mcp.auth_introspection_endpoint host {host!r} is "
                    f"private/link-local/loopback; set "
                    f"mcp.auth_introspection_allow_private_host=true to permit it"
                )

        self._endpoint = endpoint
        self._client_id = client_id
        self._secret = secret
        self._resource = resource
        self._allow_missing_aud = allow_missing_aud
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(
                connect=connect_timeout,
                read=read_timeout,
                write=read_timeout,
                pool=read_timeout,
            ),
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        import httpx

        try:
            resp = await self._client.post(
                self._endpoint,
                data={"token": token},
                auth=(self._client_id, self._secret),
            )
        except httpx.HTTPError as exc:
            raise IntrospectionError(
                f"introspection request failed: {redact(str(exc), token, self._secret)}"
            ) from exc

        if resp.status_code != 200:
            raise IntrospectionError(f"introspection endpoint returned status {resp.status_code}")
        try:
            body = resp.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise IntrospectionError(
                f"introspection response is not valid JSON: {redact(str(exc), token, self._secret)}"
            ) from exc
        if not isinstance(body, dict) or not isinstance(body.get("active"), bool):
            raise IntrospectionError("introspection response lacks a boolean 'active' field")

        if body["active"] is not True:
            return None

        # Audience (RFC 7662 §2.2 — `aud` may be a string OR a list of strings).
        if "aud" not in body:
            if not self._allow_missing_aud:
                return None
        else:
            aud = body["aud"]
            if isinstance(aud, str):
                if self._resource != aud:
                    return None
            elif isinstance(aud, (list, tuple)):
                if self._resource not in aud:
                    return None
            else:
                return None

        client_id = body.get("client_id") or body.get("sub") or "unknown"
        scope = body.get("scope", "")
        scopes = scope.split() if isinstance(scope, str) else []
        expires_at = int(body["exp"]) if "exp" in body else None  # NEVER 0 when absent
        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=scopes,
            resource=self._resource,
            subject=body.get("sub"),
            expires_at=expires_at,
        )


def _split_scopes(claims: dict) -> list[str]:
    """Map an OAuth token's scopes to a list: space-delimited ``scope`` plus a list
    (or space-delimited string) ``scp`` claim, de-duplicated preserving order."""
    scopes: list[str] = []
    raw_scope = claims.get("scope", "")
    if isinstance(raw_scope, str):
        scopes.extend(raw_scope.split())
    scp = claims.get("scp", [])
    if isinstance(scp, str):
        scopes.extend(scp.split())
    elif isinstance(scp, (list, tuple)):
        scopes.extend(str(s) for s in scp)
    seen: set[str] = set()
    out: list[str] = []
    for s in scopes:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


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
        elif strategy == "jwt":
            verifiers.append(
                JWKSTokenVerifier(
                    jwks_uri=mcp_cfg.auth_jwt_jwks_uri,
                    issuer=mcp_cfg.auth_jwt_issuer or mcp_cfg.auth_issuer_url,
                    resource=mcp_cfg.auth_resource_server_url,
                    algorithms=mcp_cfg.auth_jwt_algorithms,
                    leeway=mcp_cfg.auth_jwt_leeway,
                    refetch_cooldown=mcp_cfg.auth_jwt_jwks_refetch_cooldown,
                    timeout=mcp_cfg.auth_jwt_jwks_timeout,
                    expected_typ=mcp_cfg.auth_jwt_expected_typ,
                    allow_private_jwks_host=mcp_cfg.auth_jwt_allow_private_jwks_host,
                )
            )
        elif strategy == "introspection":
            verifiers.append(
                IntrospectionTokenVerifier(
                    endpoint=mcp_cfg.auth_introspection_endpoint,
                    client_id=mcp_cfg.auth_introspection_client_id,
                    client_secret_env=mcp_cfg.auth_introspection_client_secret_env,
                    resource=mcp_cfg.auth_resource_server_url,
                    allow_private_host=mcp_cfg.auth_introspection_allow_private_host,
                    allow_missing_aud=mcp_cfg.auth_introspection_allow_missing_aud,
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
