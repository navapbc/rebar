"""Epic a4bd / story 248f: the durable per-link PEER-CONFIRMATION record.

``managed_refs`` proves "we own this ref", never "we pushed it" — it is set the
instant a link is created LOCALLY and is strictly monotonic. So the inbound
removal path cannot tell a never-pushed local link from one the peer genuinely
deleted. This suite pins the evidence store that supplies the missing
discriminator, and the three properties that make it trustworthy:

1. It is RELATION-SCOPED. The same ordered pair under a different relation is a
   different key — the outbound ADD dedup key ``(vendor_type, target_key)`` is
   direction-agnostic and cannot express this, which is exactly why the store
   does not reuse it.
2. It records only PROVEN synchronization. A ``set_relationship`` that raises
   records nothing; an unbound target records nothing rather than writing a
   vendor key into a local-id field.
3. It is strictly FAIL-OPEN and side-effect-free with respect to the caller. A
   corrupt store, an unopenable store, or a callback that raises must leave the
   outbound path's applied/computed counters byte-identical to the pre-a4bd
   behaviour — those counters feed the silent-no-op canary, so perturbing them
   would trade a link-safety bug for a reporting bug.

The counter-parity and raising-callback cases are the load-bearing ones: a naive
implementation that records inside the dispatch loop without swallowing would
fail an outbound link that ALREADY LANDED on the vendor.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def peer_confirmations():
    return importlib.import_module("rebar_reconciler.peer_confirmations")


@pytest.fixture
def dispatch_phases():
    return importlib.import_module("rebar_reconciler.dispatch_apply_phases")


@pytest.fixture
def dispatch_one():
    return importlib.import_module("rebar_reconciler.dispatch_one")


@pytest.fixture
def tracker(tmp_path: Path) -> Path:
    """A bare tracker dir — the store only needs the directory to exist."""
    d = tmp_path / ".tickets-tracker"
    (d / ".bridge_state").mkdir(parents=True)
    return d


_DEFAULT = object()


class _LinkClient:
    """A transport implementing the full ``SupportsLinks`` protocol surface.

    Every member is required: ``_capability_present`` uses a ``runtime_checkable``
    ``isinstance`` (backed by ``hasattr``), so a partial stub would be reported as
    capability-absent and the dispatch loop would never run at all.
    """

    def __init__(self, result=_DEFAULT, raises: bool = False) -> None:
        self.result = {"id": "10042"} if result is _DEFAULT else result
        self.raises = raises
        self.calls: list[tuple] = []

    def set_relationship(self, from_id, to_id, link_type="Blocks"):
        self.calls.append((from_id, to_id, link_type))
        if self.raises:
            raise RuntimeError("vendor refused the link")
        return self.result

    def get_issue_links(self, issue_key):
        return []

    def get_issuelinks_map(self, project_key):
        return {}

    def map_remote_links(self, remote_fields):
        return []

    def link_payload_for_relation(self, relation):
        return ("Blocks", False)


def _link_mutation():
    return {
        "local_id": "aaaa-1111-bbbb-2222",
        "links": [
            {
                "action": "add",
                "type": "Blocks",
                "to_key": "PROJ-9",
                "relation": "blocks",
                "swap": False,
            }
        ],
    }


# ---------------------------------------------------------------------------
# Store semantics
# ---------------------------------------------------------------------------


def test_relation_scoped_key_does_not_confirm_a_sibling_relation(peer_confirmations, tracker):
    """AC2. Confirming ``blocks`` must NOT confirm ``relates_to`` on the same pair.

    One ordered pair can hold two net-active differently-related deps
    (``add_dependency`` is idempotent per ``(target, relation)``), so a
    pair-scoped record would silently license removing the wrong one.
    """
    store = peer_confirmations.PeerConfirmationStore(str(tracker))
    store.record("A", "B", "blocks", link_id="1", pass_id="p1")

    assert store.is_confirmed("A", "B", "blocks") is True
    assert store.is_confirmed("A", "B", "relates_to") is False
    assert store.is_confirmed("B", "A", "blocks") is False


def test_corrupt_store_degrades_to_empty_and_does_not_raise(peer_confirmations, tracker):
    """AC8. Fail-OPEN: losing the evidence costs safety, raising costs the pass."""
    path = tracker / ".bridge_state" / "peer_confirmations.json"
    path.write_text("<<<<<<< HEAD\n{not json at all\n")

    store = peer_confirmations.PeerConfirmationStore(str(tracker))

    assert len(store) == 0
    assert store.is_confirmed("A", "B", "blocks") is False


def test_absent_store_is_empty_and_does_not_raise(peer_confirmations, tmp_path):
    store = peer_confirmations.PeerConfirmationStore(str(tmp_path / "nope"))
    assert len(store) == 0
    assert store.is_confirmed("A", "B", "blocks") is False


def test_records_round_trip_through_the_file(peer_confirmations, tracker):
    store = peer_confirmations.PeerConfirmationStore(str(tracker))
    store.record("A", "B", "blocks", link_id="77", pass_id="p9")
    store.save()

    reopened = peer_confirmations.PeerConfirmationStore(str(tracker))
    record = reopened.get("A", "B", "blocks")

    assert record is not None
    assert record["link_id"] == "77"
    assert record["confirmed_pass"] == "p9"
    assert reopened.is_confirmed("A", "B", "blocks") is True


def test_store_survives_snapshot_compaction_of_the_ticket_log(peer_confirmations, tmp_path):
    """AC7. The record must outlive ticket-log SNAPSHOT compaction.

    It does so STRUCTURALLY: the evidence is a ``.bridge_state`` sidecar, not
    reducer ``compiled_state``, so compaction cannot reach it. That is the whole
    reason the sidecar home was chosen over ticket state, and this test is what
    stops a later refactor from moving it into the event log where compaction
    would have to be taught about it.
    """
    import rebar

    repo = tmp_path / "repo"
    repo.mkdir()
    for argv in (
        ("git", "init", "-q", "-b", "main"),
        ("git", "config", "user.email", "t@example.com"),
        ("git", "config", "user.name", "T"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(argv, cwd=repo, check=True, capture_output=True)
    os.environ["REBAR_ROOT"] = str(repo)
    try:
        rebar.init_repo(repo_root=str(repo))
        a = str(rebar.create_ticket("task", "a4bd compaction source", repo_root=repo))
        b = str(rebar.create_ticket("task", "a4bd compaction target", repo_root=repo))
        rebar.link(a, b, "blocks", repo_root=repo)

        store = peer_confirmations.open_store(repo)
        store.record(a, b, "blocks", link_id="55", pass_id="p1")
        store.save()

        subprocess.run(
            ["rebar", "compact", a], cwd=repo, check=False, capture_output=True, text=True
        )

        reopened = peer_confirmations.open_store(repo)
        assert reopened.is_confirmed(a, b, "blocks") is True
    finally:
        os.environ.pop("REBAR_ROOT", None)


# ---------------------------------------------------------------------------
# Durability: the commit-back staging lists
# ---------------------------------------------------------------------------


def test_commit_only_peer_confirmations_is_staged_and_committed(tmp_path, monkeypatch):
    """AC9. A confirmation-ONLY change must actually commit.

    ``_commit_binding_store_snapshot`` stages a deliberate file set and gets its
    bug-1e08 per-file idempotency from the locked store seam's pathspec-scoped
    status (ticket 11a9-b11b). Missing the sidecar from ``_rel_files`` would make
    a confirmation-only pass a silent no-op, dropping the evidence on every
    pass. This test drives a real tracker repo where ONLY the sidecar changed
    and requires the commit to land.
    """
    helpers = importlib.import_module("rebar_reconciler.reconcile_helpers")
    git_adapter = importlib.import_module("rebar_reconciler.git_adapter")

    assert "PEER_CONFIRMATIONS_FILE" in git_adapter.__all__

    repo_root = tmp_path
    tracker_dir = repo_root / git_adapter.TRACKER_DIR
    (tracker_dir / ".bridge_state").mkdir(parents=True)
    (tracker_dir / git_adapter.PEER_CONFIRMATIONS_FILE).write_text('{"version": 1, "records": {}}')
    for argv in (
        ["git", "init", "-q", str(tracker_dir)],
        ["git", "-C", str(tracker_dir), "config", "user.name", "ac9-test"],
        ["git", "-C", str(tracker_dir), "config", "user.email", "ac9@test.invalid"],
    ):
        subprocess.run(argv, check=True, capture_output=True)

    ok = helpers._commit_binding_store_snapshot(object(), repo_root, "pass-1")

    assert ok is True
    show = subprocess.run(
        ["git", "-C", str(tracker_dir), "show", "--name-only", "--format=%s", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert git_adapter.PEER_CONFIRMATIONS_FILE in show.stdout, (
        "confirmation-only change was not committed — the sidecar is missing from "
        f"the staged file set. HEAD shows:\n{show.stdout}"
    )
    assert "pass-1" in show.stdout.splitlines()[0], (
        f"the snapshot commit message must name the pass, got {show.stdout.splitlines()[0]!r}"
    )


# ---------------------------------------------------------------------------
# The dispatch write point
# ---------------------------------------------------------------------------


def test_dispatch_records_a_confirmation_on_a_successful_link_add(dispatch_one, dispatch_phases):
    """AC3. A vendor-accepted ADD hands the evidence to the sink."""
    client = _LinkClient(result={"id": "10042"})
    seen: list[dict] = []

    computed, applied, failed = dispatch_one._update_one_dispatch_links(
        _link_mutation(),
        client,
        "PROJ-1",
        link_confirm=lambda **kw: seen.append(kw),
    )

    assert (computed, applied, failed) == (1, 1, 0)
    assert seen == [{"to_key": "PROJ-9", "relation": "blocks", "link_id": "10042"}]


def test_dispatch_records_nothing_when_set_relationship_raises(dispatch_one):
    """AC3. Only a LANDED link is evidence."""
    client = _LinkClient(raises=True)
    seen: list[dict] = []

    computed, applied, failed = dispatch_one._update_one_dispatch_links(
        _link_mutation(), client, "PROJ-1", link_confirm=lambda **kw: seen.append(kw)
    )

    assert (computed, applied, failed) == (1, 0, 1)
    assert seen == []


@pytest.mark.parametrize(
    "result",
    [{}, None, "not-a-mapping", {"id": None}],
    ids=["empty", "none", "scalar", "null-id"],
)
def test_dispatch_link_id_is_none_when_the_backend_supplies_no_id(dispatch_one, result):
    """AC4. A backend may legitimately return no link id; that is not a failure."""
    client = _LinkClient(result=result)
    seen: list[dict] = []

    _computed, applied, _failed = dispatch_one._update_one_dispatch_links(
        _link_mutation(), client, "PROJ-1", link_confirm=lambda **kw: seen.append(kw)
    )

    assert applied == 1
    assert seen[0]["link_id"] is None


def test_dispatch_counters_are_unchanged_when_the_callback_raises(dispatch_one):
    """AC10. The link already landed on the vendor — a bookkeeping failure must
    never retract it, and must never perturb the silent-no-op canary's inputs."""
    client = _LinkClient()

    def _boom(**_kw):
        raise RuntimeError("store is on fire")

    computed, applied, failed = dispatch_one._update_one_dispatch_links(
        _link_mutation(), client, "PROJ-1", link_confirm=_boom
    )

    assert (computed, applied, failed) == (1, 1, 0)


