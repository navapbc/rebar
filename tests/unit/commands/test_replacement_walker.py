"""Test the shared replacement-link walk used by close disposition.

The walk checks outbound ``duplicates`` targets before inbound ``supersedes`` sources. Its
callers retain distinct non-bug narrowing, recorded-versus-live failure policies, and return
shapes. Reader seams are patched so the real walk remains under test.
"""

from __future__ import annotations

import pathlib
import re

import pytest
from _tree_scan import parsed_python_files

import rebar
from rebar._commands import close_disposition, close_precheck

_BUG = "1111-2222-3333-4444"
_CANON = "5555-6666-7777-8888"


class _Store:
    """Scripted reduce/inbound results, in the c8fd shape."""

    def __init__(self, subject: dict | None, inbound: list[dict], others: dict | None = None):
        self.subject = subject
        self.inbound = inbound
        self.others = others or {}
        self.subject_raises = False

    def reduce(self, path: str, *a, **k):
        tid = str(path).rstrip("/").rsplit("/", 1)[-1]
        if tid == _BUG:
            if self.subject_raises:
                raise OSError("unreadable subject")
            return self.subject
        return self.others.get(tid)


def _wire(monkeypatch: pytest.MonkeyPatch, store: _Store) -> None:
    """Patch the READERS, exactly as tests/unit/test_close_precheck_duplicate_link_c8fd.py:63-72
    does — never the walkers themselves, so the real walk is what runs. ``resolve_ticket_id`` is
    part of that seam: ``_is_live_ticket`` resolves before it reduces."""
    from rebar import reducer as _reducer
    from rebar._engine_support import resolver as _resolver
    from rebar.reducer import _inbound

    monkeypatch.setattr(_reducer, "reduce_ticket", store.reduce)
    monkeypatch.setattr(
        _inbound, "find_inbound_relationships", lambda *a, **k: {"inbound_links": store.inbound}
    )
    known = {_BUG, *store.others}
    monkeypatch.setattr(
        _resolver, "resolve_ticket_id", lambda tid, *a, **k: tid if tid in known else None
    )


def _live(tid: str) -> dict:
    return {"ticket_id": tid, "status": "open"}


# Directions.
def test_the_walker_finds_a_live_outbound_duplicates_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The common case: this bug ``duplicates`` a canonical ticket that is still usable."""
    store = _Store(
        subject={"deps": [{"relation": "duplicates", "target_id": _CANON}]},
        inbound=[],
        others={_CANON: _live(_CANON)},
    )
    _wire(monkeypatch, store)
    assert close_disposition.find_replacement(_BUG, "duplicate", str(tmp_path)) == _CANON


def test_the_walker_finds_a_live_inbound_supersedes_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The other direction: another ticket ``supersedes`` this one. Directional, not symmetric."""
    store = _Store(
        subject={"deps": []},
        inbound=[{"relation": "supersedes", "from_id": _CANON}],
        others={_CANON: _live(_CANON)},
    )
    _wire(monkeypatch, store)
    assert close_disposition.find_replacement(_BUG, "superseded", str(tmp_path)) == _CANON


