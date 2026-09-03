"""a07a: shared Jira-family issuelink canonicalization.

The Cloud and Data Center backends currently duplicate the same `issuelinks`
parsing, fallback-key recovery, and `(vendor_type, remote_key)` dedup logic.
This suite pins the shared pure helper in `link_direction.py`, proves the file
still loads via `spec_from_file_location`, and requires both adapters to
delegate to that one owner.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
REC = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "jira" / "issuelinks_map.json"


def _load(name: str, filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REC / filename)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _link(type_name: str, *, inward: str | None = None, outward: str | None = None) -> dict:
    data: dict[str, object] = {"type": {"name": type_name}}
    if inward is not None:
        data["inwardIssue"] = {"key": inward}
    if outward is not None:
        data["outwardIssue"] = {"key": outward}
    return data


@pytest.fixture(scope="module")
def link_direction() -> ModuleType:
    return _load("link_direction_a07a", "link_direction.py")


@pytest.fixture(scope="module")
def captured_issuelinks() -> dict[str, list[dict]]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(params=("cloud", "dc"))
def backend(request: pytest.FixtureRequest):
    if request.param == "cloud":
        from rebar_reconciler.adapters.jira.backend import JiraBackend

        return JiraBackend(transport=object())
    from rebar_reconciler.adapters.jira_datacenter.backend import JiraDataCenterBackend

    return JiraDataCenterBackend(transport=object())


@pytest.mark.parametrize(
    ("remote_fields", "expected"),
    [
        ({"issuelinks": []}, []),
        ({"issuelinks": [_link("Blocks", outward="DIG-9")]}, [("blocks", "DIG-9", "Blocks")]),
        (
            {"issuelinks": [_link("Blocks", inward="DIG-9")]},
            [("depends_on", "DIG-9", "Blocks")],
        ),
        ({"issuelinks": [_link("Relates", outward="DIG-9")]}, [("relates_to", "DIG-9", "Relates")]),
        ({"issuelinks": [_link("Mentions", outward="DIG-9")]}, [(None, "DIG-9", "Mentions")]),
        ({"issuelinks": [{"type": {"name": "Blocks"}}]}, []),
        (
            {
                "issuelinks": [
                    _link("Blocks", outward="DIG-9"),
                    _link("Blocks", outward="DIG-9"),
                    _link("Blocks", inward="DIG-7"),
                ]
            },
            [("blocks", "DIG-9", "Blocks"), ("depends_on", "DIG-7", "Blocks")],
        ),
    ],
    ids=[
        "empty",
        "outward-blocks",
        "inward-blocks",
        "symmetric-relates",
        "unmapped-keyed-fallback",
        "malformed-without-key",
        "dedup-by-vendor-type-and-key",
    ],
)
def test_canonicalize_jira_issue_links_cases(
    link_direction: ModuleType,
    remote_fields: dict,
    expected,
):
    assert link_direction.canonicalize_jira_issue_links(remote_fields) == expected


def test_backend_map_remote_links_delegates_to_core(
    backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = [("blocks", "DIG-42", "Blocks")]
    calls: list[dict[str, object]] = []
    link_direction = importlib.import_module("rebar_reconciler.link_direction")

    def fake(remote_fields: dict[str, object]) -> list[tuple[str | None, str, str]]:
        calls.append(remote_fields)
        return sentinel

    monkeypatch.setattr(link_direction, "canonicalize_jira_issue_links", fake)
    payload = {"issuelinks": [_link("Blocks", outward="DIG-9")]}
    assert backend.map_remote_links(payload) == sentinel
    assert calls == [payload]


def test_cloud_and_dc_match_captured_vectors(
    captured_issuelinks: dict[str, list[dict]], link_direction: ModuleType
) -> None:
    from rebar_reconciler.adapters.jira.backend import JiraBackend
    from rebar_reconciler.adapters.jira_datacenter.backend import JiraDataCenterBackend

    cloud = JiraBackend(transport=object())
    dc = JiraDataCenterBackend(transport=object())
    for issue_key, links in captured_issuelinks.items():
        fields = {"issuelinks": links}
        expected = link_direction.canonicalize_jira_issue_links(fields)
        assert cloud.map_remote_links(fields) == expected, issue_key
        assert dc.map_remote_links(fields) == expected, issue_key
