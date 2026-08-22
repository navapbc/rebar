"""Ticket ad0d B2 — the Gerrit bugfix-size attestation criterion.

A bug-fix change whose diff exceeds the size floor (>150 non-test lines) must carry a
VALID plan-review attestation on its `rebar-ticket:` bug; an oversized fix with a missing /
unverifiable / stale attestation is BLOCKED at code review with a teaching finding.
Fail-open on infrastructure trouble: store/verify errors yield an ADVISORY note, never a block.

Vocabulary discipline: the classifier's verdict strings are drawn from exactly
(a) `compute_validity`'s plan-review-reachable literals and (b) this gate's own `{error}`.
The coverage test pins the three buckets to that union BY SET EQUALITY so a new verdict literal
upstream fails here and forces an explicit bucket decision.

Bug 846b removed the cross-host verify layer: the gate asks whether an attested plan review WAS
COMPLETED, never which environment certified it, because a contributor cannot pin their own
signing environment and so could never satisfy a source-gated check.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from rebar.llm.code_review import bugfix_size_gate as bsg

pytestmark = pytest.mark.unit


# ── vocabulary (the (a)/(b)/(c) sets, restated literally so upstream drift fails HERE) ──

# (a) compute_validity plan-review-reachable verdicts. 'not-closed' is EXCLUDED on purpose:
# it is reachable only inside the completion-verifier arm (attest.py), never for plan-review.
_COMPUTE_VALIDITY_PLAN_REVIEW = {
    "certified",
    "unsigned",
    "wrong-kind",
    "malformed-pin",
    "malformed-phase",
    "stale-code",
    "stale-head",
    "stale-material",
    "stale-reopened",
    "stale-pin-drift",
    "stale-pin-missing",
    "unverifiable-material",
    "incompatible-phase",
}
# (b) the gate's own error verdict for read/exception paths.
#
# BUG 846b: there is deliberately no third, verify-layer set here any more. The classifier used
# to verify the op-cert against the PINNED trusted_environments.yaml keyring and could emit
# `mismatch` / `key_not_valid_at_era` / `invalid` / `unavailable` / `unknown_kind` /
# `unknown_scheme`. That chain gated on the SOURCE of certification and made the criterion
# unsatisfiable for every locally-reviewed bug, so it is gone. Those literals are now simply
# unrecognized — and `bucket_for_verdict` fails OPEN on an unrecognized literal, so the removal
# can never turn into a surprise block.
_GATE_OWN = {"error"}


def test_verdict_vocabulary_is_exactly_partitioned() -> None:
    """ACCEPTED ∪ FLAG ∪ INFRA == (a) ∪ (b), pairwise disjoint — set equality, not ⊆."""
    vocabulary = _COMPUTE_VALIDITY_PLAN_REVIEW | _GATE_OWN
    assert bsg.KNOWN_VERDICTS == frozenset(vocabulary)
    assert bsg.ACCEPTED_VERDICTS | bsg.FLAG_VERDICTS | bsg.INFRA_VERDICTS == bsg.KNOWN_VERDICTS
    assert not bsg.ACCEPTED_VERDICTS & bsg.FLAG_VERDICTS
    assert not bsg.ACCEPTED_VERDICTS & bsg.INFRA_VERDICTS
    assert not bsg.FLAG_VERDICTS & bsg.INFRA_VERDICTS
    # completion-verifier-only literal must NOT enter this gate's vocabulary.
    assert "not-closed" not in bsg.KNOWN_VERDICTS


def test_accepted_and_infra_membership() -> None:
    """Code-drift staleness is ACCEPTED (the plan was reviewed; the tree moved on — normal on
    a rebase-heavy trunk); availability trouble is INFRA (fail-open, never a block)."""
    assert bsg.ACCEPTED_VERDICTS == frozenset({"certified", "stale-code", "stale-head"})
    assert bsg.INFRA_VERDICTS == frozenset({"error"})
    # 846b: the removed verify-layer literals must not linger in the BLOCKING bucket.
    assert not bsg.FLAG_VERDICTS & {"mismatch", "key_not_valid_at_era", "invalid", "unavailable"}
    assert bsg.bucket_for_verdict("mismatch") == "infra"


def test_bucket_for_verdict_covers_every_literal_and_fails_open_on_unknown() -> None:
    for v in bsg.ACCEPTED_VERDICTS:
        assert bsg.bucket_for_verdict(v) == "accepted"
    for v in bsg.FLAG_VERDICTS:
        assert bsg.bucket_for_verdict(v) == "flag"
    for v in bsg.INFRA_VERDICTS:
        assert bsg.bucket_for_verdict(v) == "infra"
    # An unrecognized future literal must fail OPEN (infra), never block.
    assert bsg.bucket_for_verdict("some-future-verdict") == "infra"


# ── path classification + diff accounting ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "is_test"),
    [
        ("tests/unit/test_x.py", True),
        ("tests/conftest.py", True),
        ("conftest.py", True),
        ("src/rebar/conftest.py", True),  # basename rule, anywhere
        ("src/rebar/llm/foo.py", False),
        ("scripts/backtest.py", False),
        ("docs/user-guide.md", False),
        (".github/workflows/ci.yml", False),
        ("./tests/unit/test_y.py", True),  # leading ./ normalized
        ("tests\\unit\\test_z.py", True),  # backslash normalized
        ("testsuite/helper.py", False),  # prefix is tests/, not tests*
    ],
)
def test_is_test_path(path: str, is_test: bool) -> None:
    assert bsg.is_test_path(path) is is_test


def _file_diff(path: str, added: int, removed: int = 0) -> str:
    lines = [
        f"diff --git a/{path} b/{path}",
        f"--- a/{path}",
        f"+++ b/{path}",
        f"@@ -1,{max(removed, 1)} +1,{max(added, 1)} @@",
    ]
    lines += [f"-old {i}" for i in range(removed)]
    lines += [f"+new {i}" for i in range(added)]
    return "\n".join(lines) + "\n"


def test_count_non_test_diff_lines_counts_only_non_test_files() -> None:
    diff = _file_diff("src/rebar/a.py", added=5, removed=2) + _file_diff(
        "tests/unit/test_a.py", added=40
    )
    assert bsg.count_non_test_diff_lines(diff) == 7


def test_count_non_test_diff_lines_empty_and_headers() -> None:
    assert bsg.count_non_test_diff_lines("") == 0
    # +++/--- file headers are never counted as content.
    assert bsg.count_non_test_diff_lines(_file_diff("src/rebar/a.py", added=1)) == 1


def test_count_non_test_diff_lines_deletion_uses_a_side() -> None:
    # A deleted file has `+++ /dev/null`; the a/ side must classify it.
    diff = (
        "diff --git a/src/rebar/gone.py b/src/rebar/gone.py\n"
        "--- a/src/rebar/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1,3 +0,0 @@\n"
        "-a\n"
        "-b\n"
        "-c\n"
    )
    assert bsg.count_non_test_diff_lines(diff) == 3
    test_del = (
        "diff --git a/tests/unit/test_gone.py b/tests/unit/test_gone.py\n"
        "--- a/tests/unit/test_gone.py\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-a\n"
        "-b\n"
    )
    assert bsg.count_non_test_diff_lines(test_del) == 0


def test_count_non_test_diff_lines_rename_tests_to_src_counts() -> None:
    diff = (
        "diff --git a/tests/unit/helper.py b/src/rebar/helper.py\n"
        "--- a/tests/unit/helper.py\n"
        "+++ b/src/rebar/helper.py\n"
        "@@ -1,1 +1,2 @@\n"
        "-x\n"
        "+x\n"
        "+y\n"
    )
    # The +++ (new-location) side governs a rename.
    assert bsg.count_non_test_diff_lines(diff) == 3


def test_count_non_test_diff_lines_hunk_content_is_not_misparsed_as_header() -> None:
    # Deleting the content line `-- foo` from a TESTS file renders `--- foo` INSIDE the hunk.
    # A parser without hunk-state tracking reparses it as a file header and misclassifies the
    # rest of the hunk as non-test.
    diff = (
        "diff --git a/tests/unit/test_h.py b/tests/unit/test_h.py\n"
        "--- a/tests/unit/test_h.py\n"
        "+++ b/tests/unit/test_h.py\n"
        "@@ -1,3 +1,2 @@\n"
        "--- foo\n"
        "-bar\n"
        "+baz\n"
    )
    assert bsg.count_non_test_diff_lines(diff) == 0


# ── the predicate (classification + ticket resolution stubbed at the module seam) ────────


def _verdict() -> dict:
    return {"verdict": "PASS", "blocking": [], "advisory": []}


def _arm(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ticket: str | None = "beef-0000-0000-0001",
    ticket_type: str = "bug",
    classification: str = "unsigned",
) -> None:
    monkeypatch.setattr(bsg, "ticket_for_commit_message", lambda msg, repo_root=None: ticket)
    monkeypatch.setattr(
        bsg,
        "_load_ticket_state",
        lambda tid, repo_root=None: {
            "ticket_id": tid,
            "ticket_type": ticket_type,
            "status": "open",
        },
    )
    monkeypatch.setattr(
        bsg,
        "classify_plan_review_attestation",
        lambda tid, repo_root=None, state=None: {"verdict": classification, "reason": "stub"},
    )


_BIG = 160  # over the 150 floor
_SMALL = 40


def test_oversized_bug_fix_without_attestation_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    _arm(monkeypatch, classification="unsigned")
    verdict = _verdict()
    bsg.apply_bugfix_size_gate(
        verdict,
        diff_text=_file_diff("src/rebar/big.py", added=_BIG),
        commit_message="Fix thing\n\nrebar-ticket: beef-0000-0000-0001",
    )
    assert verdict["verdict"] == "BLOCK"
    assert len(verdict["blocking"]) == 1
    finding = verdict["blocking"][0]
    assert finding["criteria"] == [bsg.CRITERION_ID]
    assert finding["decision"] == "block"
    assert finding["tier"] == "DET"
    # The teaching message names the size, the ticket, the verdict, and the remediation path.
    text = finding["finding"]
    assert str(_BIG) in text and str(bsg.BUGFIX_SIZE_THRESHOLD_NON_TEST_LINES) in text
    assert "beef-0000-0000-0001" in text
    assert "unsigned" in text
    assert "review-plan" in text
    note = verdict["coverage"]["bugfix_size_gate"]
    assert note["bucket"] == "flag" and note["verdict"] == "unsigned"


@pytest.mark.parametrize("accepted", sorted(bsg.ACCEPTED_VERDICTS))
def test_oversized_bug_fix_with_accepted_attestation_passes(
    monkeypatch: pytest.MonkeyPatch, accepted: str
) -> None:
    _arm(monkeypatch, classification=accepted)
    verdict = _verdict()
    bsg.apply_bugfix_size_gate(
        verdict,
        diff_text=_file_diff("src/rebar/big.py", added=_BIG),
        commit_message="Fix thing\n\nrebar-ticket: beef-0000-0000-0001",
    )
    assert verdict["verdict"] == "PASS"
    assert verdict["blocking"] == []
    assert verdict["advisory"] == []
    # PASS-with-accepted is still observable in coverage.
    assert verdict["coverage"]["bugfix_size_gate"]["bucket"] == "accepted"


@pytest.mark.parametrize("infra", ["unavailable", "error", "brand-new-future-verdict"])
def test_infra_or_unknown_classification_abstains_indeterminate(
    monkeypatch: pytest.MonkeyPatch, infra: str
) -> None:
    """Bug 9011: an unclassifiable attestation must ABSTAIN (INDETERMINATE), never read as a
    satisfied gate — a PASS-with-advisory kept `LLM-Review +1` on an oversized bug fix."""
    _arm(monkeypatch, classification=infra)
    verdict = _verdict()
    bsg.apply_bugfix_size_gate(
        verdict,
        diff_text=_file_diff("src/rebar/big.py", added=_BIG),
        commit_message="Fix thing\n\nrebar-ticket: beef-0000-0000-0001",
    )
    assert verdict["verdict"] == "INDETERMINATE"
    assert verdict["blocking"] == []
    assert verdict["advisory"] == []  # no finding that reads as a pass
    note = verdict["coverage"]["bugfix_size_gate"]
    assert note["bucket"] == "infra" and note["verdict"] == infra


def test_infra_classification_does_not_downgrade_an_existing_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An abstain must never WEAKEN a verdict: a BLOCK already on the table stays a BLOCK."""
    _arm(monkeypatch, classification="error")
    verdict = _verdict()
    verdict["verdict"] = "BLOCK"
    verdict["blocking"] = [{"criteria": ["other"], "decision": "block"}]
    bsg.apply_bugfix_size_gate(
        verdict,
        diff_text=_file_diff("src/rebar/big.py", added=_BIG),
        commit_message="Fix thing\n\nrebar-ticket: beef-0000-0000-0001",
    )
    assert verdict["verdict"] == "BLOCK"
    assert verdict["coverage"]["bugfix_size_gate"]["bucket"] == "infra"


