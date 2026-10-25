"""The Bedrock spend cap must enroll every Bedrock-capable role it is meant to deny.

``bedrock-principal-audit`` reported ``OrganizationAccountAccessRole`` (AdministratorAccess)
as unenrolled every day, because the cap's deny did not cover it. The role is enrolled by
decision, so the Terraform default must keep it in ``bedrock_cap_target_roles``.
"""

from __future__ import annotations

import re
from pathlib import Path

_TF = Path(__file__).resolve().parents[2] / "infra" / "terraform" / "bedrock_spend_cap.tf"
_ROLE = "OrganizationAccountAccessRole"


def _variable_block(source: str, name: str) -> str:
    start = source.index(f'variable "{name}"')
    depth, index = 0, source.index("{", start)
    begin = index
    while True:
        depth += {"{": 1, "}": -1}.get(source[index], 0)
        index += 1
        if depth == 0:
            return source[begin:index]


def _default_roles(block: str) -> list[str]:
    match = re.search(r"^\s*default\s*=\s*\[(.*?)\]", block, re.MULTILINE | re.DOTALL)
    assert match, "bedrock_cap_target_roles has no list default"
    return re.findall(r'"([^"]+)"', match.group(1))


def test_the_org_access_role_is_enrolled_in_the_spend_cap() -> None:
    block = _variable_block(_TF.read_text(encoding="utf-8"), "bedrock_cap_target_roles")
    roles = _default_roles(block)
    assert len(roles) >= 11, f"parsed only {len(roles)} roles; the parser has gone blind"
    assert _ROLE in roles, f"{_ROLE} is Bedrock-capable but not enrolled in the cap: {roles}"
    assert "BedrockInvocationLoggingRole" not in roles, "the logging role must stay exempt"


def test_the_variable_description_says_what_enrolling_the_org_role_means() -> None:
    block = _variable_block(_TF.read_text(encoding="utf-8"), "bedrock_cap_target_roles")
    assert _ROLE in block.split("default", 1)[0], "the description must name the enrolled role"


def test_a_list_without_the_role_is_flagged() -> None:
    """The pre-fix shape: the 11 roles the cap enrolled before the org access role."""
    seeded = 'variable "x" {\n  default = [\n    "a",\n    "b",\n  ]\n}\n'
    assert _ROLE not in _default_roles(_variable_block(seeded, "x"))
