"""Deterministic attestation-laundering detector (bug 2f56-313f-6175-41b1; ADR-0043).

An ``[operator-attested]`` AC item whose own text (or indented continuation lines) cites
exact repository path/symbol evidence is a MISCLASSIFICATION: the proof is repo-resident,
so the completion verifier could check it — tagging it launders a code-verifiable
criterion past verification. The detector is precision-first: it fires only on repo-shaped
citations (repo-root-anchored paths, slash paths with a code extension, pytest node ids,
``test_*`` symbols) and never on legitimately external evidence (deploys, votes, console
output, URLs).
"""

from __future__ import annotations

import pytest

from rebar.llm.plan_review import det_attestation_launder as launder

pytestmark = pytest.mark.unit


def _ac(*items: str) -> str:
    return "## Acceptance Criteria\n" + "\n".join(items) + "\n"


# ── repo_evidence_citations: what counts as repo evidence ────────────────────────


def test_repo_root_anchored_path_is_a_citation() -> None:
    hits = launder.repo_evidence_citations("proxy: tests/unit/test_scan_scoping.py covers it")
    assert hits == ["tests/unit/test_scan_scoping.py"]


def test_pytest_node_id_is_a_citation() -> None:
    hits = launder.repo_evidence_citations(
        "see tests/unit/test_scan_scoping.py::test_fsck_default_skips_archived"
    )
    assert hits == ["tests/unit/test_scan_scoping.py::test_fsck_default_skips_archived"]


def test_bare_test_symbol_is_a_citation() -> None:
    hits = launder.repo_evidence_citations("covered by test_fsck_default_skips_archived et al.")
    assert hits == ["test_fsck_default_skips_archived"]


def test_slash_path_with_code_extension_is_a_citation() -> None:
    hits = launder.repo_evidence_citations("documented in mypkg/subdir/module.py")
    assert hits == ["mypkg/subdir/module.py"]


def test_repo_dir_reference_without_extension_is_a_citation() -> None:
    hits = launder.repo_evidence_citations("the guard lives under src/rebar/_commands")
    assert hits == ["src/rebar/_commands"]


def test_nested_hits_are_not_double_reported() -> None:
    # `test_scan_scoping` is a substring of the path hit — report the path once, not twice.
    hits = launder.repo_evidence_citations("tests/unit/test_scan_scoping.py")
    assert hits == ["tests/unit/test_scan_scoping.py"]


def test_external_evidence_is_not_a_citation() -> None:
    assert (
        launder.repo_evidence_citations(
            "the prod deploy is confirmed live; Gerrit change 1772 carries Verified +1 "
            "and the release vote is recorded in the console"
        )
        == []
    )


def test_urls_are_scrubbed_before_matching() -> None:
    # A URL path segment must not read as a repo path (external evidence is often a link).
    assert (
        launder.repo_evidence_citations(
            "dashboard at https://ci.example.com/builds/run.html shows green"
        )
        == []
    )


def test_url_embedding_a_repo_shaped_path_does_not_fire_the_gap() -> None:
    """The URL scrub must apply on the laundering path too: a link whose URL path embeds a
    repo-shaped fragment, even introduced by an evidence word, is external evidence."""
    plan = _ac(
        "- [x] [operator-attested] rollout green; evidence: "
        "https://ci.example.com/artifacts/tests/unit/test_scan_scoping.py.html",
        "      provenance: environment=production; principal=ops-oncall; "
        "privilege_posture=production-equivalent; instrument=live-call — CI dashboard",
    )
    assert launder.laundering_gaps(plan) == []


def test_dotted_config_key_without_slash_is_not_a_citation() -> None:
    assert launder.repo_evidence_citations("the compact.trigger=off mitigation is removed") == []


# ── laundering_gaps: which AC items fire ─────────────────────────────────────────


def test_tagged_item_citing_repo_path_fires() -> None:
    plan = _ac(
        "- [x] [operator-attested] scan scoping holds; proxy: tests/unit/test_scan_scoping.py"
    )
    gaps = launder.laundering_gaps(plan)
    assert len(gaps) == 1
    line, cites = gaps[0]
    assert "scan scoping holds" in line
    assert "tests/unit/test_scan_scoping.py" in cites


def test_citation_on_continuation_line_fires() -> None:
    plan = _ac(
        "- [ ] [operator-attested] behavior verified",
        "      evidence: tests/unit/test_scan_scoping.py::test_fsck_default_skips_archived",
    )
    gaps = launder.laundering_gaps(plan)
    assert len(gaps) == 1
    assert any("test_scan_scoping.py" in c for c in gaps[0][1])