def test_non_bug_ticket_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    _arm(monkeypatch, ticket_type="task", classification="unsigned")
    verdict = _verdict()
    bsg.apply_bugfix_size_gate(
        verdict,
        diff_text=_file_diff("src/rebar/big.py", added=_BIG),
        commit_message="Fix thing\n\nrebar-ticket: beef-0000-0000-0001",
    )
    assert verdict["verdict"] == "PASS"
    assert verdict["blocking"] == [] and verdict["advisory"] == []
    assert "bugfix_size_gate" not in verdict.get("coverage", {})


def test_unescalated_under_threshold_diff_never_reads_the_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An under-floor diff that is ALSO not a repeat fix reads nothing and says nothing.

    Ticket 1dd5 narrowed this invariant: the repeat-fix predicate now runs on under-floor
    diffs (that is the point of it), so "no store read below the floor" survives only for a
    diff that no signal escalates. The predicate is stubbed to "no priors" here; its own
    history walk is pinned in ``test_bugfix_repeat_fix_1dd5.py``."""

    def _boom(*a, **k):
        raise AssertionError("store must not be read for an unescalated diff")

    monkeypatch.setattr(bsg, "repeat_fix_escalates", lambda paths, **k: (False, []))
    monkeypatch.setattr(bsg, "ticket_for_commit_message", _boom)
    monkeypatch.setattr(bsg, "_load_ticket_state", _boom)
    monkeypatch.setattr(bsg, "classify_plan_review_attestation", _boom)
    verdict = _verdict()
    bsg.apply_bugfix_size_gate(
        verdict,
        diff_text=_file_diff("src/rebar/small.py", added=_SMALL),
        commit_message="Fix thing\n\nrebar-ticket: beef-0000-0000-0001",
    )
    assert verdict == _verdict()


def test_test_only_diff_is_exempt_even_when_huge(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a, **k):
        raise AssertionError("store must not be read for a test-only diff")

    monkeypatch.setattr(bsg, "classify_plan_review_attestation", _boom)
    verdict = _verdict()
    bsg.apply_bugfix_size_gate(
        verdict,
        diff_text=_file_diff("tests/unit/test_big.py", added=400),
        commit_message="Fix thing\n\nrebar-ticket: beef-0000-0000-0001",
    )
    assert verdict == _verdict()


def test_unresolvable_ticket_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    _arm(monkeypatch, ticket=None)
    verdict = _verdict()
    bsg.apply_bugfix_size_gate(
        verdict,
        diff_text=_file_diff("src/rebar/big.py", added=_BIG),
        commit_message="no trailer here",
    )
    assert verdict == _verdict()


def test_classifier_exception_abstains_indeterminate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bug 9011: an exception ESCAPING the classifier is the same infra class — abstain."""
    _arm(monkeypatch)

    def _explode(tid, repo_root=None, state=None):
        raise RuntimeError("store went away")

    monkeypatch.setattr(bsg, "classify_plan_review_attestation", _explode)
    verdict = _verdict()
    bsg.apply_bugfix_size_gate(
        verdict,
        diff_text=_file_diff("src/rebar/big.py", added=_BIG),
        commit_message="Fix thing\n\nrebar-ticket: beef-0000-0000-0001",
    )
    assert verdict["verdict"] == "INDETERMINATE"
    assert verdict["blocking"] == []
    assert verdict["advisory"] == []
    assert verdict["coverage"]["bugfix_size_gate"]["verdict"] == "error"