def test_dispatch_counter_parity_with_no_callback(dispatch_one):
    """AC6. ``link_confirm=None`` is byte-for-byte the pre-a4bd path."""
    client = _LinkClient()
    baseline = dispatch_one._update_one_dispatch_links(_link_mutation(), client, "PROJ-1")

    client2 = _LinkClient()
    with_sink = dispatch_one._update_one_dispatch_links(
        _link_mutation(), client2, "PROJ-1", link_confirm=lambda **_kw: None
    )

    assert baseline == with_sink == (1, 1, 0)
    assert client.calls == client2.calls


def test_dispatch_counter_parity_on_the_dedup_skip_path(dispatch_one, monkeypatch):
    """AC6. A deduped ADD is computed==0 (no false canary) and confirms nothing."""

    class _DedupClient(_LinkClient):
        def get_issue_links(self, issue_key):
            return [{"type": {"name": "Blocks"}, "outwardIssue": {"key": "PROJ-9"}}]

    monkeypatch.setattr(
        importlib.import_module("rebar_reconciler.dispatch_apply_phases"),
        "_index_existing_links",
        lambda links: {("Blocks", "PROJ-9")},
    )
    seen: list[dict] = []
    computed, applied, failed = dispatch_one._update_one_dispatch_links(
        _link_mutation(), _DedupClient(), "PROJ-1", link_confirm=lambda **kw: seen.append(kw)
    )

    assert (computed, applied, failed) == (0, 0, 0)
    assert seen == []


