"""CloudWatch alarm descriptions must fit AWS' byte-sized provider limit."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TF_DIR = _REPO_ROOT / "infra" / "terraform"
_ALARM_DESCRIPTION_BYTE_LIMIT = 1024
_MIN_EXPECTED_ALARM_DESCRIPTIONS = 34

_ALARM_RE = re.compile(
    r'resource\s+"aws_cloudwatch_metric_alarm"\s+"(?P<name>[^"]+)"\s*\{',
)
_HEREDOC_OPEN_RE = re.compile(r"<<(?P<dedent>-?)(?P<delimiter>[A-Za-z_][A-Za-z0-9_]*)")
_ALARM_DESCRIPTION_RE = re.compile(
    r"^[ \t]*alarm_description[ \t]*=[ \t]*"
    r"<<(?P<dedent>-?)(?P<delimiter>[A-Za-z_][A-Za-z0-9_]*)[ \t]*\r?\n",
    re.MULTILINE,
)


@dataclass(frozen=True)
class AlarmDescription:
    file_name: str
    resource_name: str
    value: str

    @property
    def byte_count(self) -> int:
        return len(self.value.encode("utf-8"))


def _mask_heredocs(source: str) -> str:
    """Blank heredoc bodies while preserving indices for brace matching."""
    masked = list(source)
    index = 0
    while index < len(source):
        match = _HEREDOC_OPEN_RE.search(source, index)
        if match is None:
            break
        line_end = source.find("\n", match.end())
        if line_end == -1:
            break
        terminator = re.compile(
            rf"^[ \t]*{re.escape(match.group('delimiter'))}[ \t]*\r?$",
            re.MULTILINE,
        ).search(source, line_end + 1)
        if terminator is None:
            index = line_end + 1
            continue
        for char_index in range(match.start(), terminator.end()):
            if source[char_index] != "\n":
                masked[char_index] = " "
        index = terminator.end()
    return "".join(masked)


def _alarm_blocks() -> list[tuple[str, str, str]]:
    blocks: list[tuple[str, str, str]] = []
    for path in sorted(_TF_DIR.glob("*.tf")):
        source = path.read_text(encoding="utf-8")
        masked = _mask_heredocs(source)
        assert len(masked) == len(source), f"mask desynchronised for {path}"
        for match in _ALARM_RE.finditer(source):
            start = match.end() - 1
            depth = 0
            index = start
            while index < len(masked):
                if masked[index] == "{":
                    depth += 1
                elif masked[index] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                index += 1
            assert depth == 0, f"unbalanced braces parsing alarm {match.group('name')!r} in {path}"
            file_name = path.relative_to(_REPO_ROOT).as_posix()
            blocks.append((file_name, match.group("name"), source[start : index + 1]))
    return blocks


def _dedent_indented_heredoc(body: str) -> str:
    lines = body.splitlines(keepends=True)
    indents = [len(re.match(r"[ \t]*", line).group(0)) for line in lines if line.strip()]
    if not indents:
        return body
    width = min(indents)
    return "".join(line[width:] if line.strip() else line for line in lines)


def _description_from_block(
    file_name: str,
    resource_name: str,
    block: str,
) -> AlarmDescription | None:
    match = _ALARM_DESCRIPTION_RE.search(block)
    if match is None:
        return None
    terminator = re.compile(
        rf"^[ \t]*{re.escape(match.group('delimiter'))}[ \t]*\r?$",
        re.MULTILINE,
    ).search(block, match.end())
    assert terminator is not None, (
        f"{file_name}:{resource_name} has an unterminated alarm_description heredoc"
    )
    value = block[match.end() : terminator.start()]
    if match.group("dedent") == "-":
        value = _dedent_indented_heredoc(value)
    return AlarmDescription(file_name, resource_name, value)


def _alarm_descriptions() -> list[AlarmDescription]:
    descriptions: list[AlarmDescription] = []
    for file_name, resource_name, block in _alarm_blocks():
        description = _description_from_block(file_name, resource_name, block)
        if description is not None:
            descriptions.append(description)
    return descriptions


def test_parser_finds_the_alarm_descriptions_it_guards() -> None:
    descriptions = _alarm_descriptions()
    assert len(descriptions) >= _MIN_EXPECTED_ALARM_DESCRIPTIONS, (
        f"parsed only {len(descriptions)} alarm_description heredocs under {_TF_DIR}, "
        f"expected at least {_MIN_EXPECTED_ALARM_DESCRIPTIONS}"
    )


def test_alarm_descriptions_fit_the_provider_byte_limit() -> None:
    offenders = [
        (
            f"{description.file_name}:{description.resource_name} alarm_description is "
            f"{description.byte_count} UTF-8 bytes"
        )
        for description in _alarm_descriptions()
        if description.byte_count > _ALARM_DESCRIPTION_BYTE_LIMIT
    ]
    assert not offenders, (
        "AWS validates alarm_description in UTF-8 bytes, not Python characters; limit is "
        f"{_ALARM_DESCRIPTION_BYTE_LIMIT} bytes: " + "; ".join(offenders)
    )