# ── the classifier against a REAL fixture store (cross-host chain) ───────────────────────

try:
    from rebar.attest import sshsig as _sshsig

    _sshsig.ensure_available()
    _SSH_OK = True
except Exception:  # noqa: BLE001 — availability probe; skip if ssh-keygen missing/old
    _SSH_OK = False

_needs_ssh = pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required for SSHSIG")


def _bug_in_store(tmp_path, monkeypatch) -> tuple[Path, str, str, list]:
    from _opcert_helpers import store_with_chain

    import rebar

    repo, tracker, pos = store_with_chain(tmp_path, monkeypatch, 3)
    tid = rebar.create_ticket("bug", "fixture bug for ad0d B2", repo_root=str(repo))
    return repo, str(tracker), str(tid), pos


def _mint_plan_review_record(
    repo: Path, tracker: str, tid: str, key_name: str, env_id: str, tmp_path
) -> str:
    """Mint + append a plan-review op-cert SIGNATURE record signed by a FOREIGN env key.

    Returns the signer's public key line for pinning."""
    from _opcert_helpers import keypair

    from rebar._commands._seam import append_event
    from rebar.attest import dsse, opcert
    from rebar.llm.plan_review import attest as _attest

    mat = _attest.current_material_fingerprint(tid, repo_root=str(repo))
    assert mat, "fixture ticket must have a computable material fingerprint"
    merged = "0" * 40  # subject-only field (Option B); no key-validity semantics
    manifest = ["plan-review: PASS", f"material: {mat}", "file-scope: none"]
    priv, pub = keypair(tmp_path, key_name)
    envelope = opcert.sign_opcert(
        tid, mat, merged, kind="plan-review", key_path=priv, principal=env_id, manifest=manifest
    )
    record = {
        "manifest": manifest,
        "algorithm": "sshsig",
        "envelope": dsse.encode(
            envelope.payload_type,
            envelope.payload,
            [{"keyid": s.keyid, "sig": s.sig} for s in envelope.signatures],
        ),
        "material_fingerprint": mat,
        "merged_log_commit": merged,
        "principal": env_id,
        "head_sha": "irrelevant",
        "signed_at": time.time_ns(),
        "kind": "plan-review",
    }
    append_event(tid, "SIGNATURE", record, Path(tracker), repo_root=str(repo))
    return pub


