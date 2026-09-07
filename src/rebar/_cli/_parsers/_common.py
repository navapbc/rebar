"""Provide standard-library-only argument converters for CLI parser factories.

Centralizing ``_positive_int`` keeps bridge, reconcile, and engine-loaded parsers
consistent without importing optional runtime dependencies.
"""

from __future__ import annotations

import argparse


def _positive_int(value: str) -> int:
    """Parse a positive CLI integer or raise ``ArgumentTypeError`` for parser-owned exit 2.

    Unlike the same-named environment coercer, invalid arguments never silently
    default.
    """
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed
