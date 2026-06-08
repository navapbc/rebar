"""Shared markdown section extraction helpers for test files.

Provides extract_section() for extracting content between markdown headings.
Used by tests that parse SKILL.md and other documentation files.
"""

from __future__ import annotations


def extract_section(content: str, section_heading: str) -> str:
    """Extract content from a section heading until the next same-level heading."""
    lines = content.splitlines()
    in_section = False
    section_lines = []
    heading_prefix = section_heading.split(" ")[0]  # e.g., "##" or "###"

    for line in lines:
        if line.strip() == section_heading or line.startswith(section_heading + " "):
            in_section = True
            section_lines.append(line)
            continue
        if in_section:
            # Stop at a heading of the same or higher level
            if line.startswith(heading_prefix + " ") and line != section_heading:
                break
            section_lines.append(line)

    return "\n".join(section_lines)
