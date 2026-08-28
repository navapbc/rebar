"""Bug b3dd (sibling sweep of 7dea/d284): the two reconciler .bridge_state sidecar
stores must survive two CONCURRENT same-target ``save()`` writers without either
silently losing its write.

Both ``PeerConfirmationStore.save()`` and ``ImpossibleLinkStore.save()`` are atomic
writers: they write a temp file beside the final ``.bridge_state/*.json`` and then
``os.replace`` it into place. The bug class (proven in completion_verdict_cache /
Gerrit 2329 and plan_review/sizing / Gerrit 2334) is a temp name derived
DETERMINISTICALLY from the target (``f"{self.path}.tmp"``) rather than being unique
per writer. Under two concurrent writers for the same sidecar the first ``os.replace``
consumes the one shared temp and the second raises ``FileNotFoundError``, which these
fail-open stores swallow (``logger.warning`` + return) — so the losing writer's
``_dirty`` flag is never cleared and its write is silently lost.

Each test forces the deterministic collision with a barrier at the real ``os.replace``
seam: both writers have already written their temp (which precedes the replace in the
code) before EITHER replace runs. With a shared temp the second replace is a guaranteed
``FileNotFoundError``; with a unique-per-writer temp both replaces succeed.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import threading
from pathlib import Path

import pytest


@pytest.fixture
def tracker(tmp_path: Path) -> Path:
    """A bare tracker dir with the ``.bridge_state`` sidecar directory present."""
    d = tmp_path / ".tickets-tracker"
    (d / ".bridge_state").mkdir(parents=True)
    return d


def _run_two_concurrent_savers(
    module,
    final_path: Path,
    make_saver,
) -> None:
    """Start two threads that each call ``save()`` on their OWN store instance for the
    SAME final path, gated so both reach the module's ``os.replace`` only after both
    have written their temp file. Raises nothing; the caller asserts on the outcome."""
    barrier = threading.Barrier(2, timeout=30)
    real_replace = os.replace
    final_name = final_path.name

    def barriered_replace(src, dst, *args, **kwargs):
        # Gate ONLY this store's own final write, so unrelated os.replace calls
        # (e.g. temp bookkeeping in other libraries) never deadlock on the barrier.
        if str(dst).endswith(final_name):
            barrier.wait()
        return real_replace(src, dst, *args, **kwargs)

    # Both modules reference the process-global ``os`` module; monkeypatching
    # ``module.os.replace`` patches os.replace, and the dst-name gate keeps the
    # barrier scoped to this store's write.
    saved = module.os.replace
    module.os.replace = barriered_replace  # type: ignore[attr-defined]
    try:
        stores = [make_saver(), make_saver()]
        errors: list[BaseException] = []

        def worker(store) -> None:
            try:
                store.save()
            except BaseException as exc:  # noqa: BLE001 — surfaced to the assertions
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(s,)) for s in stores]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        module.os.replace = saved  # type: ignore[attr-defined]

    # Contract 1 (fail-open PRESERVED): save() never raises into the pass.
    assert not errors, f"save() raised instead of staying fail-open: {errors!r}"
    # Contract 2 (no lost write): both writers cleared _dirty — i.e. both persisted.
    # Under the shared-temp bug the loser's os.replace -> FileNotFoundError is swallowed
    # and its _dirty stays True.
    assert all(not s._dirty for s in stores), (
        "a concurrent writer silently lost its save() (its _dirty was never cleared)"
    )
    # Contract 3 (integrity): the final file is present and valid JSON.
    assert final_path.exists(), "the sidecar was never written"
    json.loads(final_path.read_text(encoding="utf-8"))
    # Contract 4 (no residue): the atomic temp+rename leaves no *.tmp litter.
    residue = list((final_path.parent).glob("*.tmp"))
    assert not residue, f"atomic tmp+rename must leave no residue: {residue}"


def test_peer_confirmations_concurrent_save_loses_no_writer(
    tracker: Path, caplog: pytest.LogCaptureFixture
) -> None:
    module = importlib.import_module("rebar_reconciler.peer_confirmations")
    final = tracker / ".bridge_state" / "peer_confirmations.json"

    counter = {"n": 0}

    def make_saver():
        store = module.PeerConfirmationStore(str(tracker))
        counter["n"] += 1
        # Distinct records per writer so each save() writes real content.
        store.record("A", f"B{counter['n']}", "blocks", link_id=str(counter["n"]))
        return store

    with caplog.at_level(logging.WARNING, logger="rebar_reconciler.peer_confirmations"):
        _run_two_concurrent_savers(module, final, make_saver)

    # Direct symptom of the bug: the swallowed FileNotFoundError logs "could not persist".
    assert "could not persist" not in caplog.text, (
        "a concurrent writer hit the swallowed-error path (lost write)"
    )


def test_impossible_links_concurrent_save_loses_no_writer(
    tracker: Path, caplog: pytest.LogCaptureFixture
) -> None:
    module = importlib.import_module("rebar_reconciler.impossible_links")
    final = tracker / ".bridge_state" / "impossible_links.json"

    counter = {"n": 0}

    def make_saver():
        store = module.ImpossibleLinkStore(str(tracker))
        counter["n"] += 1
        # record() gate-checks reason/digest; write the store's records directly so the
        # test exercises the save() atomic-write seam (the mechanism under test), not the
        # digest machinery.
        store._records[f"key{counter['n']}"] = {
            "source_id": "A",
            "target_id": f"B{counter['n']}",
            "relation": "blocks",
            "reason": module.REASON_CLOSED_SOURCE,
            "digest": "d",
            "first_seen": 0.0,
            "last_seen": 0.0,
            "attempts": 1,
            "skips": 0,
        }
        store._dirty = True
        return store

    with caplog.at_level(logging.WARNING, logger="rebar_reconciler.impossible_links"):
        _run_two_concurrent_savers(module, final, make_saver)

    assert "could not persist" not in caplog.text, (
        "a concurrent writer hit the swallowed-error path (lost write)"
    )
