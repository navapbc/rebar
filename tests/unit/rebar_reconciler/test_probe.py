"""Held-out oracle for S8 — ``rebar bridge suggest-mapping`` (the live-Jira mapping probe).

This file is the TEST-FIRST contract for S8. It pins the OBSERVABLE behaviour of a new,
read-only ``rebar bridge suggest-mapping <PROJECT> [--write]`` verb that observes a Jira
project through a probe PORT and emits a suggested ``[mapping.*]`` config block seeded with
the project's real vocabulary and identity-seed axis maps. Everything here is FULLY OFFLINE
— a FAKE probe port supplies canned issue-type / priority / link-type / status / createmeta /
search+transitions data, so no live Jira and no CI credential are required (the portability
rule). The tests drive the REAL CLI entry point (``rebar._cli.main(["bridge",
"suggest-mapping", ...])``) wherever possible.

Contract the implementer must satisfy (the injection seam these tests target)
-----------------------------------------------------------------------------
NEW module ``rebar_reconciler.mapping_probe`` (source at
``src/rebar/_engine/rebar_reconciler/mapping_probe.py``) exposes:

* ``build_probe(...)`` — a module-level factory that returns a probe PORT constructed from
  resolved DC settings (via ``adapters/jira_datacenter/transport.build_client_from_settings``).
  These tests monkeypatch it to return a FAKE port, so the real builder runs OFFLINE. The
  ``suggest-mapping`` handler (``_cli.__init__._bridge_suggest_mapping``) MUST obtain its port
  through this factory as a MODULE ATTRIBUTE (``mapping_probe.build_probe(...)``), so a
  monkeypatch on the module takes effect.
* ``build_mapping_layer(port, project_key)`` — the pure layer builder that calls the port's
  per-axis read methods and returns ``{"projects": {project_key: {<layer>}}}``.

The probe PORT exposes exactly these read-only methods, each returning PLAIN data (the port
normalizes the raw ``jira.JIRA`` resources so the builder — and these fakes — stay simple):

* ``issue_types()``           -> list[dict] with keys ``name``, ``id``, optional ``hierarchyLevel``
* ``priorities()``            -> list[str] (priority names)
* ``issue_link_types()``      -> list[str] (link-type names)
* ``statuses()``              -> list[str] (status names)
* ``createmeta_issuetypes(key)``            -> list[dict] with ``id``, ``name``
* ``createmeta_fieldtypes(key, id)``  -> list[dict] with ``fieldId``, ``name``, ``required``
* ``search_issues(jql)``      -> list[dict] with ``key``, ``issue_type``, ``status``
* ``transitions(issue_key)``  -> list[dict] with ``name`` and ``to`` (destination status name)

The port has NO create / transition / delete method — that read-only surface is the whole
point (``test_probe_is_read_only``), distinct from ``check-access`` which creates+deletes a
throwaway issue.

``mapping_probe.build_probe`` is resolvable in this test tree because
``tests/unit/rebar_reconciler/conftest.py`` already puts the engine dir on ``sys.path`` and
extends ``rebar_reconciler.__path__`` to the engine package, so ``import
rebar_reconciler.mapping_probe`` binds the SAME module object the CLI handler imports.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import tomllib

import rebar._config_writer as cw
from rebar import config as cfg
from rebar._cli import main

pytestmark = pytest.mark.unit


# ── canned Jira vocabulary the fake port hands back (offline) ───────────────────────────
_ISSUE_TYPES = [
    {"name": "Epic", "id": "10000", "hierarchyLevel": 1},
    {"name": "Story", "id": "10001", "hierarchyLevel": 0},
    {"name": "Sub-task", "id": "10002", "hierarchyLevel": -1},
]
_PRIORITIES = ["Highest", "High", "Medium", "Low"]
_LINK_TYPES = ["Blocks", "Relates", "Duplicate"]
_STATUSES = ["To Do", "In Progress", "Done"]
_CREATEMETA_TYPES = [{"id": "10001", "name": "Story"}]
# summary is in the create BASELINE (adapters/jira/outbound_fields.py:140-158) → NO stub.
# "Team" is required and NOT in the baseline → a stub is expected.
# "Sprint" is not required → NO stub.
_CREATEMETA_FIELDS = [
    {"fieldId": "summary", "name": "Summary", "required": True},
    {"fieldId": "customfield_10050", "name": "Team", "required": True},
    {"fieldId": "customfield_10099", "name": "Sprint", "required": False},
]
# Jira returns transitions only from each sample issue's CURRENT status, so the set is
# inherently partial → the emitted hints must be flagged best-effort, never complete.
_SEARCH_SAMPLES = [{"key": "REB-1", "issue_type": "Story", "status": "In Progress"}]
_TRANSITIONS = {
    "REB-1": [
        {"name": "Back to To Do", "to": "To Do"},
        {"name": "Finish", "to": "Done"},
    ],
}

_BASELINE_CREATE_FIELDS = {
    "summary",
    "description",
    "issuetype",
    "priority",
    "assignee",
    "status",
    "parent",
}
_READ_METHODS = {
    "issue_types",
    "priorities",
    "issue_link_types",
    "statuses",
    "createmeta_issuetypes",
    "createmeta_fieldtypes",
    "search_issues",
    "transitions",
}


class _FakePort:
    """Offline stand-in for the probe port. Records every method call so a test can assert
    the probe is READ-ONLY, and raises loudly if any create/transition/delete surface is
    touched (that would make it indistinguishable from ``check-access``)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    # -- read-only axis getters --------------------------------------------------------
    def issue_types(self) -> list[dict]:
        self.calls.append("issue_types")
        return [dict(t) for t in _ISSUE_TYPES]

    def priorities(self) -> list[str]:
        self.calls.append("priorities")
        return list(_PRIORITIES)

    def issue_link_types(self) -> list[str]:
        self.calls.append("issue_link_types")
        return list(_LINK_TYPES)

    def statuses(self) -> list[str]:
        self.calls.append("statuses")
        return list(_STATUSES)

    def createmeta_issuetypes(self, key: str) -> list[dict]:
        self.calls.append("createmeta_issuetypes")
        return [dict(t) for t in _CREATEMETA_TYPES]

    def createmeta_fieldtypes(self, key: str, issue_type_id: str) -> list[dict]:
        self.calls.append("createmeta_fieldtypes")
        return [dict(f) for f in _CREATEMETA_FIELDS]

    def search_issues(self, jql: str) -> list[dict]:
        self.calls.append("search_issues")
        return [dict(s) for s in _SEARCH_SAMPLES]

    def transitions(self, issue_key: str) -> list[dict]:
        self.calls.append("transitions")
        return [dict(t) for t in _TRANSITIONS.get(issue_key, [])]

    # -- write surfaces that MUST NEVER be called (distinct from check-access) ----------
    def create_issue(self, *a: Any, **k: Any) -> Any:  # pragma: no cover - guard
        raise AssertionError("suggest-mapping must not create an issue")

    def transition_issue(self, *a: Any, **k: Any) -> Any:  # pragma: no cover - guard
        raise AssertionError("suggest-mapping must not transition an issue")

    def delete_issue(self, *a: Any, **k: Any) -> Any:  # pragma: no cover - guard
        raise AssertionError("suggest-mapping must not delete an issue")


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "REBAR_CONFIG",
        "XDG_CONFIG_HOME",
        "REBAR_ROOT",
        "JIRA_URL",
        "JIRA_USER",
        "JIRA_PROJECT",
        "JIRA_API_TOKEN",
        "JIRA_PAT",
    ):
        monkeypatch.delenv(name, raising=False)
    cfg.reset_config_cache()


