"""Argument converters shared by the CLI parser factories (story 7931).

The single home for converters that more than one factory needs. A converter is a
tiny thing to copy, which is exactly why the copies drift: three byte-identical
``_positive_int`` definitions existed (the reconciler's own in-process parser and the
``bridge`` / ``reconcile`` factories), and a fix to one of them would have silently
left the other two rejecting — or accepting — different input.

Like every module under :mod:`rebar._cli._parsers`, this imports ONLY the standard
library, so the reconciler engine (loaded by path, outside the ``rebar._engine``
package) can import it without dragging in any heavy optional runtime.
"""

from __future__ import annotations

import argparse


def _positive_int(value: str) -> int:
    """Argparse converter for a strictly positive count (mutation ceilings, batch sizes).

    Non-numeric and non-positive input both RAISE, so argparse rejects the invocation
    with exit 2 rather than proceeding on a substituted value. Not to be confused with
    :func:`rebar._config_resolvers._positive_int`, a same-named env-var coercer that
    deliberately RETURNS a default instead — silently defaulting is right for an
    ambient env var and wrong for an argument the operator typed."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed
