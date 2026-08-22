"""Carry unresolved code-review findings forward across patchsets (story
nitro-zombie-mealworm). Behavioural tests over the public seams — the classifier, the reader, the
merge injection, the Pass-2 listing addendum, the posture clamp, and the Gerrit rendering. No LLM,
no store: the prior-payload reader is stubbed the way the region-gate tests stub it."""

from __future__ import annotations

from typing import Any

from rebar.llm.code_review import carry_forward
from rebar.llm.code_review import contracts as cr_contracts
from rebar.llm.workflow import executor as _ex
from rebar.llm.workflow import steps as _steps  # noqa: F401 — registers the built-in steps

cr_contracts.register_contracts()


def _prior(findings, *, deps=None, revision="ps2"):
    return {"findings": list(findings), "deps": dict(deps or {}), "revision": revision}


def _stub_reader(monkeypatch, payload):
    monkeypatch.setattr(
        "rebar.llm.code_review.sidecar.latest_code_review_result",
        lambda key, repo_root=None: payload,
    )


def _finding(**over: Any) -> dict[str, Any]:
    base = {
        "finding": "the wall-clock assertion is machine-speed dependent",
        "criteria": ["tests"],
        "location": "a.py:40",
        "evidence": ["assert elapsed < 10"],
        "decision": "advisory",
        "priority": 0.5,
    }
    base.update(over)
    return base


def _hash_of(tmp_path, name: str) -> str:
    from rebar.llm.plan_review import attest

    return attest._hash_file(name, base=str(tmp_path))


# ── state classification ─────────────────────────────────────────────────────────────────────
def test_state_classification_covers_every_state(tmp_path) -> None:
    """One rule per state: unchanged region -> still-present, changed region -> addressed, an
    item that was already carried -> withdrawn, and an unresolvable region -> still-present."""
    (tmp_path / "a.py").write_text("x = 1\n")
    root = str(tmp_path)
    unchanged = {"a.py": _hash_of(tmp_path, "a.py")}

    assert (
        carry_forward.classify_state(_finding(), unchanged, repo_root=root)
        == carry_forward.STATE_STILL_PRESENT
    )
    assert (
        carry_forward.classify_state(_finding(), {"a.py": "deadbeef"}, repo_root=root)
        == carry_forward.STATE_ADDRESSED
    )
    # unresolvable signal (path absent from the prior deps map) -> conservative default
    assert (
        carry_forward.classify_state(_finding(), {"other.py": "abc"}, repo_root=root)
        == carry_forward.STATE_STILL_PRESENT
    )
    already = _finding(standing={"origin_revision": "ps1", "state": "still-present"})
    assert (
        carry_forward.classify_state(already, unchanged, repo_root=root)
        == carry_forward.STATE_WITHDRAWN
    )


def test_state_classification_drives_what_is_carried(tmp_path, monkeypatch) -> None:
    """Only a still-present item is carried; an addressed one is recorded and left behind."""
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y = 2\n")
    root = str(tmp_path)
    deps = {"a.py": _hash_of(tmp_path, "a.py"), "b.py": "stale-hash"}
    _stub_reader(
        monkeypatch,
        _prior(
            [_finding(location="a.py:40"), _finding(location="b.py:10", finding="b is wrong")],
            deps=deps,
        ),
    )
    coverage: dict[str, Any] = {}
    items = carry_forward.standing_items("change:I1", repo_root=root, coverage=coverage)

    assert [i["location"] for i in items] == ["a.py:40"]
    assert items[0]["standing"]["state"] == carry_forward.STATE_STILL_PRESENT
    assert items[0]["standing"]["origin_revision"] == "ps2"
    assert coverage["standing"]["states"][carry_forward.STATE_ADDRESSED] == 1


def test_withdrawn_item_is_never_carried_again(tmp_path, monkeypatch) -> None:
    """The carry is ONE-SHOT: an item that already carries `standing` is classified withdrawn and
    is not re-injected, so the chain terminates after a single patchset."""
    (tmp_path / "a.py").write_text("x = 1\n")
    root = str(tmp_path)
    deps = {"a.py": _hash_of(tmp_path, "a.py")}
    already = _finding(standing={"origin_revision": "ps2", "state": "still-present"})
    _stub_reader(monkeypatch, _prior([already], deps=deps))
    coverage: dict[str, Any] = {}

    assert carry_forward.standing_items("change:I1", repo_root=root, coverage=coverage) == []
    assert coverage["standing"]["states"][carry_forward.STATE_WITHDRAWN] == 1


