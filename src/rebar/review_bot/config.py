"""Environment-backed receiver settings and logging helpers.
SSM supplies deployment variables before startup. Verdict thresholds come
from the four-pass gate, while this module supplies Gerrit label values,
timeouts, endpoint settings, and token redaction without importing FastAPI
or the agents extra."""

from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

#: Default per-review wall-clock timeout (seconds). Shared by the live worker
#: (``app._worker``) and the backfill reconciler so both bound a single review identically.
DEFAULT_REVIEW_TIMEOUT_SECONDS = 1200

#: Drain acknowledged reviews for up to the review timeout before cancel.
#: An empty queue returns promptly. Backfill recovers work left queued.
#: Keep this setting readable without importing FastAPI and below the
#: container stop deadline.
DEFAULT_SHUTDOWN_DRAIN_SECONDS = 1200

#: Bound post-drain task cancellation and await. Offloaded OS work may
#: continue after coroutine cancellation, so hard exit has its own grace.
#: Expose this budget without importing the app.
DEFAULT_SHUTDOWN_CANCEL_SECONDS = 10

#: After this many attempts at one change revision, a retryable
#: coverage gap escalates from vote-less deferral to a fail-closed vote.
#: A successful vote or explicit reset clears the attempt ledger.
DEFAULT_RETRYABLE_GAP_MAX_ATTEMPTS = 3

#: Limit how long a retryable candidate can hold back the backfill
#: cursor. The default 24 hours bounds a permanent poison pill.
DEFAULT_RECONCILE_MAX_HOLDBACK_SECONDS = 86400

#: Marker attribute stamped on the handler this module installs, so :func:`configure_logging`
#: is idempotent (a reload/re-import never stacks duplicate handlers).
_REVIEWBOT_LOG_HANDLER_MARKER = "_reviewbot_handler"


def review_timeout_seconds() -> float:
    """Return a positive REVIEW_TIMEOUT_SECONDS or the shared default.
    Both worker and backfill use this setting without importing the app."""
    raw = os.environ.get("REVIEW_TIMEOUT_SECONDS")
    if not raw:
        return float(DEFAULT_REVIEW_TIMEOUT_SECONDS)
    try:
        val = float(raw.strip())
    except ValueError:
        return float(DEFAULT_REVIEW_TIMEOUT_SECONDS)
    return val if val > 0 else float(DEFAULT_REVIEW_TIMEOUT_SECONDS)


def shutdown_drain_seconds() -> float:
    """Bounded queue-drain window on shutdown from ``SHUTDOWN_DRAIN_SECONDS`` (default
    :data:`DEFAULT_SHUTDOWN_DRAIN_SECONDS`); a missing / unparseable / non-positive value falls
    back to the default."""
    raw = os.environ.get("SHUTDOWN_DRAIN_SECONDS")
    if not raw:
        return float(DEFAULT_SHUTDOWN_DRAIN_SECONDS)
    try:
        val = float(raw.strip())
    except ValueError:
        return float(DEFAULT_SHUTDOWN_DRAIN_SECONDS)
    return val if val > 0 else float(DEFAULT_SHUTDOWN_DRAIN_SECONDS)


def shutdown_cancel_seconds() -> float:
    """Bounded lifespan cancel/await window on shutdown from ``SHUTDOWN_CANCEL_SECONDS``
    (default :data:`DEFAULT_SHUTDOWN_CANCEL_SECONDS`); a missing / unparseable / non-positive
    value falls back to the default (a 0 or negative bound would abandon every task instantly,
    losing well-behaved tasks' prompt cancellation)."""
    raw = os.environ.get("SHUTDOWN_CANCEL_SECONDS")
    if not raw:
        return float(DEFAULT_SHUTDOWN_CANCEL_SECONDS)
    try:
        val = float(raw.strip())
    except ValueError:
        return float(DEFAULT_SHUTDOWN_CANCEL_SECONDS)
    return val if val > 0 else float(DEFAULT_SHUTDOWN_CANCEL_SECONDS)