# Policies.
def test_recorded_mode_returns_a_dead_target_that_live_mode_rejects(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The entire reason two liveness modes exist. A link naming an ARCHIVED target earns a
    different operator remedy ("re-link to a live canonical") from having recorded nothing at
    all ("run rebar link"), so the weaker question must keep answering it."""
    store = _Store(
        subject={"deps": [{"relation": "duplicates", "target_id": _CANON}]},
        inbound=[],
        others={_CANON: {"ticket_id": _CANON, "status": "archived", "archived": True}},
    )
    _wire(monkeypatch, store)

    assert close_precheck._recorded_replacement_target(_BUG, str(tmp_path)) == _CANON
    assert close_disposition.find_replacement(_BUG, "duplicate", str(tmp_path)) is None
    assert (
        close_precheck._has_live_replacement_link(_BUG, "bug", "duplicate", str(tmp_path)) is False
    )


def test_the_non_bug_administrative_narrowing_is_preserved(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Only the predicate narrows a NON-BUG ticket to ADMINISTRATIVE_CLASSES (ticket fc20):
    ``not_a_bug`` and ``escalated`` are bug-only vocabulary. ``find_replacement`` has no
    ``ticket_type`` parameter at all, so it is strictly more permissive — merging the two
    would silently widen which non-bug closes are exempt."""
    store = _Store(
        subject={"deps": [{"relation": "duplicates", "target_id": _CANON}]},
        inbound=[],
        others={_CANON: _live(_CANON)},
    )
    _wire(monkeypatch, store)

    assert "not_a_bug" not in close_disposition.ADMINISTRATIVE_CLASSES
    assert (
        close_precheck._has_live_replacement_link(_BUG, "story", "not_a_bug", str(tmp_path))
        is False
    )
    assert (
        close_precheck._has_live_replacement_link(_BUG, "bug", "not_a_bug", str(tmp_path)) is True
    )


def test_recorded_mode_continues_to_inbound_after_a_subject_reduce_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """``_recorded_replacement_target`` deliberately does NOT early-return when the subject is
    unreadable — it degrades to the inbound pass so the operator still learns a replacement was
    recorded. The two live-mode callers fail CLOSED on the same input."""
    store = _Store(
        subject=None,
        inbound=[{"relation": "supersedes", "from_id": _CANON}],
        others={_CANON: _live(_CANON)},
    )
    store.subject_raises = True
    _wire(monkeypatch, store)

    assert close_precheck._recorded_replacement_target(_BUG, str(tmp_path)) == _CANON
    assert close_disposition.find_replacement(_BUG, "superseded", str(tmp_path)) is None


def test_a_non_replacement_class_never_walks(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Negative control: a class outside the disposition vocabulary returns None even when a
    perfectly good live link exists — the class guard runs BEFORE the walk."""
    store = _Store(
        subject={"deps": [{"relation": "duplicates", "target_id": _CANON}]},
        inbound=[],
        others={_CANON: _live(_CANON)},
    )
    _wire(monkeypatch, store)
    assert close_disposition.find_replacement(_BUG, "fixed", str(tmp_path)) is None


def test_the_three_seams_keep_their_names_and_signatures() -> None:
    """Keep the three monkeypatch seams and ``find_replacement`` positional arity stable.

    The 738a tests patch one seam with ``raising=False`` and call the finder positionally, so
    silent API drift would invalidate their coverage.
    """
    import inspect

    for mod, name in (
        (close_precheck, "_has_live_replacement_link"),
        (close_precheck, "_recorded_replacement_target"),
        (close_disposition, "find_replacement"),
    ):
        assert hasattr(mod, name), f"{mod.__name__}.{name} is a monkeypatch seam and must survive"

    # find_replacement is called POSITIONALLY at 738a:174 and stubbed as
    # `lambda tid, cc, tracker` in three places — its arity is pinned.
    params = list(inspect.signature(close_disposition.find_replacement).parameters)
    assert params[:3] == ["ticket_id", "close_class", "tracker"], params


def test_the_disposition_class_sets_cannot_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    """The story's third criterion: the two sets are derived, not restated, so they cannot
    drift. Equality is what 738a asserts today; identity is strictly stronger and is what
    makes the drift structurally impossible."""
    assert close_precheck._NON_COMPLETION_BUG_CLASSES is close_disposition.DISPOSITION_CLASSES, (
        "the two sets must be the SAME object, not merely equal"
    )


def test_the_dead_target_remedy_differs_from_the_named_none_remedy(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """END TO END through the real guard. A duplicate close naming an archived canonical must
    say "re-link it to a live one" and NAME the dead target; one recording no link at all must
    say "records no 'duplicates' link". Bug 9b70: the one action that works must be named."""
    from rebar._commands._seam import CommandError

    dead = _Store(
        subject={"deps": [{"relation": "duplicates", "target_id": _CANON}]},
        inbound=[],
        others={_CANON: {"ticket_id": _CANON, "status": "archived", "archived": True}},
    )
    _wire(monkeypatch, dead)
    with pytest.raises(CommandError) as dead_exc:
        close_precheck._ensure_duplicate_close_is_linked(_BUG, "bug", "duplicate", str(tmp_path))
    assert _CANON in dead_exc.value.message
    assert "re-link" in dead_exc.value.message.lower()

    none = _Store(subject={"deps": []}, inbound=[])
    _wire(monkeypatch, none)
    with pytest.raises(CommandError) as none_exc:
        close_precheck._ensure_duplicate_close_is_linked(_BUG, "bug", "duplicate", str(tmp_path))
    assert "records no 'duplicates' link" in none_exc.value.message
    assert _CANON not in none_exc.value.message


# Uniqueness guard.

_SRC = pathlib.Path(rebar.__file__).resolve().parent
_OWNER = "_commands/close_disposition.py"
_WALK_ATOMS = ("find_inbound_relationships(", '"supersedes"')
_WALK_OK_RE = re.compile(r"#\s*replacement-walk-ok:(.*)$", re.MULTILINE)


def _replacement_walk_offenders() -> list[str]:
    """Find modules containing both walk atoms without a reasoned escape marker.

    Either atom alone is legitimate; only their conjunction identifies a duplicate walk. A
    bare ``replacement-walk-ok`` marker remains an offence.
    """
    offenders: list[str] = []
    for module in parsed_python_files(_SRC):
        rel = module.path.relative_to(_SRC).as_posix()
        if rel == _OWNER:  # the owner may refactor freely; uniqueness is about COPIES
            continue
        text = module.source
        if not all(atom in text for atom in _WALK_ATOMS):
            continue
        excused = False
        for line in text.splitlines():
            if not any(atom in line for atom in _WALK_ATOMS):
                continue
            marker = _WALK_OK_RE.search(line)
            if marker is not None and marker.group(1).strip():
                excused = True
                break
        if not excused:
            offenders.append(rel)
    return offenders


def test_the_replacement_link_walk_has_exactly_one_body() -> None:
    """Prevent another walker without constraining the owner's implementation."""
    assert _replacement_walk_offenders() == [], (
        "the duplicates/supersedes walk was re-implemented outside "
        f"{_OWNER}; call rebar._commands.close_disposition.replacement_of instead, or mark "
        "the line '# replacement-walk-ok: <reason>': " + repr(_replacement_walk_offenders())
    )


def test_the_owner_module_carries_the_reasoned_escape_marker() -> None:
    """Require the owner's escape marker to carry a reason, as all exceptions must."""
    owner_text = (_SRC / _OWNER).read_text(encoding="utf-8")
    owner_marker = _WALK_OK_RE.search(owner_text)
    assert owner_marker is not None, f"{_OWNER} should document its ownership with the marker"
    assert owner_marker.group(1).strip(), "the owner's marker must carry a reason"

    assert _WALK_OK_RE.search("x = 1  # replacement-walk-ok: legacy shim").group(1).strip()
    assert not _WALK_OK_RE.search("x = 1  # replacement-walk-ok:").group(1).strip()