def test_standing_survives_into_the_next_payload(tmp_path, monkeypatch) -> None:
    """The one-shot rule depends on `standing` being PERSISTED: the sidecar payload keeps it, so
    the next patchset's reader sees an already-carried item and classifies it withdrawn."""
    from rebar.llm.code_review import sidecar

    standing = {"origin_revision": "ps2", "origin_decision": "advisory", "state": "still-present"}
    payload = sidecar.build_payload(
        {"verdict": "PASS", "advisory": [{**_finding(), "standing": standing}]},
        target_ticket="t",
        change_id="I1",
        revision="ps3",
    )
    assert payload["advisory"][0]["standing"] == standing

    (tmp_path / "a.py").write_text("x = 1\n")
    _stub_reader(
        monkeypatch,
        _prior(payload["advisory"], deps={"a.py": _hash_of(tmp_path, "a.py")}, revision="ps3"),
    )
    assert carry_forward.standing_items("change:I1", repo_root=str(tmp_path)) == []


# ── eligibility ──────────────────────────────────────────────────────────────────────────────
def test_ineligible_prior_items_yield_nothing(tmp_path, monkeypatch) -> None:
    """No prior review, an ungrounded prior finding, and a payload the surfaced-only reader never
    returns (the `dropped` bucket) all carry nothing."""
    (tmp_path / "a.py").write_text("x = 1\n")
    root = str(tmp_path)
    deps = {"a.py": _hash_of(tmp_path, "a.py")}

    _stub_reader(monkeypatch, None)
    assert carry_forward.standing_items("change:I1", repo_root=root) == []

    coverage: dict[str, Any] = {}
    _stub_reader(monkeypatch, _prior([_finding(evidence=[])], deps=deps))
    assert carry_forward.standing_items("change:I1", repo_root=root, coverage=coverage) == []
    assert coverage["standing_suppressed"] == "ungrounded-prior"

    # A keyless review has no memory at all.
    assert carry_forward.standing_items("", repo_root=root) == []


def test_ineligible_dropped_bucket_is_never_carried(tmp_path, monkeypatch) -> None:
    """End-to-end through the REAL reader: a finding the region-gated floor permanently DROPPED
    lives in the payload's `dropped` bucket, which the surfaced-only union never returns — so it
    cannot re-enter the review by being carried forward (bug old-frilly-plankton)."""
    from rebar.llm.code_review import sidecar

    (tmp_path / "a.py").write_text("x = 1\n")
    payload = sidecar.build_payload(
        {
            "verdict": "PASS",
            "deps": {"a.py": _hash_of(tmp_path, "a.py")},
            "blocking": [],
            "advisory": [],
            "dropped": [_finding(finding="dropped by the novelty floor")],
        },
        target_ticket="t",
        change_id="I1",
        revision="ps2",
    )
    assert [f["finding"] for f in payload["dropped"]] == ["dropped by the novelty floor"]

    import rebar as _rebar

    monkeypatch.setattr(
        _rebar,
        "list_tickets",
        lambda **kw: [{"ticket_id": "art1", "title": "code-review: I1 @ ps2"}],
    )
    monkeypatch.setattr(
        sidecar, "_latest_payload_with_ts", lambda aid, repo_root=None: (payload, 1)
    )
    # The reader itself surfaces nothing...
    got = sidecar.latest_code_review_result("change:I1", repo_root=str(tmp_path))
    assert got is not None and got["findings"] == []
    # ...so carry-forward has nothing to carry, even though the region is unchanged.
    assert carry_forward.standing_items("change:I1", repo_root=str(tmp_path)) == []


def test_reader_failure_carries_nothing(tmp_path, monkeypatch) -> None:
    def _boom(key, repo_root=None):
        raise RuntimeError("store unreadable")

    monkeypatch.setattr("rebar.llm.code_review.sidecar.latest_code_review_result", _boom)
    assert carry_forward.standing_items("change:I1", repo_root=str(tmp_path)) == []


def test_standing_cap_bounds_the_carried_set(tmp_path, monkeypatch) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    root = str(tmp_path)
    deps = {"a.py": _hash_of(tmp_path, "a.py")}
    many = [_finding(finding=f"issue {i}", priority=0.1 * (i % 9)) for i in range(40)]
    _stub_reader(monkeypatch, _prior(many, deps=deps))
    items = carry_forward.standing_items("change:I1", repo_root=root)
    assert len(items) == carry_forward.STANDING_CAP


