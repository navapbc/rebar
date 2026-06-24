"""Shared posture for the opt-in close/claim verification gates.

The completion-verification *close* gate (``transition.py``) and the plan-review
*claim* gate (``claim.py``) each resolve a single ``verify.*`` config flag and, on an
**unreadable** config, fail the gate **OPEN** (skip it) with a stderr warning rather
than blocking every operation on a broken config. That resolution + fail-open posture
is identical between them and is exactly the kind of copy-pasted, security-relevant
logic that drifts between sessions — so it lives here, once.

What the gate DOES when enabled (run the LLM completion verifier vs. a local HMAC
plan-review check) is gate-specific and stays in each command module.
"""

from __future__ import annotations

import sys

__all__ = ["gate_enabled"]


def gate_enabled(
    cfg_root: str, attr: str, *, ticket_id: str, gate_label: str, extra: str = ""
) -> bool:
    """Resolve an opt-in ``verify.<attr>`` gate flag, failing OPEN on an unreadable config.

    ``attr`` is a ``VerifyConfig`` attribute name (e.g.
    ``"require_completion_verification_for_close"``). Returns ``True`` when the gate is
    enabled, ``False`` when it is off OR when the config can't be read — in the latter
    case a single-line warning is printed to stderr (``gate_label`` + optional ``extra``
    clause), so the skip is observable and never silent.

    Rationale for fail-OPEN: these are opt-in, default-off gates; an unreadable config
    must not auto-enable a (possibly billable) gate across every repo and ticket. The
    stronger signature gate fail-CLOSES independently, and a missing attestation is
    itself the "not validated" signal CI checks.
    """
    from rebar.config import ConfigError, load_config

    try:
        return bool(getattr(load_config(cfg_root).verify, attr))
    except ConfigError as exc:
        print(
            f"Warning: could not read rebar config ({exc}); {gate_label} is skipped "
            f"for {ticket_id}{extra}.",
            file=sys.stderr,
        )
        return False
