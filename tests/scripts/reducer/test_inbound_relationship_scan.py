"""The inbound scan reads only the event files that can carry a relationship.

``find_inbound_relationships`` byte-scanned every event file in the store per call
(141,085 files, 609.5 MB on a copy of the live store; bug alabaster-hairy-limpet). Only
CREATE and EDIT (``parent_id``), LINK and UNLINK (``deps``) and SNAPSHOT (both) change a
ticket's relationships, so only those files are candidates.
"""

from __future__ import annotations

import builtins
import json
from pathlib import Path
from typing import Any

import pytest

from rebar.reducer import _inbound
from rebar.reducer._inbound import find_inbound_relationships

pytestmark = [pytest.mark.unit, pytest.mark.scripts]

TARGET = "aaaa-aaaa-aaaa-aaaa"
VIA_CREATE = "bbbb-bbbb-bbbb-0001"
VIA_EDIT = "bbbb-bbbb-bbbb-0002"
VIA_LINK = "bbbb-bbbb-bbbb-0003"
VIA_SNAPSHOT = "bbbb-bbbb-bbbb-0004"
UNLINKED = "bbbb-bbbb-bbbb-0005"
COMMENT_ONLY = "bbbb-bbbb-bbbb-0006"

_ALLOWED = ("-CREATE.json", "-EDIT.json", "-LINK.json", "-UNLINK.json", "-SNAPSHOT.json")
_ENV = "00000000-0000-4000-8000-000000000001"


class _Store:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.ts = 1_000
        self.n = 0

    def event(self, tid: str, event_type: str, data: dict[str, Any]) -> str:
        tdir = self.root / tid
        tdir.mkdir(parents=True, exist_ok=True)
        self.ts += 1
        self.n += 1
        uuid = f"00000000-0000-4000-8000-{self.n:012d}"
        payload = {
            "timestamp": self.ts,
            "uuid": uuid,
            "event_type": event_type,
            "env_id": _ENV,
            "author": "Test User",
            "data": data,
        }
        (tdir / f"{self.ts}-{uuid}-{event_type}.json").write_text(json.dumps(payload))
        return uuid

    def create(self, tid: str, **extra: Any) -> None:
        self.event(tid, "CREATE", {"ticket_type": "task", "title": f"t {tid}", **extra})


@pytest.fixture
def tracker(tmp_path: Path) -> str:
    store = _Store(tmp_path / "tracker")
    store.create(TARGET)
    store.event(TARGET, "COMMENT", {"body": "target's own note"})
    store.create(VIA_CREATE, parent_id=TARGET)
    store.create(VIA_EDIT)
    store.event(VIA_EDIT, "EDIT", {"fields": {"parent_id": TARGET}})
    store.create(VIA_LINK)
    store.event(VIA_LINK, "LINK", {"target_id": TARGET, "relation": "depends_on"})
    store.event(VIA_LINK, "STATUS", {"status": "in_progress", "current_status": "open"})
    # A compacted ticket: only its SNAPSHOT carries the relationship.
    store.event(
        VIA_SNAPSHOT,
        "SNAPSHOT",
        {
            "compiled_state": {
                "ticket_id": VIA_SNAPSHOT,
                "ticket_type": "task",
                "title": "compacted",
                "status": "open",
                "parent_id": None,
                "deps": [{"target_id": TARGET, "relation": "blocks", "link_uuid": "l-1"}],
            }
        },
    )
    store.create(UNLINKED)
    link = store.event(UNLINKED, "LINK", {"target_id": TARGET, "relation": "relates_to"})
    store.event(UNLINKED, "UNLINK", {"link_uuid": link, "target_id": TARGET})
    store.create(COMMENT_ONLY)
    store.event(COMMENT_ONLY, "COMMENT", {"body": f"see {TARGET} for context"})
    return str(store.root)


def test_each_relationship_event_type_is_still_found(tracker: str) -> None:
    result = find_inbound_relationships(TARGET, tracker)
    assert result["children"] == sorted([VIA_CREATE, VIA_EDIT])
    assert result["inbound_links"] == [
        {"from_id": VIA_LINK, "relation": "depends_on"},
        {"from_id": VIA_SNAPSHOT, "relation": "blocks"},
    ]


def test_a_comment_mention_is_not_a_candidate(tracker: str) -> None:
    candidates = _inbound._mentioning_dirs(tracker, TARGET)
    assert COMMENT_ONLY not in candidates
    assert set(candidates) >= {VIA_CREATE, VIA_EDIT, VIA_LINK, VIA_SNAPSHOT, UNLINKED}


def test_the_scan_opens_only_relationship_event_files(
    tracker: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    real_open = builtins.open

    def spy(path: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy)
    _inbound._mentioning_dirs(tracker, TARGET)
    assert opened, "the scan must open the relationship-bearing files"
    stray = [p for p in opened if not p.endswith(_ALLOWED)]
    assert stray == [], f"opened non-relationship event files: {stray}"


def test_preconditions_snapshots_are_not_scanned(
    tracker: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A ticket with no relationship to TARGET: its only mention is in a preconditions
    # snapshot, which is never replayed, so the scan must not open that file.
    plain = "bbbb-bbbb-bbbb-0007"
    _Store(Path(tracker)).create(plain)
    pre = Path(tracker) / plain / "9999-0000-PRECONDITIONS-SNAPSHOT.json"
    pre.write_text(json.dumps({"data": {"note": TARGET}}))
    opened: list[str] = []
    real_open = builtins.open

    def spy(path: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy)
    _inbound._mentioning_dirs(tracker, TARGET)
    assert str(pre) not in opened
