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
    (git-probe calls from the pre-existing repo-isolation guard pass through).

    ``socket.socket.connect``/``connect_ex`` are patched via
    ``unittest.mock.patch.object`` rather than ``monkeypatch.setattr``: both
    attributes are normally *inherited* (from ``_socket.socket``, not local to
    ``socket.socket``), and the pre-existing, autouse ``_network_guard``
    fixture (``tests/conftest.py``) ALSO patches ``socket.socket.connect`` for
    every test in this tier. ``monkeypatch.setattr`` on a class records
    ``oldval`` via a plain ``target.__dict__.get(name, NOTSET)`` snapshot and
    restores via a plain ``setattr``/``delattr`` on that snapshot; when this
    fixture's patch is applied *while ``_network_guard``'s own patch is
    already active*, that snapshot captures ``_network_guard``'s patched
    function (not ``NOTSET``), so ``monkeypatch``'s teardown re-``setattr``s
    it back — creating a *new local* ``connect`` entry that permanently
    shadows the real, inherited method for every later test in the same
    worker, even ones marked ``@pytest.mark.allow_network`` (see bug
    edeb-c3ad-c051-4e6a). ``mock.patch.object`` instead detects "was this
    name local to the class before I patched it" at both enter and exit, so
    it composes correctly regardless of what else is layered on the same
    attribute — matching the mechanism ``_network_guard`` already uses.
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


@pytest.mark.allow_network  # nested pytest binds a loopback ephemeral port; no live service
def test_effect_spies_do_not_leak_socket_connect_into_later_tests(tmp_path: Path) -> None:
    """Regression for bug edeb-c3ad-c051-4e6a.

    ``effect_spies`` patches ``socket.socket.connect``/``connect_ex`` in the
    SAME pytest worker as the autouse, class-scoped ``_network_guard``
    fixture (``tests/conftest.py``) that every other test in this tier also
    relies on. A prior version of this fixture used ``monkeypatch.setattr``
    for those two attributes; because ``monkeypatch`` snapshots/restores a
    class attribute via a plain dict check rather than tracking whether the
    attribute was already local due to another active patch, tearing down
    while ``_network_guard``'s own patch was active left ``connect``
    *permanently* shadowed for every subsequent test in the worker — even
    ones marked ``@pytest.mark.allow_network`` — which is exactly the
    "Network access is forbidden" failure observed in CI on
    ``tests/unit/test_audit_serve_heldout.py``
    ``test_serve_binds_loopback_ephemeral_and_lists_ticket`` once this file
    ran first. Reproduce the real ordering (this file's tests, then that
    one) in a fresh pytest worker and assert both suites still pass."""
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