def _load_state(repo: Path, tid: str) -> dict:
    from rebar import _reads

    return _reads.show_ticket(tid, repo_root=str(repo))


def _pin(repo: Path, env_id: str, pub: str, position: str) -> None:
    (repo / ".rebar").mkdir(exist_ok=True)
    (repo / ".rebar" / "trusted_environments.yaml").write_text(
        "environments:\n"
        f"  - env_id: {env_id}\n"
        "    keys:\n"
        f"      - public_key: '{pub}'\n"
        f"        added_at_log_position: '{position}'\n"
        "        revoked_at_log_position: null\n",
        encoding="utf-8",
    )


def test_classify_no_attestation_is_unsigned(tmp_path, monkeypatch) -> None:
    repo, _tracker, tid, _pos = _bug_in_store(tmp_path, monkeypatch)
    res = bsg.classify_plan_review_attestation(tid, repo_root=str(repo))
    assert res["verdict"] == "unsigned"
    assert bsg.bucket_for_verdict(res["verdict"]) == "flag"


@_needs_ssh
def test_classify_rejects_a_cert_bound_to_another_ticket(tmp_path, monkeypatch) -> None:
    """SUBJECT BINDING survives 846b's removal of provenance checking. Replaying another bug's
    perfectly good plan-review cert onto this bug is not evidence THIS plan was reviewed —
    a question about WHAT the attestation covers, not WHO signed it."""
    import rebar
    from rebar._commands._seam import append_event
    from rebar.attest import opcert

    repo, tracker, tid, _pos = _bug_in_store(tmp_path, monkeypatch)
    other = str(rebar.create_ticket("bug", "the OTHER bug", repo_root=str(repo)))
    _mint_plan_review_record(repo, tracker, other, "replay-env", "dev@rebar.test", tmp_path)
    donor = _load_state(repo, other)["attestations"]["plan-review"]
    decoded = opcert.opcert_from_record(donor)
    assert decoded is not None and decoded[1]["ticket_id"] == other, "fixture must bind to donor"
    append_event(tid, "SIGNATURE", donor, Path(tracker), repo_root=str(repo))

    res = bsg.classify_plan_review_attestation(tid, repo_root=str(repo))
    assert res["verdict"] == "wrong-kind", res
    assert bsg.bucket_for_verdict(res["verdict"]) == "flag"


