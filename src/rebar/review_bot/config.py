"""Receiver configuration — all knobs sourced from the environment (epic d251 / S4b).

Nothing here is hardcoded into the decision logic: the verdict→label mapping
(``LLM_REVIEW_MAX_VALUE`` / ``LLM_REVIEW_BLOCK_VALUE``), the blocking-severity set,
the Gerrit endpoint + bot credential, the dedup DB path, and the reconcile cadence
are all read from environment variables. On the box those variables are populated
from SSM by ``infra/scripts/fetch-secrets.sh`` and handed to the container via the
compose ``.env`` (ADR-0008), so this module never reaches for AWS itself.

The ``LLM-Review`` value mapping (and the label range) is owned by d251's
``project.config`` (ADR-0013) and re-derived here as integers so b744-WS6 can change
the *scoring* without touching the *casting*. The blocking-severity threshold is the
one knob that maps ``review_code`` findings → PASS/BLOCK (see ``adapter.py``).

Importing this module must NOT require ``fastapi`` or the ``agents`` extra — it is
plain stdlib so ``rebar.review_bot.config`` can be read by tests and the reconciler
without standing up the ASGI app.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: Default per-review wall-clock timeout (seconds). Shared by the live worker
#: (``app._worker``) and the backfill reconciler so both bound a single review identically.
DEFAULT_REVIEW_TIMEOUT_SECONDS = 1200

#: Marker attribute stamped on the handler this module installs, so :func:`configure_logging`
#: is idempotent (a reload/re-import never stacks duplicate handlers).
_REVIEWBOT_LOG_HANDLER_MARKER = "_reviewbot_handler"


def review_timeout_seconds() -> float:
    """Per-review wall-clock timeout from ``REVIEW_TIMEOUT_SECONDS`` (default
    :data:`DEFAULT_REVIEW_TIMEOUT_SECONDS`). A missing / unparseable / non-positive value
    falls back to the default (a 0 or negative timeout would abandon every review instantly).

    Single-sourced here (not in ``app``) so ``reconcile`` can bound a backfill review with the
    SAME timeout the live worker uses without importing the fastapi-laden ``app`` module."""
    raw = os.environ.get("REVIEW_TIMEOUT_SECONDS")
    if not raw:
        return float(DEFAULT_REVIEW_TIMEOUT_SECONDS)
    try:
        val = float(raw.strip())
    except ValueError:
        return float(DEFAULT_REVIEW_TIMEOUT_SECONDS)
    return val if val > 0 else float(DEFAULT_REVIEW_TIMEOUT_SECONDS)


def configure_logging() -> None:
    """Attach a stdout handler to the ``rebar`` logger so the review-bot's structured
    ``_emit()`` INFO events (``voter_voted`` / ``voter_skip`` / ``merge_detection`` / all
    ``reconcile_*``) reach stdout → journald.

    Why this is needed: the container runs ``uvicorn rebar.review_bot.app:app``; uvicorn
    configures only its own ``uvicorn.*`` loggers and never the root or ``rebar.*`` loggers,
    so a ``logging.getLogger("rebar.review_bot.voter").info(...)`` record has no handler and
    falls through to Python's ``lastResort`` — which emits WARNING and above only. Every INFO
    ``_emit`` line was therefore silently dropped, blinding operators (only the
    ``print()``-to-stderr markers ``VOTER_ERROR`` / ``ARTIFACT_EMIT_ERROR`` survived).

    Idempotent (marker-guarded); level from ``REVIEW_BOT_LOG_LEVEL`` (default INFO, invalid →
    INFO); ``propagate=False`` so records do not double-log through uvicorn/root. Targets the
    ``rebar`` logger specifically (not root) to avoid duplicating uvicorn's access logs."""
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
    lg.propagate = False


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _severities_env(name: str, default: frozenset[str]) -> frozenset[str]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
    return frozenset(parts) if parts else default


