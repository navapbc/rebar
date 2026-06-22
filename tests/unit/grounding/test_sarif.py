"""Unit tests for rebar.grounding.sarif — SARIF 2.1.0 interchange at the edges.

Pins: our evidence model round-trips losslessly through SARIF (the rebar-only
fields ride in properties.rebar); a foreign SARIF log (no rebar bag) ingests to
normalized `match` records, reading the rule's rebar_envelope (the spike E5 map).
"""

from __future__ import annotations

import pytest

from rebar.grounding import evidence as ev
from rebar.grounding import sarif

pytestmark = pytest.mark.unit


def test_match_round_trips_losslessly() -> None:
    rec = ev.match(
        job=ev.JOB_SMELL,
        provenance_tier=ev.TIER_T1,
        coverage=ev.coverage(backend="opengrep", status=ev.STATUS_RAN, version="1.0"),
        detector_id="rebar.builtin.smell.console-log",
        location={"file": "a.js", "line_start": 1, "line_end": 1},
        attention_only=True,
        detail="console.log smell",
    )
    log = sarif.to_sarif([rec])
    assert log["version"] == "2.1.0"
    back = sarif.from_sarif(log)
    assert len(back) == 1
    assert back[0] == ev.normalize_evidence(rec)


def test_abstain_is_not_dropped_round_trips_via_property_bag() -> None:
    rec = ev.abstain("version_skew", job=ev.JOB_REFUTE, provenance_tier=ev.TIER_T1, backend="ctags", version="6.0")
    log = sarif.to_sarif([rec])
    # abstain has no faithful native SARIF kind → notApplicable, real outcome in properties.rebar
    assert log["runs"][0]["results"][0]["kind"] == "notApplicable"
    back = sarif.from_sarif(log)
    assert back[0]["outcome"] == ev.OUTCOME_ABSTAIN
    assert back[0]["reason"] == "version_skew"


def test_foreign_sarif_without_rebar_bag_ingests_via_envelope() -> None:
    # Mirrors the spike E5 fixture: a semgrep-shaped SARIF result + rule.properties.rebar_envelope.
    foreign = {
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "semgrep",
                        "rules": [
                            {
                                "id": "rebar.builtin.smell.console-log",
                                "properties": {"rebar_envelope": {"tier": "T1", "job": "smell", "attention_only": True}},
                            }
                        ],
                    }
                },
                "results": [
                    {
                        "ruleId": "rebar.builtin.smell.console-log",
                        "message": {"text": "console.log smell"},
                        "locations": [
                            {"physicalLocation": {"artifactLocation": {"uri": "a.js"}, "region": {"startLine": 1}}}
                        ],
                    }
                ],
            }
        ],
    }
    recs = sarif.from_sarif(foreign, backend="opengrep", version="1.0")
    assert len(recs) == 1
    r = recs[0]
    assert r["outcome"] == ev.OUTCOME_MATCH
    assert r["job"] == "smell"
    assert r["provenance_tier"] == "T1"
    assert r["attention_only"] is True
    assert r["location"] == {"file": "a.js", "line_start": 1}
    assert r["coverage"]["backend"] == "opengrep"
    ev.validate(r)


def test_untrusted_rebar_bag_is_ignored_when_distrusted() -> None:
    # A foreign SARIF producer injects a rebar bag claiming a refuted finding. With
    # trust_rebar_bag=False the bag is NOT honored; the result is re-derived as a
    # plain match from the rule envelope (no attacker-chosen outcome survives).
    foreign = {
        "version": "2.1.0",
        "runs": [
            {
                "tool": {"driver": {"name": "evil", "rules": [{"id": "x.y", "properties": {"rebar_envelope": {"tier": "T1", "job": "smell"}}}]}},
                "results": [
                    {
                        "ruleId": "x.y",
                        "message": {"text": "m"},
                        "properties": {"rebar": {"outcome": "refuted", "job": "refute", "provenance_tier": "T2", "detector_id": "spoofed", "coverage": {"backend": "evil", "status": "ran"}}},
                        "locations": [{"physicalLocation": {"artifactLocation": {"uri": "a.py"}}}],
                    }
                ],
            }
        ],
    }
    trusted = sarif.from_sarif(foreign, trust_rebar_bag=True)
    assert trusted[0]["outcome"] == ev.OUTCOME_REFUTED  # honored when trusted
    distrusted = sarif.from_sarif(foreign, trust_rebar_bag=False)
    assert distrusted[0]["outcome"] == ev.OUTCOME_MATCH  # bag ignored — re-derived as a match
    assert distrusted[0]["detector_id"] == "x.y"  # the rule id, not the spoofed one


def test_empty_sarif_is_handled() -> None:
    assert sarif.from_sarif({}) == []
    assert sarif.from_sarif({"runs": []}) == []
