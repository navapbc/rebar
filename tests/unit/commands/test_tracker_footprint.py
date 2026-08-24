"""Direct rendering contracts for the tracker-footprint CLI adapter."""

from __future__ import annotations

import jsonschema
import pytest

from rebar import schemas
from rebar._commands import tracker_footprint
from rebar._store import footprint

_UNAVAILABLE = {"unavailable": {"reason": footprint._ALLOCATION_UNAVAILABLE}}


def _report_with_unavailable_allocation() -> dict[str, object]:
    layer = {
        "logical_bytes": 8,
        "file_count": 2,
        "allocated_bytes": dict(_UNAVAILABLE),
        "allocation_overhead_bytes": dict(_UNAVAILABLE),
    }
    return {
        "mode": "mounted",
        "source": {
            "remote": "origin",
            "branch": "tickets",
            "requested_ref": "origin/tickets",
            "measured_ref": "refs/heads/tickets",
            "tip": "0123456789abcdef",
        },
        "object_database": {"scope": "standalone", "shared_reasons": []},
        "layers": {
            "pack": {
                "logical_bytes": 100,
                "file_count": 1,
                "scope": "standalone",
                "complete": True,
            },
            "checkout": layer,
            "git_directory": dict(layer),
            "whole_clone": {**layer, "scope": "standalone"},
        },
        "definitions": dict(footprint.DEFINITIONS),
    }


def test_render_text_labels_unavailable_allocation() -> None:
    text = tracker_footprint._render_text(_report_with_unavailable_allocation())

    assert f"unavailable ({footprint._ALLOCATION_UNAVAILABLE})" in text
    # The logical values remain concrete alongside the structured unavailable allocation.
    assert "logical_bytes=8" in text


def test_render_text_marks_complete_pack_without_the_non_exclusive_note() -> None:
    text = tracker_footprint._render_text(_report_with_unavailable_allocation())

    pack_line = next(line for line in text.splitlines() if line.strip().startswith("pack:"))
    assert "complete=True" in pack_line
    assert "non-exclusive" not in pack_line


def test_render_text_marks_non_exclusive_pack_when_alternates_present() -> None:
    report = _report_with_unavailable_allocation()
    report["object_database"] = {"scope": "shared", "shared_reasons": ["alternates"]}
    pack = report["layers"]["pack"]  # type: ignore[index]
    assert isinstance(pack, dict)
    pack["scope"] = "shared"
    pack["complete"] = False

    text = tracker_footprint._render_text(report)

    pack_line = next(line for line in text.splitlines() if line.strip().startswith("pack:"))
    assert "complete=False" in pack_line
    assert "non-exclusive" in pack_line


def test_schema_requires_the_pack_complete_field() -> None:
    """The output contract enforces `complete`, matching the ticket's schema acceptance."""

    validator = schemas.validator(schemas.TRACKER_FOOTPRINT)
    report = _report_with_unavailable_allocation()
    validator.validate(report)

    pack = report["layers"]["pack"]  # type: ignore[index]
    assert isinstance(pack, dict)
    del pack["complete"]
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(report)
