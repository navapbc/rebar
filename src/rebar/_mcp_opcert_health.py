"""Serve-degraded op-cert signer health for the MCP server.

This detects the same-environment failure a derived key cannot: a bound startup
signer whose (environment, public key) pair is not in this environment's own op-cert
trust list, ``verify.trusted_opcert_keys`` (ADR 0125).
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
    """Compare a bound signer with this environment's op-cert trust list (ADR 0125).

    Returns ``{bound, expected, env_id?, trust_source?, matched?, error?}``. ``expected`` is
    true only when ``verify.trusted_opcert_keys`` is set (``trust_source`` is ``trust_list``);
    without a list the server verifies validity only, so nothing degrades. Matching uses the
    key's type and base64 body, not its comment. Composition already validates signability;
    this probe covers the trust-list match. Other resolution faults populate ``error`` instead
    of raising; removed inputs still fail hard.
    """
    from rebar._deprecations import RemovedInputError

    if binding is None:
        return {"bound": False, "expected": False}
    env_id = getattr(binding, "principal", None)
    key_path = getattr(binding, "key_path", None)
    status: dict[str, Any] = {"bound": True, "expected": False, "env_id": env_id}
    try:
        status.update(_trust_list_status(env_id, key_path, repo_root) or {})
    except RemovedInputError:
        # A removed, still-set, load-bearing input must fail hard rather than be reported as a
        # merely-degraded signer (mirrors store_status / run_startup_store_sweep).
        raise
    except Exception as exc:  # noqa: BLE001 - the probe never raises (see docstring)
        status["error"] = str(exc)
    return status


def _trust_list_status(env_id: Any, key_path: Any, repo_root: str | None) -> dict | None:
    """Judge the bound signer against ``verify.trusted_opcert_keys`` (ADR 0125).

    ``None`` when no list is configured. A set list makes the signer ``expected``, and
    ``matched`` only when its ``(env_id, key)`` pair is listed."""
    from rebar._opcert_signing import _read_opcert_pub, _ssh_pub_body
    from rebar._opcert_verify import trusted_opcert_pairs

    pairs = trusted_opcert_pairs(repo_root)
    if pairs is None:
        return None
    signer_body = _ssh_pub_body(_read_opcert_pub(key_path) if key_path else None)
    return {
        "expected": True,
        "trust_source": "trust_list",
        "matched": signer_body is not None and (env_id, signer_body) in pairs,
    }


def opcert_signer_degraded(status: dict[str, Any]) -> bool:
    """True when a bound signer is in a degraded (but still-serving) state: either the trust-list
    resolution FAULTED (an ``error`` was recorded — a genuine fault must not be silently swallowed)
    or a deployment with a trust list has a bound signer whose key is NOT listed for its
    environment. Non-blocking: a degraded signer still serves valid signatures, which this
    environment's own verify path then reports as ``untrusted`` (ADR 0125)."""
    if not status.get("bound"):
        return False
    if status.get("error"):
        return True
    return bool(status.get("expected") and not status.get("matched"))


def run_startup_opcert_check(binding: Any, repo_root: str | None = None) -> None:
    """Warn with the principal when a bound signer is not on the trust list.

    The check otherwise serves degraded and never aborts boot; removed inputs still
    fail hard, matching :func:`rebar._mcp_health.run_startup_store_sweep`.
    """
    from rebar._deprecations import RemovedInputError

    try:
        status = opcert_signing_status(binding, repo_root)
        if opcert_signer_degraded(status):
            logging.getLogger("rebar").warning(
                "startup: bound op-cert signer's public key is NOT listed for principal %s in "
                "verify.trusted_opcert_keys — its op-certs are valid signatures but this server "
                "reports them untrusted. Serving DEGRADED (boot continues). See ADR 0125.",
                status.get("env_id"),
            )
    except RemovedInputError:
        # A removed, still-set, load-bearing input must fail MCP startup hard rather than be
        # swallowed into a silent boot (mirrors run_startup_store_sweep).
        raise
    except Exception:  # a health check must never abort boot
        logging.getLogger("rebar").debug("startup op-cert check skipped", exc_info=True)