def shutdown_force_exit_grace_seconds() -> float:
    """Return the lock-acquisition plus git-call budget after bounded cancel.
    A blocked to_thread worker may survive coroutine cancellation. The
    hard-exit grace gives a store write time to release its lock and still
    precedes the container stop deadline."""
    from rebar._store import lock as _lock
    from rebar._store.event_commit_git import _GIT_TIMEOUT

    return float(_lock._DEFAULT_TIMEOUT * _lock._DEFAULT_ATTEMPTS + _GIT_TIMEOUT)


def configure_logging() -> None:
    """Add one stdout handler to the rebar logger for structured INFO events.
    Use REVIEW_BOT_LOG_LEVEL with INFO fallback. Leave propagation unchanged
    so root handlers such as pytest caplog still receive records."""
    level_name = os.environ.get("REVIEW_BOT_LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    if not isinstance(level, int):  # e.g. an attr that exists but is not a level constant
        level = logging.INFO
    lg = logging.getLogger("rebar")
    if not any(getattr(h, _REVIEWBOT_LOG_HANDLER_MARKER, False) for h in lg.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        setattr(handler, _REVIEWBOT_LOG_HANDLER_MARKER, True)
        lg.addHandler(handler)
    lg.setLevel(level)


#: Marker attribute stamped on the ``uvicorn.access`` logger once the redaction filter is
#: installed, so :func:`install_access_log_redaction` is idempotent under a reload/re-import.
_ACCESS_REDACTION_MARKER = "_reviewbot_access_redaction"

#: Scrub token query values before access-log formatting while
#: preserving routes and other query fields. Headers avoid request-line tokens.
_TOKEN_QUERY_RE = re.compile(r"(?i)(token=)[^&\s\"']*")


def _redact_token(value: object) -> object:
    """Redact ``token=`` values inside a string; pass non-strings through untouched."""
    if isinstance(value, str):
        return _TOKEN_QUERY_RE.sub(r"\1<redacted>", value)
    return value


class TokenRedactingFilter(logging.Filter):
    """Replace query-token values in a log record's string args and message
    before formatting. Always pass the redacted record onward."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple):
            record.args = tuple(_redact_token(a) for a in args)
        elif isinstance(args, dict):
            record.args = {k: _redact_token(v) for k, v in args.items()}
        if isinstance(record.msg, str):
            record.msg = _TOKEN_QUERY_RE.sub(r"\1<redacted>", record.msg)
        return True


def install_access_log_redaction() -> None:
    """Install the token filter once on uvicorn.access.
    Header authentication avoids request-line logging. The deployed Gerrit
    webhook uses query-token authentication, so this filter protects it."""
    access = logging.getLogger("uvicorn.access")
    if getattr(access, _ACCESS_REDACTION_MARKER, False):
        return
    if not any(isinstance(f, TokenRedactingFilter) for f in access.filters):
        access.addFilter(TokenRedactingFilter())
    setattr(access, _ACCESS_REDACTION_MARKER, True)


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


@dataclass(frozen=True)
class ReceiverConfig:
    """Immutable, env-sourced receiver configuration (one snapshot per process)."""

    #: The ``LLM-Review`` value cast for a PASS (submit-enabling MAX). ADR-0013.
    llm_review_max_value: int = 1
    #: The ``LLM-Review`` value cast for a BLOCK / error (leaves change unsubmittable).
    llm_review_block_value: int = -1
    #: SQLite dedup store on the box's data volume (single-box appropriate).
    dedup_db_path: str = "/var/gerrit/site/reviewbot/voted.db"
    #: Gerrit REST base; the receiver reaches Gerrit over the compose network as the
    #: ``gerrit`` service on 8080 (NOT through public nginx). See docker-compose.yml.
    gerrit_base_url: str = "http://gerrit:8080"
    #: Canonical public Gerrit URL emitted by the webhooks plugin in ``X-Origin-Url``.
    gerrit_canonical_web_url: str = "https://rebar.solutions.navateam.com"
    #: The bot's Gerrit account name (HTTP basic-auth user). ADR-0013 / S4a.
    bot_user: str = "rebar-review-bot"
    #: The bot's Gerrit HTTP token (basic-auth password). SSM-sourced; never defaulted.
    gerrit_bot_token: str = ""
    #: Public inbound shared secret (``X-Rebar-Token`` preferred, legacy ``?token=`` accepted).
    #: Per ADR-0014 this is the SAME value as the bot token; Gerrit's internal webhook path
    #: authenticates by network + built-in origin header instead.
    webhook_token: str = ""
    #: Backfill reconciler cadence (seconds); startup + every interval.
    reconcile_interval_seconds: int = 300
    #: Attempts before a retryable coverage gap escalates to the fail-closed ``-1``.
    retryable_gap_max_attempts: int = DEFAULT_RETRYABLE_GAP_MAX_ATTEMPTS
    #: Hold the reconciler cursor behind retryable candidates, but no
    #: more than 24 hours behind the newest event. An expired holdback emits
    #: RECONCILE_DEGRADED so the stuck change stays visible.
    reconcile_max_holdback_seconds: int = DEFAULT_RECONCILE_MAX_HOLDBACK_SECONDS
    #: The Gerrit project the bot reviews; non-matching projects are skipped.
    project: str = "rebar"
    #: Fetch the tickets branch from the GitHub mirror beside the
    #: Gerrit change clone. Gerrit does not host this orphan branch.
    tickets_remote: str = "https://github.com/navapbc/rebar"
    #: Persisted reconciler cursor (last-processed events-log event time). Empty →
    #: derive it next to the dedup DB (``<dedup dir>/reconcile_cursor``). Survives a
    #: restart so the poller resumes from its tail rather than rescanning the whole log.
    reconcile_cursor_path: str = ""

    @property
    def cursor_path(self) -> str:
        """The resolved reconcile-cursor file path. Defaults to ``reconcile_cursor``
        beside the dedup DB so it lives on the same persistent data volume."""
        if self.reconcile_cursor_path:
            return self.reconcile_cursor_path
        return str(Path(self.dedup_db_path).parent / "reconcile_cursor")

    @classmethod
    def from_env(cls) -> ReceiverConfig:
        """Build the config from the process environment (the only source)."""
        bot_token = os.environ.get("GERRIT_BOT_TOKEN", "").strip()
        # WEBHOOK_TOKEN defaults to the bot token (ADR-0014: same public receiver secret).
        webhook_token = os.environ.get("WEBHOOK_TOKEN", "").strip() or bot_token
        return cls(
            llm_review_max_value=_int_env("LLM_REVIEW_MAX_VALUE", 1),
            llm_review_block_value=_int_env("LLM_REVIEW_BLOCK_VALUE", -1),
            dedup_db_path=os.environ.get(
                "DEDUP_DB_PATH", "/var/gerrit/site/reviewbot/voted.db"
            ).strip(),
            gerrit_base_url=os.environ.get("GERRIT_BASE_URL", "http://gerrit:8080")
            .strip()
            .rstrip("/"),
            gerrit_canonical_web_url=(
                os.environ.get("GERRIT_CANONICAL_WEB_URL")
                or os.environ.get("CANONICAL_WEB_URL")
                or "https://rebar.solutions.navateam.com"
            )
            .strip()
            .rstrip("/"),
            bot_user=os.environ.get("BOT_USER", "rebar-review-bot").strip(),
            gerrit_bot_token=bot_token,
            webhook_token=webhook_token,
            reconcile_interval_seconds=_int_env("RECONCILE_INTERVAL_SECONDS", 300),
            retryable_gap_max_attempts=_int_env(
                "RETRYABLE_GAP_MAX_ATTEMPTS", DEFAULT_RETRYABLE_GAP_MAX_ATTEMPTS
            ),
            reconcile_max_holdback_seconds=_int_env(
                "RECONCILE_MAX_HOLDBACK_SECONDS", DEFAULT_RECONCILE_MAX_HOLDBACK_SECONDS
            ),
            project=os.environ.get("GERRIT_PROJECT", "rebar").strip(),
            reconcile_cursor_path=os.environ.get("RECONCILE_CURSOR_PATH", "").strip(),
            tickets_remote=os.environ.get("TICKETS_REMOTE", "https://github.com/navapbc/rebar")
            .strip()
            .rstrip("/"),
        )
