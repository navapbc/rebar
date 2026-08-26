"""HELD-OUT structural oracle for the Jira-family sub-seam (story J2, epic e369).

This file is HELD OUT from the implementation subagent. It is the machine-checked
import contract the epic requires (§6): the drift this seam exists to prevent
recurs the moment a contributor adds Cloud-specific logic to the shared layer, so
the boundary ships enforced rather than conventional.

It is implemented as an AST walk inside the ordinary ``make test`` run that CI's
``Verified`` gate already executes — no new CI job and no new dev dependency —
matching how this repo already enforces structural rules (``test_backend_neutrality.py``).

What it pins:

1. the permitted import graph under ``adapters/`` (acyclic, one direction);
2. that ``jira_family/`` imports NONE of the Cloud-pinned vendor modules — its
   ADF/comment-limit dependencies arrive as injected contracts;
3. that no re-export shim was left at any pre-move path;
4. that exactly one dict literal defines each value map WITHIN ``adapters/``;
5. that no location-pinned module changed path (ADR 0035 §(a));
6. that the new imports survive a by-path ``spec_from_file_location`` load with no
   package context — i.e. they are absolute, never relative.

The checker itself is given teeth by ``test_checker_flags_a_synthetic_violation``:
a tautological graph checker that never fires would pass every assertion here.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest
from _tree_scan import parsed_python_files

_SRC = Path(__file__).resolve().parents[3] / "src" / "rebar" / "_engine"
_REC = _SRC / "rebar_reconciler"
_ADAPTERS = _REC / "adapters"

_ROOT_PKG = "rebar_reconciler"
_JIRA = f"{_ROOT_PKG}.adapters.jira"
_FAMILY = f"{_ROOT_PKG}.adapters.jira_family"
_DC = f"{_ROOT_PKG}.adapters.jira_datacenter"

# The location-pinned modules (ADR 0035 §(a)) — loaded BY PATH, so relocating one
# breaks the dynamic loader. Their CONTENTS may change; their paths may not.
_PINNED_PATHS = (
    "adapters/jira/adf.py",
    "adapters/jira/outbound_fields.py",
    "adapters/jira/comment_limits.py",
)

# Cloud-pinned vendor modules the shared layer must never reach for.
_FORBIDDEN_IN_FAMILY = ("adf", "comment_limits", "outbound_fields")


# ---------------------------------------------------------------------------
# Import-graph extraction
# ---------------------------------------------------------------------------


def _package_of(path: Path) -> str:
    """The dotted package a module file lives in (``…adapters.jira`` for acli.py)."""
    rel = path.relative_to(_SRC).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    else:
        parts.pop()
    return ".".join(parts)


def _imported_modules(source: str, package: str) -> set[str]:
    """Fully-qualified module names imported by ``source``, resolving relatives.

    Relative imports are resolved against ``package`` so ``from ..jira import x``
    inside ``adapters/jira_family/`` is reported as ``…adapters.jira``.
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = package.split(".")
                # level 1 == current package, level 2 == its parent, …
                trimmed = base_parts[: len(base_parts) - (node.level - 1)]
                base = ".".join(trimmed)
                if node.module:
                    found.add(f"{base}.{node.module}")
                else:
                    # ``from . import adf`` — each name is a submodule
                    found.update(f"{base}.{alias.name}" for alias in node.names)
            elif node.module:
                found.add(node.module)
    return found


def _is_within(module: str, package: str) -> bool:
    return module == package or module.startswith(package + ".")


def _violations(source: str, package: str) -> list[str]:
    """Import-contract violations for one module. Empty list == compliant."""
    problems: list[str] = []
    imports = _imported_modules(source, package)

    if _is_within(package, _FAMILY):
        for imported in sorted(imports):
            if _is_within(imported, _JIRA) or _is_within(imported, _DC):
                problems.append(
                    f"{package} (shared layer) imports {imported} — the Jira-family "
                    f"layer must not depend on a concrete backend. Inject the "
                    f"dependency as a contract object instead."
                )
            elif imported.split(".")[-1] in _FORBIDDEN_IN_FAMILY:
                problems.append(
                    f"{package} (shared layer) imports the Cloud-pinned vendor module "
                    f"{imported!r} — it must arrive as an injected contract."
                )
    else:
        for imported in sorted(imports):
            if _is_within(imported, _FAMILY) and not (
                _is_within(package, _JIRA) or _is_within(package, _DC)
            ):
                problems.append(
                    f"{package} imports {imported} — only adapters/jira/ and "
                    f"adapters/jira_datacenter/ may consume the Jira-family layer."
                )

    # Sibling adapter packages are mutually sealed, in BOTH directions. J2 could
    # assert only Cloud -> DC because adapters/jira_datacenter/ did not exist yet;
    # story J7 closes the reciprocal half. Asymmetry would leave the seam half-open:
    # the drift this boundary prevents is a concrete backend reaching sideways for
    # the other's vendor internals instead of sharing through jira_family/.
    for near, far in ((_JIRA, _DC), (_DC, _JIRA)):
        if _is_within(package, near):
            for imported in sorted(imports):
                if _is_within(imported, far):
                    problems.append(f"{package} imports sibling adapter package {imported}.")

    return problems


