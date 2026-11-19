"""Mint environment-bound Ed25519 DSSE op-certs for ticket gate verdicts.

Own race-safe key custody, principal resolution, and same-environment key lookup.
``rebar.signing`` exposes the public seam; lazy imports avoid a module cycle.
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

# First signing generates this git-ignored Ed25519 keypair; readers use its
# public half without creating another key.
OPCERT_KEY_FILE = ".opcert-key"

# The DSSE subject ``kind`` bound into a raw ``rebar sign`` op-cert that carries no kind-prefixed
# manifest[0]. Such a cert is UNGATED (the merge-gate only verifies plan-review / completion), so
# the label is never checked on the read path — the shape-aware wrapper selects the scheme by the
# fixed ``OPCERT_KIND`` policy, not the subject kind.
OPCERT_GENERIC_KIND = "attestation"


class OpcertKeyUnavailable(Exception):
    """Signal unavailable signing keys or OpenSSH without wedging local operations.

    The public seam converts this to ``SigningError`` and records ``signed: false``.
    """

    def __init__(self, message: str, returncode: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.returncode = returncode


# ── environment key custody (asymmetric Ed25519) ──────────────────────────────
def opcert_key_path(tracker: str | os.PathLike[str]) -> str:
    """The environment's op-cert PRIVATE key path (``<tracker>/.opcert-key``)."""
    return str(Path(tracker) / OPCERT_KEY_FILE)


def _derive_opcert_pub(key_path: str) -> str | None:
    """Derive and return the public key; cache its ``.pub`` file best-effort.

    Return the text even when a read-only secrets mount blocks cache writes.
    """
    pub_path = key_path + ".pub"
    try:
        proc = subprocess.run(["ssh-keygen", "-y", "-f", key_path], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        logger.debug(
            "could not derive op-cert public key %s (best-effort)", pub_path, exc_info=True
        )
        return None
    try:
        # Use a unique temp per thread: a pid-named temp collides within MCP.
        # The derivative cache may be replaced and failure cleans up its temp.
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
    """Generate a staged Ed25519 key, then publish it with exclusive ``os.link``.

    A racing signer adopts the winner's private key. The public cache is
    re-derivable, and staging is always removed.
    """
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
    """Resolve the signing key from binding, env override, or tracker genesis.

    A bound signer takes precedence and never consults the process override.
    Verification with ``create_if_missing=False`` never creates a key. Missing
    bound or configured paths raise ``OpcertKeyUnavailable``.
    """
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
    """Resolve principal from binding, env override, then store ``.env-id``."""
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
    """Extract SSH key type and base64 body, ignoring optional comments."""
    if not line:
        return None
    parts = line.split()
    if len(parts) < 2:
        return None
    return f"{parts[0]} {parts[1]}"


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
    """Build an unpersisted DSSE SIGNATURE record from environment key and manifest.

    Binding overrides process key/principal values. Manifest supplies material and
    verified SHA, falling back to current HEAD; unavailable signing raises.
    """
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
    material_fingerprint = _manifest_material_fingerprint(steps) or ""
    # Bound commit: the manifest's signed `verified-at-sha:` when present (an attested review or
    # close), else current HEAD. HEAD is resolved once: nothing below moves it.
    current_head = head_sha(config.repo_root(repo_root))
    merged_log_commit = verified_at_sha_from_manifest(steps) or current_head
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
        # (compute_validity) works identically for every record shape.
        "head_sha": current_head,
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
    """Append a DSSE op-cert SIGNATURE binding ticket, material, commit, and kind.

    The signed manifest supplies the reducer's kind; no HMAC signature is stored.
    ``rebar.signing`` re-exports this entry point.
    """
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
