"""Op-cert producer-signing machinery for the ``signing.sign_manifest`` seam (story 8d8e).

The gate producers mint ``rebar.opcert.v1`` DSSE op-certs with the ambient environment's
auto-generated Ed25519 key (``<tracker>/.opcert-key``) instead of the legacy per-clone HMAC secret,
so a local run and a trusted-server run produce the SAME artifact — one signature per verdict, no
double-signing. This module owns the environment-key custody (race-safe genesis + principal
resolution), the mint path, and the same-environment verify path; ``rebar.signing`` keeps the
public seam (``sign_manifest`` / ``verify_attestation_record``) as thin delegators over it.

It is split out of ``signing.py`` purely to keep both units under the module-size soft cap
(docs/architecture.md); it deliberately does NOT import ``rebar.signing`` at module scope (the two
pure manifest helpers it needs are imported lazily inside the verify path) so there is no cycle.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from rebar import config
from rebar._store.fsutil import atomic_write

if TYPE_CHECKING:
    from rebar._opcert_binding import OpcertBinding

logger = logging.getLogger(__name__)

# The environment's op-cert key: a passphrase-free Ed25519 keypair auto-generated on first signing
# use at ``<tracker>/.opcert-key`` (+``.pub``), git-ignored, mirroring the HMAC ``.signing-key``
# genesis it replaces. The producers sign with it; anyone verifies against the env's public key.
OPCERT_KEY_FILE = ".opcert-key"

# The DSSE subject ``kind`` bound into a raw ``rebar sign`` op-cert that carries no kind-prefixed
# manifest[0]. Such a cert is UNGATED (the merge-gate only verifies plan-review / completion), so
# the label is never checked on the read path — the shape-aware wrapper selects the scheme by the
# fixed ``OPCERT_KIND`` policy, not the subject kind.
OPCERT_GENERIC_KIND = "attestation"


class OpcertKeyUnavailable(Exception):
    """The op-cert signature cannot be produced AND cannot be recovered — a missing/too-old
    ``ssh-keygen`` (OpenSSH < 8.9) or an unwritable tracker dir. This is the DEGRADE signal: the
    seam converts it to a :class:`rebar.signing.SigningError` so a signing call site records the
    in-band ``{signed: false}`` outcome (no local op is wedged) and a gate that REQUIRES the
    signature blocks with an OpenSSH ≥ 8.9 remediation."""

    def __init__(self, message: str, returncode: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.returncode = returncode


# ── environment key custody (asymmetric Ed25519) ──────────────────────────────
def opcert_key_path(tracker: str | os.PathLike[str]) -> str:
    """The environment's op-cert PRIVATE key path (``<tracker>/.opcert-key``)."""
    return str(Path(tracker) / OPCERT_KEY_FILE)


