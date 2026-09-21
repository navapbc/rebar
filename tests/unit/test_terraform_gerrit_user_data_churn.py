"""Regression guard for the Gerrit EC2 ``user_data`` hash churn [rebar:4bfa].

The production instance manages its boot payload through ``user_data_base64`` because the
plain ``user_data`` script is larger than EC2's raw ``UserData`` limit. Older state can still
carry a legacy ``user_data`` hash alongside that managed base64 payload; in AWS provider
5.100.0 the legacy field is ``Optional + Computed`` with a hash ``StateFunc``, so Terraform
can re-hash that state-only value forever even when ``user_data_base64`` is unchanged.

This source-shape guard keeps the intentional split explicit: ignore the provider's ghost
``user_data`` comparison, but keep managing the real ``user_data_base64`` payload.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_TF = REPO_ROOT / "infra" / "terraform" / "main.tf"

_GERRIT_INSTANCE_RE = re.compile(
    r'resource\s+"aws_instance"\s+"gerrit"\s*\{',
)
_LIFECYCLE_RE = re.compile(r"\blifecycle\s*\{")
_IGNORE_CHANGES_RE = re.compile(r"\bignore_changes\s*=\s*\[(?P<body>[^\]]*)\]", re.DOTALL)
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _mask_noncode(src: str) -> str:
    """Blank comments and string bodies while preserving indices for brace parsing."""
    out = list(src)
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        if ch == "#" or (ch == "/" and i + 1 < n and src[i + 1] == "/"):
            while i < n and src[i] != "\n":
                out[i] = " "
                i += 1
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "*":
            while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
                if src[i] != "\n":
                    out[i] = " "
                i += 1
            if i < n:
                out[i] = " "
                i += 1
            if i < n:
                out[i] = " "
                i += 1
            continue
        if ch == '"':
            out[i] = " "
            i += 1
            while i < n and src[i] != '"':
                if src[i] == "\\" and i + 1 < n:
                    out[i] = " "
                    i += 1
                if i < n:
                    if src[i] != "\n":
                        out[i] = " "
                    i += 1
            if i < n:
                out[i] = " "
                i += 1
            continue
        i += 1
    return "".join(out)


def _block_at(masked: str, open_brace: int) -> str:
    depth = 0
    for idx in range(open_brace, len(masked)):
        if masked[idx] == "{":
            depth += 1
        elif masked[idx] == "}":
            depth -= 1
            if depth == 0:
                return masked[open_brace : idx + 1]
    raise AssertionError("unbalanced braces parsing aws_instance.gerrit")


def _gerrit_instance_block() -> str:
    src = MAIN_TF.read_text(encoding="utf-8")
    masked = _mask_noncode(src)
    match = _GERRIT_INSTANCE_RE.search(src)
    assert match is not None, "infra/terraform/main.tf no longer declares aws_instance.gerrit"
    return _block_at(masked, match.end() - 1)


def _lifecycle_block(instance_block: str) -> str:
    match = _LIFECYCLE_RE.search(instance_block)
    assert match is not None, "aws_instance.gerrit must declare a lifecycle block"
    return _block_at(instance_block, match.end() - 1)


def _ignore_changes_tokens(lifecycle_block: str) -> set[str]:
    match = _IGNORE_CHANGES_RE.search(lifecycle_block)
    assert match is not None, "aws_instance.gerrit lifecycle must declare ignore_changes"
    return set(_IDENTIFIER_RE.findall(match.group("body")))


def test_gerrit_instance_manages_user_data_base64_payload() -> None:
    """The actual boot payload stays managed through ``user_data_base64``."""
    block = _gerrit_instance_block()

    assert re.search(r"\buser_data_base64\s*=\s*base64gzip\s*\(", block), (
        "aws_instance.gerrit must keep managing its gzipped boot payload through "
        "user_data_base64; dropping this silently stops Terraform managing boot config"
    )
    assert not re.search(r"\buser_data\s*=", block), (
        "aws_instance.gerrit must not switch back to raw user_data; the rendered script is "
        "larger than EC2's plain UserData limit"
    )


def test_gerrit_instance_ignores_only_the_ghost_user_data_hash() -> None:
    """Ignore the provider's state-only ``user_data`` hash, not the managed payload."""
    ignored = _ignore_changes_tokens(_lifecycle_block(_gerrit_instance_block()))

    assert "user_data" in ignored, (
        "aws_instance.gerrit must ignore the provider's legacy user_data hash; otherwise "
        "Terraform re-hashes the state-only value forever even when user_data_base64 matches AWS"
    )
    assert "user_data_base64" not in ignored, (
        "aws_instance.gerrit must keep managing the real user_data_base64 payload; ignoring it "
        "would hide boot-configuration drift"
    )
