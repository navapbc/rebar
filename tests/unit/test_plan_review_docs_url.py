"""Regression: plan-review coaching deep-links must resolve for CONSUMERS by default.

Bug (client report §5): with REBAR_DOCS_URL unset, plan_review_docs_url() defaulted to a
``file://<repo-root>/docs/plan-review-criteria-guide.md`` URI. In a consumer repo (rebar
installed, no rebar docs/ tree) every coaching guide_url therefore pointed at a nonexistent
local file, so the deep-link affordance was dead out of the box. The default must be a
canonical hosted URL (still overridable by REBAR_DOCS_URL).
"""

from __future__ import annotations

from rebar import config


def test_default_docs_url_is_canonical_hosted_not_consumer_file(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("REBAR_DOCS_URL", raising=False)
    # tmp_path stands in for a consumer repo with no rebar docs/ tree.
    url = config.plan_review_docs_url(tmp_path)
    assert url.startswith("https://"), f"default deep-link base was {url!r}, expected an https URL"
    assert not url.startswith("file://"), "the default must not be a consumer-repo file:// path"
    assert str(tmp_path) not in url, "the default must not embed the consuming repo's path"


def test_default_anchor_uses_hosted_base_and_lowercased_id(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("REBAR_DOCS_URL", raising=False)
    anchor = config.plan_review_guide_anchor("E2", tmp_path)
    assert anchor.startswith("https://")
    assert anchor.endswith("#e2")


def test_rebar_docs_url_env_still_overrides(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("REBAR_DOCS_URL", "https://example.test/guide/")
    assert config.plan_review_docs_url(tmp_path) == "https://example.test/guide"
    assert config.plan_review_guide_anchor("F1", tmp_path) == "https://example.test/guide#f1"
