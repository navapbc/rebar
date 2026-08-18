"""HELD-OUT edge oracle for RP-04 S5 (5851) — AC4 rotation, AC5 telemetry, AC1 freshness.

The implementer does NOT see this file. It asserts the OBSERVABLE contracts that:

* AC4 — the startup binding is an immutable snapshot of its config's non-secret policy:
  a running process holds ONE binding (no auto-rebind on ambient change), while a fresh
  compose (the restart analogue) observes rotated material;
* AC5 — partial/rejected telemetry credentials disable telemetry fail-open (``setup_tracing``
  returns ``False`` and never raises), so an operation result cannot depend on it;
* AC1 — each operation composes a FRESH non-secret snapshot: a captured snapshot is stable
  while the next compose observes an intervening config change.

Run: copy into ``tests/unit/review_bot/`` as ``test_rp04_s5_rotation_heldout.py``.
"""

from __future__ import annotations

import dataclasses

import pytest

from rebar.review_bot.config import ReceiverConfig


def _cfg(**overrides) -> ReceiverConfig:
    base = dict(gerrit_bot_token="tok", webhook_token="tok", project="rebar")
    base.update(overrides)
    return ReceiverConfig(**base)


# ── AC4: the startup binding is an immutable, per-config snapshot ────────────
def test_startup_binding_reflects_its_config_and_is_frozen() -> None:
    from rebar.review_bot.startup import compose_startup_binding

    binding = compose_startup_binding(_cfg(gerrit_base_url="https://a.example"))
    # Frozen: a running process cannot mutate the binding in place.
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        binding.policy = {}  # type: ignore[misc]


def test_a_fresh_compose_observes_rotated_nonsecret_material() -> None:
    """Two composes around a changed non-secret field differ — the restart/redeploy that
    rebinds a running process picks up rotated static material — while neither binding
    mutates the other (independent snapshots)."""
    from rebar.review_bot.startup import compose_startup_binding

    before = compose_startup_binding(_cfg(gerrit_base_url="https://before.example"))
    after = compose_startup_binding(_cfg(gerrit_base_url="https://after.example"))

    assert before.policy != after.policy
    # The earlier binding is untouched by the later compose (no shared mutable state).
    assert "before.example" in repr(before.policy)
    assert "after.example" not in repr(before.policy)


# ── AC5: telemetry disables fail-open on partial/rejected credentials ────────
def test_setup_tracing_fails_open_on_partial_credentials() -> None:
    """Partial Langfuse credentials disable telemetry (return ``False``) without raising —
    the operation result cannot be changed by a telemetry misconfiguration."""
    from rebar.llm.tracing import LangfuseConfig, setup_tracing

    partial = LangfuseConfig(public_key="pk-only", secret_key="", host="")
    result = setup_tracing(partial)
    assert result is False


def test_setup_tracing_no_config_is_noop() -> None:
    from rebar.llm.tracing import setup_tracing

    assert setup_tracing(None) is False


# ── AC1: each operation composes a fresh non-secret snapshot ─────────────────
def test_each_operation_composes_a_fresh_snapshot(tmp_path) -> None:
    """Each operation composes its OWN snapshot from its inputs: two operations with
    different operation input produce different snapshots, and a captured snapshot is an
    immutable value that does not observe a later operation's input."""
    from rebar._operation_config import compose_operation_snapshot

    first = compose_operation_snapshot(
        repo_root=str(tmp_path),
        cli_overrides={"ticket": {"default_assignee": "before@example.com"}},
    )
    first_fp = first.fingerprint()

    # A DIFFERENT operation composes freshly and differs.
    second = compose_operation_snapshot(
        repo_root=str(tmp_path), cli_overrides={"ticket": {"default_assignee": "after@example.com"}}
    )
    assert second.fingerprint() != first_fp

    # The earlier snapshot is an immutable capture: stable fingerprint, still its own value.
    assert first.fingerprint() == first_fp
    assert (
        first.canonical_document()["values"]["ticket"]["default_assignee"] == "before@example.com"
    )
