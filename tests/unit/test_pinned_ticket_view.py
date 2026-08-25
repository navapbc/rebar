"""Behavioral contracts for the lazy, OID-pinned completion ticket view.

These tests deliberately use real temporary Git repositories.  The contract is about
immutable Git objects, resolver inputs, and descendant validation; mocking those seams would
not prove that a live tracker advance can coexist with a stable read at the older revision.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest

import rebar
from rebar import config
from rebar._engine_support.reads import use_ticket_view
from rebar._engine_support.ticket_query import TicketQuery
from rebar._snapshot.ticket_view import (
    CodeOID,
    CompletionReadBasis,
    PinnedTicketNotFound,
    PinnedTicketView,
    PinnedTicketViewError,
    TicketsOID,
    UnsupportedPinnedQuery,
    tracker_head,
    validate_receipt,
)

pytestmark = pytest.mark.unit


def _git(repo: str | Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    monkeypatch.setenv("REBAR_ROOT", str(root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-empty"))
    config.reset_config_cache()
    rebar.init_repo(repo_root=str(root))
    _git(root, "commit", "--allow-empty", "-q", "-m", "code root")
    return root


def _tracker(repo: Path) -> str:
    return str(config.tracker_dir(str(repo)))


def _commit_bindings(tracker: str, reverse: dict[str, str]) -> None:
    path = Path(tracker) / ".bridge_state" / "bindings.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"bindings": {}, "reverse": reverse}), encoding="utf-8")
    _git(tracker, "add", ".bridge_state/bindings.json")
    _git(tracker, "commit", "-q", "-m", "test: bindings")


def _write_raw_event(
    tracker: str,
    ticket_id: str,
    event_type: str,
    data: dict,
    *,
    timestamp: int,
) -> None:
    event_uuid = str(uuid.uuid4())
    event = {
        "event_type": event_type,
        "timestamp": timestamp,
        "uuid": event_uuid,
        "env_id": "eeee-0000-4000-8000-000000000001",
        "author": "test",
        "data": data,
    }
    path = Path(tracker) / ticket_id / f"{timestamp:020d}-{event_uuid}-{event_type}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(event), encoding="utf-8")


def test_pinned_state_remains_at_a_after_a_normal_live_write_b(repo: Path) -> None:
    observed = rebar.create_ticket("task", "observed at A", repo_root=str(repo))
    unrelated = rebar.create_ticket("task", "not demanded", repo_root=str(repo))
    tracker = _tracker(repo)
    oid_a = tracker_head(tracker)
    clean_before = _git(tracker, "status", "--porcelain")
    total_event_blobs = len(
        [
            path
            for path in _git(tracker, "ls-tree", "-r", "--name-only", oid_a.value).splitlines()
            if path.endswith(".json") and "/" in path
        ]
    )

    with PinnedTicketView.at_oid(tracker, oid_a, run_id="stable-a") as view:
        state_a = view.show_ticket(observed)
        receipt = view.receipt()

        rebar.comment(observed, "written only at B", repo_root=str(repo))
        live_b = rebar.show_ticket(observed, repo_root=str(repo))
        pinned_a = view.show_ticket(observed)

        assert [c["body"] for c in live_b["comments"]] == ["written only at B"]
        assert pinned_a == state_a
        assert pinned_a["comments"] == []
        assert view.metrics["ticket_object_reads"] < total_event_blobs
        assert unrelated not in view.receipt()["exact"]

    assert _git(tracker, "status", "--porcelain") == clean_before
    validation = validate_receipt(tracker, receipt)
    assert validation.valid is False
    assert f"ticket:{observed}" in validation.conflicts


def test_runtime_missing_read_is_negative_at_a_and_invalidates_when_created_at_b(
    repo: Path,
) -> None:
    rebar.create_ticket("task", "seed", repo_root=str(repo))
    tracker = _tracker(repo)
    oid_a = tracker_head(tracker)

    with PinnedTicketView.at_oid(tracker, oid_a, run_id="negative-a") as view:
        created_at_b = rebar.create_ticket("task", "created after pin", repo_root=str(repo))
        alias_b = rebar.show_ticket(created_at_b, repo_root=str(repo))["alias"]

        with pytest.raises(PinnedTicketNotFound, match="not found at tickets OID"):
            view.show_ticket(alias_b)
        receipt = view.receipt()
        assert receipt["negative"] == [alias_b]
        assert receipt["resolutions"][alias_b]["value"] is None

    validation = validate_receipt(tracker, receipt)
    assert validation.valid is False
    assert f"resolution:{alias_b}" in validation.conflicts


def test_jira_resolution_and_its_global_binding_are_pinned_and_revalidated(
    repo: Path,
) -> None:
    first = rebar.create_ticket("task", "first binding", repo_root=str(repo))
    second = rebar.create_ticket("task", "second binding", repo_root=str(repo))
    tracker = _tracker(repo)
    _commit_bindings(tracker, {"REB-42": first})
    oid_a = tracker_head(tracker)

    with PinnedTicketView.at_oid(tracker, oid_a, run_id="jira-a") as view:
        assert view.show_ticket("REB-42")["ticket_id"] == first
        # Jira resolution has the same case-insensitive compatibility as the live resolver.
        assert view.show_ticket("reb-42")["ticket_id"] == first
        receipt = view.receipt()

    _commit_bindings(tracker, {"REB-42": second})
    validation = validate_receipt(tracker, receipt)
    assert validation.valid is False
    assert "resolution:REB-42" in validation.conflicts
    assert "resolution:reb-42" in validation.conflicts


def test_multilevel_hierarchy_and_link_predicates_detect_relevant_drift(repo: Path) -> None:
    root = rebar.create_ticket("story", "root", repo_root=str(repo))
    child = rebar.create_ticket("task", "child", parent=root, repo_root=str(repo))
    grandchild = rebar.create_ticket("task", "grandchild", parent=child, repo_root=str(repo))
    linked = rebar.create_ticket("task", "linked", repo_root=str(repo))
    rebar.link(child, linked, "relates_to", repo_root=str(repo))
    tracker = _tracker(repo)

    with PinnedTicketView.at_oid(tracker, tracker_head(tracker), run_id="graph-a") as view:
        assert [item["ticket_id"] for item in view.transitive_descendants(root)] == [
            child,
            grandchild,
        ]
        assert view.inbound_links(linked) == [(child, "relates_to", "open")]
        assert view.relation_reachable(child, linked, relations=["relates_to"]) is True
        receipt = view.receipt()

    new_descendant = rebar.create_ticket(
        "task", "great-grandchild", parent=grandchild, repo_root=str(repo)
    )
    rebar.link(root, linked, "relates_to", repo_root=str(repo))
    validation = validate_receipt(tracker, receipt)

    assert validation.valid is False
    assert "descendants:" + root in validation.conflicts
    assert "direct_children:" + grandchild in validation.conflicts
    assert "inbound:" + linked in validation.conflicts
    assert new_descendant in {
        item["ticket_id"] for item in rebar.list_tickets(parent=grandchild, repo_root=str(repo))
    }


def test_unrelated_descendant_write_does_not_invalidate_a_demanded_read(repo: Path) -> None:
    observed = rebar.create_ticket("task", "observed", repo_root=str(repo))
    unrelated = rebar.create_ticket("task", "unrelated", repo_root=str(repo))
    tracker = _tracker(repo)

    with PinnedTicketView.at_oid(tracker, tracker_head(tracker), run_id="unrelated-a") as view:
        view.show_ticket(observed)
        receipt = view.receipt()

    rebar.comment(unrelated, "unrelated descendant commit", repo_root=str(repo))
    validation = validate_receipt(tracker, receipt)
    assert validation.valid is True
    assert validation.conflicts == ()


def test_graph_scans_do_not_promote_unobserved_ticket_fields_into_the_receipt(
    repo: Path,
) -> None:
    parent = rebar.create_ticket("story", "graph parent", repo_root=str(repo))
    child = rebar.create_ticket("task", "real child", parent=parent, repo_root=str(repo))
    target = rebar.create_ticket("task", "link target", repo_root=str(repo))
    source = rebar.create_ticket("task", "link source", repo_root=str(repo))
    false_positive = rebar.create_ticket("task", "grep-only candidate", repo_root=str(repo))
    rebar.link(source, target, "relates_to", repo_root=str(repo))
    rebar.comment(
        false_positive,
        f"mentions {parent} but is neither a child nor a demanded ticket",
        repo_root=str(repo),
    )
    tracker = _tracker(repo)

    with PinnedTicketView.at_oid(tracker, tracker_head(tracker)) as view:
        assert [item["ticket_id"] for item in view.direct_children(parent)] == [child]
        assert view.inbound_links(target) == [(source, "relates_to", "open")]
        assert view.relation_reachable(source, target, relations=["relates_to"]) is True
        receipt = view.receipt()
        assert false_positive not in receipt["exact"]
        assert source not in receipt["exact"]

    rebar.comment(false_positive, "irrelevant grep-candidate edit", repo_root=str(repo))
    rebar.comment(source, "irrelevant to its link and status", repo_root=str(repo))
    validation = validate_receipt(tracker, receipt)
    assert validation.valid is True
    assert validation.conflicts == ()


def test_material_fingerprint_records_child_membership_and_status_not_full_state(
    repo: Path,
) -> None:
    from rebar.llm.completion_child_gate import (
        build_child_closure_evidence,
        child_closure_findings,
    )
    from rebar.llm.plan_review.attest import current_material_fingerprint

    parent = rebar.create_ticket("story", "material parent", repo_root=str(repo))
    child = rebar.create_ticket("task", "material child", parent=parent, repo_root=str(repo))
    archived = rebar.create_ticket(
        "task", "archived material child", parent=parent, repo_root=str(repo)
    )
    rebar.archive(archived, repo_root=str(repo))
    tracker = _tracker(repo)

    with PinnedTicketView.at_oid(tracker, tracker_head(tracker)) as view:
        with use_ticket_view(view):
            blocking, uncertified = child_closure_findings(parent, str(repo))
            assert len(blocking) == 1
            assert blocking[0]["criterion"] == f"direct child {child} is closed"
            assert uncertified == []
            assert "This ticket has 1 direct child ticket(s)." in build_child_closure_evidence(
                parent, str(repo), uncertified
            )
            assert current_material_fingerprint(parent, repo_root=str(repo))
        receipt = view.receipt()
        assert child not in receipt["exact"]
        assert "status" in receipt["fields"][child]

    rebar.comment(child, "does not change plan membership", repo_root=str(repo))
    assert validate_receipt(tracker, receipt).valid is True

    rebar.transition(child, "open", "in_progress", repo_root=str(repo))
    validation = validate_receipt(tracker, receipt)
    assert validation.valid is False
    assert f"field:{child}:status" in validation.conflicts


def test_nonancestor_ticket_history_is_rejected(repo: Path) -> None:
    ticket = rebar.create_ticket("task", "receipt base", repo_root=str(repo))
    tracker = _tracker(repo)
    with PinnedTicketView.at_oid(tracker, tracker_head(tracker)) as view:
        view.show_ticket(ticket)
        receipt = view.receipt()

    empty_tree = _git(tracker, "rev-parse", "HEAD^{tree}")
    orphan = subprocess.run(
        ["git", "-C", tracker, "commit-tree", empty_tree],
        input="unrelated root\n",
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    validation = validate_receipt(tracker, receipt, current_oid=TicketsOID(orphan))
    assert validation.valid is False
    assert validation.conflicts == ("non_ancestor_tickets_history",)


def test_typed_oids_reject_swapped_code_and_ticket_handles(repo: Path) -> None:
    ticket = rebar.create_ticket("task", "typed roots", repo_root=str(repo))
    tracker = _tracker(repo)
    tickets_oid = tracker_head(tracker)
    code_oid = CodeOID(_git(repo, "rev-parse", "HEAD"))

    with pytest.raises(TypeError, match="TicketsOID"):
        PinnedTicketView.at_oid(tracker, code_oid)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="TicketsOID"):
        validate_receipt(tracker, {}, current_oid=code_oid)  # type: ignore[arg-type]

    with PinnedTicketView.at_oid(tracker, tickets_oid) as view:
        view.show_ticket(ticket)
        basis = view.completion_basis(code_oid)
        with pytest.raises(TypeError, match="CodeOID"):
            view.completion_basis(tickets_oid)  # type: ignore[arg-type]

    swapped = basis.to_dict()
    swapped["code_oid"], swapped["tickets_oid"] = (
        swapped["tickets_oid"],
        swapped["code_oid"],
    )
    with pytest.raises(ValueError, match="different tickets OIDs"):
        CompletionReadBasis.from_dict(swapped)


def test_parent_bounded_queries_work_and_broad_queries_fail_closed(repo: Path) -> None:
    parent = rebar.create_ticket("story", "parent", repo_root=str(repo))
    child = rebar.create_ticket("task", "child", parent=parent, repo_root=str(repo))
    tracker = _tracker(repo)

    with PinnedTicketView.at_oid(tracker, tracker_head(tracker)) as view:
        states = view.list_by_query(TicketQuery(parent=parent, ticket_type="task"))
        assert [state["ticket_id"] for state in states] == [child]
        with pytest.raises(UnsupportedPinnedQuery, match="parent-bounded"):
            view.list_by_query(TicketQuery(status="open"))
        with pytest.raises(UnsupportedPinnedQuery, match="aggregate/blocking"):
            view.list_by_query(TicketQuery(parent=parent, min_children=1))


def test_rollout_switch_defaults_off_and_can_be_enabled(repo: Path) -> None:
    assert config.compose_config(str(repo)).verify.completion_pinned_ticket_view is False

    (repo / "rebar.toml").write_text(
        "[verify]\ncompletion_pinned_ticket_view = true\n", encoding="utf-8"
    )
    config.reset_config_cache()
    assert config.compose_config(str(repo)).verify.completion_pinned_ticket_view is True


def test_rollout_retains_materialized_path_when_push_is_not_synchronous(repo: Path) -> None:
    from rebar.llm.completion import _pinned_ticket_view_selection

    (repo / "rebar.toml").write_text(
        "[verify]\ncompletion_pinned_ticket_view = true\n[sync]\npush = 'async'\n",
        encoding="utf-8",
    )
    config.reset_config_cache()

    assert _pinned_ticket_view_selection(str(repo)) == (False, "materialized_push_async")


def test_verify_completion_threads_one_pinned_view_through_library_and_agent_reads(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar import _reads
    from rebar.llm import completion, pai_tools

    ticket = rebar.create_ticket("task", "threaded stable view", repo_root=str(repo))
    (repo / "rebar.toml").write_text(
        "[verify]\ncompletion_pinned_ticket_view = true\n[sync]\npush = 'always'\n",
        encoding="utf-8",
    )
    config.reset_config_cache()
    captured_oid: TicketsOID | None = None
    observed: dict[str, dict] = {}
    real_capture = PinnedTicketView.try_capture

    def capture_then_advance(cls, repo_root, *, fetch, run_id=None):
        nonlocal captured_oid
        view = real_capture(repo_root, fetch=fetch, run_id=run_id)
        assert view is not None
        captured_oid = view.tickets_oid
        rebar.comment(ticket, "visible only after the pin", repo_root=str(repo))
        return view

    def fake_verify(ticket_id, **kwargs):
        cfg = kwargs["config"]
        assert cfg.ticket_view is not None
        observed["library"] = _reads.show_ticket(ticket_id, repo_root=kwargs["repo_root"])
        show_tool = pai_tools.rebar_tools(
            cfg.tickets_path or cfg.repo_path,
            allow_comment=False,
            ticket_view=cfg.ticket_view,
        )[0]
        observed["agent"] = json.loads(show_tool(ticket_id))
        return {
            "verdict": "PASS",
            "findings": [],
            "criteria": [],
            "runner": "threading-contract",
            "model": "threading-contract",
            "certifiable": True,
            "metrics": {},
        }

    monkeypatch.setattr(PinnedTicketView, "try_capture", classmethod(capture_then_advance))
    monkeypatch.setattr(completion, "_verify_completion_inner", fake_verify)

    result = completion.verify_completion(
        ticket,
        ref="HEAD",
        source="attested",
        fetch=False,
        repo_root=str(repo),
    )

    assert captured_oid is not None
    assert captured_oid != tracker_head(_tracker(repo))
    assert observed["library"]["comments"] == []
    assert observed["agent"]["comments"] == []
    assert rebar.show_ticket(ticket, repo_root=str(repo))["comments"][0]["body"] == (
        "visible only after the pin"
    )
    assert result["ticket_read_mode"] == "lazy_pinned"
    assert result["completion_read_basis"]["tickets_oid"] == captured_oid.value


def test_verify_completion_reuses_an_owned_ticket_session_without_recapture_or_close(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar import _reads
    from rebar.llm import completion

    ticket = rebar.create_ticket("task", "caller-owned stable view", repo_root=str(repo))
    (repo / "rebar.toml").write_text(
        "[verify]\ncompletion_pinned_ticket_view = true\n[sync]\npush = 'always'\n",
        encoding="utf-8",
    )
    config.reset_config_cache()
    tracker = _tracker(repo)
    view = PinnedTicketView.at_oid(tracker, tracker_head(tracker), run_id="one-close-session")

    def unexpected_recapture(*_args, **_kwargs):
        raise AssertionError("a supplied close session must not capture another tickets OID")

    def fake_verify(ticket_id, **kwargs):
        assert kwargs["config"].ticket_view is view
        return {
            "verdict": "PASS",
            "findings": [],
            "criteria": [],
            "runner": "owned-session-contract",
            "model": "owned-session-contract",
            "certifiable": True,
            "metrics": {},
            "observed_title": _reads.show_ticket(ticket_id)["title"],
        }

    monkeypatch.setattr(PinnedTicketView, "try_capture", classmethod(unexpected_recapture))
    monkeypatch.setattr(completion, "_verify_completion_inner", fake_verify)

    result = completion.verify_completion(
        ticket,
        ref="HEAD",
        source="attested",
        fetch=False,
        repo_root=str(repo),
        ticket_view=view,
    )

    assert result["observed_title"] == "caller-owned stable view"
    assert result["completion_read_basis"]["tickets_oid"] == view.tickets_oid.value
    assert view.show_ticket(ticket)["title"] == "caller-owned stable view"
    view.close()


def test_active_pinned_session_rejects_unmodeled_library_reads(repo: Path) -> None:
    from rebar import _reads
    from rebar._engine_support import reads as ticket_reads

    ticket = rebar.create_ticket("task", "tripwire", repo_root=str(repo))
    tracker = _tracker(repo)
    with PinnedTicketView.at_oid(tracker, tracker_head(tracker)) as view:
        with ticket_reads.use_ticket_view(view):
            operations = (
                lambda: _reads.deps(ticket, repo_root=str(repo)),
                lambda: _reads.ready(repo_root=str(repo)),
                lambda: _reads.next_batch(ticket, repo_root=str(repo)),
                lambda: _reads.search("tripwire", repo_root=str(repo)),
                lambda: _reads.recent_session_logs(repo_root=str(repo)),
            )
            for operation in operations:
                with pytest.raises(UnsupportedPinnedQuery, match="pinned completion"):
                    operation()


def test_receipt_binds_view_and_reducer_schema_even_without_tracker_advance(repo: Path) -> None:
    ticket = rebar.create_ticket("task", "receipt schema", repo_root=str(repo))
    tracker = _tracker(repo)
    with PinnedTicketView.at_oid(tracker, tracker_head(tracker)) as view:
        view.show_ticket(ticket)
        receipt = view.receipt()

    assert receipt["view_schema_version"] == 1
    assert isinstance(receipt["reducer_schema_version"], int)
    receipt["reducer_schema_version"] += 1
    validation = validate_receipt(tracker, receipt)
    assert validation.valid is False
    assert validation.conflicts == ("receipt_schema_mismatch",)


def test_completion_basis_receipt_is_deeply_immutable(repo: Path) -> None:
    ticket = rebar.create_ticket("task", "immutable receipt", repo_root=str(repo))
    tracker = _tracker(repo)
    code_oid = CodeOID(_git(repo, "rev-parse", "HEAD"))
    with PinnedTicketView.at_oid(tracker, tracker_head(tracker)) as view:
        view.show_ticket(ticket)
        basis = view.completion_basis(code_oid)

    with pytest.raises(TypeError):
        basis.receipt["resolutions"][ticket]["value"] = "changed"
    assert basis.to_dict()["receipt"]["resolutions"][ticket]["value"] == ticket
    assert validate_receipt(tracker, basis.receipt).valid is True


def test_link_reduction_has_full_alias_short_and_jira_target_parity(repo: Path) -> None:
    target = rebar.create_ticket("task", "link target parity", repo_root=str(repo))
    target_state = rebar.show_ticket(target, repo_root=str(repo))
    raw_targets = (target, target_state["alias"], target[:9], "REB-999")
    sources = [
        rebar.create_ticket("task", f"raw link {index}", repo_root=str(repo))
        for index in range(len(raw_targets))
    ]
    tracker = _tracker(repo)
    _commit_bindings(tracker, {"REB-999": target})
    for index, (source, raw_target) in enumerate(zip(sources, raw_targets, strict=True)):
        event_uuid = str(uuid.uuid4())
        timestamp = 1_700_000_000_000_000_000 + index
        event = {
            "event_type": "LINK",
            "timestamp": timestamp,
            "uuid": event_uuid,
            "env_id": "eeee-0000-4000-8000-000000000001",
            "author": "test",
            "data": {"target_id": raw_target, "relation": "relates_to"},
        }
        path = Path(tracker) / source / f"{timestamp:020d}-{event_uuid}-LINK.json"
        path.write_text(json.dumps(event), encoding="utf-8")
    _git(tracker, "add", *sources)
    _git(tracker, "commit", "-q", "-m", "test: raw link reference forms")

    with PinnedTicketView.at_oid(tracker, tracker_head(tracker)) as view:
        for source in sources:
            deps = view.show_ticket(source)["deps"]
            assert [dep["target_id"] for dep in deps] == [target]
        assert view.inbound_links(target) == [
            (source, "relates_to", "open") for source in sorted(sources)
        ]


def test_ambiguous_alias_link_resolution_is_independent_of_prior_reads(repo: Path) -> None:
    tracker = _tracker(repo)
    first = "aaaa-bbbb-cccc-dddd"
    second = "eeee-ffff-1111-2222"
    source = "9999-8888-7777-6666"
    shared_alias = "shared-ambiguous-alias"
    base = 1_700_000_100_000_000_000
    _write_raw_event(
        tracker,
        first,
        "CREATE",
        {"ticket_type": "task", "title": "first", "alias": shared_alias},
        timestamp=base,
    )
    _write_raw_event(
        tracker,
        second,
        "CREATE",
        {"ticket_type": "task", "title": "second", "alias": shared_alias},
        timestamp=base + 1,
    )
    _write_raw_event(
        tracker,
        source,
        "CREATE",
        {"ticket_type": "task", "title": "source", "alias": "raw-link-source"},
        timestamp=base + 2,
    )
    _write_raw_event(
        tracker,
        source,
        "LINK",
        {"target_id": shared_alias, "relation": "relates_to"},
        timestamp=base + 3,
    )
    _git(tracker, "add", first, second, source)
    _git(tracker, "commit", "-q", "-m", "test: ambiguous raw alias")

    live = rebar.show_ticket(source, repo_root=str(repo))
    assert [dep["target_id"] for dep in (live.get("deps") or [])] == [shared_alias]
    with PinnedTicketView.at_oid(tracker, tracker_head(tracker)) as view:
        assert view.resolve(shared_alias) is None
        view.show_ticket(first)
        pinned = view.show_ticket(source)
        assert [dep["target_id"] for dep in pinned["deps"]] == [shared_alias]


def test_corrupt_event_json_fails_closed(repo: Path) -> None:
    ticket = rebar.create_ticket("task", "corrupt object", repo_root=str(repo))
    tracker = _tracker(repo)
    event_uuid = str(uuid.uuid4())
    path = Path(tracker) / ticket / f"1700000000000000000-{event_uuid}-COMMENT.json"
    path.write_text("{not-json", encoding="utf-8")
    _git(tracker, "add", str(path.relative_to(tracker)))
    _git(tracker, "commit", "-q", "-m", "test: corrupt event")

    with PinnedTicketView.at_oid(tracker, tracker_head(tracker)) as view:
        with pytest.raises(PinnedTicketViewError, match="corrupt JSON"):
            view.show_ticket(ticket)


def test_tree_listed_ticket_blob_missing_from_object_database_fails_closed(repo: Path) -> None:
    ticket = rebar.create_ticket("task", "missing object", repo_root=str(repo))
    tracker = _tracker(repo)
    create_path = next(Path(tracker, ticket).glob("*-CREATE.json")).relative_to(tracker)
    blob_oid = _git(tracker, "rev-parse", f"HEAD:{create_path.as_posix()}")
    object_root = Path(_git(tracker, "rev-parse", "--git-path", "objects"))
    if not object_root.is_absolute():
        object_root = Path(tracker) / object_root
    loose_object = object_root / blob_oid[:2] / blob_oid[2:]
    assert loose_object.is_file()
    loose_object.unlink()

    with PinnedTicketView.at_oid(tracker, tracker_head(tracker)) as view:
        with pytest.raises(PinnedTicketViewError, match="missing Git object"):
            view.show_ticket(ticket)


def test_non_regular_ticket_tree_entry_fails_closed(repo: Path) -> None:
    ticket = rebar.create_ticket("task", "unsafe mode", repo_root=str(repo))
    tracker = _tracker(repo)
    event_uuid = str(uuid.uuid4())
    path = Path(tracker) / ticket / f"1700000000000000000-{event_uuid}-COMMENT.json"
    path.symlink_to("missing-event-target")
    _git(tracker, "add", str(path.relative_to(tracker)))
    _git(tracker, "commit", "-q", "-m", "test: symlink event")

    with PinnedTicketView.at_oid(tracker, tracker_head(tracker)) as view:
        with pytest.raises(PinnedTicketViewError, match="unsupported object mode"):
            view.show_ticket(ticket)


def test_completion_prechecks_and_resumptions_share_the_captured_ticket_revision(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar._commands import close_precheck

    ticket = rebar.create_ticket(
        "task",
        "stable deterministic checks",
        description="## Acceptance Criteria\n- [x] implemented",
        repo_root=str(repo),
    )
    (repo / "rebar.toml").write_text(
        "[verify]\n"
        "require_completion_verification_for_close = true\n"
        "completion_pinned_ticket_view = true\n"
        "[sync]\npush = 'always'\n",
        encoding="utf-8",
    )
    config.reset_config_cache()
    real_capture = PinnedTicketView.try_capture
    captured: PinnedTicketView | None = None

    def capture_then_make_live_state_ineligible(cls, repo_root, *, fetch, run_id=None):
        nonlocal captured
        captured = real_capture(repo_root, fetch=fetch, run_id=run_id)
        assert captured is not None
        rebar.edit_ticket(
            ticket,
            description="## Acceptance Criteria\n- [ ] changed after capture",
            repo_root=str(repo),
        )
        return captured

    def fake_verify(ticket_id, **kwargs):
        assert ticket_id == ticket
        assert kwargs["ticket_view"] is captured
        return {
            "verdict": "PASS",
            "findings": [],
            "criteria": [],
            "runner": "stable-close-session",
            "model": "stable-close-session",
            "certifiable": True,
        }

    monkeypatch.setattr(
        PinnedTicketView, "try_capture", classmethod(capture_then_make_live_state_ineligible)
    )
    monkeypatch.setattr("rebar.llm.verify_completion", fake_verify)
    monkeypatch.setattr(close_precheck, "_emit_completion_sidecar", lambda *_a, **_k: True)

    result, expectation = close_precheck._completion_precheck(
        ticket,
        "task",
        str(repo),
        str(repo),
        reason="",
        force_close="",
        ref="HEAD",
    )

    assert result is not None and result["verdict"] == "PASS"
    assert expectation == "required"
    assert captured is not None
    with pytest.raises(PinnedTicketViewError, match="closed"):
        captured.show_ticket(ticket)


def test_completion_precheck_preserves_materialized_mode_selected_at_operation_start(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar._commands import close_precheck
    from rebar.llm import completion

    ticket = rebar.create_ticket(
        "task",
        "fixed read mode",
        description="## Acceptance Criteria\n- [x] implemented",
        repo_root=str(repo),
    )
    (repo / "rebar.toml").write_text(
        "[verify]\nrequire_completion_verification_for_close = true\n",
        encoding="utf-8",
    )
    config.reset_config_cache()
    observed: dict[str, object] = {}

    monkeypatch.setattr(
        completion,
        "capture_completion_ticket_view",
        lambda *_args, **_kwargs: (None, "materialized_push_async"),
    )

    def fake_run(ticket_id: str, **kwargs):
        observed.update(kwargs)
        return {
            "verdict": "PASS",
            "findings": [],
            "criteria": [],
            "runner": "fixed-mode",
            "model": "fixed-mode",
            "certifiable": True,
        }

    monkeypatch.setattr(close_precheck, "_verify_with_duration_metrics", fake_run)
    monkeypatch.setattr(close_precheck, "_emit_completion_sidecar", lambda *_a, **_k: True)

    result, expectation = close_precheck._completion_precheck(
        ticket,
        "task",
        str(repo),
        str(repo),
        reason="",
        force_close="",
        ref="HEAD",
    )

    assert result is not None and result["verdict"] == "PASS"
    assert expectation == "required"
    assert observed["ticket_read_mode"] == "materialized_push_async"
