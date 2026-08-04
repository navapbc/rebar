"""Repo-only oracles for the live-DC isolation controls repaired by bug 59b2.

WHY THESE LIVE IN THE UNIT TIER. Every assertion 59b2 repaired shares one defect: it could not
fail. A fix for that class is unverifiable unless the fixed assertion is shown to go RED, and a
demonstration that only runs on a booted amd64-only harness image is a demonstration nobody will
repeat. So the two pieces of logic that CAN be lifted out of the live cells — the ``base_url``
collector and the inherited-environment reader — live in ``_dc_support`` and are mutation-checked
here, on every commit.

What is NOT covered here, recorded honestly rather than implied: the two Finding B positive
controls (the idempotence cell's pending-plan check and the row-14 filter-reach check) call the
live reconcile pass, so they cannot be exercised without the harness. They are asserted in the
live tier and exercised by a harness run.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

_SUPPORT = Path(__file__).resolve().parents[1] / "external" / "live_jira_dc" / "_dc_support.py"
_HARNESS_URL = "http://localhost:2990/jira"
_FOREIGN_URL = "https://real-jira.example.com"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_dc_support_59b2", _SUPPORT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def dc_support() -> ModuleType:
    return _load()


# ---------------------------------------------------------------------------
# Finding A — the base_url collector must be able to SEE a foreign URL
# ---------------------------------------------------------------------------


def test_the_collector_finds_a_foreign_base_url_nested_under_dot_rebar(dc_support, tmp_path):
    """THE MUTATION CHECK for the live cell's decoy control.

    The live assertion is `set(found) == {BASE}` over the store copy. That passes whether the
    collector works or returns nothing, because the copy legitimately contains exactly one
    base_url. This proves the collector reports a second value when one exists — the property
    the live assertion silently depends on.
    """
    (tmp_path / ".rebar").mkdir()
    (tmp_path / "rebar.toml").write_text(f'[reconciler]\nbase_url = "{_HARNESS_URL}"\n')
    (tmp_path / ".rebar" / "nested.toml").write_text(f'[reconciler]\nbase_url = "{_FOREIGN_URL}"\n')

    found = dc_support.collect_base_urls(tmp_path)

    assert set(found) == {_HARNESS_URL, _FOREIGN_URL}
    assert found[_FOREIGN_URL] == [str(Path(".rebar") / "nested.toml")]


def test_the_collector_looks_beyond_rebar_toml(dc_support, tmp_path):
    """A collector that only read rebar.toml would pass the live assertion and miss the hazard.

    Named separately because "only inspects the one file the fixture wrote" is exactly how an
    assertion ends up comparing a fixture against itself.
    """
    (tmp_path / "rebar.toml").write_text(f'[reconciler]\nbase_url = "{_HARNESS_URL}"\n')
    (tmp_path / "pyproject.toml").write_text(f'[tool.rebar]\nbase_url = "{_FOREIGN_URL}"\n')

    found = dc_support.collect_base_urls(tmp_path)

    assert _FOREIGN_URL in found, (
        "a base_url in pyproject.toml was not collected; the live isolation assertion would "
        "report a clean repo while a stray production URL sat in a file it never opened"
    )


def test_the_collector_reports_nothing_for_a_tree_with_no_config(dc_support, tmp_path):
    """The negative side: no config surfaces means an empty mapping, not a crash."""
    assert dc_support.collect_base_urls(tmp_path) == {}


# ---------------------------------------------------------------------------
# Finding A — the inherited-environment reader must refuse to pass vacuously
# ---------------------------------------------------------------------------


def test_a_missing_snapshot_is_a_hard_error_not_an_empty_dict(dc_support, tmp_path):
    """The whole point: an absent snapshot must NOT read as "no credentials leaked".

    Returning `{}` would make the isolation cell's credential assertion pass vacuously — the
    identical defect 59b2 exists to remove, reintroduced one layer down.
    """
    with pytest.raises(AssertionError, match="did not record the inherited environment"):
        dc_support.read_inherited_env(tmp_path)


def test_a_recorded_leak_is_reported_so_the_assertion_can_fail(dc_support, tmp_path):
    """MUTATION CHECK: with a credential recorded as inherited, the cell's predicate is non-empty.

    Mirrors the live cell's computation over the snapshot. The pre-fix assertion read
    `os.environ` AFTER the fixture had deleted these very names, so no job environment could
    ever make it fail; this shows the post-fix input can.
    """
    (tmp_path / dc_support.INHERITED_ENV_FILE).write_text(
        json.dumps({"JIRA_API_TOKEN": "leaked-value", "JIRA_EMAIL": None, "REBAR_SYNC_PUSH": None})
    )

    inherited = dc_support.read_inherited_env(tmp_path)
    leaked = {
        name: value
        for name, value in inherited.items()
        if name in dc_support.CLOUD_CREDENTIAL_VARS and value
    }

    assert leaked == {"JIRA_API_TOKEN": "leaked-value"}


def test_a_clean_snapshot_reports_no_leak(dc_support, tmp_path):
    """The passing direction, so the assertion is not simply always-red."""
    (tmp_path / dc_support.INHERITED_ENV_FILE).write_text(
        json.dumps({name: None for name in dc_support.CLOUD_CREDENTIAL_VARS})
    )

    inherited = dc_support.read_inherited_env(tmp_path)

    assert not [n for n, v in inherited.items() if n in dc_support.CLOUD_CREDENTIAL_VARS and v]


# ---------------------------------------------------------------------------
# Finding A — the checked credential set is broader than the fixture's old list
# ---------------------------------------------------------------------------


def test_the_credential_vocabulary_covers_the_names_the_old_list_missed(dc_support):
    """`JIRA_TOKEN` and `JIRA_URL` were unchecked, and both can aim a pass at a real instance."""
    names = set(dc_support.CLOUD_CREDENTIAL_VARS)

    assert {"JIRA_API_TOKEN", "JIRA_EMAIL", "ATLASSIAN_API_TOKEN"} <= names, (
        "the original three names must remain covered"
    )
    assert {"JIRA_TOKEN", "JIRA_URL"} <= names, (
        "Finding A requires the checked set be broader than the fixture's original three"
    )


def test_the_fixture_and_the_assertion_share_one_definition():
    """No second hardcoded list: the fixture's delenv loop must consume the shared constant.

    Finding A's defect was that the cell's list and the fixture's list were the same three names
    written twice — so the fixture guaranteed the cell could not fail. Grep-based because the
    subject IS the source text: a duplicated literal is what must not come back.
    """
    live = Path(__file__).resolve().parents[1] / "external" / "live_jira_dc"
    conftest = (live / "conftest.py").read_text(encoding="utf-8")
    cell = (live / "test_store_copy_isolation.py").read_text(encoding="utf-8")

    assert "for cloud_var in CLOUD_CREDENTIAL_VARS:" in conftest, (
        "the fixture no longer clears the SHARED credential set — it has grown its own list again"
    )
    assert "CLOUD_CREDENTIAL_VARS" in cell
    assert '"JIRA_API_TOKEN", "JIRA_EMAIL", "ATLASSIAN_API_TOKEN"' not in conftest, (
        "a hardcoded credential-name list is back in the fixture"
    )


# ---------------------------------------------------------------------------
# Finding B / C — the live-tier controls, pinned as source-level guards
# ---------------------------------------------------------------------------


def test_every_empty_plan_idempotence_verdict_is_preceded_by_a_positive_control():
    """EVERY such verdict, counted — not just the first one the ticket happened to name.

    This is how the second offender was found: 59b2's Finding B cited one idempotence cell (by a
    line number that had already drifted), but two cells draw the same conclusion from an empty
    filtered plan, and fixing only the cited one would have left the identical vacuity in place
    while the ticket read as closed. Pairing the counts is what makes an unguarded new cell fail
    here rather than pass silently.
    """
    body = (
        Path(__file__).resolve().parents[1] / "external" / "live_jira_dc" / "test_dc_mutations.py"
    ).read_text(encoding="utf-8")

    verdicts = [i for i in range(len(body)) if body.startswith("NOT IDEMPOTENT:", i)]
    controls = [i for i in range(len(body)) if body.startswith("assert pending, (", i)]

    assert verdicts, "no empty-plan idempotence verdict found — has the cell been renamed?"
    assert len(controls) == len(verdicts), (
        f"{len(verdicts)} empty-plan idempotence verdict(s) but only {len(controls)} positive "
        f"control(s): every cell concluding convergence from an empty FILTERED plan needs its own "
        f"proof that the filter can surface an entry for its pair"
    )
    for verdict, control in zip(verdicts, controls, strict=True):
        assert control < verdict, (
            "each positive control must run BEFORE its verdict; after it, an unmatched filter "
            "would already have been read as convergence"
        )


def test_the_row14_cell_establishes_filter_reach_before_deleting_the_issue():
    """Reach has to be shown while the pair still exists — after the delete it is unprovable."""
    body = (
        Path(__file__).resolve().parents[1] / "external" / "live_jira_dc" / "test_dc_mutations.py"
    ).read_text(encoding="utf-8")

    reach = body.find("assert _reach, (")
    delete = body.find("dc_transport.delete_issue(key)")

    assert reach != -1, "the row-14 cell lost its filter-reach positive control"
    assert delete != -1
    assert reach < delete, "filter reach must be established before the DC issue is deleted"


def test_out_status_refuses_a_prestate_it_did_not_create():
    """Finding C: the helper must always perform its transition, never skip it conditionally."""
    body = (
        Path(__file__).resolve().parents[1] / "external" / "live_jira_dc" / "test_dc_mutations.py"
    ).read_text(encoding="utf-8")
    start = body.find("def _out_status(")
    assert start != -1
    helper = body[start : body.find("\ndef ", start + 1)]

    assert 'if current != "in_progress":' not in helper, (
        "_out_status is conditionally mutating again — on an already-in_progress ticket the "
        "oracle would assert pre-existing state"
    )
    assert 'assert current != "in_progress"' in helper, (
        "_out_status must establish (and loudly refuse) its pre-state rather than tolerate it"
    )
