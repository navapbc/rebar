"""Provision the environment's op-cert signing key for a single job (story ee0b).

``ssh-keygen -Y sign`` needs a private-key FILE path, so the worker fetches the SSM-held
SecureString, materializes it to a **0600 ``mkstemp`` temp file**, points the seam override
``REBAR_OPCERT_KEY_PATH`` (+ the ``REBAR_OPCERT_ENV_ID`` principal) at it, and **deletes the file
in a ``finally``** — so the key is never persisted, even when the gate run raises. The seam then
signs exactly once with the provisioned key (see ``rebar._opcert_signing.ensure_opcert_key``).
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Iterator

from rebar.opcert_service.config import OpcertServiceConfig
from rebar.opcert_service.ssm import SsmKeyFetcher


@contextlib.contextmanager
def provisioned_signing_key(cfg: OpcertServiceConfig, fetcher: SsmKeyFetcher) -> Iterator[str]:
    """Materialize the SSM key to a 0600 temp file and export the seam env for the enclosed block.

    Yields the temp key path. The file is ``os.unlink``ed in a ``finally`` unconditionally (even on
    a raised exception), and the mutated env (``REBAR_OPCERT_KEY_PATH`` / ``REBAR_OPCERT_ENV_ID``)
    is restored on exit.
    """
    value = fetcher(cfg.ssm_key_param)
    if not value.endswith("\n"):  # OpenSSH private keys need a trailing newline
        value += "\n"
    fd, path = tempfile.mkstemp(prefix="opcert-key-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(value)
        os.chmod(path, 0o600)  # explicit — mkstemp is already 0600, but make the contract loud
        env = {"REBAR_OPCERT_KEY_PATH": path}
        if cfg.env_id:
            env["REBAR_OPCERT_ENV_ID"] = cfg.env_id
        with _patched_env(env):
            yield path
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)  # never persist the key — removed even on a raised exception


@contextlib.contextmanager
def _patched_env(values: dict[str, str]) -> Iterator[None]:
    prior = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for k, old in prior.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old
