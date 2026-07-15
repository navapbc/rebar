"""Typed core Config (rebar.config.Config) — parse/validate/defaults/aliases and
loud unknown-key handling (config-refinement task 252e). The TOML loader,
discovery, and CLI/env/file layering are separate tasks; here we test the schema
+ from_mapping in isolation against a nested mapping (the TOML [tool.rebar] shape).
"""

from __future__ import annotations

import logging

import pytest

from rebar.config import Config, ConfigError

pytestmark = pytest.mark.unit


def test_defaults_when_empty() -> None:
    c = Config.from_mapping(None)
    assert c.verify.require_plan_review_for_claim is False
    assert c.ticket.display_mode == "auto"
    assert c.compact.threshold == 10
    assert c.sync.push == "always" and c.sync.pull == "on"
    assert c.mcp.readonly is False and c.mcp.allow_llm is False and c.mcp.allow_jira_sync is False
    assert c.reconciler.deletion_probe_limit == 20
    assert c.reconciler.lock_lease_secs == 120 and c.reconciler.id_guard_bypass_unsafe is False
    assert c.jira.url == "" and c.scratch.base_dir == ""


def test_parses_known_keys_typed() -> None:
    c = Config.from_mapping(
        {
            "verify": {"require_plan_review_for_claim": True},
            "compact": {"threshold": 25},
            "sync": {"push": "async", "pull": "off"},
            "mcp": {"allow_jira_sync": True},
            "reconciler": {"lock_lease_secs": 90, "id_guard_bypass_unsafe": True},
            "jira": {"url": "https://x.atlassian.net", "project": "DSO"},
            "scratch": {"base_dir": "/tmp/s"},
        }
    )
    assert c.verify.require_plan_review_for_claim is True
    assert c.compact.threshold == 25
    assert c.sync.push == "async" and c.sync.pull == "off"
    assert c.mcp.allow_jira_sync is True
    assert c.reconciler.lock_lease_secs == 90 and c.reconciler.id_guard_bypass_unsafe is True
    assert c.jira.url == "https://x.atlassian.net" and c.jira.project == "DSO"
    assert c.scratch.base_dir == "/tmp/s"


def test_string_coercion_from_env_or_flat_file() -> None:
    # Values arriving as strings (env / legacy flat file) coerce to the typed shape.
    c = Config.from_mapping(
        {
            "verify": {"require_plan_review_for_claim": "yes"},
            "compact": {"threshold": "30"},
            "sync": {"push": "OFF"},  # case-insensitive choice
            "mcp": {"readonly": "1"},
        }
    )
    assert c.verify.require_plan_review_for_claim is True
    assert c.compact.threshold == 30
    assert c.sync.push == "off"
    assert c.mcp.readonly is True


@pytest.mark.parametrize(
    "raw, msg",
    [
        ({"sync": {"push": "sometimes"}}, "sync.push"),
        ({"sync": {"pull": "maybe"}}, "sync.pull"),
        ({"verify": {"require_plan_review_for_claim": "kinda"}}, "boolean"),
        ({"compact": {"threshold": "lots"}}, "integer"),
        ({"compact": {"threshold": 0}}, ">= 1"),  # below minimum
        ({"compact": {"threshold": True}}, "boolean"),  # bool rejected as int
        ({"jira": {"url": {"nested": 1}}}, "string"),
        ({"verify": "notatable"}, "table/section"),
    ],
)
def test_invalid_values_fail_closed(raw: dict, msg: str) -> None:
    with pytest.raises(ConfigError) as exc:
        Config.from_mapping(raw)
    assert msg in str(exc.value)


def test_tracker_defaults() -> None:
    c = Config.from_mapping(None)
    assert c.tracker.dir == ".tickets-tracker"
    assert c.tracker.branch == "tickets"


@pytest.mark.parametrize(
    "branch",
    ["tickets", "rebar-tickets", "feature/x", "release-1.2", "tickets/v2", "a-b_c.d"],
)
def test_tracker_branch_accepts_valid_refs(branch: str) -> None:
    assert Config.from_mapping({"tracker": {"branch": branch}}).tracker.branch == branch


@pytest.mark.parametrize(
    "branch",
    [
        "",  # empty
        "bad branch",  # interior space
        "-leading",  # leading dash
        "a..b",  # double dot
        "foo.lock",  # .lock suffix
        "x.lock/y",  # .lock on any component
        "feat/",  # trailing slash
        "/abs",  # leading slash
        "a//b",  # double slash
        "ends.",  # trailing dot
        ".hidden",  # component starts with dot
        "a/.b",  # later component starts with dot
        "ti\tcket",  # control char (tab)
        "ca~ret",  # forbidden ~
        "co:lon",  # forbidden :
        "ref@{x}",  # @{ sequence
        "@",  # bare @
    ],
)
def test_tracker_branch_rejects_invalid_refs(branch: str) -> None:
    with pytest.raises(ConfigError) as exc:
        Config.from_mapping({"tracker": {"branch": branch}})
    assert "tracker.branch" in str(exc.value)


@pytest.mark.parametrize(
    "dir_",
    [".tickets-tracker", "my-tickets", ".rebar/tracker", "/var/lib/rebar/store"],
)
def test_tracker_dir_accepts_valid(dir_: str) -> None:
    # Bare relative names AND absolute paths (the EV-3b relocated store) are allowed.
    assert Config.from_mapping({"tracker": {"dir": dir_}}).tracker.dir == dir_


@pytest.mark.parametrize(
    "dir_",
    ["", "   ", "../escape", "a/../b", "tic\x00ken", "ctrl\x01"],
)
def test_tracker_dir_rejects_unsafe(dir_: str) -> None:
    with pytest.raises(ConfigError) as exc:
        Config.from_mapping({"tracker": {"dir": dir_}})
    assert "tracker.dir" in str(exc.value)


def test_unknown_key_and_section_warn_not_drop(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="rebar.config"):
        c = Config.from_mapping(
            {
                "verify": {"require_plan_review_for_claim": True, "tpyo": 1},
                "bogus_section": {"x": 1},
            },
            source="rebar.toml",
        )
    # The valid key still parses; the unknown key/section are warned, NOT silently dropped.
    assert c.verify.require_plan_review_for_claim is True
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "verify.tpyo" in text and "rebar.toml" in text
    assert "[bogus_section]" in text


def test_removed_verdict_key_is_a_load_bearing_tombstone() -> None:
    # require_verdict_for_close is a load-bearing TOMBSTONE (story 36c7): coercion must
    # FAIL LOUD with RemovedInputError naming the replacement, not swallow it as an
    # unknown key (its removal changes close-gate semantics).
    from rebar._deprecations import RemovedInputError

    with pytest.raises(RemovedInputError, match="require_completion_verification_for_close"):
        Config.from_mapping({"verify": {"require_verdict_for_close": True}})
