"""Effect-spy safety oracle for the shadow comparator (ADR 0107, e9d5).

AC2: "The shadow comparator executes no Jira transport or ticket-store write,
proven with failing spies on every effect seam." This file patches every
named effect seam (subprocess, socket connect, clock sleep, and the ticket
store's write entry points) to RAISE if called, then drives the ENTIRE
replay corpus (both match and reject scenarios) plus the payload-dataclass
construction paths through those patches active.

Per the task's TDD discipline ("get RED-then-GREEN against a deliberately
leaky stub before trusting them"): ``test_spies_actually_fire_on_a_leaky_stub``
proves each spy is a real, load-bearing tripwire — not a fixture that merely
never gets exercised — by calling the patched target directly and asserting
the resulting ``EffectViolation``. Only after that self-check do the
corpus-wide tests below trust an ABSENCE of ``EffectViolation`` as proof of
"no effect happened", rather than "the spy was never wired at all".
"""

from __future__ import annotations

import json
import re
import socket
import subprocess
import time
from pathlib import Path

import pytest

from rebar._store import event_append
from rebar_reconciler import mutation as mutation_mod
from rebar_reconciler import mutation_payloads, payload_shadow

CORPUS_PATH = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "reconciler"
    / "payload_corpus"
    / "v1"
    / "scenarios.json"
)


class EffectViolation(AssertionError):
    """Raised by a spy in place of the real effect it replaces."""


def _boom(name: str):
    def _raise(*args, **kwargs):
        raise EffectViolation(
            f"disallowed effect during shadow replay: {name}(args={args!r}, kwargs={kwargs!r})"
        )

    return _raise


def _boom_unless_git(name: str, real):
    """Like :func:`_boom`, but pass through a ``git`` invocation to *real*.

    ``tests/_isolation.py``'s repo-isolation guard (autouse, ``tests/conftest.py``)
    samples ``git rev-parse HEAD`` / ``git status --porcelain`` around EVERY test
    in this suite via the real ``subprocess.run``/``Popen`` — that guard is
    infrastructure this test must not break. Real Jira/vendor subprocess calls
    (e.g. the ``acli`` binary) never start with ``"git"``, so this still catches
    any actual effect this story's code would trigger while leaving the
    pre-existing isolation guard functional.
    """

    def _raise_or_delegate(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args")
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "git":
            return real(*args, **kwargs)
        raise EffectViolation(
            f"disallowed effect during shadow replay: {name}(args={args!r}, kwargs={kwargs!r})"
        )

    return _raise_or_delegate


@pytest.fixture
def effect_spies(monkeypatch):
    """Patch every named effect seam to raise ``EffectViolation`` if invoked
    (git-probe calls from the pre-existing repo-isolation guard pass through)."""
    monkeypatch.setattr(subprocess, "run", _boom_unless_git("subprocess.run", subprocess.run))
    monkeypatch.setattr(subprocess, "Popen", _boom_unless_git("subprocess.Popen", subprocess.Popen))
    monkeypatch.setattr(
        subprocess,
        "check_output",
        _boom_unless_git("subprocess.check_output", subprocess.check_output),
    )
    monkeypatch.setattr(
        subprocess, "check_call", _boom_unless_git("subprocess.check_call", subprocess.check_call)
    )
    monkeypatch.setattr(socket.socket, "connect", _boom("socket.socket.connect"))
    monkeypatch.setattr(socket.socket, "connect_ex", _boom("socket.socket.connect_ex"))
    monkeypatch.setattr(time, "sleep", _boom("time.sleep"))
    # Ticket-store write entry points (rebar._store.event_append).
    monkeypatch.setattr(event_append, "write_and_push", _boom("event_append.write_and_push"))
    monkeypatch.setattr(
        event_append, "batch_write_and_push", _boom("event_append.batch_write_and_push")
    )
    monkeypatch.setattr(event_append, "stage_and_commit", _boom("event_append.stage_and_commit"))
    monkeypatch.setattr(
        event_append, "batch_stage_and_commit", _boom("event_append.batch_stage_and_commit")
    )
    monkeypatch.setattr(event_append, "delete_events", _boom("event_append.delete_events"))
    return monkeypatch


# ---------------------------------------------------------------------------
# Self-check: the spies must be real tripwires, not inert fixtures.
# ---------------------------------------------------------------------------


def test_spies_actually_fire_on_a_leaky_stub(effect_spies):
    """A deliberately leaky stub calling each patched target must raise
    EffectViolation — proving the fixture is load-bearing (RED-then-GREEN
    per the task's TDD discipline), not merely unexercised scaffolding."""
    with pytest.raises(EffectViolation, match=re.escape("subprocess.run")):
        subprocess.run(["true"], check=False)
    with pytest.raises(EffectViolation, match=re.escape("subprocess.Popen")):
        subprocess.Popen(["true"])
    with pytest.raises(EffectViolation, match=re.escape("time.sleep")):
        time.sleep(0)
    with pytest.raises(EffectViolation, match=re.escape("event_append.write_and_push")):
        event_append.write_and_push("tracker", [], "msg")
    with pytest.raises(EffectViolation, match=re.escape("event_append.stage_and_commit")):
        event_append.stage_and_commit("tracker", [], "msg")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(EffectViolation, match=re.escape("socket.socket.connect")):
            sock.connect(("example.invalid", 80))
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# The actual safety proof: replay the whole corpus with spies armed.
# ---------------------------------------------------------------------------


def _load_corpus() -> list[dict]:
    return json.loads(CORPUS_PATH.read_text())


def test_full_corpus_replay_never_trips_an_effect_spy(effect_spies):
    corpus = _load_corpus()
    match_scenarios = [s for s in corpus if s.get("expect", "match") == "match"]
    reject_scenarios = [s for s in corpus if s.get("expect") == "reject"]

    results = payload_shadow.compare_corpus(mutation_mod, match_scenarios)
    assert all(r.matched for r in results.values())

    for scenario in reject_scenarios:
        with pytest.raises((ValueError, TypeError, mutation_payloads.UnknownMutationKindError)):
            payload_shadow.build_typed_mutation(
                mutation_mod,
                direction=scenario["direction"],
                action=scenario["action"],
                target=scenario["target"],
                payload=scenario["payload"],
                provenance=scenario.get("provenance", {}),
            )


def test_payload_dataclass_construction_never_trips_an_effect_spy(effect_spies):
    """Construct every payload type directly (not just via the corpus) —
    dataclass __post_init__ validation must never reach for I/O."""
    mutation_payloads.OutboundCreatePayload(fields={"a": 1})
    mutation_payloads.OutboundUpdatePayload(changed_fields={"a": 1})
    mutation_payloads.OutboundDeletePayload()
    mutation_payloads.OutboundProbePayload()
    mutation_payloads.OutboundConflictPayload(reason="x")
    mutation_payloads.InboundCreatePayload(fields={})
    mutation_payloads.InboundUpdatePayload()
    mutation_payloads.InboundCleanLabelPayload(labels_to_remove=("rebar-id-1",))
    mutation_payloads.InboundRepairPropertyPayload(local_id="x")
    mutation_payloads.InboundConflictPayload(reason="x", jira_key="ABC-1")