# ---------------------------------------------------------------------------
# The production wiring: handle_update's callback
# ---------------------------------------------------------------------------


class _BindingStore:
    def __init__(self, reverse: dict[str, str]) -> None:
        self._reverse = reverse

    def get_local_id(self, jira_key):
        return self._reverse.get(jira_key)


def _ctx(tmp_path, binding_store):
    handlers = importlib.import_module("rebar_reconciler.apply_handlers")
    return handlers.BatchApplyContext(
        client=object(),
        repo_root=tmp_path,
        pass_id="pass-77",
        binding_store=binding_store,
    )


def test_handle_update_callback_writes_a_local_id_keyed_record(tmp_path, peer_confirmations):
    """AC3/AC5. The dispatched entry carries JIRA keys; the record is keyed on LOCAL ids."""
    handlers = importlib.import_module("rebar_reconciler.apply_handlers")
    tracker = tmp_path / ".tickets-tracker"
    (tracker / ".bridge_state").mkdir(parents=True)

    ctx = _ctx(tmp_path, _BindingStore({"PROJ-9": "target-local-id"}))
    confirm = handlers._make_link_confirm(_link_mutation(), ctx)
    assert confirm is not None
    confirm(to_key="PROJ-9", relation="blocks", link_id="10042")

    store = peer_confirmations.open_store(tmp_path)
    record = store.get("aaaa-1111-bbbb-2222", "target-local-id", "blocks")

    assert record is not None
    assert record["direction"] == peer_confirmations.DIRECTION_OUTBOUND
    assert record["source"] == peer_confirmations.SOURCE_PUSH
    assert record["confirmed_pass"] == "pass-77"
    assert record["link_id"] == "10042"


