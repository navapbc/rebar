"""Process-wide configuration for the trusted op-cert gate service (story ee0b).

Env-sourced (the deploy story materializes the SSM-held secrets into these vars at boot),
mirroring ``rebar.review_bot.config.ReceiverConfig``. Deliberately stdlib-only: importing this
module never pulls FastAPI/boto3 (the importability contract — see ``opcert_service/app.py``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: Per-run wall-clock ceiling for a single gate job. Generous by design: gate runs take
#: 30s-minutes, so the default is 900s (15 min). Override via ``REBAR_OPCERT_JOB_TIMEOUT_SECONDS``.
DEFAULT_JOB_TIMEOUT_SECONDS = 900

#: Bounded window (seconds) for the lifespan's shutdown cancel + await of the background worker
#: tasks (``REBAR_OPCERT_SHUTDOWN_CANCEL_SECONDS``). A well-behaved task cancels promptly, but one
#: slow to honor cancellation — a shielded region, a synchronous ``finally`` — would otherwise make
#: the join unbounded. Anything still pending when the window closes is ABANDONED (the process is
#: exiting anyway). This bounds the TASK join only; the in-flight job's OS thread is bounded
#: separately by the app-owned executor (see ``app._offload``), because a thread cannot be
#: force-cancelled. Small by design: the drain already happened, this is only the cancel tail.
DEFAULT_SHUTDOWN_CANCEL_SECONDS = 5

#: The SSM SecureString holding the environment's passphrase-free Ed25519 op-cert PRIVATE key.
DEFAULT_SSM_KEY_PARAM = "/rebar/prod/opcert-ed25519-key"

#: The code branch fetched from the review remote (its tip is the ``merged_log_commit``).
DEFAULT_REVIEW_BRANCH = "main"

DEFAULT_PORT = 8080


@dataclass
class OpcertServiceConfig:
    """Resolved service configuration (see :meth:`from_env`)."""

    review_remote_url: str | None = None
    tickets_remote_url: str | None = None
    review_branch: str = DEFAULT_REVIEW_BRANCH
    guard: str | None = None
    env_id: str | None = None
    ssm_key_param: str = DEFAULT_SSM_KEY_PARAM
    job_timeout_seconds: float = float(DEFAULT_JOB_TIMEOUT_SECONDS)
    shutdown_cancel_seconds: float = float(DEFAULT_SHUTDOWN_CANCEL_SECONDS)
    port: int = DEFAULT_PORT

    @classmethod
    def from_env(cls) -> OpcertServiceConfig:
        """Build the config from the ``REBAR_OPCERT_*`` environment (deploy-injected)."""
        return cls(
            review_remote_url=_str_env("REBAR_OPCERT_REVIEW_REMOTE_URL"),
            tickets_remote_url=_str_env("REBAR_OPCERT_TICKETS_REMOTE_URL"),
            review_branch=_str_env("REBAR_OPCERT_REVIEW_BRANCH") or DEFAULT_REVIEW_BRANCH,
            guard=_str_env("REBAR_OPCERT_GUARD"),
            env_id=_str_env("REBAR_OPCERT_ENV_ID"),
            ssm_key_param=_str_env("REBAR_OPCERT_SSM_KEY_PARAM") or DEFAULT_SSM_KEY_PARAM,
            job_timeout_seconds=_timeout_env(),
            shutdown_cancel_seconds=_shutdown_cancel_env(),
            port=_port_env(),
        )


def _str_env(name: str) -> str | None:
    raw = os.environ.get(name)
    if raw and raw.strip():
        return raw.strip()
    return None


def _timeout_env() -> float:
    """``REBAR_OPCERT_JOB_TIMEOUT_SECONDS`` (default ``DEFAULT_JOB_TIMEOUT_SECONDS``); a
    missing / unparseable / non-positive value falls back to the default."""
    raw = os.environ.get("REBAR_OPCERT_JOB_TIMEOUT_SECONDS")
    if not raw:
        return float(DEFAULT_JOB_TIMEOUT_SECONDS)
    try:
        val = float(raw.strip())
    except ValueError:
        return float(DEFAULT_JOB_TIMEOUT_SECONDS)
    return val if val > 0 else float(DEFAULT_JOB_TIMEOUT_SECONDS)


def _shutdown_cancel_env() -> float:
    """``REBAR_OPCERT_SHUTDOWN_CANCEL_SECONDS`` (default
    :data:`DEFAULT_SHUTDOWN_CANCEL_SECONDS`); a missing / unparseable / non-positive value falls
    back to the default (a 0 or negative bound would abandon every task instantly, losing
    well-behaved tasks' prompt cancellation)."""
    raw = os.environ.get("REBAR_OPCERT_SHUTDOWN_CANCEL_SECONDS")
    if not raw:
        return float(DEFAULT_SHUTDOWN_CANCEL_SECONDS)
    try:
        val = float(raw.strip())
    except ValueError:
        return float(DEFAULT_SHUTDOWN_CANCEL_SECONDS)
    return val if val > 0 else float(DEFAULT_SHUTDOWN_CANCEL_SECONDS)


def _port_env() -> int:
    raw = os.environ.get("REBAR_OPCERT_PORT")
    if not raw:
        return DEFAULT_PORT
    try:
        return int(raw.strip())
    except ValueError:
        return DEFAULT_PORT