#: The review_code severity vocabulary (mirrors ``rebar.llm.findings.SEVERITIES``).
#: Kept local so ``config`` stays free of the ``agents`` extra; the adapter compares
#: a finding's severity against ``BLOCKING_SEVERITIES`` ⊆ this set.
SEVERITIES: frozenset[str] = frozenset({"critical", "high", "medium", "low", "info"})

#: Default blocking set: a finding at critical OR high blocks the change.
DEFAULT_BLOCKING_SEVERITIES: frozenset[str] = frozenset({"critical", "high"})


@dataclass(frozen=True)
class ReceiverConfig:
    """Immutable, env-sourced receiver configuration (one snapshot per process)."""

    #: The ``LLM-Review`` value cast for a PASS (submit-enabling MAX). ADR-0013.
    llm_review_max_value: int = 1
    #: The ``LLM-Review`` value cast for a BLOCK / error (leaves change unsubmittable).
    llm_review_block_value: int = -1
    #: Findings at/above any of these severities block (PASS→BLOCK threshold).
    blocking_severities: frozenset[str] = field(default_factory=lambda: DEFAULT_BLOCKING_SEVERITIES)
    #: SQLite dedup store on the box's data volume (single-box appropriate).
    dedup_db_path: str = "/var/gerrit/site/reviewbot/voted.db"
    #: Gerrit REST base; the receiver reaches Gerrit over the compose network as the
    #: ``gerrit`` service on 8080 (NOT through public nginx). See docker-compose.yml.
    gerrit_base_url: str = "http://gerrit:8080"
    #: The bot's Gerrit account name (HTTP basic-auth user). ADR-0013 / S4a.
    bot_user: str = "rebar-review-bot"
    #: The bot's Gerrit HTTP token (basic-auth password). SSM-sourced; never defaulted.
    gerrit_bot_token: str = ""
    #: Inbound webhook shared secret (the ``?token=`` query value). Per ADR-0014 this
    #: is the SAME value as the bot token (the plugin offers no HMAC).
    webhook_token: str = ""
    #: Backfill reconciler cadence (seconds); startup + every interval.
    reconcile_interval_seconds: int = 300
    #: The Gerrit project the bot reviews; non-matching projects are skipped.
    project: str = "rebar"
    #: Remote holding the rebar ``tickets`` branch, fetched alongside the change clone so the
    #: code-review gate can materialize the ticket-store snapshot (the agent's ticket access —
    #: a requirement). The store lives on the orphan ``tickets`` branch, which is NOT on Gerrit
    #: (Gerrit carries only ``main`` + change refs) — it is on the public GitHub mirror, so we
    #: fetch it from there (public repo → no auth). See clone_change_ref.
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
        # WEBHOOK_TOKEN defaults to the bot token (ADR-0014: same secret, URL-embedded).
        webhook_token = os.environ.get("WEBHOOK_TOKEN", "").strip() or bot_token
        return cls(
            llm_review_max_value=_int_env("LLM_REVIEW_MAX_VALUE", 1),
            llm_review_block_value=_int_env("LLM_REVIEW_BLOCK_VALUE", -1),
            blocking_severities=_severities_env("BLOCKING_SEVERITIES", DEFAULT_BLOCKING_SEVERITIES),
            dedup_db_path=os.environ.get(
                "DEDUP_DB_PATH", "/var/gerrit/site/reviewbot/voted.db"
            ).strip(),
            gerrit_base_url=os.environ.get("GERRIT_BASE_URL", "http://gerrit:8080")
            .strip()
            .rstrip("/"),
            bot_user=os.environ.get("BOT_USER", "rebar-review-bot").strip(),
            gerrit_bot_token=bot_token,
            webhook_token=webhook_token,
            reconcile_interval_seconds=_int_env("RECONCILE_INTERVAL_SECONDS", 300),
            project=os.environ.get("GERRIT_PROJECT", "rebar").strip(),
            reconcile_cursor_path=os.environ.get("RECONCILE_CURSOR_PATH", "").strip(),
            tickets_remote=os.environ.get("TICKETS_REMOTE", "https://github.com/navapbc/rebar")
            .strip()
            .rstrip("/"),
        )
