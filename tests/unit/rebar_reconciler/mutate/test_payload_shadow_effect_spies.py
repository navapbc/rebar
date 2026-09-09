"""Effect-spy contract for side-effect-free payload shadow replay.

The match and reject corpus plus payload construction run with subprocess, socket,
sleep, and ticket-store writes replaced by failing spies. A self-check calls the
tripwires before the corpus relies on their silence.
"""

from __future__ import annotations

import json
import re
import socket
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from _nested_pytest import REPO_ROOT, run_nested_pytest
from _subprocess_env import subprocess_env

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
    """Raise for subprocess effects while allowing test-isolation Git probes."""

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
    """Install tripwires for subprocess, sleep, store writes, and socket connects.

    Git probes pass through for repository isolation. Socket methods use nested-aware
    patching so teardown restores inherited methods after the autouse network guard.
    """
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
    with (
        patch.object(socket.socket, "connect", _boom("socket.socket.connect")),
        patch.object(socket.socket, "connect_ex", _boom("socket.socket.connect_ex")),
    ):
        yield monkeypatch


# ---------------------------------------------------------------------------
# Self-check: the spies must be real tripwires, not inert fixtures.
# ---------------------------------------------------------------------------


def test_spies_actually_fire_on_a_leaky_stub(effect_spies):
    """Representative direct calls prove that the tripwire fixture is active."""
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


@pytest.mark.allow_network  # nested pytest binds a loopback ephemeral port; no live service
def test_effect_spies_do_not_leak_socket_connect_into_later_tests(tmp_path: Path) -> None:
    """Nested pytest proves socket teardown restores a later network-enabled test."""
    this_file = Path(__file__).resolve()
    audit_serve_file = REPO_ROOT / "tests" / "unit" / "test_audit_serve_heldout.py"
    assert audit_serve_file.is_file()

    result = run_nested_pytest(
        tmp_path,
        "-k",
        "not test_effect_spies_do_not_leak_socket_connect_into_later_tests",
        "-q",
        str(this_file),
        str(audit_serve_file),
        env=subprocess_env(),
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


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
