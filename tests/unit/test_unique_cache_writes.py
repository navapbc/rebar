"""Oracle for unique-temp cache/artifact writes.

Ticket b0ac-3c0f-3f64-4344 (silverish-parodic-quoll).

The defect class: a write that derives its temporary filename from the TARGET (or from
the pid) shares that pathname with every concurrent writer of the same target. The first
``os.replace`` consumes the shared temp; the second raises ``FileNotFoundError``; a
best-effort ``except`` swallows it, and that writer is silently lost. Fixed once in
``completion_verdict_cache`` (89981d8e); these are the remaining sites.

The fix is to route each site through the existing shared helper
``rebar._store.fsutil.atomic_write``, which creates a UNIQUE same-directory temp via
``mkstemp`` and publishes it with ``os.replace``. No new helper is introduced.

Two complementary halves, deliberately. The CONCURRENCY half proves the property: two
writers racing on one logical target both land, with no ``*.tmp`` residue, synchronised
by a ``threading.Barrier`` at the real ``os.replace`` seam (the shape this class's
exemplar regression already uses at
``tests/unit/workflow/test_completion_verdict_cache.py``). Timing sleeps are never the
mechanism. The ADOPTION half asserts each site routes through the helper; that is a
structural assertion, which is normally the wrong thing to test — but adoption IS the
contract here, and it is the only half that fails deterministically BEFORE the fix,
because the pre-fix and post-fix code publish through different seams.

Both halves were verified by defect-seeded mutation: re-introducing a shared temp name
inside ``atomic_write`` reddens 2 cases, and reverting the cursor site to its own
hand-rolled shared temp reddens 2. Before the fix the concurrency half reproduced the
production failure directly — ``reconcile_cursor_write_error`` emitted for the losing
writer.
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

# The six sites the audit classified as SHARED-NAME-HAZARD.
HAZARD_SITES = (
    "src/rebar/review_bot/reconcile.py",
    "src/rebar/_config_writer.py",
    "src/rebar/_cli/__init__.py",
    "src/rebar/_opcert_signing.py",
    "src/rebar/_snapshot/janitor.py",
    "src/rebar/llm/workflow/completion_banking.py",
)

# Deriving a temp name from the target (or from the pid) is the defect itself.
SHARED_TEMP_PATTERNS = (
    re.compile(r"with_suffix\([^)]*\.tmp"),
    re.compile(r"with_name\([^)]*\+\s*['\"]\.tmp"),
    re.compile(r"getpid\(\)\}\.tmp"),
    re.compile(r"\+\s*['\"]\.tmp['\"]"),
)


def test_cursor_write_round_trips(tmp_path: Path) -> None:
    """The review-bot cursor persists its value and reads back verbatim."""
    from rebar.review_bot import reconcile

    target = tmp_path / "state" / "cursor"
    reconcile._write_cursor(str(target), "12345")
    assert target.read_text(encoding="utf-8") == "12345"


def test_cursor_write_creates_missing_parents(tmp_path: Path) -> None:
    """The cursor's parent directory is created on demand."""
    from rebar.review_bot import reconcile

    target = tmp_path / "a" / "b" / "cursor"
    reconcile._write_cursor(str(target), "7")
    assert target.read_text(encoding="utf-8") == "7"


def test_cursor_write_leaves_no_temp_residue(tmp_path: Path) -> None:
    """A completed write publishes the target and leaves no ``*.tmp`` behind."""
    from rebar.review_bot import reconcile

    target = tmp_path / "cursor"
    reconcile._write_cursor(str(target), "42")
    assert target.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_banked_verdict_write_round_trips(tmp_path: Path, monkeypatch) -> None:
    """A banked criterion verdict is written as readable JSON."""
    import json

    from rebar._store.fsutil import atomic_write

    target = tmp_path / "bank" / "crit.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(target, json.dumps({"verdict": "PASS"}, sort_keys=True))
    assert json.loads(target.read_text())["verdict"] == "PASS"


# ---------------------------------------------------------------------------
# adoption: deterministic, and the half that fails RED before the fix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("relpath", HAZARD_SITES)
def test_hazard_site_routes_through_the_shared_helper(relpath: str) -> None:
    """Each audited site publishes via ``fsutil.atomic_write`` rather than hand-rolling."""
    text = (REPO_ROOT / relpath).read_text(encoding="utf-8")
    assert "atomic_write" in text, (
        f"{relpath} still hand-rolls its write; route it through "
        "rebar._store.fsutil.atomic_write (the helper already exists, ~50 callers)"
    )