# ---------------------------------------------------------------------------
# 1–2. the permitted import graph
# ---------------------------------------------------------------------------


def test_jira_family_package_exists() -> None:
    """Precondition: the shared layer was actually created (otherwise the graph
    assertions below would pass vacuously)."""
    assert (_ADAPTERS / "jira_family").is_dir(), (
        "adapters/jira_family/ does not exist — the shared layer was not extracted."
    )
    assert (_ADAPTERS / "jira_family" / "__init__.py").is_file()


def test_import_graph_contract_holds_for_every_engine_module() -> None:
    """The acyclic, one-direction seam: shared layer <- concrete backends."""
    all_problems: list[str] = []
    for module in parsed_python_files(_REC):
        if "__pycache__" in module.path.parts:
            continue
        all_problems.extend(_violations(module.source, _package_of(module.path)))
    assert not all_problems, "import contract violated:\n  " + "\n  ".join(all_problems)


def test_shared_layer_reaches_for_no_cloud_vendor_module() -> None:
    """Explicit form of the epic's AC: grep-level proof over jira_family/ alone."""
    family = _ADAPTERS / "jira_family"
    offenders = []
    for module in parsed_python_files(family):
        for forbidden in (*_FORBIDDEN_IN_FAMILY, "acli"):
            for node in ast.walk(module.tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    tail = name.split(".")[-1]
                    if tail == forbidden or (forbidden == "acli" and tail.startswith("acli")):
                        offenders.append(f"{module.path.name} imports {name}")
    assert not offenders, "jira_family/ imports Cloud vendor modules: " + "; ".join(offenders)


def test_checker_flags_a_synthetic_violation() -> None:
    """Teeth. A checker that never fires would pass every assertion above, so prove
    it rejects each forbidden edge — and, as the negative control, that it does NOT
    fire on the permitted direction.

    The ``package`` argument is what ``_package_of`` yields for a module in that
    package (the package, not the module), which is what fixes the relative-import
    arithmetic below.
    """
    # shared layer -> concrete backend, in all three import spellings
    assert _violations("from rebar_reconciler.adapters.jira.adf import text_to_adf", _FAMILY)
    assert _violations("from ..jira import adf", _FAMILY)
    assert _violations("from . import adf", _FAMILY)
    assert _violations("import rebar_reconciler.adapters.jira.comment_limits", _FAMILY)
    # a module outside the two sanctioned backends consuming the shared layer
    assert _violations(f"from {_FAMILY} import sanitize_label", _ROOT_PKG)
    # sideways edge between sibling adapter packages — BOTH directions.
    # J2 could only assert Cloud -> DC, because adapters/jira_datacenter/ did not
    # exist yet; a DC module importing adapters/jira/ passed the checker until J7.
    # The seam is only real if it is symmetric: the whole point is that neither
    # concrete backend may reach into the other, sharing ONLY via jira_family/.
    assert _violations(f"from {_DC} import x", _JIRA)
    assert _violations(f"from {_JIRA} import x", _DC)
    assert _violations("from rebar_reconciler.adapters.jira.adf import text_to_adf", _DC)
    assert _violations("import rebar_reconciler.adapters.jira.comment_limits", _DC)
    assert _violations("from ..jira import adf", _DC)

    # negative controls — the permitted direction must NOT be flagged
    assert not _violations(f"from {_FAMILY} import sanitize_label", _JIRA)
    assert not _violations(f"from {_FAMILY} import sanitize_label", _DC)
    assert not _violations("from rebar_reconciler.mutation import Mutation", _FAMILY)
    assert not _violations("from rebar_reconciler.adapters.jira.adf import x", _JIRA)


# ---------------------------------------------------------------------------
# 3. no re-export shim at any pre-move path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("module_path", "moved_name"),
    [
        ("adapters/jira/jira_fields.py", "_LOCAL_STATUS_TO_JIRA"),
        ("adapters/jira/jira_fields.py", "_LOCAL_PRIORITY_TO_JIRA"),
        ("adapters/jira/jira_fields.py", "_RELATION_TO_JIRA_LINK"),
    ],
)
def test_no_reexport_shim_remains_at_pre_move_path(module_path: str, moved_name: str) -> None:
    """The moved value maps and link vocabulary are GONE from their old module.

    Re-adding a binding so an old import keeps resolving is exactly the shim ADR
    0035 decision 4 forbids (tests patch module-qualified, so a shim creates a
    patch-binding bug) — and for the value maps it would also re-create the second
    copy this story exists to delete.
    """
    text = (_REC / module_path).read_text()
    tree = ast.parse(text)
    bound = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            bound.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            bound.add(node.target.id)
        elif isinstance(node, ast.ImportFrom):
            bound.update(a.asname or a.name for a in node.names)
    assert moved_name not in bound, (
        f"{module_path} still binds {moved_name} — it moved to adapters/jira_family/ "
        f"and no re-export shim may remain at the old path."
    )