def test_untagged_item_citing_repo_path_does_not_fire() -> None:
    plan = _ac("- [ ] scan scoping holds; proxy: tests/unit/test_scan_scoping.py")
    assert launder.laundering_gaps(plan) == []


def test_tagged_external_item_with_provenance_does_not_fire() -> None:
    plan = _ac(
        "- [ ] [operator-attested] the prod deploy is confirmed live",
        "      provenance: environment=production; principal=release-operator; "
        "privilege_posture=production-equivalent; instrument=live-call — console shows green",
    )
    assert launder.laundering_gaps(plan) == []


def test_citation_outside_the_tagged_items_block_does_not_fire() -> None:
    plan = _ac(
        "- [ ] [operator-attested] the prod deploy is confirmed live",
        "- [ ] the parser handles tests/unit/test_parser.py fixtures",
    )
    assert launder.laundering_gaps(plan) == []


def test_no_ac_section_yields_no_gaps() -> None:
    assert launder.laundering_gaps("Body citing tests/unit/test_x.py only.") == []


def test_live_grunion_ac19_text_fires() -> None:
    # The live laundering instance this bug reproduces (epic fb8a-7363-e406-4e36 AC-19).
    plan = _ac(
        "- [x] [operator-attested] `rebar fsck` and `rebar compact-all` default to active "
        "tickets and reach archived ones only with `--include-archived`; proxy: "
        "tests/unit/test_scan_scoping.py (merged 39de64871c)"
    )
    gaps = launder.laundering_gaps(plan)
    assert len(gaps) == 1
    assert "tests/unit/test_scan_scoping.py" in gaps[0][1]


# ── mixed criteria vs orientation prose (tickets 7f69/5796 false-positive class) ─


def test_mixed_criterion_naming_a_test_file_fires() -> None:
    """A tagged AC whose repo-verifiable HALF names a test file fires even when the
    external half carries a complete provenance line — the authoring policy is SPLIT
    (evidence-kind's mixed_evidence_is_split), and test artifacts are inherently
    completion evidence, never orientation."""
    plan = _ac(
        "- [ ] [operator-attested] `pytest tests/e2e/test_live_probe.py` exits 0 "
        "against the prod endpoint",
        "      provenance: environment=production; principal=ops-oncall; "
        "privilege_posture=production-equivalent; instrument=live-call — exit code in console",
    )
    gaps = launder.laundering_gaps(plan)
    assert len(gaps) == 1
    assert "tests/e2e/test_live_probe.py" in gaps[0][1]


def test_orientation_prose_path_with_external_provenance_does_not_fire() -> None:
    """A legitimately-external AC whose prose mentions a non-test file for ORIENTATION
    (no evidence marker introduces it) must not be blocked."""
    plan = _ac(
        "- [ ] [operator-attested] the fix shipped in src/rebar/_store/sync.py is deployed to prod",
        "      provenance: environment=production; principal=release-operator; "
        "privilege_posture=production-equivalent; instrument=live-call — dashboard green",
    )
    assert launder.laundering_gaps(plan) == []


def test_non_test_path_introduced_by_evidence_marker_fires() -> None:
    """The same non-test path IS a citation when the line presents it as the proof."""
    plan = _ac(
        "- [x] [operator-attested] rollout steps recorded; proof: docs/runbook.md section applied"
    )
    gaps = launder.laundering_gaps(plan)
    assert len(gaps) == 1
    assert "docs/runbook.md" in gaps[0][1]


def test_marker_after_the_path_does_not_make_it_evidence() -> None:
    """The evidence word must INTRODUCE the citation (precede it on the line): a path whose
    own deployment is externally verified is orientation, not repo proof."""
    plan = _ac(
        "- [ ] [operator-attested] src/rebar/store.py deployment is verified in prod console",
        "      provenance: environment=production; principal=ops-oncall; "
        "privilege_posture=production-equivalent; instrument=live-call — console green",
    )
    assert launder.laundering_gaps(plan) == []


def test_same_citation_on_two_lines_reported_once() -> None:
    plan = _ac(
        "- [x] [operator-attested] holds; proxy: tests/unit/test_scan_scoping.py",
        "      evidence: tests/unit/test_scan_scoping.py",
    )
    gaps = launder.laundering_gaps(plan)
    assert len(gaps) == 1
    assert gaps[0][1] == ["tests/unit/test_scan_scoping.py"]


def test_bare_test_symbol_fires_without_a_marker() -> None:
    """Test symbols are evidence-shaped on their own — no marker word needed."""
    plan = _ac("- [x] [operator-attested] behavior holds, test_fsck_default_skips_archived passes")
    gaps = launder.laundering_gaps(plan)
    assert len(gaps) == 1
    assert "test_fsck_default_skips_archived" in gaps[0][1]