def test_origin_decision_is_read_from_the_surfaced_bucket_stamp(tmp_path, monkeypatch) -> None:
    """The reader stamps each surfaced item with its origin BUCKET (see
    test_code_review_artifact.py); carry-forward copies that into `standing.origin_decision`, which
    is what the posture clamp clamps to. A payload without the stamp falls back to the finding's
    own decision, and an item with neither is treated as advisory — never as a block."""
    _stub_reader(
        monkeypatch,
        _prior([{**_finding(decision=None), "origin_decision": "block"}], revision="ps2"),
    )
    stamped = carry_forward.standing_items("change:I1", repo_root=str(tmp_path))[0]
    assert stamped["standing"]["origin_decision"] == "block"

    _stub_reader(monkeypatch, _prior([_finding(decision="block")], revision="ps2"))
    legacy = carry_forward.standing_items("change:I1", repo_root=str(tmp_path))[0]
    assert legacy["standing"]["origin_decision"] == "block"

    _stub_reader(monkeypatch, _prior([_finding(decision=None)], revision="ps2"))
    bare = carry_forward.standing_items("change:I1", repo_root=str(tmp_path))[0]
    assert bare["standing"]["origin_decision"] == "advisory", (
        "an undecided prior item must never be clamped to a blocking posture"
    )


# ── injection at merge ───────────────────────────────────────────────────────────────────────
def _merge(inputs):
    ctx = _ex.StepContext(
        run_id="r",
        step_id="merge",
        kind="uses",
        step={"uses": "merge_findings"},
        inputs=inputs,
        workflow={},
        repo_root=None,
    )
    return _ex.STEP_REGISTRY["merge_findings"](ctx)


def test_merge_injects_standing_items_the_fresh_run_missed() -> None:
    standing = [
        {
            **_finding(),
            "standing": {
                "origin_revision": "ps2",
                "origin_decision": "advisory",
                "state": carry_forward.STATE_STILL_PRESENT,
            },
        }
    ]
    out = _merge(
        {
            "base_findings": [{"finding": "unrelated", "criteria": ["docs"], "location": "z.py:1"}],
            "round_a_findings": [],
            "round_b_findings": [],
            "standing_findings": standing,
        }
    )
    carried = [f for f in out["findings"] if isinstance(f.get("standing"), dict)]
    assert len(carried) == 1, "a standing item the fresh finders missed must be injected"
    assert carried[0]["standing"]["state"] == carry_forward.STATE_STILL_PRESENT
    assert carried[0]["standing"]["origin_revision"] == "ps2"
    assert out["standing_count"] == 1
    assert {f["id"] for f in out["findings"]} == {"0", "1"}


def test_merge_injects_standing_only_when_the_fresh_run_missed_it() -> None:
    """An item the fresh finders re-raised is NOT duplicated: the fresh finding is the live one."""
    from rebar.llm.plan_review.sidecar import norm_id

    fresh = _finding()
    standing = [{**fresh, "norm_id": norm_id(fresh), "standing": {"origin_revision": "ps2"}}]
    out = _merge(
        {
            "base_findings": [dict(fresh)],
            "round_a_findings": [],
            "round_b_findings": [],
            "standing_findings": standing,
        }
    )
    assert out["standing_count"] == 0
    assert len(out["findings"]) == 1


def test_merge_without_standing_items_is_unchanged() -> None:
    out = _merge(
        {
            "base_findings": [{"finding": "f", "criteria": ["docs"], "location": "z.py:1"}],
            "round_a_findings": [],
            "round_b_findings": [],
        }
    )
    assert out["standing_count"] == 0
    assert len(out["findings"]) == 1


# ── the Pass-2 listing addendum ──────────────────────────────────────────────────────────────
def _verify_inputs(findings):
    ctx = _ex.StepContext(
        run_id="r",
        step_id="verify_inputs",
        kind="uses",
        step={"uses": "code_review_verify_inputs"},
        inputs={"findings": findings},
        workflow={},
        repo_root=None,
    )
    return _ex.STEP_REGISTRY["code_review_verify_inputs"](ctx)["instructions"]