def test_identity_module_left_no_shim_behind() -> None:
    """``identity.py`` relocates whole, so nothing may remain at the old path."""
    assert not (_ADAPTERS / "jira" / "identity.py").exists(), (
        "adapters/jira/identity.py still exists — JiraIdentityConvention relocates "
        "whole to adapters/jira_family/ with no shim left behind."
    )


# ---------------------------------------------------------------------------
# 4. exactly one dict literal per value map, within adapters/
# ---------------------------------------------------------------------------


def _modules_defining_map(*, keys: set[str], value_sample: str) -> list[str]:
    """Modules under adapters/ that assign a dict LITERAL containing ``keys``."""
    hits: list[str] = []
    for module in parsed_python_files(_ADAPTERS):
        if "__pycache__" in module.path.parts:
            continue
        for node in ast.walk(module.tree):
            value = None
            if isinstance(node, ast.Assign):
                value = node.value
            elif isinstance(node, ast.AnnAssign):
                value = node.value
            if not isinstance(value, ast.Dict):
                continue
            literal_keys = {k.value for k in value.keys if isinstance(k, ast.Constant)}
            literal_values = {v.value for v in value.values if isinstance(v, ast.Constant)}
            if keys <= literal_keys and value_sample in literal_values:
                hits.append(str(module.path.relative_to(_ADAPTERS)))
    return hits


def test_exactly_one_status_map_literal_within_adapters() -> None:
    """``config.py``'s third copy is knowingly deferred to bug fe15 and lives
    OUTSIDE adapters/, so the scope here is adapters/ — matching the story's AC."""
    hits = _modules_defining_map(keys={"open", "in_progress", "closed"}, value_sample="To Do")
    assert len(hits) == 1, f"expected exactly one local->Jira status map literal, found: {hits}"
    assert hits[0].startswith("jira_family/"), (
        f"the sole status map must live in jira_family/, found {hits[0]}"
    )


def test_exactly_one_priority_map_literal_within_adapters() -> None:
    hits = _modules_defining_map(keys={0, 1, 2, 3, 4}, value_sample="Highest")
    assert len(hits) == 1, f"expected exactly one local->Jira priority map literal, found: {hits}"
    assert hits[0].startswith("jira_family/"), (
        f"the sole priority map must live in jira_family/, found {hits[0]}"
    )


def test_exactly_one_type_map_literal_within_adapters() -> None:
    """Story bd9e: the local->Jira TYPE map was the third instance of this defect
    class — duplicated across the Cloud and DC create paths with no guard covering
    it. Same shape as the status/priority guards above, so a future third copy fails
    the build instead of silently diverging the two create paths."""
    hits = _modules_defining_map(keys={"bug", "story", "task", "epic"}, value_sample="Bug")
    assert len(hits) == 1, f"expected exactly one local->Jira type map literal, found: {hits}"
    assert hits[0].startswith("jira_family/"), (
        f"the sole ticket-type map must live in jira_family/, found {hits[0]}"
    )


def test_both_create_paths_resolve_to_the_single_type_definition() -> None:
    """Identity, not equality: the Cloud create path and the DC create path must read
    the SAME object, and Cloud must still expose it under the historical private name
    (``test_outbound_differ_session_log_exclusion`` reads it as a module attribute)."""
    from rebar_reconciler.adapters.jira import outbound_fields
    from rebar_reconciler.adapters.jira_datacenter import backend
    from rebar_reconciler.adapters.jira_family import LOCAL_TYPE_TO_JIRA

    assert outbound_fields._LOCAL_TO_JIRA_TYPE is LOCAL_TYPE_TO_JIRA
    assert backend._LOCAL_TO_JIRA_TYPE is LOCAL_TYPE_TO_JIRA


@pytest.mark.parametrize(
    ("local_type", "jira_type"),
    [("task", "Task"), ("story", "Story"), ("bug", "Bug"), ("epic", "Epic")],
)
def test_create_paths_pin_the_issue_type_by_value(local_type: str, jira_type: str) -> None:
    """Pinned BY VALUE through both create paths, not by the map's shape: the reason
    the duplication mattered is that a drift would change the built ``issuetype``
    field per deployment. The two copies were content-identical before the
    unification, so both paths must still emit these exact values."""
    from rebar_reconciler.adapters.jira.outbound_fields import _map_local_to_jira_fields
    from rebar_reconciler.adapters.jira_datacenter.backend import _map_local_to_dc_fields

    ticket = {"ticket_type": local_type, "title": "t", "description": "d"}
    assert _map_local_to_jira_fields(dict(ticket))["issuetype"] == jira_type
    assert _map_local_to_dc_fields(dict(ticket))["issuetype"] == jira_type


