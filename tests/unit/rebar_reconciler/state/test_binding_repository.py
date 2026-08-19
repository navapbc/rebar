"""Direct characterization oracles for the byte-preserving ``BindingRepository``.

RP-02 S1 T1 (vivacious-widish-indianabat). ``BindingRepository`` is extracted as the sole
owner of the four binding-state files — live ``bindings.json``, retired
``bindings-retired.json``, the ``get_rotation.json`` sidecar, and best-effort lifecycle
alerts. This module is the DIRECT oracle for that ownership: it pins the load rules, the
exact serialization bytes, the unconditional save boundary, and every ordered
partial-state failure BEFORE any facade delegation happens (that is T2).

The characterization source on the reviewed base is ``BindingStore._load``,
``_load_retired``, ``_retired_entries``, ``_save_retired``, ``_alert``, ``save``,
``_retire``, and ``get_rotation.save``. Behavior here must match those functions exactly;
a difference is a regression, not an improvement.

Deliberately NOT claimed by these tests: multi-file atomicity, a journal, an eager-load
rewrite, deep-copied views, tickets-branch publication, or write elision. ``save()``
remains unconditional — it always attempts rotation persistence before replacing live
state, even with no in-memory change.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from rebar_reconciler import binding_repository, get_rotation
from rebar_reconciler.binding_repository import BindingRepository

# Canonical serialization contract shared by every binding-state file.
_INDENT = 2


def _binding_doc(
    *,
    inline: str | None = None,
    extra_top: dict[str, Any] | None = None,
    extra_entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A production-shaped live-store document.

    ``inline`` seeds the LEGACY inline ``last_get_pass`` stamp (the rotation floor).
    ``extra_top`` / ``extra_entry`` seed UNKNOWN fields — a store written by a newer or
    older rebar. Unknown fields must survive a load/save round trip untouched, because
    the reconciler shares this file with other writers.
    """
    entry: dict[str, Any] = {
        "jira_key": "DIG-A",
        "state": "confirmed",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    if inline is not None:
        entry["last_get_pass"] = inline
    if extra_entry:
        entry.update(extra_entry)
    doc: dict[str, Any] = {
        "version": 2,
        "bindings": {"loc-A": entry},
        "reverse": {"DIG-A": "loc-A"},
        "comment_ids": {},
    }
    if extra_top:
        doc.update(extra_top)
    return doc


def _canonical(payload: Any) -> bytes:
    """The exact byte contract: ``indent=2``, ``sort_keys=True``, trailing newline."""
    return (json.dumps(payload, indent=_INDENT, sort_keys=True) + "\n").encode("utf-8")


def _tracker(root: Path) -> Path:
    return root / ".tickets-tracker"


def _bridge(root: Path) -> Path:
    return _tracker(root) / ".bridge_state"


def _seed(
    root: Path,
    *,
    doc: dict[str, Any] | None = None,
    sidecar: dict[str, str] | None = None,
    retired: Any = None,
    raw_live: str | None = None,
    raw_retired: str | None = None,
) -> Path:
    """Write on-disk state and return the tracker dir a repository is constructed over."""
    bridge = _bridge(root)
    bridge.mkdir(parents=True, exist_ok=True)
    if raw_live is not None:
        (bridge / "bindings.json").write_text(raw_live, encoding="utf-8")
    elif doc is not None:
        (bridge / "bindings.json").write_bytes(_canonical(doc))
    if sidecar is not None:
        (bridge / "get_rotation.json").write_bytes(
            _canonical({"version": 1, "last_get_pass": sidecar})
        )
    if raw_retired is not None:
        (bridge / "bindings-retired.json").write_text(raw_retired, encoding="utf-8")
    elif retired is not None:
        (bridge / "bindings-retired.json").write_bytes(
            _canonical({"version": 1, "retired": retired})
        )
    return _tracker(root)


def _temps(root: Path) -> list[Path]:
    """Any leftover atomic-write temp file — the cleanup oracle."""
    bridge = _bridge(root)
    if not bridge.exists():
        return []
    return sorted(
        p
        for p in bridge.iterdir()
        if p.suffix == ".tmp"
        and (
            p.name.startswith("bindings_")
            or p.name.startswith("bindings_retired_")
            or p.name.startswith("get_rotation_")
        )
    )


def _alert_lines(root: Path) -> list[dict[str, Any]]:
    """Every appended alert record (``<repo_root>/bridge_state/bridge_alerts/*.jsonl``)."""
    alerts_dir = root / "bridge_state" / "bridge_alerts"
    if not alerts_dir.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(alerts_dir.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


# ---------------------------------------------------------------------------
# Load rules and the exact byte contract
# ---------------------------------------------------------------------------


def test_construction_over_seeded_state_performs_no_eager_write(tmp_path: Path) -> None:
    """Construction READS; it must never write. An eager rewrite on load would churn
    the tickets branch on every pass and could rewrite a store this rebar does not
    fully understand (unknown fields) before anything has changed."""
    doc = _binding_doc(extra_top={"future_top": {"k": 1}})
    tracker = _seed(tmp_path, doc=doc, sidecar={"DIG-A": "p-1"}, retired={})
    live = _bridge(tmp_path) / "bindings.json"
    rotation = _bridge(tmp_path) / "get_rotation.json"
    retired = _bridge(tmp_path) / "bindings-retired.json"
    before = (live.read_bytes(), rotation.read_bytes(), retired.read_bytes())

    BindingRepository(tracker)

    assert (live.read_bytes(), rotation.read_bytes(), retired.read_bytes()) == before
    assert _temps(tmp_path) == []


def test_construction_over_absent_state_creates_no_files(tmp_path: Path) -> None:
    """A first-ever pass must not materialize state until something is saved."""
    tracker = _tracker(tmp_path)

    repo = BindingRepository(tracker)

    assert not _bridge(tmp_path).exists()
    assert repo.bindings == {}
    assert repo.reverse == {}
    assert repo.retired_keys() == set()


def test_unknown_fields_round_trip_byte_identical(tmp_path: Path) -> None:
    """An unknown top-level key and an unknown per-entry key survive load->save with
    byte-identical output. This is the compatibility guarantee that lets a newer writer
    share the file: the repository preserves what it does not recognize."""
    doc = _binding_doc(
        extra_top={"future_top": {"nested": [1, 2]}, "aaa_sorts_first": "x"},
        extra_entry={"future_entry": "keep-me"},
    )
    tracker = _seed(tmp_path, doc=doc)
    live = _bridge(tmp_path) / "bindings.json"
    before = live.read_bytes()

    repo = BindingRepository(tracker)
    repo.save()

    assert live.read_bytes() == before
    assert live.read_bytes() == _canonical(doc)
    reloaded = json.loads(live.read_text(encoding="utf-8"))
    assert reloaded["future_top"] == {"nested": [1, 2]}
    assert reloaded["bindings"]["loc-A"]["future_entry"] == "keep-me"


def test_save_writes_exact_sorted_indented_newline_bytes(tmp_path: Path) -> None:
    """The literal serialization contract: ``indent=2``, ``sort_keys=True``, one
    trailing newline. Pinned as bytes because the file is committed and diffed."""
    tracker = _tracker(tmp_path)
    repo = BindingRepository(tracker)
    repo.bindings["loc-Z"] = {"jira_key": "DIG-Z", "state": "confirmed"}
    repo.reverse["DIG-Z"] = "loc-Z"

    repo.save()

    raw = (_bridge(tmp_path) / "bindings.json").read_bytes()
    assert raw == _canonical(repo.data)
    assert raw.endswith(b"\n")
    assert not raw.endswith(b"\n\n")
    text = raw.decode("utf-8")
    assert '\n  "bindings": {' in text
    keys = [line for line in text.splitlines() if line.startswith('  "')]
    assert keys == sorted(keys)


def test_views_are_open_over_the_original_dictionaries(tmp_path: Path) -> None:
    """The repository hands out the SAME dict objects it loaded, not copies — the
    facade and lifecycle owners mutate live state in place, so a defensive copy here
    would silently drop their writes."""
    tracker = _seed(tmp_path, doc=_binding_doc())
    repo = BindingRepository(tracker)

    assert repo.bindings is repo.data["bindings"]
    assert repo.reverse is repo.data["reverse"]
    entry = repo.bindings["loc-A"]
    entry["state"] = "pending"
    assert repo.data["bindings"]["loc-A"]["state"] == "pending"


def test_retired_entries_round_trip_and_expose_keys(tmp_path: Path) -> None:
    """Retired state is a sibling file so the live store stays clean; the key set and
    the full entry map are both readable, and a save round-trips byte-exactly."""
    entries = {"DIG-A": {"local_id": "loc-A", "retired_at": "2026-01-01T00:00:00Z"}}
    tracker = _seed(tmp_path, doc=_binding_doc(), retired=entries)
    repo = BindingRepository(tracker)

    assert repo.retired_keys() == {"DIG-A"}
    assert repo.retired_entries() == entries

    repo.save_retired(entries)

    raw = (_bridge(tmp_path) / "bindings-retired.json").read_bytes()
    assert raw == _canonical({"version": 1, "retired": entries})


def test_legacy_list_form_retired_file_degrades_to_keys(tmp_path: Path) -> None:
    """A legacy list-form retired file still yields its key set (additive read)."""
    tracker = _seed(tmp_path, doc=_binding_doc(), retired=["DIG-A", "DIG-B"])
    repo = BindingRepository(tracker)

    assert repo.retired_keys() == {"DIG-A", "DIG-B"}
    assert repo.retired_entries() == {}


def test_save_without_in_memory_change_still_persists_rotation(tmp_path: Path) -> None:
    """``save()`` is UNCONDITIONAL. A legacy inline-only store materializes its
    equivalent sidecar even though nothing changed in memory, and the inline stamp is
    scrubbed only once the sidecar durably holds it."""
    tracker = _seed(tmp_path, doc=_binding_doc(inline="p-2"))
    rotation = _bridge(tmp_path) / "get_rotation.json"
    assert not rotation.exists()

    repo = BindingRepository(tracker)
    repo.save()

    assert json.loads(rotation.read_text(encoding="utf-8"))["last_get_pass"] == {"DIG-A": "p-2"}
    live = json.loads((_bridge(tmp_path) / "bindings.json").read_text(encoding="utf-8"))
    assert "last_get_pass" not in live["bindings"]["loc-A"]


def test_save_persists_rotation_before_replacing_live(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rotation-before-live is the ordering that makes a crash recoverable: if the
    sidecar lands first, live can be replaced with the inline stamp scrubbed. Reversing
    it would scrub the floor with no durable copy anywhere.

    Discriminated by DESTINATION, not by module: ``binding_repository`` and
    ``get_rotation`` both hold a reference to the one stdlib ``os`` module, so two
    separate ``setattr`` patches would collide and the second would win.
    """
    tracker = _seed(tmp_path, doc=_binding_doc(inline="p-3"))
    rotation_path = _bridge(tmp_path) / "get_rotation.json"
    live_path = _bridge(tmp_path) / "bindings.json"
    order: list[str] = []
    real_replace = os.replace

    def record(src: Any, dst: Any) -> Any:
        destination = Path(dst)
        if destination == rotation_path:
            order.append("rotation")
        elif destination == live_path:
            order.append("live")
        return real_replace(src, dst)

    monkeypatch.setattr(binding_repository.os, "replace", record)
    BindingRepository(tracker).save()

    assert order == ["rotation", "live"]


def test_successful_save_leaves_no_temp_files(tmp_path: Path) -> None:
    tracker = _seed(tmp_path, doc=_binding_doc())
    repo = BindingRepository(tracker)
    repo.save()
    repo.save_retired({"DIG-A": {"local_id": "loc-A"}})

    assert _temps(tmp_path) == []
    names = {p.name for p in _bridge(tmp_path).iterdir()}
    assert names == {"bindings.json", "get_rotation.json", "bindings-retired.json"}


# ---------------------------------------------------------------------------
# Corruption dispositions: live fails CLOSED, retired fails OPEN
# ---------------------------------------------------------------------------


def test_corrupt_live_state_raises_value_error_and_preserves_the_file(tmp_path: Path) -> None:
    """Fail CLOSED. A corrupt/conflict-marked live store degrading to empty would treat
    every ticket as unbound -> mass duplicate creates. The corrupt bytes must survive so
    the operator can resolve the conflict."""
    raw = '{"version": 2, "bindings": {<<<<<<< HEAD\n'
    tracker = _seed(tmp_path, raw_live=raw)
    live = _bridge(tmp_path) / "bindings.json"

    with pytest.raises(ValueError, match="corrupt or contains git conflict"):
        BindingRepository(tracker)

    assert live.read_text(encoding="utf-8") == raw
    assert _temps(tmp_path) == []


def test_corrupt_live_error_names_the_file_and_recovery_route(tmp_path: Path) -> None:
    tracker = _seed(tmp_path, raw_live="{not json")

    with pytest.raises(ValueError) as excinfo:
        BindingRepository(tracker)

    message = str(excinfo.value)
    assert "bindings.json" in message
    assert "git show tickets:" in message


def test_corrupt_retired_state_fails_open_alerts_and_preserves_the_file(
    tmp_path: Path,
) -> None:
    """Fail OPEN, in deliberate contrast to live state. A retired binding wrongly seen as
    live costs one wasted GET (it re-404s and re-retires) — never a duplicate — so a
    corrupt retired file degrades to an empty set plus a deduped alert."""
    raw = "{corrupt retired"
    tracker = _seed(tmp_path, doc=_binding_doc(), raw_retired=raw)
    retired = _bridge(tmp_path) / "bindings-retired.json"

    repo = BindingRepository(tracker)

    assert repo.retired_keys() == set()
    assert repo.retired_entries() == {}
    assert retired.read_text(encoding="utf-8") == raw
    kinds = [record.get("kind") for record in _alert_lines(tmp_path)]
    assert "binding-retired-file-corrupt" in kinds


def test_corrupt_retired_alert_dedupes_across_passes(
    tmp_path: Path,
) -> None:
    """A repeated corrupt-retired condition appends ONE record, deduped across passes.

    ``alert_store.append`` stamps ``timestamp_ns`` centrally, so the second
    ``BindingRepository`` pass over the same corrupt ``bindings-retired.json`` finds the
    first record inside the 24h window and suppresses its duplicate — the dedup the
    ``_alert`` docstring always promised (bug 8384). The stored record carries the stamp.
    """
    tracker = _seed(tmp_path, doc=_binding_doc(), raw_retired="{corrupt")

    BindingRepository(tracker)
    BindingRepository(tracker)

    corrupt = [r for r in _alert_lines(tmp_path) if r.get("kind") == "binding-retired-file-corrupt"]
    assert len(corrupt) == 1
    assert corrupt[0]["key"] == "retired-file-corrupt"
    assert corrupt[0]["resolved"] is False
    assert isinstance(corrupt[0]["timestamp_ns"], int)


# ---------------------------------------------------------------------------
# Ordered partial-state failures
# ---------------------------------------------------------------------------


def _fail_mkstemp_for(mp: pytest.MonkeyPatch, module: Any, prefix: str) -> None:
    """Fail ONLY the atomic write whose temp prefix matches, leaving others real."""
    real = module.tempfile.mkstemp

    def fake(*args: Any, prefix: str = "", **kwargs: Any) -> Any:
        if prefix == prefix_target:
            raise OSError(f"{prefix_target} temp is unwritable")
        return real(*args, prefix=prefix, **kwargs)

    prefix_target = prefix
    mp.setattr(module.tempfile, "mkstemp", fake)


def _fail_replace_onto(mp: pytest.MonkeyPatch, module: Any, target: Path) -> None:
    """Fail ONLY the ``os.replace`` landing on ``target`` — the real atomic commit
    point of that one file, so every earlier write in the sequence really happened."""
    real = module.os.replace

    def fake(src: Any, dst: Any) -> Any:
        if Path(dst) == target:
            raise OSError(f"replace onto {target.name} failed")
        return real(src, dst)

    mp.setattr(module.os, "replace", fake)


def test_rotation_tempfile_failure_keeps_sidecar_old_and_live_new_with_floor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rotation is an OPTIMIZATION and fails open: a sidecar failure must not abort the
    save. Live IS still replaced, and because the sidecar does not hold the stamp the
    inline ``last_get_pass`` floor is RETAINED rather than scrubbed."""
    tracker = _seed(tmp_path, doc=_binding_doc(inline="p-4"))
    rotation = _bridge(tmp_path) / "get_rotation.json"
    _fail_mkstemp_for(monkeypatch, get_rotation, "get_rotation_")

    BindingRepository(tracker).save()

    assert not rotation.exists()
    live = json.loads((_bridge(tmp_path) / "bindings.json").read_text(encoding="utf-8"))
    assert live["bindings"]["loc-A"]["last_get_pass"] == "p-4"
    assert _temps(tmp_path) == []


def test_rotation_replace_failure_keeps_sidecar_old_and_live_new_with_floor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = _seed(tmp_path, doc=_binding_doc(inline="p-6"), sidecar={"DIG-A": "p-5"})
    rotation = _bridge(tmp_path) / "get_rotation.json"
    before = rotation.read_bytes()
    _fail_replace_onto(monkeypatch, get_rotation, rotation)

    BindingRepository(tracker).save()

    assert rotation.read_bytes() == before
    live = json.loads((_bridge(tmp_path) / "bindings.json").read_text(encoding="utf-8"))
    assert live["bindings"]["loc-A"]["last_get_pass"] == "p-6"
    assert _temps(tmp_path) == []


def test_next_save_after_rotation_failure_converges_sidecar_from_inline_floor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recovery half of the fail-open rotation contract: the retained inline floor is
    what a later successful save promotes into the sidecar. Without the floor the newest
    stamp would be lost outright, silently re-GETting the issue forever."""
    tracker = _seed(tmp_path, doc=_binding_doc(inline="p-7"))
    rotation = _bridge(tmp_path) / "get_rotation.json"
    _fail_mkstemp_for(monkeypatch, get_rotation, "get_rotation_")
    BindingRepository(tracker).save()
    assert not rotation.exists()

    monkeypatch.undo()
    reloaded = BindingRepository(tracker)
    assert reloaded.rotation.get("DIG-A") == "p-7"
    reloaded.save()

    assert json.loads(rotation.read_text(encoding="utf-8"))["last_get_pass"] == {"DIG-A": "p-7"}
    live = json.loads((_bridge(tmp_path) / "bindings.json").read_text(encoding="utf-8"))
    assert "last_get_pass" not in live["bindings"]["loc-A"]
    assert _temps(tmp_path) == []


def test_live_replace_failure_after_rotation_keeps_live_old_and_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live persistence fails CLOSED: it raises. The EARLIER successful rotation write is
    retained (not rolled back) — that is the documented ordered partial state, and the
    newest rotation maximum is safe to keep."""
    tracker = _seed(tmp_path, doc=_binding_doc(inline="p-9"), sidecar={"DIG-A": "p-8"})
    live = _bridge(tmp_path) / "bindings.json"
    rotation = _bridge(tmp_path) / "get_rotation.json"
    before_live = live.read_bytes()
    _fail_replace_onto(monkeypatch, binding_repository, live)

    with pytest.raises(OSError, match=r"replace onto bindings\.json failed"):
        BindingRepository(tracker).save()

    assert live.read_bytes() == before_live
    assert json.loads(rotation.read_text(encoding="utf-8"))["last_get_pass"] == {"DIG-A": "p-9"}
    assert _temps(tmp_path) == []


def test_live_tempfile_failure_keeps_live_old_and_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = _seed(tmp_path, doc=_binding_doc())
    live = _bridge(tmp_path) / "bindings.json"
    before = live.read_bytes()
    _fail_mkstemp_for(monkeypatch, binding_repository, "bindings_")

    with pytest.raises(OSError, match="bindings_ temp is unwritable"):
        BindingRepository(tracker).save()

    assert live.read_bytes() == before
    assert _temps(tmp_path) == []


def test_live_save_recovers_cleanly_after_a_transient_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restoration plus a successful retry is what proves the atomicity contract."""
    tracker = _seed(tmp_path, doc=_binding_doc())
    live = _bridge(tmp_path) / "bindings.json"
    _fail_replace_onto(monkeypatch, binding_repository, live)
    repo = BindingRepository(tracker)
    repo.bindings["loc-B"] = {"jira_key": "DIG-B", "state": "confirmed"}
    with pytest.raises(OSError):
        repo.save()

    monkeypatch.undo()
    repo.save()

    assert live.read_bytes() == _canonical(repo.data)
    assert json.loads(live.read_text(encoding="utf-8"))["bindings"]["loc-B"]["jira_key"] == "DIG-B"
    assert _temps(tmp_path) == []


def test_retired_tempfile_failure_keeps_retired_and_live_old_and_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retired persistence is the COMMITTED side of the retired-first protocol, so its
    write fails closed. Nothing else may move when it fails."""
    tracker = _seed(tmp_path, doc=_binding_doc(), retired={"DIG-OLD": {"local_id": "loc-old"}})
    retired = _bridge(tmp_path) / "bindings-retired.json"
    live = _bridge(tmp_path) / "bindings.json"
    before = (retired.read_bytes(), live.read_bytes())
    repo = BindingRepository(tracker)
    _fail_mkstemp_for(monkeypatch, binding_repository, "bindings_retired_")

    with pytest.raises(OSError, match="bindings_retired_ temp is unwritable"):
        repo.save_retired({"DIG-NEW": {"local_id": "loc-new"}})

    assert (retired.read_bytes(), live.read_bytes()) == before
    assert _temps(tmp_path) == []


def test_retired_replace_failure_keeps_retired_and_live_old_and_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = _seed(tmp_path, doc=_binding_doc(), retired={"DIG-OLD": {"local_id": "loc-old"}})
    retired = _bridge(tmp_path) / "bindings-retired.json"
    live = _bridge(tmp_path) / "bindings.json"
    before = (retired.read_bytes(), live.read_bytes())
    repo = BindingRepository(tracker)
    _fail_replace_onto(monkeypatch, binding_repository, retired)

    with pytest.raises(OSError, match=r"replace onto bindings-retired\.json failed"):
        repo.save_retired({"DIG-NEW": {"local_id": "loc-new"}})

    assert (retired.read_bytes(), live.read_bytes()) == before
    assert _temps(tmp_path) == []


def test_live_failure_after_successful_retired_write_retains_the_tombstone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retired-first ordering under a crash: the tombstone is durable and the live
    pair is NOT yet removed. This exact overlap is the detectable, completable state
    RP-02 S3 later repairs — so the repository must produce it faithfully rather than
    rolling the tombstone back."""
    tracker = _seed(tmp_path, doc=_binding_doc(), retired={})
    live = _bridge(tmp_path) / "bindings.json"
    retired = _bridge(tmp_path) / "bindings-retired.json"
    before_live = live.read_bytes()
    repo = BindingRepository(tracker)
    tombstone = {"DIG-A": {"local_id": "loc-A", "retired_at": "2026-01-02T00:00:00Z"}}

    repo.save_retired(tombstone)
    _fail_replace_onto(monkeypatch, binding_repository, live)
    repo.bindings.pop("loc-A", None)
    repo.reverse.pop("DIG-A", None)
    with pytest.raises(OSError, match=r"replace onto bindings\.json failed"):
        repo.save()

    assert json.loads(retired.read_text(encoding="utf-8"))["retired"] == tombstone
    assert live.read_bytes() == before_live
    assert json.loads(live.read_text(encoding="utf-8"))["bindings"]["loc-A"]["jira_key"] == "DIG-A"
    assert _temps(tmp_path) == []


def test_alert_persistence_failure_is_swallowed_and_preserves_the_state_write(
    tmp_path: Path,
) -> None:
    """Alerting is best-effort and must NEVER break a sync pass. A real filesystem fault
    at the alert store (its parent path is a regular file, so ``mkdir`` fails) is
    swallowed, and the state write that preceded it still lands byte-exactly."""
    tracker = _seed(tmp_path, doc=_binding_doc(), raw_retired="{corrupt")
    (tmp_path / "bridge_state").write_text("not a directory", encoding="utf-8")

    repo = BindingRepository(tracker)
    repo.bindings["loc-C"] = {"jira_key": "DIG-C", "state": "confirmed"}
    repo.save()

    assert repo.retired_keys() == set()
    live = _bridge(tmp_path) / "bindings.json"
    assert live.read_bytes() == _canonical(repo.data)
    assert json.loads(live.read_text(encoding="utf-8"))["bindings"]["loc-C"]["jira_key"] == "DIG-C"
    assert (tmp_path / "bridge_state").is_file()
    assert _temps(tmp_path) == []


def test_alert_records_carry_the_deduplication_key_and_unresolved_flag(
    tmp_path: Path,
) -> None:
    """The alert record shape operators and the dedup window key on."""
    tracker = _seed(tmp_path, doc=_binding_doc())
    repo = BindingRepository(tracker)

    repo.alert(key="binding-retired:DIG-A", record={"kind": "binding-retired", "jira_key": "DIG-A"})

    records = [r for r in _alert_lines(tmp_path) if r.get("kind") == "binding-retired"]
    assert len(records) == 1
    assert records[0]["key"] == "binding-retired:DIG-A"
    assert records[0]["resolved"] is False


def test_repository_owns_the_four_state_paths(tmp_path: Path) -> None:
    """Path ownership is the extraction's whole point: one owner, fixed layout."""
    tracker = _seed(tmp_path, doc=_binding_doc())
    repo = BindingRepository(tracker)

    bridge = _bridge(tmp_path)
    assert repo.path == bridge / "bindings.json"
    assert repo.retired_path == bridge / "bindings-retired.json"
    assert repo.rotation_path == bridge / "get_rotation.json"
    assert repo.repo_root == tmp_path


def test_save_creates_the_bridge_state_directory_on_a_first_pass(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    repo = BindingRepository(tracker)
    repo.bindings["loc-A"] = {"jira_key": "DIG-A", "state": "confirmed"}

    repo.save()

    assert (_bridge(tmp_path) / "bindings.json").read_bytes() == _canonical(repo.data)