def test_handle_update_callback_records_nothing_for_an_unbound_target(tmp_path, peer_confirmations):
    """AC5. Never write a vendor key into a local-id field; retry next pass instead."""
    handlers = importlib.import_module("rebar_reconciler.apply_handlers")
    tracker = tmp_path / ".tickets-tracker"
    (tracker / ".bridge_state").mkdir(parents=True)

    ctx = _ctx(tmp_path, _BindingStore({}))
    confirm = handlers._make_link_confirm(_link_mutation(), ctx)
    assert confirm is not None
    confirm(to_key="PROJ-9", relation="blocks", link_id="10042")

    store = peer_confirmations.open_store(tmp_path)
    assert len(store) == 0
    assert not (tracker / ".bridge_state" / "peer_confirmations.json").exists()


def test_handle_update_callback_is_none_without_a_binding_store(tmp_path):
    """Fail-open: no reverse map means no way to key the evidence, so record nothing."""
    handlers = importlib.import_module("rebar_reconciler.apply_handlers")
    assert handlers._make_link_confirm(_link_mutation(), _ctx(tmp_path, None)) is None


def test_handle_update_callback_is_none_without_a_local_id(tmp_path):
    handlers = importlib.import_module("rebar_reconciler.apply_handlers")
    ctx = _ctx(tmp_path, _BindingStore({"PROJ-9": "t"}))
    assert handlers._make_link_confirm({"links": []}, ctx) is None


def test_store_file_is_json_with_a_schema_version(tmp_path, peer_confirmations):
    store = peer_confirmations.open_store(tmp_path)
    store.record("A", "B", "blocks", pass_id="p1")
    store.save()

    payload = json.loads(
        (tmp_path / ".tickets-tracker" / ".bridge_state" / "peer_confirmations.json").read_text()
    )
    assert payload["version"] == peer_confirmations.SCHEMA_VERSION
    assert list(payload["records"]) == ["A|B|blocks"]