def test_both_cloud_callers_resolve_to_the_single_definition() -> None:
    """The ACLI transport and the Backend port read the SAME object — not equal
    copies. Identity, not equality, is what proves de-duplication."""
    from rebar_reconciler.adapters.jira import acli, outbound_fields
    from rebar_reconciler.adapters.jira_family import (
        LOCAL_PRIORITY_TO_JIRA,
        LOCAL_STATUS_TO_JIRA,
    )

    assert acli._LOCAL_STATUS_TO_JIRA is LOCAL_STATUS_TO_JIRA
    assert acli._LOCAL_PRIORITY_TO_JIRA is LOCAL_PRIORITY_TO_JIRA
    assert outbound_fields._LOCAL_TO_JIRA_STATUS is LOCAL_STATUS_TO_JIRA
    assert outbound_fields._LOCAL_TO_JIRA_PRIORITY is LOCAL_PRIORITY_TO_JIRA


# ---------------------------------------------------------------------------
# 5. collateral invariant: no location-pinned module changed path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("relpath", _PINNED_PATHS)
def test_location_pinned_module_still_at_its_pinned_path(relpath: str) -> None:
    """ADR 0035 §(a): these are loaded by filename, so a rename breaks the loader.
    Relocating them is ADR 0035 Phase 2's work and out of scope for this story."""
    assert (_REC / relpath).is_file(), f"location-pinned module moved: {relpath}"


def test_loader_module_still_exists_and_was_not_relocated() -> None:
    """``_loader.py`` is explicitly out of scope: it resolves the pinned modules by
    path, so teaching it a new sub-package is ADR 0035 Phase 2's work. That its
    CONTENT is byte-identical is verified at review time by ``git diff --stat``;
    what a test can cheaply pin is that it was not moved or deleted."""
    assert (_REC / "_loader.py").is_file()


def test_no_pinned_module_was_copied_into_the_shared_layer() -> None:
    family = _ADAPTERS / "jira_family"
    for relpath in _PINNED_PATHS:
        name = Path(relpath).name
        assert not (family / name).exists(), (
            f"{name} was copied into jira_family/ — pinned modules are shared by "
            f"dependency inversion, not relocation."
        )


# ---------------------------------------------------------------------------
# 6. the new imports are ABSOLUTE — proven under by-path load
# ---------------------------------------------------------------------------


def _load_by_path(relpath: str, synthetic_name: str):
    """Load a module the way the reconciler's dynamic loader and several existing
    tests do: ``spec_from_file_location`` under a synthetic name, so the module has
    NO package context (``__package__ == ''``). A relative import fails here with
    ``ImportError: attempted relative import with no known parent package``."""
    path = _REC / relpath
    spec = importlib.util.spec_from_file_location(synthetic_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[synthetic_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(synthetic_name, None)
    return module


def test_pinned_outbound_fields_still_loads_by_path_after_its_import_rewrite() -> None:
    """``outbound_fields.py`` keeps its path but its dict literals became an import.
    It is loaded by path from three existing tests, so that import must be absolute."""
    module = _load_by_path("adapters/jira/outbound_fields.py", "_j2_probe_outbound_fields")
    assert module._LOCAL_TO_JIRA_STATUS["idea"] == "IDEA"
    assert module._LOCAL_TO_JIRA_PRIORITY[0] == "Highest"
    # Story bd9e: the type map became an import here too, so it is covered by the
    # same absolute-import requirement.
    assert module._LOCAL_TO_JIRA_TYPE["epic"] == "Epic"


def test_relocated_value_map_module_loads_by_path() -> None:
    """The shared layer's own map module must also survive the no-package-context
    load mode, since it is now what the by-path parity test reads."""
    family = _ADAPTERS / "jira_family"
    candidates = [
        module.path
        for module in parsed_python_files(family)
        if module.path.name != "__init__.py" and "LOCAL_STATUS_TO_JIRA" in module.source
    ]
    assert candidates, "no module under jira_family/ defines the status map"
    target = candidates[0]
    module = _load_by_path(str(target.relative_to(_REC)), f"_j2_probe_{target.stem}")
    status_map = getattr(module, "LOCAL_STATUS_TO_JIRA", None)
    assert status_map is not None, "the shared status map is not exported under a public name"
    assert status_map["idea"] == "IDEA"
    assert status_map["deleted"] == "Done"
