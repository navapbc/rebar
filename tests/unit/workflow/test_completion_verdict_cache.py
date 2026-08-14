"""Cross-run PASS-verdict cache for the completion verifier (ticket 8d74-2c9f-c98f-4f0b).

Re-verification after exhaustion used to re-prove EVERY criterion (~10 min/cycle). The cache
persists each validated PASS verdict at finalize under
``.rebar/cache/completion_verdicts/<ticket>/<criterion-hash>.json``, keyed by (criterion-text
hash, scoped content fingerprint), and SEEDS still-valid entries into the next run's
run-scoped CriterionBank (stamped ``seeded: true``). PASS-only by design: insufficiency/FAIL
records are never cached — the cache can only credit what an earlier validated run proved.

Fingerprint scope pins (all RED-first):
* own ``file_impact`` → git blob SHAs of those paths (an in-scope edit rotates the blob and
  invalidates; an unrelated commit does NOT — the whole-repo tree sha would).
* absent path → sentinel; git failure → None (reuse disabled for the run, fail-open to
  re-verification).
* empty own impact + children → union of DIRECT-child file_impact blobs.
* childless + empty impact → whole-tree sha fallback.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._config_schema import VerifyConfig
from rebar.llm.workflow import completion_banking as cb
from rebar.llm.workflow import completion_verdict_cache as cvc

pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "t@e.com"),
        ("config", "user.name", "t"),
        ("commit", "-q", "--allow-empty", "-m", "root"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return repo


def _write_and_commit(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", rel], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", f"touch {rel}"], cwd=repo, check=True, capture_output=True
    )


def _seed_ticket(repo: Path, *, impact: list[str] | None = None, parent: str | None = None) -> str:
    tid = rebar.create_ticket(
        "task",
        "cache me",
        description="Body.\n\n## Acceptance Criteria\n- [ ] the fix works\n- [ ] docs updated\n",
        parent=parent,
        repo_root=str(repo),
    )
    if impact:
        rebar.set_file_impact(
            tid, [{"path": p, "reason": "test"} for p in impact], repo_root=str(repo)
        )
    return tid


def _ticket(repo: Path, tid: str) -> dict:
    return rebar.show_ticket(tid, repo_root=str(repo))


# ── scoped content fingerprint ──────────────────────────────────────────────────────
def test_fingerprint_from_own_file_impact_blobs(store: Path) -> None:
    """The fingerprint hashes the file_impact paths' BLOB shas: stable across calls,
    rotated by an in-scope edit, NOT rotated by an unrelated commit."""
    repo = store
    _write_and_commit(repo, "src/a.py", "a = 1\n")
    _write_and_commit(repo, "src/b.py", "b = 1\n")
    tid = _seed_ticket(repo, impact=["src/a.py", "src/b.py"])
    ticket = _ticket(repo, tid)

    fp1 = cvc.scoped_content_fingerprint(ticket, [], str(repo))
    assert fp1 is not None
    assert fp1 == cvc.scoped_content_fingerprint(ticket, [], str(repo))

    _write_and_commit(repo, "unrelated.txt", "noise\n")  # outside file_impact
    assert cvc.scoped_content_fingerprint(ticket, [], str(repo)) == fp1

    _write_and_commit(repo, "src/a.py", "a = 2\n")  # in-scope edit → blob rotates
    assert cvc.scoped_content_fingerprint(ticket, [], str(repo)) != fp1


def test_fingerprint_absent_path_uses_sentinel(store: Path) -> None:
    repo = store
    _write_and_commit(repo, "src/a.py", "a = 1\n")
    tid = _seed_ticket(repo, impact=["src/a.py", "src/missing.py"])
    ticket = _ticket(repo, tid)

    fp = cvc.scoped_content_fingerprint(ticket, [], str(repo))
    assert fp is not None
    # materializing the missing path changes the fingerprint (sentinel → real blob)
    _write_and_commit(repo, "src/missing.py", "m = 1\n")
    assert cvc.scoped_content_fingerprint(ticket, [], str(repo)) != fp


def test_fingerprint_git_failure_disables_reuse(tmp_path: Path) -> None:
    ticket = {"file_impact": ["src/a.py"]}
    nongit = tmp_path / "nongit"
    nongit.mkdir()
    assert cvc.scoped_content_fingerprint(ticket, [], str(nongit)) is None


def test_fingerprint_epic_unions_direct_child_impacts(store: Path) -> None:
    """Empty own file_impact + children → the union of DIRECT-child file_impact blobs."""
    repo = store
    _write_and_commit(repo, "src/c1.py", "c1 = 1\n")
    _write_and_commit(repo, "src/c2.py", "c2 = 1\n")
    parent = _seed_ticket(repo)
    c1 = _seed_ticket(repo, impact=["src/c1.py"], parent=parent)
    _seed_ticket(repo, impact=["src/c2.py"], parent=parent)
    assert c1
    ticket = _ticket(repo, parent)
    children = cvc.direct_children(parent, str(repo))
    assert len(children) == 2

    fp = cvc.scoped_content_fingerprint(ticket, children, str(repo))
    assert fp is not None
    _write_and_commit(repo, "src/c1.py", "c1 = 2\n")  # a child's in-scope edit rotates it
    assert cvc.scoped_content_fingerprint(ticket, children, str(repo)) != fp


def test_fingerprint_childless_empty_impact_falls_back_to_tree(store: Path) -> None:
    repo = store
    tid = _seed_ticket(repo)
    ticket = _ticket(repo, tid)

    fp = cvc.scoped_content_fingerprint(ticket, [], str(repo))
    assert fp is not None and fp.startswith("tree:")
    _write_and_commit(repo, "anything.txt", "x\n")  # ANY commit rotates the tree fallback
    assert cvc.scoped_content_fingerprint(ticket, [], str(repo)) != fp


def test_fingerprint_children_without_impacts_disables_reuse(store: Path) -> None:
    """Childful + no impacts anywhere → None (the tree fallback is ONLY for childless)."""
    repo = store
    parent = _seed_ticket(repo)
    _seed_ticket(repo, parent=parent)
    ticket = _ticket(repo, parent)
    children = cvc.direct_children(parent, str(repo))

    assert cvc.scoped_content_fingerprint(ticket, children, str(repo)) is None


def test_direct_child_count_fails_open(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = store
    parent = _seed_ticket(repo)
    _seed_ticket(repo, parent=parent)
    assert cvc.direct_child_count(parent, str(repo)) == 1

    def _boom(*args, **kwargs):
        raise RuntimeError("store read failed")

    monkeypatch.setattr("rebar._reads.list_tickets", _boom)
    assert cvc.direct_child_count(parent, str(repo)) == 0


# ── persist on validated PASS at finalize ───────────────────────────────────────────
def _verdict(criteria: list[dict]) -> dict:
    return {"verdict": "PASS", "criteria": criteria, "findings": []}


def test_persist_writes_only_validated_pass_records(store: Path) -> None:
    repo = store
    _write_and_commit(repo, "src/a.py", "a = 1\n")
    tid = _seed_ticket(repo, impact=["src/a.py"])
    expected = ["the fix works", "docs updated"]
    ids = cb.criterion_id_map(expected)
    bank_entries = {
        ids["the fix works"]: {"criterion_id": ids["the fix works"], "met": True, "evidence": "E1"},
        ids["docs updated"]: {
            "criterion_id": ids["docs updated"],
            "met": False,
            "evidence": "gap",
            "evidence_sufficient": False,
        },
    }
    merged = _verdict(
        [
            {"criterion": "the fix works", "criterion_id": ids["the fix works"], "met": True},
            {
                "criterion": "docs updated",
                "criterion_id": ids["docs updated"],
                "met": False,
                "evidence_sufficient": False,
            },
        ]
    )

    written = cvc.persist_pass_verdicts(tid, merged, bank_entries, str(repo))

    assert written == 1
    cache = cvc.cache_dir(str(repo), tid)
    files = sorted(cache.glob("*.json"))
    assert [f.name for f in files] == [f"{cvc.criterion_cache_key('the fix works')}.json"]
    entry = json.loads(files[0].read_text(encoding="utf-8"))
    assert entry["met"] is True and entry["criterion"] == "the fix works"
    assert entry["evidence"] == "E1"
    assert entry["fingerprint"]
    assert not list(cache.glob("*.tmp")), "atomic tmp+rename must leave no residue"


def test_persist_downgraded_or_unverified_records_are_never_cached(store: Path) -> None:
    """A bank PASS the finalizer downgraded, and an unverified placeholder, are not cached."""
    repo = store
    _write_and_commit(repo, "src/a.py", "a = 1\n")
    tid = _seed_ticket(repo, impact=["src/a.py"])
    ids = cb.criterion_id_map(["the fix works"])
    bank_entries = {
        ids["the fix works"]: {"criterion_id": ids["the fix works"], "met": True, "evidence": "E"}
    }
    merged = _verdict(
        [
            {
                "criterion": "the fix works",
                "criterion_id": ids["the fix works"],
                "met": False,  # finalizer downgrade wins same-run → must not be cached
            }
        ]
    )

    assert cvc.persist_pass_verdicts(tid, merged, bank_entries, str(repo)) == 0
    assert not list(cvc.cache_dir(str(repo), tid).glob("*.json"))


# ── load + seeding ──────────────────────────────────────────────────────────────────
def _persist_pass(repo: Path, tid: str, text: str = "the fix works") -> None:
    ids = cb.criterion_id_map([text])
    bank_entries = {ids[text]: {"criterion_id": ids[text], "met": True, "evidence": "E1"}}
    merged = _verdict([{"criterion": text, "criterion_id": ids[text], "met": True}])
    assert cvc.persist_pass_verdicts(tid, merged, bank_entries, repo_root=str(repo)) == 1


def test_still_valid_entries_load_and_seed_the_bank(store: Path) -> None:
    repo = store
    _write_and_commit(repo, "src/a.py", "a = 1\n")
    tid = _seed_ticket(repo, impact=["src/a.py"])
    _persist_pass(repo, tid)
    expected = ["the fix works", "docs updated"]
    ids = cb.criterion_id_map(expected)
    ticket = _ticket(repo, tid)

    loaded = cvc.load_valid_pass_entries(tid, ticket, expected, str(repo))
    assert set(loaded) == {"the fix works"}

    bank = cb.CriterionBank.for_run("run-1", cb.resolve_bank_stamps(tid, str(repo)))
    seeded = cvc.seed_bank_from_cache(bank, tid, ticket, expected, ids, str(repo))
    assert seeded == frozenset({ids["the fix works"]})
    entry = bank.get(ids["the fix works"])
    assert entry is not None and entry["met"] is True and entry.get("seeded") is True


def test_in_scope_edit_invalidates_cached_pass(store: Path) -> None:
    """RED AC: a file under file_impact changes (ticket text unchanged) → blob sha rotates →
    the cached PASS is invalid; an unrelated commit does NOT invalidate."""
    repo = store
    _write_and_commit(repo, "src/a.py", "a = 1\n")
    tid = _seed_ticket(repo, impact=["src/a.py"])
    _persist_pass(repo, tid)
    expected = ["the fix works"]
    ticket = _ticket(repo, tid)

    _write_and_commit(repo, "elsewhere.txt", "noise\n")
    assert set(cvc.load_valid_pass_entries(tid, ticket, expected, str(repo))) == {"the fix works"}

    _write_and_commit(repo, "src/a.py", "a = 2\n")
    assert cvc.load_valid_pass_entries(tid, ticket, expected, str(repo)) == {}


def test_criterion_text_change_invalidates_cached_pass(store: Path) -> None:
    repo = store
    _write_and_commit(repo, "src/a.py", "a = 1\n")
    tid = _seed_ticket(repo, impact=["src/a.py"])
    _persist_pass(repo, tid)
    ticket = _ticket(repo, tid)

    assert cvc.load_valid_pass_entries(tid, ticket, ["the fix REALLY works"], str(repo)) == {}


def test_seeded_ids_are_omitted_from_the_primary_manifest() -> None:
    expected = ["the fix works", "docs updated"]
    ids = cb.criterion_id_map(expected)
    full = cb.primary_criteria_manifest(expected, ids)
    assert ids["the fix works"] in full and ids["docs updated"] in full

    pruned = cb.primary_criteria_manifest(
        expected, ids, seeded_ids=frozenset({ids["the fix works"]})
    )
    assert ids["the fix works"] not in pruned
    assert ids["docs updated"] in pruned

    all_seeded = cb.primary_criteria_manifest(expected, ids, seeded_ids=frozenset(ids.values()))
    assert all_seeded == ""


def test_seeded_context_block_directs_the_primary_to_skip() -> None:
    expected = ["the fix works"]
    ids = cb.criterion_id_map(expected)
    block = cvc.seeded_context_block(expected, ids)
    assert "already credited" in block and "do not re-verify" in block
    assert ids["the fix works"] in block and "the fix works" in block
    assert cvc.seeded_context_block([], ids) == ""


# ── merge semantics: seeded records bypass finalizer judgment ───────────────────────
def _merge(result: dict, criteria: list[str], entries: dict) -> dict:
    return cb.merge_finalizer_with_bank(
        result, criteria, entries, id_by_text=cb.criterion_id_map(criteria)
    )


def test_finalizer_cannot_downgrade_or_drop_a_seeded_record() -> None:
    criteria = ["the fix works", "docs updated"]
    ids = cb.criterion_id_map(criteria)
    entries = {
        ids["the fix works"]: {
            "criterion_id": ids["the fix works"],
            "met": True,
            "evidence": "cached E",
            "seeded": True,
        },
        ids["docs updated"]: {
            "criterion_id": ids["docs updated"],
            "met": True,
            "evidence": "cached D",
            "seeded": True,
        },
    }
    result = {
        "verdict": "FAIL",
        # echo DOWNGRADES the first seeded record; the second is OMITTED entirely.
        "criteria": [{"criterion": "the fix works", "met": False, "evidence": "hedge"}],
    }

    merged = _merge(result, criteria, entries)

    by_text = {r["criterion"]: r for r in merged["criteria"]}
    assert by_text["the fix works"]["met"] is True
    assert by_text["the fix works"]["evidence"] == "cached E"
    assert by_text["docs updated"]["met"] is True
    assert merged["verdict"] == "PASS"


def test_same_run_finalizer_downgrade_stays_preserved() -> None:
    """Regression: the seeded bypass keys STRICTLY on the seeded flag — a same-run
    (non-seeded) bank PASS the finalizer downgraded stays downgraded."""
    criteria = ["the fix works"]
    ids = cb.criterion_id_map(criteria)
    entries = {
        ids["the fix works"]: {"criterion_id": ids["the fix works"], "met": True, "evidence": "E"}
    }
    result = {
        "verdict": "FAIL",
        "criteria": [{"criterion": "the fix works", "met": False, "evidence": "contradicted"}],
    }

    merged = _merge(result, criteria, entries)

    assert merged["criteria"][0]["met"] is False
    assert merged["verdict"] == "FAIL"


# ── run-scoped bank isolation (regression) ──────────────────────────────────────────
def test_run_scoped_banks_stay_isolated_across_concurrent_closes(store: Path) -> None:
    repo = store
    tid = _seed_ticket(repo)
    stamps = cb.resolve_bank_stamps(tid, str(repo))
    a = cb.CriterionBank.for_run("run-a", stamps)
    b = cb.CriterionBank.for_run("run-b", stamps)

    a.upsert("c00-aaaaaaaa", True, "A")
    assert b.get("c00-aaaaaaaa") is None
    assert set(a.banked_ids()) == {"c00-aaaaaaaa"} and b.banked_ids() == set()


# ── budget: runaway backstop unchanged under the larger budget ──────────────────────
def test_looping_tool_still_trips_backstop_under_larger_budget() -> None:
    """The recalibrated (larger) budget does not weaken the runaway guard: a looping tool
    fixture running under the FULL recalibrated clamp (960 steps → 480 requests) is still
    aborted by loop detection long before the budget runs out."""
    pytest.importorskip("pydantic_ai")
    from dataclasses import replace

    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from rebar.llm.completion import verify_step_floor
    from rebar.llm.config import LLMConfig
    from rebar.llm.errors import RunawayToolLoopError
    from rebar.llm.runner import PydanticAIRunner, RunRequest

    assert verify_step_floor(40, VerifyConfig()) == 960  # the larger clamp is in force
    cfg = replace(LLMConfig.from_env(), runner="pydantic_ai", repo_path=".", max_iterations=960)
    counter = {"n": 0}

    def loop(messages, info: AgentInfo):
        counter["n"] += 1
        return ModelResponse(
            parts=[ToolCallPart(tool_name=info.function_tools[0].name, args={"path": "same.py"})]
        )

    req = RunRequest(
        system_prompt="x", instructions="gather evidence", config=cfg, mode="text", reviewers=[]
    )
    with pytest.raises(RunawayToolLoopError):
        PydanticAIRunner(cfg, model_override=FunctionModel(loop)).run(req)
    assert counter["n"] < 40, f"detection, not budget, must abort: {counter['n']} requests"
