"""Freshness check for the VENDORED security rule subset (epic b744 / WS5; ADR 0012).

The High/Critical security rules + the gitleaks secret families are vendored + pinned (not a live
registry pull) for reproducible/offline scanning, so they must not silently rot. This module is
the CI **freshness check**: it reads the pin manifest (``builtin/security_rules_pin.json``) and
WARNS — never fails — when the recorded ``vendored_at`` date is older than ``cadence_days``
(quarterly by default).

Time-based + network-free BY DESIGN: it compares the recorded refresh date against today, so it
needs no upstream registry access (an upstream-version diff is the documented follow-on). Warn-only
because a HARD fail on a time cadence would block every unrelated PR the moment the cadence lapses;
the warning prompts a deliberate ``make vendor-security-rules`` refresh PR (which re-pins the
families and bumps ``vendored_at``).

Run in CI as ``python -m rebar.grounding.detectors.security_pin`` (emits a GitHub Actions
``::warning::`` annotation when stale; always exits 0).
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path
from typing import Any

#: The pinned cadence is quarterly unless the manifest overrides it.
DEFAULT_CADENCE_DAYS = 90


def _pin_path() -> Path:
    return Path(__file__).parent / "builtin" / "security_rules_pin.json"


def load_pin(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Load the freshness pin manifest. Raises ``FileNotFoundError`` if it is missing (the pin is
    a committed, required artifact — its absence is a real error, not a soft pass)."""
    p = Path(path) if path is not None else _pin_path()
    return json.loads(p.read_text(encoding="utf-8"))


def _parse_date(value: str) -> _dt.date:
    return _dt.date.fromisoformat(value)


def freshness(today: _dt.date, *, pin: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compute the freshness of the vendored rules as of ``today``.

    Returns ``{vendored_at, cadence_days, age_days, stale, families}``. ``stale`` is True when the
    pin is older than ``cadence_days``. A malformed/missing ``vendored_at`` is treated as STALE
    (fail-toward-refresh: an unparseable pin should prompt a refresh, not silently pass)."""
    pin = pin if pin is not None else load_pin()
    cadence = int(pin.get("cadence_days") or DEFAULT_CADENCE_DAYS)
    raw = pin.get("vendored_at")
    try:
        vendored_at = _parse_date(str(raw))
        age_days = (today - vendored_at).days
        stale = age_days > cadence
        vendored_repr: str | None = vendored_at.isoformat()
    except (ValueError, TypeError):
        age_days = None
        stale = True
        vendored_repr = None
    return {
        "vendored_at": vendored_repr,
        "cadence_days": cadence,
        "age_days": age_days,
        "stale": stale,
        "families": list(pin.get("families") or []),
    }


def format_warning(status: dict[str, Any]) -> str | None:
    """Render a one-line GitHub Actions ``::warning::`` annotation when stale, else None."""
    if not status.get("stale"):
        return None
    if status.get("vendored_at") is None:
        detail = "security_rules_pin.json has a missing/unparseable `vendored_at`"
    else:
        detail = (
            f"vendored security rules are {status['age_days']}d old "
            f"(> {status['cadence_days']}d cadence; pinned {status['vendored_at']})"
        )
    return (
        f"::warning title=Vendored security rules stale::{detail}. "
        "Refresh with `make vendor-security-rules` and bump `vendored_at` in "
        "src/rebar/grounding/detectors/builtin/security_rules_pin.json (ADR 0012)."
    )


def main() -> int:
    """CI entry point: print a warning annotation when the pin is stale; ALWAYS exit 0 (warn-only,
    per the WS5 AC). Returns the exit code (0)."""
    status = freshness(_dt.date.today())
    warning = format_warning(status)
    # T201: this is a CI gate — stdout (the GitHub Actions `::warning::` annotation / OK line) IS
    # its operational output, a legitimate-print surface like the other rebar CLI gates.
    if warning:
        print(warning)  # noqa: T201
    else:
        print(  # noqa: T201
            f"Security-rules freshness gate: OK (pinned {status['vendored_at']}, "
            f"{status['age_days']}d old; cadence {status['cadence_days']}d)."
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via the CI step + a unit test of main()
    raise SystemExit(main())
