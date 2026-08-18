"""Startup composition of the op-cert signing material (RP-04 S6, story 6f14).

Instead of a per-job SSM fetch + process-global env patch (the old ``provisioned_signing_key``),
the service composes ONE immutable :class:`OpcertSigner` ONCE at startup, from a deployment-
materialized key FILE (``REBAR_OPCERT_KEY_PATH``, preferred) or an inline PEM
(``REBAR_OPCERT_PRIVATE_KEY``, compat). :func:`compose_signer`:

* validates EXACTLY ONE source is set (missing / both-set are hard errors);
* for the FILE source, requires an existing, regular, 0600-or-stricter, readable path;
* copies the key bytes into a PROCESS-OWNED runtime dir (0700) / file (0600) — it NEVER
  modifies or deletes the deployment SOURCE;
* validates the copied key is a well-formed, passphrase-free, Ed25519 OpenSSH key
  (``ssh-keygen -y -P ""``); a malformed / encrypted / non-Ed25519 key is rejected;
* returns a frozen signer whose ``.cleanup()`` removes ONLY rebar's copy (dir + file).

The signer is threaded context-locally into the signing seam (see
:mod:`rebar._opcert_binding`), so signing never re-reads ``REBAR_OPCERT_KEY_PATH`` /
``REBAR_OPCERT_ENV_ID`` from the process environment. Deliberately stdlib-only (no boto3):
the deployment materializes the key outside the app before start.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from rebar.opcert_service.config import OpcertServiceConfig

#: The fixed basename of the process-owned key copy inside the 0700 runtime dir.
_KEY_COPY_NAME = "opcert-key"


class OpcertKeyError(Exception):
    """The startup op-cert key source is invalid — raised for every AC1 rejection case
    (missing / both-set / unreadable / non-regular / too-permissive / malformed / encrypted /
    non-Ed25519). Fails BEFORE any queue / worker / network / workspace creation."""


@dataclass(frozen=True)
class OpcertSigner:
    """An immutable startup-composed op-cert signer.

    ``key_path`` is the PROCESS-OWNED 0600 copy of the private key (never the deployment
    source); ``principal`` is the DSSE principal (SSHSIG keyid) to sign under; ``runtime_dir``
    is the process-owned 0700 dir holding the copy. :meth:`cleanup` removes ONLY rebar's copy.
    """

    key_path: str
    principal: str | None
    runtime_dir: str

    def cleanup(self) -> None:
        """Remove rebar's process-owned copy (dir + file). Never touches the source."""
        shutil.rmtree(self.runtime_dir, ignore_errors=True)


def compose_signer(cfg: OpcertServiceConfig) -> OpcertSigner:
    """Validate exactly-one-source, copy the key into a process-owned 0700 dir / 0600 file,
    and return the frozen :class:`OpcertSigner`. Raises :class:`OpcertKeyError` on any invalid
    source (see the module docstring for the full case list)."""
    key_bytes = _read_source_key(cfg)
    runtime_dir = tempfile.mkdtemp(prefix="rebar-opcert-")
    os.chmod(runtime_dir, 0o700)
    copy_path = os.path.join(runtime_dir, _KEY_COPY_NAME)
    try:
        _write_copy(copy_path, key_bytes)
        _validate_key_file(copy_path)
    except OpcertKeyError:
        shutil.rmtree(runtime_dir, ignore_errors=True)
        raise
    return OpcertSigner(key_path=copy_path, principal=cfg.env_id, runtime_dir=runtime_dir)


def _read_source_key(cfg: OpcertServiceConfig) -> bytes:
    """Enforce exactly-one-source and return the raw source key bytes."""
    key_path = (cfg.key_path or "").strip()
    private_key = cfg.private_key or ""
    if key_path and private_key.strip():
        raise OpcertKeyError(
            "exactly one op-cert key source must be set, but BOTH REBAR_OPCERT_KEY_PATH and "
            "REBAR_OPCERT_PRIVATE_KEY are set"
        )
    if not key_path and not private_key.strip():
        raise OpcertKeyError(
            "no op-cert key source is set — set exactly one of REBAR_OPCERT_KEY_PATH "
            "(preferred, a file path) or REBAR_OPCERT_PRIVATE_KEY (inline PEM)"
        )
    if key_path:
        return _read_path_source(key_path)
    return private_key.encode("utf-8")


def _read_path_source(key_path: str) -> bytes:
    """Validate the FILE source (existing, regular, 0600-or-stricter, readable) and read it."""
    try:
        st = os.stat(key_path)
    except OSError as exc:
        raise OpcertKeyError(
            f"REBAR_OPCERT_KEY_PATH={key_path!r} does not exist or is unreadable: {exc}"
        ) from None
    if not stat.S_ISREG(st.st_mode):
        raise OpcertKeyError(f"REBAR_OPCERT_KEY_PATH={key_path!r} is not a regular file")
    if st.st_mode & 0o077:
        raise OpcertKeyError(
            f"REBAR_OPCERT_KEY_PATH={key_path!r} must be 0600 or stricter "
            f"(group/other bits set: mode {oct(st.st_mode & 0o777)})"
        )
    try:
        return Path(key_path).read_bytes()
    except OSError as exc:
        raise OpcertKeyError(f"REBAR_OPCERT_KEY_PATH={key_path!r} is unreadable: {exc}") from None


def _write_copy(copy_path: str, key_bytes: bytes) -> None:
    """Write ``key_bytes`` to a fresh 0600 file (OpenSSH keys need a trailing newline)."""
    data = key_bytes if key_bytes.endswith(b"\n") else key_bytes + b"\n"
    fd = os.open(copy_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.chmod(copy_path, 0o600)


def _validate_key_file(copy_path: str) -> None:
    """Reject a malformed / encrypted / non-Ed25519 key.

    ``ssh-keygen -y -P ""`` derives the public key with an EMPTY passphrase: it exits non-zero
    for a malformed key OR a passphrase-protected key (empty passphrase is wrong), and prints
    the derived ``ssh-<type> ...`` public line on success — from which the key type is read."""
    try:
        proc = subprocess.run(
            ["ssh-keygen", "-y", "-P", "", "-f", copy_path],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise OpcertKeyError(
            f"cannot validate the op-cert key (ssh-keygen unavailable): {exc}"
        ) from None
    if proc.returncode != 0:
        raise OpcertKeyError(
            "the op-cert key is malformed, encrypted (passphrase-protected), or otherwise "
            f"unusable: {proc.stderr.strip() or 'ssh-keygen -y failed'}"
        )
    pub = proc.stdout.strip()
    if not pub.startswith("ssh-ed25519 "):
        key_type = pub.split(" ", 1)[0] if pub else "unknown"
        raise OpcertKeyError(f"the op-cert key must be an Ed25519 key, but it is {key_type!r}")
