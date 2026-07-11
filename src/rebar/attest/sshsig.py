"""SSHSIG signing scheme via OpenSSH ``ssh-keygen -Y`` (stdlib subprocess).

The first asymmetric scheme in the attest substrate (epic
brilliant-curly-songbird): verify-by-anyone / forge-by-none over attestation
bytes, offline, reusing developers' existing SSH keys and the OpenSSH reference
implementation — **no new core runtime dependency**.

Design (fail-closed):

* ``verify`` shells out to ``ssh-keygen -Y verify`` with an **argv array** (never
  a shell string), feeds the signed bytes on stdin, and branches **only on the
  process exit code** (0 = valid). Wrong namespace, unknown principal, a
  substituted key, tampered bytes, or an expired validity window all make
  ``ssh-keygen`` exit non-zero → a non-verified ``Verdict``.
* ``ssh-keygen`` availability (and version >= 8.9, for validity intervals) is
  detected; absence fails **closed** with a clear reason — never a silent pass.
* ``-Y check-novalidate`` (structure-only) is never used for authorization.

API STUB — bodies filled by the implementer.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from typing import cast

from rebar.attest import dsse, registry

SCHEME_NAME = "sshsig"

# OpenSSH 8.9 is the floor: it introduced valid-after/valid-before validity
# intervals in allowed_signers, which the trust model relies on.
SSHSIG_MIN_VERSION = (8, 9)

# ``ssh -V`` prints e.g. ``OpenSSH_10.2p1, LibreSSL 3.3.6`` — to stderr.
_VERSION_RE = re.compile(r"OpenSSH_(\d+)\.(\d+)")


class SshKeygenUnavailable(Exception):
    """``ssh-keygen`` is absent or too old to provide the SSHSIG trust model."""


def ssh_keygen_version() -> tuple[int, int] | None:
    """Detected ``(major, minor)`` OpenSSH version, or ``None`` if absent/unparseable."""
    try:
        proc = subprocess.run(["ssh", "-V"], capture_output=True, text=True)
    except FileNotFoundError:
        return None
    # OpenSSH prints its banner to stderr; fall back to stdout defensively.
    match = _VERSION_RE.search(proc.stderr) or _VERSION_RE.search(proc.stdout)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def ensure_available() -> None:
    """Raise :class:`SshKeygenUnavailable` if ssh-keygen is missing or < 8.9."""
    version = ssh_keygen_version()
    if version is None:
        raise SshKeygenUnavailable(
            "ssh-keygen (OpenSSH) not found; SSHSIG requires OpenSSH >= "
            f"{SSHSIG_MIN_VERSION[0]}.{SSHSIG_MIN_VERSION[1]}"
        )
    if version < SSHSIG_MIN_VERSION:
        raise SshKeygenUnavailable(
            f"OpenSSH {version[0]}.{version[1]} is too old for SSHSIG; requires >= "
            f"{SSHSIG_MIN_VERSION[0]}.{SSHSIG_MIN_VERSION[1]}"
        )


def sign(pae_bytes: bytes, key_path: str, namespace: str) -> bytes:
    """Return an armored SSHSIG signature over ``pae_bytes`` under the key at ``key_path``.

    The payload is fed on stdin (no file operand), so ``ssh-keygen`` writes the
    armored signature to stdout.
    """
    proc = subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", key_path, "-n", namespace],
        input=pae_bytes,
        capture_output=True,
        check=True,
    )
    return proc.stdout


def _has_control_chars(value: str) -> bool:
    """True if ``value`` contains an ASCII control char (incl. newline/CR/NUL)."""
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


class SshsigScheme:
    """SSHSIG scheme: ``trust_root`` is allowed_signers content; keyid is the principal."""

    name = SCHEME_NAME

    def verify(
        self,
        pae_bytes: bytes,
        signatures: list[dsse.Signature],
        namespace: str,
        trust_root: object,
    ) -> registry.Verdict:
        # Fail closed if the verifier tool is unavailable — never a silent pass.
        if ssh_keygen_version() is None:
            return registry.Verdict(
                verified=False,
                verdict="unavailable",
                reason="ssh-keygen (OpenSSH) not found; cannot verify SSHSIG",
            )
        if not signatures:
            return registry.Verdict(
                verified=False,
                verdict="mismatch",
                reason="no signatures to verify",
            )

        principal = signatures[0].keyid
        if not principal or _has_control_chars(principal):
            return registry.Verdict(
                verified=False,
                verdict="invalid",
                reason="principal (keyid) is empty or contains control characters",
            )
        if not namespace or _has_control_chars(namespace):
            return registry.Verdict(
                verified=False,
                verdict="invalid",
                reason="namespace is empty or contains control characters",
            )

        allowed_signers = cast(str, trust_root)
        with tempfile.TemporaryDirectory() as tmpdir:
            signers_path = os.path.join(tmpdir, "allowed_signers")
            sig_path = os.path.join(tmpdir, "signature")
            with open(signers_path, "w", encoding="utf-8") as fh:
                fh.write(allowed_signers)
            with open(sig_path, "wb") as fh:
                fh.write(signatures[0].sig)
            proc = subprocess.run(
                [
                    "ssh-keygen",
                    "-Y",
                    "verify",
                    "-f",
                    signers_path,
                    "-I",
                    principal,
                    "-n",
                    namespace,
                    "-s",
                    sig_path,
                ],
                input=pae_bytes,
                capture_output=True,
            )

        if proc.returncode == 0:
            return registry.Verdict(
                verified=True,
                verdict="certified",
                reason=f"ssh-keygen -Y verify accepted signature for principal {principal!r}",
            )
        return registry.Verdict(
            verified=False,
            verdict="mismatch",
            reason=f"ssh-keygen -Y verify rejected signature (exit {proc.returncode})",
        )


def register_sshsig_scheme() -> None:
    """Register :class:`SshsigScheme` into the registry (no policy kinds pinned here)."""
    registry.register_scheme(SshsigScheme())