@_needs_ssh
def test_classify_rejects_a_cert_of_another_kind(tmp_path, monkeypatch) -> None:
    """The cross-KIND half of the same invariant: a completion-verifier cert for this very ticket
    is not a plan review, so it must not satisfy the plan-review gate."""
    from _opcert_helpers import keypair

    from rebar._commands._seam import append_event
    from rebar.attest import dsse, opcert
    from rebar.llm.plan_review import attest as _attest

    repo, tracker, tid, _pos = _bug_in_store(tmp_path, monkeypatch)
    mat = _attest.current_material_fingerprint(tid, repo_root=str(repo))
    priv, _pub = keypair(tmp_path, "wrongkind-env")
    env = opcert.sign_opcert(
        tid,
        mat,
        "0" * 40,
        kind="completion-verifier",  # the WRONG kind, same ticket, same material
        key_path=priv,
        principal="dev@rebar.test",
        manifest=["completion: PASS"],
    )
    append_event(
        tid,
        "SIGNATURE",
        {
            "manifest": ["plan-review: PASS", f"material: {mat}"],  # lying plaintext mirror
            "algorithm": "sshsig",
            "envelope": dsse.encode(
                env.payload_type,
                env.payload,
                [{"keyid": s.keyid, "sig": s.sig} for s in env.signatures],
            ),
            "material_fingerprint": mat,
            "merged_log_commit": "0" * 40,
            "principal": "dev@rebar.test",
            "signed_at": time.time_ns(),
            "kind": "plan-review",  # the mirror claims plan-review; the SIGNED payload does not
        },
        Path(tracker),
        repo_root=str(repo),
    )
    res = bsg.classify_plan_review_attestation(tid, repo_root=str(repo))
    assert res["verdict"] == "wrong-kind", res
    assert bsg.bucket_for_verdict(res["verdict"]) == "flag"


