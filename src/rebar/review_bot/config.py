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

import os
from dataclasses import dataclass, field


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
        )
