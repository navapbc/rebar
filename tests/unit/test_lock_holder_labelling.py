"""A lock timeout must NAME the compaction sweep holding the lock, not a bare pid
(camerashy-erectable-frog).

Field evidence: a sweep held the store write lock for 24+ minutes; the blocked writer saw
only ``holder: host=... pid=... held=47s`` — a bare pid — and had to reach for ``lsof``/``ps``
to learn it was compaction and therefore safe to SIGTERM. These tests pin the fix: an optional
``op=`` label on the v2 ownership stamp, set AMBIENTLY by the sweep via a ``contextvars``
label, rendered into ``describe_lock_holder`` and the ``LockTimeout`` message, together with a
stated safe-to-interrupt remedy — while the v2 forward-compat contract (colon-free line,
unknown fields ignored) and the label-blind reclamation logic are preserved.
"""

from __future__ import annotations

from rebar._commands import compact, compact_trigger
from rebar._store import lock as _lock
from rebar._store import lock_owner as _owner


def _seed_stamp(tmp_path, stamp: str) -> str:
    lock_dir = tmp_path / _lock.MKDIR_LOCK_NAME
    lock_dir.mkdir(exist_ok=True)
    (lock_dir / _owner._MKDIR_OWNER_FILE).write_text(stamp)
    return str(lock_dir)


# ------------------------------------------------------------------- the stamp gets a label


def test_owner_stamp_carries_op_label_only_inside_the_context():
    """The label is ambient: set inside ``operation_label`` and cleared on exit, so an
    ordinary writer's stamp is unchanged."""
    assert " op=" not in _owner._owner_stamp()
    with _owner.operation_label("compact-sweep"):
        labelled = _owner._owner_stamp()
    assert "op=compact-sweep" in labelled
    assert " op=" not in _owner._owner_stamp()


def test_labelled_stamp_stays_colon_free_and_single_token():
    """The v2 line must stay colon-free (legacy-reader refusal) and the label must remain a
    single ``key=value`` token even when the raw label carries ``:``/``=``/whitespace."""
    with _owner.operation_label("weird:op=name here"):
        stamp = _owner._owner_stamp()
    assert ":" not in stamp
    fields = _owner._parse_v2_stamp(stamp)
    assert fields is not None and "op" in fields
    assert ":" not in fields["op"]
    assert "=" not in fields["op"]
    assert " " not in fields["op"]
    # Still a well-formed v2 stamp — the required fields survive.
    assert {"host", "ns", "pid", "start"} <= fields.keys()


def test_parse_v2_ignores_an_unknown_extra_field():
    """Forward compat the other way: a NEWER rebar's stamp with a field this reader does
    not know is accepted, its known fields intact."""
    stamp = _owner._owner_stamp() + " futurefield=xyz"
    fields = _owner._parse_v2_stamp(stamp)
    assert fields is not None
    assert {"host", "ns", "pid", "start"} <= fields.keys()
    assert fields.get("futurefield") == "xyz"


# ---------------------------------------------------------------- describe_lock_holder render


def test_describe_lock_holder_renders_the_op_label(tmp_path):
    with _owner.operation_label("compact-sweep"):
        _seed_stamp(tmp_path, _owner._owner_stamp())
    assert "op=compact-sweep" in _lock.describe_lock_holder(str(tmp_path))


def test_unlabelled_holder_renders_without_op(tmp_path):
    """An ordinary writer sees no format change: no ``op=`` in the rendering."""
    _seed_stamp(tmp_path, _owner._owner_stamp())
    assert "op=" not in _lock.describe_lock_holder(str(tmp_path))


# ------------------------------------------------------------------- LockTimeout remedy text


def test_lock_timeout_states_the_safe_remedy_for_a_sweep():
    holder = "host=name-h pid=98440 start=1 held=181s pid_state=live op=compact-sweep"
    msg = str(_lock.LockTimeout(181, holder))
    assert "op=compact-sweep" in msg
    assert "safe to interrupt" in msg
    assert "loses no data" in msg


def test_lock_timeout_has_no_remedy_for_an_ordinary_holder():
    holder = "host=name-h pid=4321 start=1 held=48s pid_state=live"
    msg = str(_lock.LockTimeout(48, holder))
    assert "safe to interrupt" not in msg
    assert "loses no data" not in msg


# ------------------------------------------------------------- reclamation ignores the label


def test_reclamation_is_label_blind(tmp_path, monkeypatch):
    """A labelled stale lock and an unlabelled stale lock adjudicate identically — the
    ``op=`` field never feeds a staleness decision."""
    monkeypatch.setattr(_owner, "_pid_alive", lambda _pid: False)  # dead ⇒ stale
    plain = tmp_path / "plain.lock"
    plain.write_text(_owner._owner_stamp())
    with _owner.operation_label("compact-sweep"):
        labelled_stamp = _owner._owner_stamp()
    labelled = tmp_path / "labelled.lock"
    labelled.write_text(labelled_stamp)
    assert _owner.stamped_file_is_stale(str(plain)) is True
    assert _owner.stamped_file_is_stale(str(labelled)) == _owner.stamped_file_is_stale(str(plain))


# --------------------------------------------------------- end-to-end through real acquisition


def test_real_acquire_under_label_stamps_the_op(tmp_path):
    """The ambient contextvar reaches ``_owner_stamp`` through the ACTUAL acquisition path
    (the same ``lock.acquire`` the sweep drives via ``_compact_locked``)."""
    with _owner.operation_label("compact-sweep"):
        handle = _lock.acquire(str(tmp_path), timeout=2, attempts=1)
        try:
            holder = _lock.describe_lock_holder(str(tmp_path))
        finally:
            handle.release()
    assert "op=compact-sweep" in holder


# ------------------------------------------------------- the sweep entry points set the label


def test_trigger_run_sweep_sets_the_ambient_label(tmp_path, monkeypatch):
    monkeypatch.setattr(compact_trigger, "_acquire_trigger_lock", lambda t: 3)
    monkeypatch.setattr(compact_trigger, "release_trigger_lock", lambda t, fd: None)
    monkeypatch.setattr(_lock, "write_lock_is_busy", lambda t: False)
    seen: dict[str, str | None] = {}

    def fake_all(argv, *, repo_root=None):
        seen["label"] = _owner._operation_label.get()
        return 0

    monkeypatch.setattr(compact, "compact_all_cli", fake_all)
    compact_trigger.run_sweep(str(tmp_path))
    assert seen["label"] == "compact-sweep"


def test_compact_run_sweep_sets_the_ambient_label(monkeypatch):
    monkeypatch.setattr(compact, "_sweep_position_map", lambda repo_root: {})
    monkeypatch.setattr(compact._lock, "write_lock_is_busy", lambda t: False)
    seen: dict[str, str | None] = {}

    def fake_one(tracker, tid, repo_root, position_commits, *, no_commit=False):
        seen["label"] = _owner._operation_label.get()
        return "-"

    monkeypatch.setattr(compact, "_sweep_one_ticket", fake_one)
    compact._run_sweep("tracker", ["t1"], None)
    assert seen["label"] == "compact-sweep"
