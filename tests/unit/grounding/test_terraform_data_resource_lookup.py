"""Regression tests for Terraform data-resource address matching."""

from __future__ import annotations

from typing import Any

from rebar.grounding.terraform_corroborator import _match, _parse_subject


def _parsed_data_subject():
    subject = _parse_subject("declaration_present", "data.aws_ami.base", "")
    assert subject is not None
    assert subject.klass == "data_resource"
    return subject


def _module_json(data_resources: dict[str, Any]) -> dict[str, Any]:
    return {"data_resources": data_resources}


def test_data_resource_matches_full_tool_address() -> None:
    subject = _parsed_data_subject()
    data = _module_json(
        {
            "data.aws_ami.base": {
                "mode": "data",
                "type": "aws_ami",
                "name": "base",
                "pos": {"filename": "main.tf", "line": 4},
            }
        }
    )

    location, detail = _match(data, subject, ".")

    assert detail is None
    assert location == {"file": "main.tf", "line_start": 4, "line_end": 4}


def test_data_resource_does_not_match_stripped_address() -> None:
    subject = _parsed_data_subject()
    data = _module_json(
        {
            "aws_ami.base": {
                "mode": "data",
                "type": "aws_ami",
                "name": "base",
                "pos": {"filename": "main.tf", "line": 4},
            }
        }
    )

    location, detail = _match(data, subject, ".")

    assert location is None
    assert detail == "no_unique_address"