@_needs_ssh
def test_classify_pinned_foreign_environment_reaches_accepted(tmp_path, monkeypatch) -> None:
    """THE CROSS-HOST AC: a same-material op-cert signed by ANOTHER environment whose key is
    PINNED in trusted_environments.yaml classifies ACCEPTED — the review bot (a different host
    from the signer) must not flag an attestation merely for being foreign."""
    repo, tracker, tid, pos = _bug_in_store(tmp_path, monkeypatch)
    env_id = "foreign-ci@rebar.test"
    pub = _mint_plan_review_record(repo, tracker, tid, "foreign-env", env_id, tmp_path)
    _pin(repo, env_id, pub, pos[0][0])
    res = bsg.classify_plan_review_attestation(tid, repo_root=str(repo))
    assert res["verdict"] == "certified", res
    assert bsg.bucket_for_verdict(res["verdict"]) == "accepted"


@_needs_ssh
def test_classify_unpinned_signing_environment_is_accepted(tmp_path, monkeypatch) -> None:
    """BUG 846b — THE SOURCE-OF-CERTIFICATION AC. A plan review run and signed in an ordinary
    developer environment carries a principal that no project pins, and pinning is not something
    a contributor can grant themselves. Gating on the SOURCE of certification therefore made this
    criterion unsatisfiable for every locally-reviewed bug. The gate asks only whether an attested
    plan review WAS COMPLETED — so an unpinned principal, same material, must reach ACCEPTED."""
    repo, tracker, tid, _pos = _bug_in_store(tmp_path, monkeypatch)
    env_id = "unpinned-dev@rebar.test"
    _mint_plan_review_record(repo, tracker, tid, "unpinned-env", env_id, tmp_path)
    # Precondition: nothing pins this principal — the exact situation a developer is in.
    assert not (repo / ".rebar" / "trusted_environments.yaml").exists()
    res = bsg.classify_plan_review_attestation(tid, repo_root=str(repo))
    assert res["verdict"] == "certified", res
    assert bsg.bucket_for_verdict(res["verdict"]) == "accepted"


