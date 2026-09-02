"""Glob + YAML mechanism detectors: ``ci_gate`` and ``test_helper``.

Split from the other detectors by INPUT SURFACE: neither of these is answered by parsing
Python semantics. A gate script and a test helper are identified by WHERE THEY LIVE (a
filename glob), and a workflow step by its position in a YAML document — so this module owns
the only PyYAML dependency in the ratchet (already a dev dep, and only ever used to READ).

``ci_gate``
    Two shapes, one kind. A ``scripts/check_*.py`` file IS a gate — the repository's own
    convention, and the shape ``make lint`` wires in — and a workflow step with a ``run:`` is
    a gate the build runs directly. Both add a way for the build to fail that did not exist
    before, which is exactly the surface this ratchet bounds. Note that this counts the
    ratchet's own gate script: bounding its own epic is intended, not an accident.

``test_helper``
    ``tests/_*.py`` — the underscore prefix is how this repository marks a module that is
    imported by tests rather than collected as one. A new shared helper is new coupling
    across the suite: every test that grows a dependency on it inherits its assumptions.

Both shapes are FILENAME-GLOB shaped, so their markers live in the matched file's first
lines rather than beside a definition; the workflow-step shape is line-anchored to the
step's ``- name:``/``run:``.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .markers import Site

GATE_SCRIPT_GLOB = "check_*.py"
GATE_SCRIPT_DIR = "scripts"
WORKFLOW_DIR = ".github/workflows"
WORKFLOW_SUFFIXES = (".yml", ".yaml")
TEST_HELPER_DIR = "tests"
TEST_HELPER_GLOB = "_*.py"

# Longest run-snippet used to name an unnamed workflow step.
_SNIPPET_CHARS = 60


def _detect_gate_scripts(repo_root: Path) -> list[Site]:
    """Every ``scripts/check_*.py`` gate, named by its repo-relative path."""
    directory = repo_root / GATE_SCRIPT_DIR
    if not directory.is_dir():
        return []
    return [
        (path.relative_to(repo_root).as_posix(), path, None)
        for path in sorted(directory.glob(GATE_SCRIPT_GLOB))
    ]


def _scalar(node: yaml.Node | None) -> str:
    return node.value if isinstance(node, yaml.ScalarNode) else ""


def _step_name(mapping: yaml.MappingNode) -> tuple[str, int] | None:
    """``(step name, 1-based marker line)`` for a mapping that is a ``run:`` step.

    The step's own ``name:`` is preferred; an unnamed step falls back to a truncated
    snippet of its first ``run:`` line, which is stable as long as the command is.
    """
    run_node: yaml.Node | None = None
    name_node: yaml.Node | None = None
    run_key: yaml.Node | None = None
    name_key: yaml.Node | None = None
    for key, value in mapping.value:
        if not isinstance(key, yaml.ScalarNode):
            continue
        if key.value == "run":
            run_key, run_node = key, value
        elif key.value == "name":
            name_key, name_node = key, value
    if run_node is None or run_key is None:
        return None
    anchor = name_key if name_key is not None else run_key
    label = _scalar(name_node).strip()
    if not label:
        first = next((ln.strip() for ln in _scalar(run_node).splitlines() if ln.strip()), "")
        label = first[:_SNIPPET_CHARS]
    if not label:
        return None
    return label, anchor.start_mark.line + 1


def _walk_nodes(node: yaml.Node):
    yield node
    if isinstance(node, yaml.MappingNode):
        for key, value in node.value:
            yield from _walk_nodes(key)
            yield from _walk_nodes(value)
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            yield from _walk_nodes(item)


def _detect_workflow_steps(repo_root: Path) -> list[Site]:
    """Every workflow step carrying a ``run:``, named ``<workflow path>::<step>``."""
    directory = repo_root / WORKFLOW_DIR
    if not directory.is_dir():
        return []
    sites: list[Site] = []
    for path in sorted(directory.iterdir()):
        if path.suffix not in WORKFLOW_SUFFIXES or not path.is_file():
            continue
        try:
            root = yaml.compose(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if root is None:
            continue
        rel = path.relative_to(repo_root).as_posix()
        for node in _walk_nodes(root):
            if not isinstance(node, yaml.MappingNode):
                continue
            named = _step_name(node)
            if named is not None:
                sites.append((f"{rel}::{named[0]}", path, named[1]))
    return sites


def detect_ci_gates(repo_root: Path) -> list[Site]:
    """Gate scripts and workflow ``run:`` steps — one kind, two detection shapes."""
    return _detect_gate_scripts(repo_root) + _detect_workflow_steps(repo_root)


def detect_test_helpers(repo_root: Path) -> list[Site]:
    """Every ``tests/_*.py`` shared helper module, named by its repo-relative path."""
    directory = repo_root / TEST_HELPER_DIR
    if not directory.is_dir():
        return []
    return [
        (path.relative_to(repo_root).as_posix(), path, None)
        for path in sorted(directory.glob(TEST_HELPER_GLOB))
    ]