def _derive_opcert_pub(key_path: str) -> str | None:
    """(Re)derive the public line for ``key_path`` via ``ssh-keygen -y`` and RETURN it.

    The public key is DERIVATIVE — never a commit point — so it is safe to (re)write any time it
    is missing. Both the derivation and the ``<key_path>.pub`` cache write are best-effort (the
    private key is the authority). The RETURNED TEXT, not the cache file, is what callers rely on:
    a deployment-materialized key lives on a READ-ONLY secrets mount where the cache can never be
    written, and verification must still find the public half there."""
    pub_path = key_path + ".pub"
    try:
        proc = subprocess.run(["ssh-keygen", "-y", "-f", key_path], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        logger.debug(
            "could not derive op-cert public key %s (best-effort)", pub_path, exc_info=True
        )
        return None
    try:
        # A UNIQUE same-dir temp (mkstemp) + os.replace — derivative artifact, so replace is
        # fine (not a commit point). The temp name must NOT be derived from the target or the
        # pid: `<pub>.<pid>.tmp` is unique across PROCESSES but SHARED by every thread of one
        # process, and the MCP server is threaded — so the first replace consumes the temp and
        # the second thread's write is silently lost in the best-effort `except` below
        # (ticket b0ac-3c0f-3f64-4344). atomic_write unlinks its own temp on failure.
        atomic_write(pub_path, proc.stdout, mode="wb")
    except OSError:
        logger.debug("could not write op-cert public key %s (best-effort)", pub_path, exc_info=True)
    return proc.stdout.decode("utf-8", "replace").strip() or None


def _ensure_opcert_pub(key_path: str) -> str:
    """Ensure ``<key_path>.pub`` exists (re-derive it from the private key if absent); return it."""
    pub_path = key_path + ".pub"
    if not os.path.exists(pub_path):
        _derive_opcert_pub(key_path)
    return pub_path


def _generate_opcert_key(key_path: str) -> None:
    """Race-safe genesis of the environment's Ed25519 op-cert key at ``key_path``.

    ``ssh-keygen -f <path>`` writes BOTH ``<path>`` and ``<path>.pub`` (it cannot write to an fd),
    so we generate into a private 0700 ``mkdtemp`` staging dir and then ``os.link`` the PRIVATE key
    into place as the SINGLE exclusive-create commit point: ``os.link`` fails with ``EEXIST`` if a
    concurrent first-signer already committed a key, and the loser ADOPTS the winner's key by
    re-reading the existing file (never ``os.replace``, which is not exclusive and would clobber a
    concurrently-committed key). The ``.pub`` is DERIVATIVE — written from the committed private key
    via ``ssh-keygen -y`` — never a commit point. The staging dir is removed in ``finally``."""
    import shutil
    import tempfile

    from rebar.attest import sshsig

    # SshKeygenUnavailable → degrade path (mint converts it to OpcertKeyUnavailable).
    sshsig.ensure_available()
    try:
        staging = tempfile.mkdtemp(prefix=".opcert-key.", dir=str(Path(key_path).parent))
    except OSError as exc:
        raise OpcertKeyUnavailable(
            f"Error: could not create op-cert key staging dir (tracker unwritable?): {exc}"
        ) from None
    try:
        staging_priv = os.path.join(staging, "key")
        proc = subprocess.run(
            [
                "ssh-keygen",
                "-t",
                "ed25519",
                "-N",
                "",
                "-f",
                staging_priv,
                "-q",
                "-C",
                "rebar-opcert",
            ],
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0 or not os.path.exists(staging_priv):
            raise OpcertKeyUnavailable(
                "Error: ssh-keygen failed to generate the op-cert key: "
                f"{proc.stderr.decode('utf-8', 'replace').strip()}"
            )
        os.chmod(staging_priv, 0o600)
        try:
            os.link(staging_priv, key_path)  # SINGLE exclusive-create commit point
        except FileExistsError:
            pass  # a concurrent first-signer won; adopt its key (re-read on return — no overwrite)
        except OSError as exc:
            raise OpcertKeyUnavailable(
                f"Error: could not commit op-cert key at {key_path}: {exc}"
            ) from None
        _ensure_opcert_pub(key_path)  # derivative — re-derivable from the committed private key
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def ensure_opcert_key(
    tracker: str | os.PathLike[str],
    *,
    create_if_missing: bool = True,
    binding: OpcertBinding | None = None,
) -> str:
    """Resolve the environment's op-cert PRIVATE key path, generating it race-safely on first use.

    With ``create_if_missing=True`` (signing) a missing key is generated via
    :func:`_generate_opcert_key`; a deleted/unreadable key is thereby regenerated on the next sign
    (a fresh keypair under the same principal — the same stale-attestation lifecycle as any other).
    With ``create_if_missing=False`` (VERIFY side) a missing key is NEVER created — verification is
    read-only and must not write a secret to disk. Raises :class:`OpcertKeyUnavailable` when the key
    is absent and cannot be created (verify side, or an unwritable tracker).

    STARTUP BINDING (story 6f14): when a bound op-cert signer is active — passed explicitly as
    ``binding`` or threaded context-locally (:func:`rebar._opcert_binding.current_binding`) — its
    process-owned key copy is used verbatim, WITHOUT reading ``REBAR_OPCERT_KEY_PATH`` from the
    process environment. A bound-but-missing key file is a hard :class:`OpcertKeyUnavailable`.

    When NOT bound the resolution is UNCHANGED: the legacy ``REBAR_OPCERT_KEY_PATH`` env override
    takes precedence over the ``<tracker>/.opcert-key`` genesis and is returned verbatim; a
    configured-but-missing override is a hard :class:`OpcertKeyUnavailable`."""
    effective = binding if binding is not None else _current_binding()
    if effective is not None:
        bound_path = effective.key_path
        if not bound_path or not os.path.exists(bound_path):
            raise OpcertKeyUnavailable(
                f"Error: the bound op-cert key {bound_path!r} does not exist "
                "(the composed signer's key copy is absent)"
            )
        return bound_path
    override = os.environ.get("REBAR_OPCERT_KEY_PATH")  # read-via: credential-deployment-override
    if override and override.strip():
        override = override.strip()
        if not os.path.exists(override):
            raise OpcertKeyUnavailable(
                f"Error: REBAR_OPCERT_KEY_PATH={override!r} does not exist "
                "(the provisioned op-cert key file is absent)"
            )
        return override
    key_path = opcert_key_path(tracker)
    if os.path.exists(key_path):
        _ensure_opcert_pub(key_path)
        return key_path
    if not create_if_missing:
        raise OpcertKeyUnavailable(
            f"Error: op-cert key {key_path} is absent (the verify side never creates a key)"
        )
    _generate_opcert_key(key_path)
    return key_path


def opcert_principal(
    tracker: str | os.PathLike[str], *, binding: OpcertBinding | None = None
) -> str:
    """The DSSE principal (SSHSIG keyid) op-certs are signed under.

    STARTUP BINDING (story 6f14): when a bound signer is active (explicit ``binding`` or the
    context-local one) and carries a non-empty principal, it is used WITHOUT reading
    ``REBAR_OPCERT_ENV_ID`` from the process environment. When NOT bound (or the binding has no
    principal) the resolution is UNCHANGED: ``REBAR_OPCERT_ENV_ID`` when set (deployment
    override), else the store's ``.env-id`` (via ``_seam.env_id``)."""
    effective = binding if binding is not None else _current_binding()
    if effective is not None and effective.principal and effective.principal.strip():
        return effective.principal.strip()
    override = os.environ.get("REBAR_OPCERT_ENV_ID")  # read-via: identity-deployment-override
    if override and override.strip():
        return override.strip()
    from rebar._commands._seam import env_id as _env_id

    return _env_id(Path(tracker))


def _current_binding() -> OpcertBinding | None:
    """The context-local op-cert signer binding, or ``None`` (the unbound env/genesis path)."""
    from rebar._opcert_binding import current_binding

    return current_binding()


def _opcert_own_key_paths(tracker: str | os.PathLike[str]) -> list[str]:
    """Every private-key path THIS environment could have signed under, in resolution order.

    Mirrors :func:`ensure_opcert_key`'s WRITE-side chain — the bound startup signer's
    process-owned copy, then the ``REBAR_OPCERT_KEY_PATH`` deployment override, then the
    ``<tracker>/.opcert-key`` genesis — so the verify side looks for the public half where the
    signing key actually IS. A deployment that materializes the key outside the tracker (the
    on-box MCP server) otherwise signs certs no reader on the same box can certify. Read-only:
    resolves paths, never creates a key."""
    paths: list[str] = []
    binding = _current_binding()
    if binding is not None and binding.key_path:
        paths.append(binding.key_path)
    override = os.environ.get("REBAR_OPCERT_KEY_PATH")  # read-via: credential-deployment-override
    if override and override.strip():
        paths.append(override.strip())
    paths.append(opcert_key_path(tracker))
    return list(dict.fromkeys(paths))


def _read_opcert_pub(key_path: str) -> str | None:
    """The public-key line for ``key_path`` — the cached ``.pub`` if readable, else derived from
    the private key (:func:`_derive_opcert_pub`, which caches best-effort). None when neither."""
    try:
        text = Path(key_path + ".pub").read_text(encoding="utf-8").strip()
        if text:
            return text
    except OSError:
        pass
    if not os.path.exists(key_path):
        return None
    return _derive_opcert_pub(key_path)


def _ssh_pub_body(line: str | None) -> str | None:
    """The MATCHING identity of an SSH public key: ``"<type> <base64>"`` with the optional
    trailing comment field dropped. Mirrors how ssh's ``allowed_signers`` verification compares
    keys (on type + base64 body, ignoring the comment), so a key that differs ONLY in its comment
    is not mistaken for a different key. ``None`` for a blank or malformed line."""
    if not line:
        return None
    parts = line.split()
    if len(parts) < 2:
        return None
    return f"{parts[0]} {parts[1]}"


def _opcert_own_public_keys(tracker: str | os.PathLike[str]) -> list[str]:
    """The public-key lines (``ssh-ed25519 AAAA…``) for every key this environment holds — the
    trust root for SAME-ENVIRONMENT certification. Empty when it holds none, which is the honest
    "cannot certify anything here" signal. Read-only: never CREATES a private key."""
    pubs = [_read_opcert_pub(kp) for kp in _opcert_own_key_paths(tracker)]
    return list(dict.fromkeys([p for p in pubs if p]))


def _warn_if_verify_cannot_resolve(
    tracker: str | os.PathLike[str], key_path: str, principal: str
) -> None:
    """Bug 879b guard: after minting under ``key_path``, WARN when the same-environment verify
    resolver (:func:`_opcert_own_public_keys`) cannot resolve a public counterpart for that key.

    That divergence is exactly the class 2337 fixed: signing SUCCEEDS but ``verify_signature`` on
    this same box returns ``foreign_key`` and the claim gate then refuses every ticket — a silent,
    deferred failure with no guard surfacing it at the point of minting. This helper is purely
    ADDITIVE: it logs, it never refuses, and it never changes the minted record. It stays silent
    on the healthy path (post-2337 the signing key's public half IS in the resolvable set), firing
    only on the real regression divergence."""
    try:
        signed_body = _ssh_pub_body(_read_opcert_pub(key_path))
        own = {b for b in (_ssh_pub_body(p) for p in _opcert_own_public_keys(tracker)) if b}
        if signed_body is not None and signed_body in own:
            return
        logger.warning(
            "op-cert signed under a key whose public counterpart the same-environment verify path "
            "cannot resolve (key_path=%s, principal=%s): the signature is valid but "
            "verify_signature on this environment will not certify it (foreign_key). See bug "
            "879b-9bf0-86fd-4a6b.",
            key_path,
            principal,
        )
    except Exception:
        logger.debug(
            "op-cert public-counterpart guard skipped (best-effort; key_path=%s)",
            key_path,
            exc_info=True,
        )


def _pinned_public_key_bodies(principal: str, repo_root) -> set[str] | None:
    """The MATCHING bodies (``"<type> <base64>"``) of every ACTIVE key pinned for ``principal``
    in ``.rebar/trusted_environments.yaml``, or ``None`` when ``principal`` is not pinned at all.

    The ``None``-vs-``set()`` split is load-bearing, and so is the refusal below. Three states
    must stay distinguishable, because two of them were previously collapsed into one:

    * **not pinned** (no keyring for this env id) -> ``None``. The caller no-ops; this is every
      ordinary developer box and the fail-open case ``load_trusted_environments`` returns
      ``None`` for when the config is simply ABSENT.
    * **pinned, with active keys** -> that set of bodies.
    * **pinned, but every key REVOKED** -> an EMPTY set, which is NOT "not pinned": the caller
      refuses. ``trusted_env_keyring`` returns every record regardless of
      ``revoked_at_log_position``, and the verify path filters by key era
      (``key_not_valid_at_era``) while this sign-time guard has no storage anchor to filter
      against. But a revoked key is revoked as of a PAST log position and a signature made now
      is necessarily later, so it can never yield a cert that verifies at its own anchor.

    UNREADABLE IS NOT UNPINNED. ``load_trusted_environments`` raises
    :class:`~rebar.attest.trusted_env.TrustedEnvError` for a present-but-malformed or unreadable
    config, and swallowing that into "nothing pinned" would silently DISABLE this guard — the
    very failure mode of bug ff4a-2832-def4-4e55 (an absence of enforcement reported as success)
    reproduced inside its own fix. When the trust root cannot be read, whether this principal is
    pinned is UNKNOWN, and unknown must not read as fine: raise, so the seam records
    ``{signed: false}`` in band and an operator sees it. An ABSENT config still fails open, so
    a checkout with no pin file is unaffected.

    Imported lazily (and from ``rebar.attest.trusted_env``, never ``rebar._opcert_verify``) to
    avoid an import cycle with this module's end-of-file re-exports."""
    from rebar.attest import trusted_env

    try:
        keyring = trusted_env.trusted_env_keyring(principal, repo_root)
    except Exception as exc:  # noqa: BLE001 — an undeterminable trust root must never read as "unpinned"
        raise OpcertKeyUnavailable(
            "Error: cannot mint op-cert signature: the trusted-environments pin config could "
            f"not be read, so whether {principal!r} is a pinned environment is unknown "
            f"({exc}). Refusing rather than signing an unverifiable certificate. "
            "See bug ff4a-2832-def4-4e55."
        ) from None
    if not keyring:
        return None
    return {
        b
        for b in (
            _ssh_pub_body(k.get("public_key"))
            for k in keyring
            if k.get("revoked_at_log_position") is None
        )
        if b
    }


def _refuse_if_pinned_principal_key_mismatch(key_path: str, principal: str, repo_root) -> None:
    """Bug ff4a guard: REFUSE to mint a cert that CLAIMS a pinned environment while being signed
    under a key that environment does not pin.

    That divergence is the ff4a class: an async gate daemon that lost its context-local signer
    binding resolved ``principal`` from the process-global ``REBAR_OPCERT_ENV_ID`` (shared by
    threads) but its KEY from the ``<tracker>/.opcert-key`` genesis path, which silently
    auto-generates a fresh keypair. The cert then claims production and fails
    ``ssh-keygen -Y verify`` against that environment's pinned public key — a silent,
    hours-later close-gate refusal. :func:`_warn_if_verify_cannot_resolve` cannot catch it: it
    checks the signing process's SELF-consistency (trivially true in the unbound daemon), never
    principal-to-key consistency.

    Raising is the fail-closed answer and reaches the caller IN BAND — the signing seam converts
    :class:`OpcertKeyUnavailable` into a ``rebar.signing.SigningError`` so the call site records
    ``{signed: false}`` without wedging the local op.

    NO-OP when the principal is not pinned (every ordinary developer box, whose env-id appears in
    no ``trusted_environments.yaml``), so the developer-local genesis path is unchanged. Also a
    no-op when the signing key's own public half cannot be read at all — that diagnosis belongs
    to the existing paths. A principal that IS pinned but whose keys are all revoked yields an
    empty set, not ``None``, and so refuses (see :func:`_pinned_public_key_bodies`)."""
    pinned = _pinned_public_key_bodies(principal, repo_root)
    if pinned is None:  # not a pinned environment — the developer-local path, unchanged
        return
    signed_body = _ssh_pub_body(_read_opcert_pub(key_path))
    if signed_body is None or signed_body in pinned:
        return
    raise OpcertKeyUnavailable(
        "Error: cannot mint op-cert signature: the certificate would claim the pinned "
        f"environment {principal!r} but is signed under a key that environment does not pin "
        f"(key_path={key_path}). Such a certificate cannot verify against that environment's "
        "pinned public key. See bug ff4a-2832-def4-4e55."
    )


def _manifest_material_fingerprint(manifest) -> str | None:
    """Extract the bound ``material: <fingerprint>`` value from a manifest (the material both the
    plan-review and completion manifests carry), or None when absent."""
    for step in manifest or []:
        if isinstance(step, str) and step.startswith("material:"):
            return step.split(":", 1)[1].strip()
    return None


# ── mint (write-new) ──────────────────────────────────────────────────────────
def mint_opcert_record(
    resolved: str,
    steps: list[str],
    *,
    kind: str | None,
    repo_root,
    binding: OpcertBinding | None = None,
) -> dict:
    """Build the envelope-bearing SIGNATURE record for ``resolved`` (NOT persisted here).

    Mints a ``rebar.opcert.v1`` DSSE op-cert with the environment's Ed25519 key, deriving the values
    the caller does not supply: the DSSE ``principal`` (the bound signer's principal, else
    ``REBAR_OPCERT_ENV_ID``, else ``.env-id``); the material fingerprint from the manifest's
    ``material:`` line; the bound commit from the manifest's ``verified-at-sha`` line, else
    current ``HEAD``. When a startup signer is bound (``binding`` or context-local), the key +
    principal come from that binding instead of the process env (story 6f14). Raises
    :class:`OpcertKeyUnavailable` on the degrade path (missing/too-old ssh-keygen, or an
    unwritable tracker)."""
    from rebar.attest import dsse, opcert, sshsig
    from rebar.reducer._processors import attestation_kind
    from rebar.signing import head_sha, verified_at_sha_from_manifest

    tracker = config.tracker_dir(repo_root)
    try:
        sshsig.ensure_available()
    except sshsig.SshKeygenUnavailable as exc:
        raise OpcertKeyUnavailable(f"Error: cannot mint op-cert signature: {exc}") from None
    key_path = ensure_opcert_key(str(tracker), create_if_missing=True, binding=binding)

    principal = opcert_principal(str(tracker), binding=binding)
    _warn_if_verify_cannot_resolve(tracker, key_path, principal)
    _refuse_if_pinned_principal_key_mismatch(key_path, principal, repo_root)
    material_fingerprint = _manifest_material_fingerprint(steps) or ""
    # Bound commit: the manifest's signed `verified-at-sha:` when present (an attested review or
    # close), else current HEAD.
    merged_log_commit = verified_at_sha_from_manifest(steps) or head_sha(
        config.repo_root(repo_root)
    )
    # Subject kind: manifest[0] is authoritative for gated kinds (plan-review / completion); fall
    # back to the caller hint, then a generic label for a raw `rebar sign` (ungated).
    subject_kind = (
        attestation_kind(steps, {"kind": kind} if kind else {}) or kind or OPCERT_GENERIC_KIND
    )

    env = opcert.sign_opcert(
        resolved,
        material_fingerprint,
        merged_log_commit,
        kind=subject_kind,
        key_path=key_path,
        principal=principal,
        # Bind the full manifest into the SIGNED payload so the plan-review freshness checks
        # (stale-code via manifest_deps, stale-regver via manifest_regver) read authenticated
        # inputs, not the attacker-writable plaintext record manifest.
        manifest=steps,
    )
    envelope = dsse.encode(
        env.payload_type,
        env.payload,
        [{"keyid": s.keyid, "sig": s.sig} for s in env.signatures],
    )
    record = {
        "manifest": steps,
        "algorithm": "sshsig",
        "envelope": envelope,
        "material_fingerprint": material_fingerprint,
        "merged_log_commit": merged_log_commit,
        "principal": principal,
        # Retained so the plan-review claim gate's UNSCOPED whole-HEAD freshness check
        # (compute_validity) works identically to the legacy HMAC record.
        "head_sha": head_sha(config.repo_root(repo_root)),
    }
    if kind is not None:
        record["kind"] = kind
    return record


def sign_opcert_manifest(
    ticket_id: str,
    manifest,
    *,
    material_fingerprint: str,
    merged_log_commit: str,
    key_path: str,
    principal: str,
    repo_root=None,
) -> dict:
    """Sign a manifest as an ASYMMETRIC op-cert (keystone e4df); append an envelope-bearing
    SIGNATURE event.

    Builds a DSSE envelope via :func:`rebar.attest.opcert.sign_opcert` binding
    ``{ticket_id, material_fingerprint, merged_log_commit}``, then appends a SIGNATURE event whose
    record carries the encoded ``envelope`` + those bound fields + ``algorithm="sshsig"`` and the
    signed ``manifest`` (first line ``"<kind>: …"`` so the reducer derives the attestation kind) —
    but NO HMAC ``signature``. The kind-keyed ``attestations`` map then holds an op-cert record the
    merge-gate (4214) verifies. Re-exported as ``rebar.signing.sign_opcert_manifest``."""
    import time

    from rebar._commands._seam import (
        CommandError,
        append_event,
        require_id,
        require_not_ghost,
    )
    from rebar.attest import opcert
    from rebar.attest.dsse import encode
    from rebar.reducer._processors import attestation_kind
    from rebar.signing import SigningError, parse_manifest

    if not ticket_id:
        raise SigningError("Error: ticket_id must be non-empty")
    steps = parse_manifest(manifest)

    tracker = config.tracker_dir(repo_root)
    try:
        resolved = require_id(ticket_id, tracker)
        require_not_ghost(resolved, tracker)
    except CommandError as exc:
        raise SigningError(exc.message, exc.returncode) from None

    # The attestation kind (from the manifest) is bound INTO the signed op-cert subject, so a cert
    # cannot be filed under / accepted for a different kind than it was signed for (kind-confusion).
    kind = attestation_kind(steps, {})
    if kind is None:
        raise SigningError("Error: op-cert manifest[0] must encode a kind (e.g. 'plan-review: …')")
    env = opcert.sign_opcert(
        resolved,
        material_fingerprint,
        merged_log_commit,
        kind=kind,
        key_path=key_path,
        principal=principal,
        # Bind the full manifest into the SIGNED payload (see mint_opcert_record) so downstream
        # freshness checks read authenticated dep-hashes / regver, not the plaintext mirror.
        manifest=steps,
    )
    envelope = encode(
        env.payload_type,
        env.payload,
        [{"keyid": s.keyid, "sig": s.sig} for s in env.signatures],
    )
    record = {
        "manifest": steps,
        "algorithm": "sshsig",
        "envelope": envelope,
        "material_fingerprint": material_fingerprint,
        "merged_log_commit": merged_log_commit,
        "signed_at": time.time_ns(),
        # Unsigned routing hint mirroring the manifest-authoritative kind the reducer derives
        # (the kind is ALSO bound into the signed envelope subject above).
        "kind": kind,
    }
    try:
        append_event(resolved, "SIGNATURE", record, tracker, repo_root=repo_root)
    except CommandError as exc:
        raise SigningError(exc.message, exc.returncode) from None
    return {**record, "ticket_id": resolved}


# ── verify path (carved into _opcert_verify for the module-size cap) ──────────
# Re-exported so ``rebar._opcert_signing.verify_opcert_record`` stays a stable import site.
# Placed at end-of-file (after the custody helpers above are defined) so the
# _opcert_verify -> _opcert_signing import resolves without a cycle.
from rebar._opcert_verify import verify_opcert_record as verify_opcert_record  # noqa: E402
