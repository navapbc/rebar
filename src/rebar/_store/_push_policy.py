"""Pure push-policy classifier (no IO).

Extracted from ``push.py``'s ``_push_mode`` so the raw-value → mode mapping — the
one place that decides whether the tickets-branch auto-push runs synchronously,
detaches, or is disabled — can be exercised (and mutation-tested) in isolation,
apart from the subprocess-heavy push orchestration around it. See
docs/mutation-testing.md.
"""

from __future__ import annotations

_MODES = frozenset({"always", "async", "off"})


def normalize_push_mode(raw: str | None) -> str:
    """Map a raw ``sync.push`` value to ``'always' | 'async' | 'off'`` (default ``'always'``).

    Matching is case- and whitespace-insensitive (``" OFF "`` → ``"off"``,
    ``"ASYNC"`` → ``"async"``). Anything unknown or malformed — including ``None``
    and the empty string — falls back to ``"always"`` so a bad value can never
    silently disable (or misroute) the auto-push.
    """
    if raw is None:
        return "always"
    s = raw.strip().lower()
    return s if s in _MODES else "always"