def test_verifier_is_told_which_findings_are_standing() -> None:
    plain = _verify_inputs([_finding()])
    with_standing = _verify_inputs(
        [_finding(standing={"origin_revision": "ps2", "state": "still-present"})]
    )
    assert "standing" not in plain.lower(), "no standing wording when nothing was carried"
    assert "standing since patchset ps2" in with_standing
    assert plain in with_standing, "the shared kernel listing is appended to, never replaced"


# ── the posture clamp ────────────────────────────────────────────────────────────────────────
_GRADED_YES = {
    "is_verifiable": "yes",
    "evidence_entails_finding": "yes",
    "path_reachable": "yes",
    "impact_follows_necessarily": "yes",
    "no_viable_alternative_explanation": "yes",
    "no_existing_mitigation": "yes",
    "severity_claim_justified": "yes",
}
# A high-impact production defect: enough priority to clear any block threshold.
_SEVERE = {
    "security_bypass_not_enforced_elsewhere": "yes",
    "trigger_likelihood": "common",
    "silent_failure": "yes",
}


def _decide(findings):
    verifs = [
        {"index": i, "binary": dict(_GRADED_YES), "severity_attributes": dict(_SEVERE)}
        for i in range(len(findings))
    ]
    ctx = _ex.StepContext(
        run_id="r",
        step_id="decide",
        kind="uses",
        step={"uses": "code_review_decide"},
        inputs={"findings": findings, "verifications": verifs},
        workflow={},
        repo_root=None,
    )
    return _ex.STEP_REGISTRY["code_review_decide"](ctx)


def test_posture_clamp_keeps_a_carried_advisory_advisory() -> None:
    """`security` is blocking-enabled with a low threshold, so a severe finding under it blocks.
    A CARRIED one whose origin decision was advisory must not be raised to block by memory."""
    fresh = {
        "finding": "unsanitized input reaches the shell",
        "criteria": ["security"],
        "location": "a.py:9",
        "evidence": ["os.system(user_input)"],
    }
    carried = {
        **fresh,
        "location": "b.py:9",
        "standing": {
            "origin_revision": "ps2",
            "origin_decision": "advisory",
            "state": carry_forward.STATE_STILL_PRESENT,
        },
    }
    out = _decide([fresh, carried])
    by_loc = {f["location"]: f for f in out["decided"]}
    assert by_loc["a.py:9"]["decision"] == "block", "the fresh finding still blocks"
    assert by_loc["b.py:9"]["decision"] == "advisory", "carry-forward must never raise a posture"
    assert by_loc["b.py:9"]["reason"] == "standing-clamp"
    assert not any(f.get("standing") for f in out["blocking"])


def test_posture_clamp_leaves_an_origin_block_alone() -> None:
    decided = [
        {"decision": "block", "standing": {"origin_decision": "block"}},
        {"decision": "advisory", "standing": {"origin_decision": "block"}},
        {"decision": "block"},
    ]
    carry_forward.clamp_standing_decisions(decided)
    assert [f["decision"] for f in decided] == ["block", "advisory", "block"]


# ── Gerrit rendering ─────────────────────────────────────────────────────────────────────────
def test_gerrit_text_marks_a_carried_finding_with_its_origin_patchset() -> None:
    from rebar.review_bot import finding_publish

    standing = {"origin_revision": "ps2", "origin_decision": "advisory", "state": "still-present"}
    findings = [
        {"detail": "d1", "dimension": "tests", "location": "a.py:40", "standing": standing},
        {"detail": "d2", "dimension": "tests", "location": "b.py:1"},
    ]
    block = finding_publish.render_findings_block(findings, kind="advisory")
    assert "standing since patchset ps2" in block
    assert block.count("standing since") == 1, "an uncarried finding is unmarked"
    inline = finding_publish.build_inline_comments(findings)
    assert "standing since patchset ps2" in inline["a.py"][0]["message"]
    assert "standing since" not in inline["b.py"][0]["message"]


def test_adapter_carries_standing_through_to_the_receiver_shape() -> None:
    from rebar.review_bot import adapter

    standing = {"origin_revision": "ps2", "origin_decision": "advisory", "state": "still-present"}
    verdict = {
        "blocking": [],
        "advisory": [
            {"finding": "f1", "criteria": ["tests"], "location": "a.py:40", "standing": standing},
            {"finding": "f2", "criteria": ["tests"], "location": "b.py:1"},
        ],
    }
    out = adapter._translate_findings(verdict)
    assert out[0]["standing"] == standing
    assert "standing" not in out[1], "an uncarried finding gains no key"