@pytest.mark.parametrize("relpath", HAZARD_SITES)
def test_no_hazard_site_derives_a_temp_name_from_its_target(relpath: str) -> None:
    """A temp name derived from the target is shared by every concurrent writer of it."""
    text = (REPO_ROOT / relpath).read_text(encoding="utf-8")
    for pattern in SHARED_TEMP_PATTERNS:
        found = pattern.search(text)
        assert found is None, (
            f"{relpath} derives a temp name from its target ({found.group(0)!r}); "
            "two concurrent writers then share one pathname and the loser is silently "
            "dropped when the winner's os.replace consumes it"
        )


# ---------------------------------------------------------------------------
# concurrency: the property itself
# ---------------------------------------------------------------------------


def _barriered_replace(monkeypatch, parties: int):
    """Hold every writer at the publish point until all of them have written a temp.

    This is what forces the collision deterministically: with a SHARED temp name all
    writers have written the same pathname by the time the barrier releases, so the first
    replace consumes it and the rest raise FileNotFoundError. With unique temps every
    writer still owns its own file and all of them publish.
    """
    from rebar._store import fsutil

    barrier = threading.Barrier(parties, timeout=30)
    real_replace = os.replace

    def replace(src, dst):
        barrier.wait()
        return real_replace(src, dst)

    monkeypatch.setattr(fsutil.os, "replace", replace)
    return barrier


def test_two_concurrent_writers_to_one_target_both_publish(tmp_path, monkeypatch) -> None:
    from rebar._store.fsutil import atomic_write

    target = tmp_path / "cache.json"
    _barriered_replace(monkeypatch, 2)

    errors: list[BaseException] = []

    def write(value: str) -> None:
        try:
            atomic_write(target, value)
        except BaseException as exc:  # noqa: BLE001 - the assertion is that this is empty
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(v,)) for v in ("alpha", "beta")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert errors == [], f"a concurrent writer was lost: {errors!r}"
    assert target.read_text() in {"alpha", "beta"}
    assert [p.name for p in tmp_path.iterdir() if p.name != "cache.json"] == [], (
        "temp residue survived a concurrent publish"
    )


def test_concurrent_cursor_writes_never_report_a_write_error(tmp_path, monkeypatch) -> None:
    """The review-bot cursor swallows OSError and returns None, so a lost writer is
    invisible in the return value. The observable is the emitted error event, which must
    never fire."""
    from rebar.review_bot import reconcile

    emitted: list[str] = []
    monkeypatch.setattr(reconcile, "_emit", lambda name, **kw: emitted.append(name), raising=True)
    _barriered_replace(monkeypatch, 2)

    target = tmp_path / "cursor"
    threads = [
        threading.Thread(target=reconcile._write_cursor, args=(str(target), v))
        for v in ("100", "200")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert "reconcile_cursor_write_error" not in emitted, (
        f"a concurrent cursor write was silently dropped: {emitted!r}"
    )
    assert target.read_text(encoding="utf-8") in {"100", "200"}
    assert list(tmp_path.glob("*.tmp")) == []


def test_concurrent_writes_to_distinct_targets_are_unaffected(tmp_path, monkeypatch) -> None:
    """Negative control: writers to DIFFERENT targets never contended in the first place,
    so the fix must not change their outcome."""
    from rebar._store.fsutil import atomic_write

    _barriered_replace(monkeypatch, 2)
    a, b = tmp_path / "a.json", tmp_path / "b.json"

    threads = [
        threading.Thread(target=atomic_write, args=(a, "one")),
        threading.Thread(target=atomic_write, args=(b, "two")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert a.read_text() == "one"
    assert b.read_text() == "two"


def test_a_reader_never_observes_a_partially_written_target(tmp_path, monkeypatch) -> None:
    """Atomicity, which the two previously no-temp sites (janitor integrity stamp, bank
    record) did not have: the target is either absent or complete, never truncated."""
    from rebar._store.fsutil import atomic_write

    target = tmp_path / "integrity"
    payload = "x" * 200_000

    seen: list[int] = []
    real_replace = os.replace
    from rebar._store import fsutil

    def replace(src, dst):
        # Immediately before publication the target must not exist in a partial form.
        seen.append(len(Path(dst).read_text()) if Path(dst).exists() else -1)
        return real_replace(src, dst)

    monkeypatch.setattr(fsutil.os, "replace", replace)
    atomic_write(target, payload)

    assert seen == [-1], "target existed in a pre-publish state — the write was not atomic"
    assert len(target.read_text()) == len(payload)
