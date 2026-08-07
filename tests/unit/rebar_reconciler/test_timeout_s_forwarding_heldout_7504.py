"""Held-out validation for task 7504 (a) — authored independently of the implementation.

`timeout_s` sat as a NAMED keyword-only parameter in front of `**kwargs` on
`_call_with_retry`, the wrapper on the path for EVERY Jira write. A caller passing
`timeout_s=60` for the wrapped Jira client had it SWALLOWED by the wrapper rather than
forwarded. Deleting the parameter is what restores forwarding, so the fix is observable
behaviour, not signature hygiene.

Lives here rather than beside the metrics half of the ticket because `rebar_reconciler`
is only importable as a top-level package under this directory's conftest.
"""

from __future__ import annotations

import inspect
from typing import Any

# ── (a) timeout_s now FORWARDS instead of being swallowed ───────────────────


def _call_with_retry():
    from rebar_reconciler.dispatch_one import _call_with_retry as f

    return f


def test_timeout_s_is_no_longer_a_named_parameter() -> None:
    params = inspect.signature(_call_with_retry()).parameters
    assert "timeout_s" not in params, (
        "while named, it shadows **kwargs and swallows a caller's value"
    )
    assert any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()), (
        "**kwargs must remain, or nothing forwards"
    )


def test_timeout_s_reaches_the_wrapped_callable() -> None:
    """The behavioural point of the deletion, on the path for every Jira write."""
    seen: dict[str, Any] = {}

    def fn(*args: Any, **kwargs: Any) -> str:
        seen.update(kwargs)
        return "ok"

    assert _call_with_retry()(fn, "DIG-1", timeout_s=60) == "ok"
    assert seen.get("timeout_s") == 60, f"swallowed by the wrapper: {seen}"


def test_other_kwargs_still_forward_and_max_retries_does_not_leak() -> None:
    """Negative control: `max_retries` is the wrapper's OWN parameter and must keep being
    consumed rather than leaking into the wrapped callable."""
    seen: dict[str, Any] = {}

    def fn(*args: Any, **kwargs: Any) -> str:
        seen.update(kwargs)
        return "ok"

    _call_with_retry()(fn, "DIG-1", max_retries=1, extra="x")
    assert seen.get("extra") == "x"
    assert "max_retries" not in seen, "the wrapper's own knob must not leak downstream"
