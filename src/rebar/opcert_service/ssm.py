"""The SSM key-fetch seam for the trusted op-cert gate service (story ee0b).

The worker fetches the environment's passphrase-free Ed25519 PRIVATE key from an SSM
SecureString parameter, materializes it to a 0600 temp file for ``ssh-keygen -Y sign``, and
deletes it afterward. The network fetch is abstracted behind :class:`SsmKeyFetcher` (a small
Protocol) so tests inject a fake that returns a key value with NO network / no real AWS —
``boto3`` is imported lazily inside the real implementation, so importing this module (and the
whole ``opcert_service`` package) never requires the ``reviewbot`` extra.
"""

from __future__ import annotations

from typing import Protocol


class SsmKeyFetcher(Protocol):
    """Fetch the decrypted SecureString value at ``parameter_name`` (the raw PEM/OpenSSH key
    bytes as text). Raises on any failure — the worker maps that to an ``internal`` job error."""

    def __call__(self, parameter_name: str) -> str: ...


def boto3_ssm_fetcher(parameter_name: str) -> str:
    """The production fetcher: read the SSM SecureString with ``WithDecryption=True`` via the
    ambient AWS credentials/region. ``boto3`` is imported HERE (lazily) so the package stays
    importable without the ``reviewbot`` extra; a deploy that routes real jobs installs it."""
    import boto3  # noqa: PLC0415 — lazy: only the running service (with the extra) needs boto3

    client = boto3.client("ssm")
    resp = client.get_parameter(Name=parameter_name, WithDecryption=True)
    return resp["Parameter"]["Value"]