def _proj(tmp: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git-rooted project dir the writer/discovery treats as the repo root."""
    p = tmp / "proj"
    p.mkdir(parents=True)
    (p / ".git").mkdir()
    monkeypatch.setenv("REBAR_ROOT", str(p))
    # Minimal jira coordinates so any settings resolution before build_probe succeeds; the
    # fake port replaces build_probe entirely, so no network is ever touched.
    monkeypatch.setenv("JIRA_URL", "https://jira.example")
    monkeypatch.setenv("JIRA_USER", "probe@example.com")
    monkeypatch.setenv("JIRA_PAT", "unused-because-build_probe-is-faked")
    cfg.reset_config_cache()
    return p


def _install_fake_port(monkeypatch: pytest.MonkeyPatch) -> _FakePort:
    """Bind the NEW ``mapping_probe.build_probe`` factory to return our offline fake.

    Imported lazily so this file still COLLECTS (and the config-writer regression guard
    still runs) before ``mapping_probe`` exists — a missing module surfaces here as a
    ModuleNotFoundError in exactly the tests that need it, the correct RED signal."""
    import rebar_reconciler.mapping_probe as mapping_probe

    fake = _FakePort()
    monkeypatch.setattr(mapping_probe, "build_probe", lambda *a, **k: fake, raising=False)
    return fake


def _mapping_for(parsed: dict, key: str) -> dict:
    """Extract the per-project mapping table from emitted/round-tripped TOML, accepting the
    ``[mapping.*]`` (rebar.toml) form or the ``[tool.rebar.mapping.*]`` (pyproject) form."""
    table = parsed.get("mapping")
    if table is None:
        table = parsed.get("tool", {}).get("rebar", {}).get("mapping")
    assert isinstance(table, dict), f"no mapping block in emitted config: {parsed!r}"
    projects = table.get("projects", {})
    assert key in projects, f"no projects.{key} in mapping block: {table!r}"
    return projects[key]


# ── AC1: suggest-mapping emits the discovered vocabulary + identity axis maps ───────────
def test_probe_emits_mapping(tmp_path, monkeypatch, capsys) -> None:
    """`rebar bridge suggest-mapping REB` against the fake port emits a mapping block with
    discovered vocabulary (issue_types, statuses, link_types, priorities, hierarchy) and
    identity-seed axis maps, plus create_defaults stubs."""
    _proj(tmp_path, monkeypatch)
    _install_fake_port(monkeypatch)

    code = main(["bridge", "suggest-mapping", "REB"])
    assert code == 0
    out = capsys.readouterr().out
    parsed = tomllib.loads(out)
    layer = _mapping_for(parsed, "REB")

    # discovered vocabulary
    assert set(layer["issue_types"]) == {"Epic", "Story", "Sub-task"}
    assert set(layer["statuses"]) == {"To Do", "In Progress", "Done"}
    assert set(layer["link_types"]) == {"Blocks", "Relates", "Duplicate"}
    # hierarchy present because hierarchyLevel was supplied; value is the level
    assert layer["hierarchy"] == {"Epic": 1, "Story": 0, "Sub-task": -1}

    # identity-seed axis maps (local key -> same remote value)
    assert layer["status_map"] == {s: s for s in _STATUSES}
    assert layer["type_map"] == {t: t for t in ("Epic", "Story", "Sub-task")}
    assert layer["link_map"] == {link: link for link in _LINK_TYPES}
    assert layer["priority_map"] == {p: p for p in _PRIORITIES}

    # create_defaults: a stub ONLY for the required, non-baseline field ("Team"); NEVER for
    # a baseline field (summary/…) and NEVER for a non-required field ("Sprint").
    create_defaults = layer["create_defaults"]
    stub_keys = set(create_defaults)
    assert stub_keys & {"Team", "customfield_10050"}, create_defaults
    assert not (stub_keys & _BASELINE_CREATE_FIELDS)
    assert "summary" not in stub_keys and "Summary" not in stub_keys
    assert "Sprint" not in stub_keys and "customfield_10099" not in stub_keys


# ── AC2: default invocation is READ-ONLY (no file, no writes to Jira) ────────────────────
def test_probe_is_read_only(tmp_path, monkeypatch, capsys) -> None:
    """Default (no --write) writes NO config file and the fake port records only READS —
    no create/transition/delete, unlike check-access / bridge probe."""
    proj = _proj(tmp_path, monkeypatch)
    fake = _install_fake_port(monkeypatch)

    code = main(["bridge", "suggest-mapping", "REB"])
    assert code == 0
    capsys.readouterr()  # drain

    # No config file was created or edited by a default (read-only) run.
    assert not (proj / "rebar.toml").exists()
    assert not (proj / "pyproject.toml").exists()

    # The probe touched only read methods, and touched at least one.
    assert fake.calls, "the probe made no calls at all"
    assert set(fake.calls) <= _READ_METHODS, f"non-read method used: {fake.calls}"
    for forbidden in ("create_issue", "transition_issue", "delete_issue"):
        assert forbidden not in fake.calls


# ── AC3: --write preserves existing sections + hand edits; never touches pyproject ──────
def test_probe_write_preserves_edits(tmp_path, monkeypatch) -> None:
    """--write into a rebar.toml that already has flat [jira]+[tracker] PRESERVES both and
    adds [mapping.*]; the deep-merge under projects.REB keeps pre-existing hand-edited
    mapping keys (existing keys win); a sibling pyproject.toml is NEVER edited."""
    proj = _proj(tmp_path, monkeypatch)
    rebar_toml = proj / "rebar.toml"
    rebar_toml.write_text(
        "[jira]\n"
        'url = "https://jira.example"\n'
        'user = "probe@example.com"\n'
        'project = "REB"\n'
        "\n"
        "[tracker]\n"
        'branch = "tickets"\n'
        "\n"
        "[mapping.projects.REB.status_map]\n"
        # a hand override that COLLIDES with an identity seed key ("To Do") — existing wins
        '"To Do" = "Custom Todo"\n'
        # a hand key with no identity-seed counterpart — must survive untouched
        'open = "Backlog"\n',
        encoding="utf-8",
    )
    pyproject = proj / "pyproject.toml"
    pyproject.write_text("[tool.black]\nline-length = 100\n", encoding="utf-8")
    pyproject_before = pyproject.read_text(encoding="utf-8")

    _install_fake_port(monkeypatch)
    code = main(["bridge", "suggest-mapping", "REB", "--write"])
    assert code == 0

    data = tomllib.loads(rebar_toml.read_text(encoding="utf-8"))
    # flat siblings preserved
    assert data["jira"]["project"] == "REB"
    assert data["tracker"]["branch"] == "tickets"
    # mapping added + hand edits win over the identity seed
    layer = _mapping_for(data, "REB")
    assert layer["status_map"]["To Do"] == "Custom Todo"  # existing key wins
    assert layer["status_map"]["open"] == "Backlog"  # hand key survives
    # newly-discovered vocabulary still merged in
    assert set(layer["issue_types"]) == {"Epic", "Story", "Sub-task"}
    # the user's pyproject.toml is never edited
    assert pyproject.read_text(encoding="utf-8") == pyproject_before


# ── AC4: transition hints are best-effort / partial; status_map stays identity ──────────
def test_probe_transitions_best_effort(tmp_path, monkeypatch, capsys) -> None:
    """The port returns search samples + per-issue transitions limited to each sample's
    current status; the emitted transition hints are flagged best-effort/partial (a comment
    or a marker), never asserted complete, and status_map stays identity-seeded."""
    _proj(tmp_path, monkeypatch)
    fake = _install_fake_port(monkeypatch)

    code = main(["bridge", "suggest-mapping", "REB"])
    assert code == 0
    out = capsys.readouterr().out

    # The probe actually walked the transitions surface (search + transitions).
    assert "search_issues" in fake.calls
    assert "transitions" in fake.calls

    # A best-effort / partial marker appears in the emitted output (TOML comment or a key),
    # so the hints are never presented as complete.
    lowered = out.lower()
    markers = ("best-effort", "best effort", "partial", "advisory")
    assert any(marker in lowered for marker in markers), out

    # status_map is NOT rewritten from transition data — it stays identity-seeded.
    layer = _mapping_for(tomllib.loads(out), "REB")
    assert layer["status_map"] == {s: s for s in _STATUSES}


# ── AC5-7: the NEW config-writer serialization helpers ──────────────────────────────────
def test_emit_nested_toml_dotted_headers() -> None:
    """`_emit_nested_toml("mapping", {...})` renders a nested table as dotted-header
    sub-tables (`[mapping.projects.REB.status_map]`)."""
    text = cw._emit_nested_toml("mapping", {"projects": {"REB": {"status_map": {"open": "To Do"}}}})
    assert "[mapping.projects.REB.status_map]" in text
    # and it round-trips to the same nested structure
    parsed = tomllib.loads(text)
    assert parsed["mapping"]["projects"]["REB"]["status_map"]["open"] == "To Do"


def test_emit_config_toml_mixes_flat_and_nested() -> None:
    """`_emit_config_toml` emits the flat [jira] via the existing flat path AND the nested
    [mapping.*] via the nested path, joined correctly, and round-trips through tomllib."""
    text = cw._emit_config_toml(
        {"jira": {"url": "x"}, "mapping": {"projects": {"REB": {"status_map": {"a": "b"}}}}}
    )
    assert "[jira]" in text
    assert "[mapping.projects.REB.status_map]" in text
    data = tomllib.loads(text)
    assert data["jira"]["url"] == "x"
    assert data["mapping"]["projects"]["REB"]["status_map"]["a"] == "b"


def test_emit_toml_still_fail_closed_on_nesting() -> None:
    """Regression guard: the EXISTING `_emit_toml` is UNTOUCHED and still fail-closes on a
    nested sub-table (the pinned invariant at tests/unit/test_jira_onboard.py:149)."""
    with pytest.raises(cfg.ConfigError):
        cw._emit_toml({"jira": {"nested": {"k": "v"}}})