@_needs_ssh
def test_oversized_bug_fix_reviewed_in_an_unpinned_environment_does_not_block(
    tmp_path, monkeypatch
) -> None:
    """BUG 846b end to end: the gate itself, not just the classifier. An oversized bug fix whose
    plan review was signed locally must pass — no blocking finding on provenance grounds."""
    repo, tracker, tid, _pos = _bug_in_store(tmp_path, monkeypatch)
    _mint_plan_review_record(repo, tracker, tid, "unpinned-env-e2e", "dev@rebar.test", tmp_path)
    verdict = bsg.apply_bugfix_size_gate(
        {"verdict": "PASS", "blocking": [], "advisory": []},
        diff_text=_file_diff("src/rebar/big.py", added=_BIG),
        commit_message=f"Fix thing\n\nrebar-ticket: {tid}",
        repo_root=str(repo),
    )
    assert verdict["verdict"] == "PASS", verdict
    assert verdict["blocking"] == []
    assert verdict["coverage"]["bugfix_size_gate"]["bucket"] == "accepted"


# ── pipeline wiring: finalize runs the gate for Gerrit requests only ─────────────────────


def _finalize(
    verdict: dict,
    *,
    repo: Path,
    change_id: str,
    commit_message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> dict:
    from rebar.llm.code_review import detectors as _detectors
    from rebar.llm.code_review.finalize import finalize_code_review_verdict

    # WS5's fail-closed security abstain (`unsupported_lang`) fires in a toy fixture repo and
    # would mask THIS gate's contribution — neutralize it; it has its own suite.
    monkeypatch.setattr(_detectors, "apply_failclosed", lambda v, **k: v)

    request = SimpleNamespace(
        repo_root=str(repo),
        change_id=change_id,
        commit_message=commit_message,
        session_id=None,
        target_ticket=None,
        head="HEAD",
    )
    prep = SimpleNamespace(
        rec=SimpleNamespace(steps=[]),
        dc=SimpleNamespace(changed_files=[], diff_text=_file_diff("src/rebar/big.py", added=_BIG)),
    )
    return finalize_code_review_verdict(
        verdict,
        request=request,
        prep=prep,
        cfg=SimpleNamespace(model="offline-model"),
        runner_sel=SimpleNamespace(name="offline"),
        total_ms=1.0,
    )


def test_finalize_blocks_oversized_unattested_bug_fix_for_gerrit(tmp_path, monkeypatch) -> None:
    repo, _tracker, tid, _pos = _bug_in_store(tmp_path, monkeypatch)
    out = _finalize(
        {"verdict": "PASS", "blocking": [], "advisory": []},
        repo=repo,
        change_id="1234",
        commit_message=f"Fix thing\n\nrebar-ticket: {tid}",
        monkeypatch=monkeypatch,
    )
    assert out["verdict"] == "BLOCK"
    assert any(f.get("criteria") == [bsg.CRITERION_ID] for f in out["blocking"])


def test_finalize_skips_the_gate_for_local_reviews(tmp_path, monkeypatch) -> None:
    repo, _tracker, tid, _pos = _bug_in_store(tmp_path, monkeypatch)
    out = _finalize(
        {"verdict": "PASS", "blocking": [], "advisory": []},
        repo=repo,
        change_id="",  # local review-code run: no Gerrit change
        commit_message=f"Fix thing\n\nrebar-ticket: {tid}",
        monkeypatch=monkeypatch,
    )
    assert out["verdict"] == "PASS"
    assert out["blocking"] == []
