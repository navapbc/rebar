"""Serve-degraded op-cert signer health for the MCP server.

This detects the same-environment failure a derived key cannot: a bound startup
signer's public key differs from its principal's pinned trusted-environment key.
Health and startup-warning surfaces report the mismatch without aborting service
because environment binding remains advisory (ADR 0104 decision 3).

The size-capped :mod:`rebar._mcp_health` module wires this module's status constant,
:func:`opcert_signing_status`, and :func:`run_startup_opcert_check`.
"""

from __future__ import annotations

import logging
from typing import Any

_OPCERT_STATUS_ATTR = "_rebar_opcert_status"


def opcert_signing_status(binding: Any, repo_root: str | None = None) -> dict[str, Any]:
    """Compare a bound signer's public-key body with its principal's active pins.

    Returns ``{bound, expected, env_id?, matched?, error?}``. ``expected`` is true
    only when trusted-environment configuration opts into pinning, so its absence
    never degrades service. Composition already validates signability; this probe
    covers the non-redundant pinned-key match. Other resolution faults populate
    ``error`` instead of raising; removed inputs still fail hard.
    """
    from rebar._deprecations import RemovedInputError
    from rebar._opcert_signing import _read_opcert_pub, _ssh_pub_body
    from rebar.attest.trusted_env import load_trusted_environments, trusted_env_keyring

    if binding is None:
        return {"bound": False, "expected": False}
    env_id = getattr(binding, "principal", None)
    key_path = getattr(binding, "key_path", None)
    status: dict[str, Any] = {"bound": True, "expected": False, "env_id": env_id}
    try:
        status["expected"] = load_trusted_environments(repo_root) is not None
        if not status["expected"]:
            return status
        signer_body = _ssh_pub_body(_read_opcert_pub(key_path) if key_path else None)
        keyring = trusted_env_keyring(env_id, repo_root) if env_id else None
        pinned = {
            b
            for b in (
                _ssh_pub_body(k.get("public_key"))
                for k in (keyring or [])
                if k.get("revoked_at_log_position") is None
            )
            if b
        }
        # Match on the key's type+base64 BODY, not the full .pub line: ssh's allowed_signers verify
        # (the path required-environment binding uses) ignores the trailing comment field, so a key
        # differing only in its comment must NOT read as a mismatch (Gerrit 2360 review).
        status["matched"] = signer_body is not None and signer_body in pinned
    except RemovedInputError:
        # A removed, still-set, load-bearing input must fail hard rather than be reported as a
        # merely-degraded signer (mirrors store_status / run_startup_store_sweep).
        raise
    except Exception as exc:  # noqa: BLE001 - the probe never raises (see docstring)
        status["error"] = str(exc)
    return status


def opcert_signer_degraded(status: dict[str, Any]) -> bool:
    """True when a bound signer is in a degraded (but still-serving) state: either the pinned-key
    resolution FAULTED (an ``error`` was recorded — a genuine fault must not be silently swallowed)
    or a deployment that opted into pinning has a bound signer whose public key is NOT the pinned
    trusted-environment key. Non-blocking: a degraded signer still serves — those are valid
    signatures that only fail the advisory environment-binding check (ADR 0104 dec. 3)."""
    if not status.get("bound"):
        return False
    if status.get("error"):
        return True
    return bool(status.get("expected") and not status.get("matched"))


def run_startup_opcert_check(binding: Any, repo_root: str | None = None) -> None:
    """Warn with the principal when a bound signer misses its pinned key.

    The check otherwise serves degraded and never aborts boot; removed inputs still
    fail hard, matching :func:`rebar._mcp_health.run_startup_store_sweep`.
    """
    from rebar._deprecations import RemovedInputError

    try:
        status = opcert_signing_status(binding, repo_root)
        if opcert_signer_degraded(status):
            logging.getLogger("rebar").warning(
                "startup: bound op-cert signer's public key is NOT the pinned trusted-environment "
                "key for principal %s — its op-certs are valid signatures but FAIL a required-"
                "environment (pinned-key) verify. Serving DEGRADED (this binding check is advisory "
                "today; boot continues). See bug 879b-9bf0-86fd-4a6b.",
                status.get("env_id"),
            )
    except RemovedInputError:
        # A removed, still-set, load-bearing input must fail MCP startup hard rather than be
        # swallowed into a silent boot (mirrors run_startup_store_sweep).
        raise
    except Exception:  # a health check must never abort boot
        logging.getLogger("rebar").debug("startup op-cert check skipped", exc_info=True)
