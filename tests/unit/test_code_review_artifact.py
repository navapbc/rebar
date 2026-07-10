"""Change-scoped code_review artifact type + reviewbot emit (story limestone-unethical-zebrafinch).

Covers the `code_review` ticket TYPE (mirrors session_log: excluded from default `list` /
dependency graph / Jira sync, searchable, gate-exempt, lifecycle-exempt, relates_to-only), the
diff-scoped `change_fingerprint` + norm_id-stamped payload, and the reviewbot
`emit_code_review_artifact` (create + trailer resolution → relates_to links, idempotent).

Proving command:
    .venv/bin/pytest tests/unit/test_code_review_artifact.py -v
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._errors import RebarError
from rebar.llm.code_review import sidecar

pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@e.com"),
        ("git", "config", "user.name", "t"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("REBAR_SIGNING_KEY", "test-signing-key-zeb")
    rebar.init_repo(repo_root=str(repo))
    return repo


# ── change_fingerprint ────────────────────────────────────────────────────────────────────
def test_change_fingerprint_is_stable_and_16_hex() -> None:
    a = sidecar.change_fingerprint("Iabc", "rev1", ["a.py", "b.py"], "diff X")
    b = sidecar.change_fingerprint("Iabc", "rev1", ["a.py", "b.py"], "diff X")
    assert a == b and len(a) == 16 and all(c in "0123456789abcdef" for c in a)


def test_change_fingerprint_is_file_order_insensitive() -> None:
    assert sidecar.change_fingerprint(
        "I", "r", ["a.py", "b.py"], "d"
    ) == sidecar.change_fingerprint("I", "r", ["b.py", "a.py"], "d")


def test_change_fingerprint_sensitive_to_each_input() -> None:
    base = sidecar.change_fingerprint("I", "r1", ["a.py"], "d1")
    assert base != sidecar.change_fingerprint("J", "r1", ["a.py"], "d1")  # change_id
    assert base != sidecar.change_fingerprint("I", "r2", ["a.py"], "d1")  # revision
    assert base != sidecar.change_fingerprint("I", "r1", ["b.py"], "d1")  # files
    assert base != sidecar.change_fingerprint("I", "r1", ["a.py"], "d2")  # diff text


# ── build_payload ─────────────────────────────────────────────────────────────────────────
def test_build_payload_carries_change_keys_and_norm_ids() -> None:
    verdict = {
        "verdict": "PASS",
        "blocking": [{"finding": "bug here", "criteria": ["tests"]}],
        "advisory": [{"finding": "nit", "criteria": ["docs"]}],
    }
    p = sidecar.build_payload(
        verdict, target_ticket="T1", change_id="Iabc", revision="r1", change_fp="deadbeefdeadbeef"
    )
    assert p["change_id"] == "Iabc"
    assert p["revision"] == "r1"
    assert p["change_fingerprint"] == "deadbeefdeadbeef"
    assert p["blocking"][0]["norm_id"].startswith("n")
    assert p["advisory"][0]["norm_id"].startswith("n")


# ── the code_review ticket TYPE (session_log mirror) ──────────────────────────────────────
def test_code_review_type_is_valid_and_hidden_from_default_list(store: Path) -> None:
    root = str(store)
    art = rebar.create_ticket("code_review", "code-review: I1 @ r1", repo_root=root)
    rebar.create_ticket("task", "a real task", repo_root=root)
    # Default list hides the artifact; an explicit type filter surfaces it.
    default_ids = {t.get("ticket_id") for t in rebar.list_tickets(repo_root=root)}
    assert art not in default_ids
    typed = rebar.list_tickets(ticket_type="code_review", repo_root=root)
    assert any(t.get("ticket_id") == art for t in typed)


def test_code_review_is_searchable(store: Path) -> None:
    root = str(store)
    art = rebar.create_ticket(
        "code_review", "code-review: Izzz @ rQ", description="findings galore", repo_root=root
    )
    hits = {t.get("ticket_id") for t in rebar.search("galore", repo_root=root)}
    assert art in hits


def test_code_review_excluded_from_jira_sync() -> None:
    # The reconciler config loads with its own sys.path (bash->Python strangler engine), so assert
    # the exclusion at the source level: code_review is in EXCLUDED_SYNC_TYPES and NOT in either
    # local->Jira type map (so any leak past the filter surfaces rather than silently syncing).
    from pathlib import Path

    cfg = Path("src/rebar/_engine/rebar_reconciler/config.py").read_text()
    assert '"code_review"' in cfg and "EXCLUDED_SYNC_TYPES" in cfg
    for m in ("outbound_fields.py", "reconcile_check.py"):
        assert "code_review" not in Path(f"src/rebar/_engine/rebar_reconciler/{m}").read_text()


def test_code_review_cannot_be_transitioned(store: Path) -> None:
    root = str(store)
    art = rebar.create_ticket("code_review", "code-review: I2 @ r2", repo_root=root)
    with pytest.raises(RebarError):  # lifecycle-exempt — transition is authoritatively refused
        rebar.transition(art, "open", "closed", repo_root=root)


def test_code_review_allows_relates_to_but_not_blocks(store: Path) -> None:
    root = str(store)
    art = rebar.create_ticket("code_review", "code-review: I3 @ r3", repo_root=root)
    task = rebar.create_ticket("task", "worked ticket", repo_root=root)
    rebar.link(art, task, "relates_to", repo_root=root)  # allowed
    with pytest.raises(RebarError):  # blocking link to an artifact is refused
        rebar.link(art, task, "blocks", repo_root=root)


def test_code_review_is_gate_exempt(store: Path) -> None:
    from rebar._engine_support.gates import clarity_check_compute

    result, code = clarity_check_compute("code_review", "no headings here", 5)
    assert result["verdict"] == "pass" and code == 0


# ── reviewbot emit_code_review_artifact ───────────────────────────────────────────────────
def _decision(verdict_val: str = "PASS") -> dict:
    return {
        "decision": verdict_val,
        "verdict": {"verdict": verdict_val, "blocking": [], "advisory": []},
    }


def test_emit_creates_artifact_and_links_resolvable_trailer(store: Path) -> None:
    from rebar.review_bot.voter import emit_code_review_artifact

    root = str(store)
    work = rebar.create_ticket("task", "cited work", return_alias=True, repo_root=root)
    msg = f"do the thing\n\nrebar-ticket: {work['alias']}\n"
    art = emit_code_review_artifact(
        _decision(),
        change_id="Ione",
        revision="r1",
        commit_message=msg,
        diff_text="+x",
        repo_root=root,
    )
    assert art is not None
    # the artifact is a code_review ticket, relates_to the cited work ticket
    linked = rebar.show_ticket(art, repo_root=root)
    rels = {d.get("target_id") for d in linked.get("deps", [])}
    assert work["id"] in rels


def test_emit_is_idempotent_per_change_revision(store: Path) -> None:
    from rebar.review_bot.voter import emit_code_review_artifact

    root = str(store)
    a1 = emit_code_review_artifact(
        _decision(),
        change_id="Isame",
        revision="rv",
        commit_message="x",
        diff_text="d",
        repo_root=root,
    )
    a2 = emit_code_review_artifact(
        _decision(),
        change_id="Isame",
        revision="rv",
        commit_message="x",
        diff_text="d",
        repo_root=root,
    )
    assert a1 == a2  # same (change_id, revision) reuses the same artifact


def test_emit_unresolved_trailer_is_nonfatal(store: Path) -> None:
    from rebar.review_bot.voter import emit_code_review_artifact

    root = str(store)
    msg = "feat: x\n\nrebar-ticket: nonexistent-ticket-zzz\n"
    art = emit_code_review_artifact(
        _decision(),
        change_id="Iunres",
        revision="r1",
        commit_message=msg,
        diff_text="d",
        repo_root=root,
    )
    # the artifact is still created; the unresolved ref is logged, not raised
    assert art is not None


def test_emit_skips_when_no_verdict(store: Path) -> None:
    from rebar.review_bot.voter import emit_code_review_artifact

    root = str(store)
    # a fail-closed review-error carries no verdict → nothing durable to persist
    art = emit_code_review_artifact(
        {"decision": "BLOCK", "verdict": {}},
        change_id="Ierr",
        revision="r1",
        commit_message="x",
        diff_text="d",
        repo_root=root,
    )
    assert art is None


# ── session_id + deps payload / collector / reader (story revenued-thickset-dassie) ───────────
def test_build_payload_carries_session_id_and_deps() -> None:
    """`session_id` + `deps` are read off the verdict; absent ⇒ None / {} (so the Gerrit path, which
    sets neither, degrades cleanly)."""
    with_fields = sidecar.build_payload(
        {"verdict": "PASS", "session_id": "sess1", "deps": {"a.py": "hh"}, "advisory": []},
        target_ticket="T1",
    )
    assert with_fields["session_id"] == "sess1"
    assert with_fields["deps"] == {"a.py": "hh"}
    without = sidecar.build_payload({"verdict": "PASS", "advisory": []}, target_ticket="T1")
    assert without["session_id"] is None
    assert without["deps"] == {}


def test_cited_paths_code_review_parses_location() -> None:
    """Paths are parsed from each finding's `location` string (stripping `:line`), over the
    blocking + advisory buckets only — code-review findings have no `citations[kind==file]` list."""
    verdict = {
        "blocking": [{"location": "src/a.py:42"}, {"location": "src/b.py"}],
        "advisory": [{"location": "src/c.py:7:3"}, {"location": ""}, {"nope": 1}],
        "coaching": [{"location": "src/never.py"}],  # not a surfaced bucket → excluded
    }
    assert sidecar._cited_paths_code_review(verdict) == {"src/a.py", "src/b.py", "src/c.py"}


def test_reviewed_file_hashes_absent_sentinel(store: Path) -> None:
    """The collector reuses `attest._hash_file`: a present file hashes to a sha256; a missing path
    hashes to the `absent` sentinel (so a create/delete is detectable)."""
    from rebar.llm.plan_review import attest

    root = str(store)
    (store / "present.py").write_text("x = 1\n")
    deps = sidecar.reviewed_file_hashes(["present.py", "gone.py"], repo_root=root)
    assert deps["gone.py"] == attest._ABSENT_HASH
    assert deps["present.py"] != attest._ABSENT_HASH and len(deps["present.py"]) == 64


def _emit_session_artifact(root: str, session_id: str, verdict: dict) -> str:
    import rebar

    title = f"code-review: session:{session_id}"
    art = rebar.create_ticket("code_review", title, return_alias=True, repo_root=root)
    aid = str(art["id"] if isinstance(art, dict) else art)
    sidecar.emit(verdict, target_ticket=aid, repo_root=root)
    return aid


def test_latest_code_review_result_session_key_surfaced_only(store: Path) -> None:
    """The reader resolves a `session:<id>` key to the exact-title artifact and returns SURFACED
    findings (blocking + advisory buckets, never coaching) plus the `deps` map."""
    root = str(store)
    _emit_session_artifact(
        root,
        "sess1",
        {
            "verdict": "BLOCK",
            "deps": {"src/a.py": "aaaa"},
            "blocking": [{"finding": "real bug", "location": "src/a.py:1"}],
            "advisory": [{"finding": "nit", "location": "src/a.py:2"}],
            "coaching": [{"move_id": "m1"}],
        },
    )
    got = sidecar.latest_code_review_result("session:sess1", repo_root=root)
    assert got is not None
    findings_text = {f.get("finding") for f in got["findings"]}
    assert findings_text == {"real bug", "nit"}  # surfaced only; coaching excluded
    assert got["deps"] == {"src/a.py": "aaaa"}


def test_latest_code_review_result_change_key_and_misses(store: Path) -> None:
    """`change:<id>` strips the tag and matches the `code-review: {id} @` title prefix (spanning
    revisions); an unknown key kind and an absent key both degrade to None (⇒ no drops)."""
    import rebar

    root = str(store)
    title = "code-review: Ichg @ rev2"
    art = rebar.create_ticket("code_review", title, return_alias=True, repo_root=root)
    aid = str(art["id"] if isinstance(art, dict) else art)
    sidecar.emit(
        {"verdict": "PASS", "deps": {"x.py": "hh"}, "advisory": [{"finding": "carry"}]},
        target_ticket=aid,
        change_id="Ichg",
        revision="rev2",
        repo_root=root,
    )
    got = sidecar.latest_code_review_result("change:Ichg", repo_root=root)
    assert got is not None and got["deps"] == {"x.py": "hh"}
    assert {f.get("finding") for f in got["findings"]} == {"carry"}
    # misses → None
    assert sidecar.latest_code_review_result("bogus:Ichg", repo_root=root) is None
    assert sidecar.latest_code_review_result("session:never", repo_root=root) is None
    assert sidecar.latest_code_review_result("", repo_root=root) is None


def test_local_session_artifact_does_not_seed_change_reader(store: Path) -> None:
    """Cross-keyspace isolation (epic super-path-bag success criterion; ADR 0037): a prior LOCAL
    session review must NOT contaminate a Gerrit CHANGE-keyed reader call — so a local review can
    never seed a change's FIRST Gerrit review. A `session:<id>` artifact with surfaced findings
    exists, yet a `change:<id>` query returns None (the two keyspaces are disjoint by title scheme);
    the same session key still resolves it, proving the artifact is present and it's the KEY TYPE —
    not absence — that isolates it."""
    root = str(store)
    _emit_session_artifact(
        root,
        "sess-iso",
        {
            "verdict": "BLOCK",
            "deps": {"a.py": "h"},
            "blocking": [{"finding": "local-only finding", "location": "a.py:1"}],
            "advisory": [],
        },
    )
    # A change-keyed read (any change id) finds NOTHING from the local session artifact.
    assert sidecar.latest_code_review_result("change:sess-iso", repo_root=root) is None
    assert sidecar.latest_code_review_result("change:some-change", repo_root=root) is None
    # Sanity: the SESSION key does resolve it — so it's the keyspace, not absence, that isolates.
    got = sidecar.latest_code_review_result("session:sess-iso", repo_root=root)
    assert got is not None and got["findings"][0]["finding"] == "local-only finding"
