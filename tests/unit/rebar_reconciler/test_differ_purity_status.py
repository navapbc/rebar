"""AST + string-literal purity test for differ.py (story 3d5a / dd-3).

The differ MUST be agnostic to status-mapping logic; that's the applier's
concern (via DSO_RECONCILER_STATUS_GATING + local_to_jira_status). This
test catches dynamic references (getattr, importlib, dict lookups) that
would couple the differ to status mapping at runtime.
"""
import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DIFFER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "differ.py"

FORBIDDEN_NAME = "local_to_jira_status"


def test_differ_ast_does_not_reference_local_to_jira_status():
    """AST walk: no Name, Attribute, or ImportFrom references local_to_jira_status."""
    source = DIFFER_PATH.read_text()
    tree = ast.parse(source)
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == FORBIDDEN_NAME:
            hits.append(("Name", node.lineno))
        if isinstance(node, ast.Attribute) and node.attr == FORBIDDEN_NAME:
            hits.append(("Attribute", node.lineno))
        if isinstance(node, ast.ImportFrom):
            for alias in (node.names or []):
                if alias.name == FORBIDDEN_NAME:
                    hits.append(("ImportFrom", node.lineno))
    assert not hits, f"differ.py references {FORBIDDEN_NAME!r} at: {hits}"


def test_differ_source_does_not_contain_string_literal():
    """String-literal grep: catches getattr/dynamic name access via the literal string."""
    source = DIFFER_PATH.read_text()
    # A bare literal occurrence would be caught by AST too, but dynamic getattr
    # uses string literals which look like normal strings — so we grep.
    assert FORBIDDEN_NAME not in source, (
        f"differ.py source contains literal {FORBIDDEN_NAME!r} — possible dynamic coupling"
    )
