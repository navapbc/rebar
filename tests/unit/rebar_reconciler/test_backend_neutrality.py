"""Backend-neutrality gate for the reconciler core (epic bbf1 / story S4).

S4 routes the backend-neutral core through the ``Backend`` port instead of naming
Jira concretely. These assertions pin the two unambiguous neutrality wins so a
regression that re-introduces an inline Jira transport construction (or a hard-coded
provider literal) fails CI:

(a) No core module constructs an ``AcliClient(...)`` inline. The transport is now
    obtained from the configured backend (``select_backend(load_config()).transport``)
    via the ``_load_acli`` / ``build_acli_client_from_env`` seams; the ONLY sanctioned
    ``AcliClient(...)`` construction lives in ``adapters/jira/backend.py``'s factory,
    which these files must not duplicate.

(b) ``apply_inbound_records.py`` no longer carries the two provider-identity ``"jira"``
    literals (they now flow from the selected backend's ``vendor``); the sole remaining
    ``"jira"`` token is the deliberately-retained ``validate_creation_channel("jira")``
    creation-channel VOCABULARY key, which is out of scope.

Implemented as a source-text gate (grep-style, reading the files) so it is independent
of import wiring and fails BEFORE the S4 rewiring, PASSES after.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REC = Path(__file__).resolve().parents[3] / "src" / "rebar" / "_engine" / "rebar_reconciler"

# Core modules that must NOT construct an AcliClient inline (the seam returns the
# backend transport instead). adapters/jira/ is deliberately excluded — the factory
# there is the single sanctioned construction site.
_NO_INLINE_ACLICLIENT = (
    "applier.py",
    "fetcher.py",
    "run_differs.py",
    "_attestation.py",
)


@pytest.mark.parametrize("filename", _NO_INLINE_ACLICLIENT)
def test_no_inline_acliclient_construction(filename: str) -> None:
    """The core transport/construction sites route through the Backend port, so no
    ``AcliClient(`` construction (in code, comment, or docstring) remains."""
    path = _REC / filename
    text = path.read_text()
    assert "AcliClient(" not in text, (
        f"{filename} still constructs an AcliClient inline — route it through the "
        f"backend transport (select_backend(load_config()).transport) instead. The "
        f"only sanctioned AcliClient(...) construction lives in adapters/jira/backend.py."
    )


def test_apply_inbound_records_has_single_jira_literal() -> None:
    """``apply_inbound_records.py`` carries exactly one ``"jira"`` literal — the
    retained ``validate_creation_channel("jira")`` vocabulary key. The two former
    provider-identity literals now come from the selected backend's ``vendor``."""
    path = _REC / "apply_inbound_records.py"
    text = path.read_text()
    occurrences = text.count('"jira"')
    assert occurrences == 1, (
        f'expected exactly one "jira" literal in apply_inbound_records.py (the '
        f"validate_creation_channel vocabulary key), found {occurrences}. The two "
        f"provider-identity literals must be replaced by the backend vendor."
    )
    assert 'validate_creation_channel("jira")' in text, (
        'the single retained "jira" literal must be the '
        'validate_creation_channel("jira") vocabulary key'
    )


# ---------------------------------------------------------------------------
# Ticket 4af8 — the differ/apply CORE importer sites no longer import the vendor
# field MAPPER; they receive it via dependency injection (a Backend-port
# OutboundMapper/InboundMapper) from the orchestrator that owns the configured
# backend. This gate pins that win: a regression that re-adds a module-level
# `from ...adapters.jira.outbound_fields import _map_local_to_jira_fields` (or the
# inbound `from rebar_reconciler.inbound_fields import _map_jira_to_local_fields`)
# fails CI.
#
# The assertion is on MODULE-LEVEL imports only (via AST): a lazy import of the
# NEUTRAL registry seam (`from rebar_reconciler._backend_registry import
# select_backend`) inside a function — used by the injection fallback and by
# apply_inbound's hard-delete re-create — is fine, because it names no vendor
# mapper. Non-mapping helpers with no port equivalent (e.g. outbound_fields'
# `_diff_fields` / `_extract_jira_field`, or inbound_fields' value maps) may still
# be imported; only the mapper entrypoint is forbidden.
#
# reconcile_check.py is DELIBERATELY NOT covered here: it uses outbound_fields'
# INTERNAL `_extract_jira_field` / `_assignee_matches` (not on the Backend port) via
# a lazy `_load_sibling(...)` INSIDE its functions (not a module-level import), and
# routing those internals through the port would over-expose vendor internals for no
# behavioural gain. It stays on the lazy by-path loader and is out of scope for this
# mapper-injection gate.
_MAPPER_INJECTED = (
    ("outbound_differ.py", "_map_local_to_jira_fields"),
    ("inbound_differ.py", "_map_jira_to_local_fields"),
    ("apply_inbound.py", "_map_local_to_jira_fields"),
)


def _module_level_imported_names(path: Path) -> set[str]:
    """Return the set of symbol names imported by MODULE-LEVEL ``from X import ...``
    statements (imports nested inside functions/classes are excluded)."""
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.name)
    return names


@pytest.mark.parametrize("filename,forbidden_mapper", _MAPPER_INJECTED)
def test_differ_apply_core_does_not_import_vendor_mapper(
    filename: str, forbidden_mapper: str
) -> None:
    """The differ/apply core receives the vendor field mapper by injection, so it must
    NOT import the mapper entrypoint at module scope (ticket 4af8)."""
    names = _module_level_imported_names(_REC / filename)
    assert forbidden_mapper not in names, (
        f"{filename} imports the vendor mapper {forbidden_mapper!r} at module scope — "
        f"route it off the vendor mapper via the injected Backend-port mapper "
        f"(outbound_mapper/inbound_mapper) supplied by the orchestrator instead."
    )


# ---------------------------------------------------------------------------
# Ticket 97f2 — the four scalar-coupled core modules import NO ``adapters.jira`` /
# ``acli_subprocess`` symbol at ANY nesting depth (module scope OR inside a function).
# Their last vendor couplings — the applier's cross-project ``project`` scope, the
# fetcher's un-defaulted ``query_project`` JQL scope, the ``_attestation`` JIRA_*
# fail-fast, and the ``apply_handlers`` assignee-error catch — now route through the
# Backend port (``project`` / ``query_project`` / ``assert_env_ready`` /
# ``BackendAssigneeNotFoundError``). Unlike the top-level-only
# ``_module_level_imported_names`` gate above, every removed site was a function-nested
# lazy import, so this walks the WHOLE tree.
#
# outbound_differ.py / outbound_links.py are NOT covered here: their vendor field/link
# helpers are neutralized by sibling stories 625b / eefd, which extend this tuple.
_ZERO_ADAPTER_IMPORT_SCALAR_CORE = (
    "fetcher.py",
    "applier.py",
    "apply_handlers.py",
    "_attestation.py",
    # Ticket aff0: the inbound probe's Jira mechanics moved to adapters/jira/probe.py
    # behind the SupportsAbsenceProbe capability; the root module keeps only the neutral
    # vocabulary and imports no vendor symbol.
    "inbound_probe.py",
    # Ticket 625b: the outbound differ now compares in canonical shape (snapshot mapped via
    # the injected InboundMapper), so it no longer imports the vendor field-diff helpers.
    "outbound_differ.py",
    # Ticket eefd: the outbound link differ compares canonical relations (remote links mapped
    # via SupportsLinks.map_remote_links), so it no longer imports the vendor link-type map.
    "outbound_links.py",
)

_FORBIDDEN_IMPORT_SUBSTRINGS = ("adapters.jira", "acli_subprocess")

# Ticket 625b: after canonicalization the outbound differ must not name any RAW Jira snapshot
# key. Those vendor-shaped reads live only under adapters/jira/ now.
_VENDOR_SNAPSHOT_KEY_LITERALS = (
    "accountId",
    "emailAddress",
    "displayName",
    "issuetype",
    "issuelinks",
    "inwardIssue",
    "outwardIssue",
)


def test_outbound_differ_names_no_vendor_snapshot_key() -> None:
    """The canonical outbound differ reads local field names only; no raw Jira snapshot key
    used AS A STRING LITERAL (a dict-key read like ``jira_fields["accountId"]``) appears in its
    source (ticket 625b). Bare mentions in comments/docstrings are not reads and are allowed."""
    text = (_REC / "outbound_differ.py").read_text()
    offenders = sorted(
        k for k in _VENDOR_SNAPSHOT_KEY_LITERALS if f'"{k}"' in text or f"'{k}'" in text
    )
    assert not offenders, (
        f"outbound_differ.py reads raw Jira snapshot key(s) {offenders} as string literals — "
        f"compare in canonical (local) shape via the injected InboundMapper; vendor-key reads "
        f"belong under adapters/jira/."
    )


def _all_imported_modules(path: Path) -> set[str]:
    """Every module string reachable via ``import X`` / ``from X import ...`` ANYWHERE
    in the file — module scope and nested inside functions/classes (full AST walk)."""
    tree = ast.parse(path.read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.add(node.module)
            modules.update(f"{node.module or ''}.{a.name}" for a in node.names)
    return modules


@pytest.mark.parametrize("filename", _ZERO_ADAPTER_IMPORT_SCALAR_CORE)
def test_scalar_core_imports_no_jira_adapter(filename: str) -> None:
    """The scalar-neutralized core file imports NOTHING from ``adapters.jira`` /
    ``acli_subprocess`` at any nesting depth (ticket 97f2)."""
    modules = _all_imported_modules(_REC / filename)
    offenders = sorted(m for m in modules if any(sub in m for sub in _FORBIDDEN_IMPORT_SUBSTRINGS))
    assert not offenders, (
        f"{filename} still imports vendor adapter symbol(s) {offenders} — route the site "
        f"through the Backend port (project / query_project / assert_env_ready / "
        f"BackendAssigneeNotFoundError) instead of importing adapters.jira."
    )


def _load_by_path(name: str, filename: str) -> ModuleType:
    """Load a reconciler module standalone by path (the reconciler test convention)."""
    spec = importlib.util.spec_from_file_location(name, _REC / filename)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


class _RecordingOutboundMapper:
    """A fake OutboundMapper that returns a sentinel so we can prove the differ used
    the INJECTED mapper (not a hidden vendor import)."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def map_local_to_remote(
        self, ticket, binding_store=None, local_ticket_types=None, emit_detach_clear=False
    ) -> dict:
        self.calls.append(ticket)
        return {"summary": "SENTINEL-FROM-INJECTED-MAPPER"}


class _RecordingInboundMapper:
    """A fake InboundMapper returning a sentinel status to prove injection is honored."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def map_remote_to_local(self, remote_fields) -> dict:
        self.calls.append(remote_fields)
        return {"status": "closed"}


class _NeutralityStubBindingStore:
    def __init__(self, bindings: dict[str, str] | None = None) -> None:
        self._l2j = bindings or {}
        self._j2l = {v: k for k, v in self._l2j.items()}

    def get_jira_key(self, local_id):
        return self._l2j.get(local_id)

    def is_bound(self, local_id):
        return local_id in self._l2j

    def get_local_id(self, jira_key):
        return self._j2l.get(jira_key)

    def is_pending(self, local_id):
        return False

    def get_baseline(self, local_id):
        return None


def test_outbound_differ_uses_injected_mapper() -> None:
    """A passed-in ``outbound_mapper`` is actually used by ``compute_outbound_mutations``
    (proves the injection seam is wired, not bypassed by a hidden vendor call)."""
    outbound_differ = _load_by_path("outbound_differ_neutrality", "outbound_differ.py")
    fake = _RecordingOutboundMapper()
    ticket = {
        "ticket_id": "loc-1",
        "title": "T",
        "description": "d",
        "status": "open",
        "priority": 2,
        "ticket_type": "task",
        "assignee": "",
        "tags": [],
        "comments": [],
        "deps": [],
    }
    result, _ = outbound_differ.compute_outbound_mutations(
        [ticket],
        {},
        _NeutralityStubBindingStore(),
        outbound_mapper=fake,
    )
    assert fake.calls, "the injected outbound_mapper was never called"
    assert result[0].fields == {"summary": "SENTINEL-FROM-INJECTED-MAPPER"}


def test_inbound_differ_uses_injected_mapper() -> None:
    """A passed-in ``inbound_mapper`` is actually used by ``compute_inbound_mutations``."""
    inbound_differ = _load_by_path("inbound_differ_neutrality", "inbound_differ.py")
    fake = _RecordingInboundMapper()
    bind = _NeutralityStubBindingStore({"loc-1": "DIG-1"})
    local = {"ticket_id": "loc-1", "title": "T", "description": "d", "status": "open"}
    _muts, _ = inbound_differ.compute_inbound_mutations(
        {"DIG-1": {"summary": "T", "status": "Done"}},
        bind,
        {"loc-1": local},
        inbound_mapper=fake,
    )
    assert fake.calls, "the injected inbound_mapper was never called"


# ---------------------------------------------------------------------------
# Ticket 21ca — the UNION lazy-load-literal sweep. After the rich-text codec + comment
# limits move behind the port (InboundMapper.normalize_rich_text / FieldSanitizer.
# fit_comment), NO package-root core module may carry a
# ``"rebar_reconciler.adapters.jira[...]"`` string constant (an actual lazy-load key) —
# with a SINGLE recorded exemption: inbound_fields.py IS the Jira InboundMapper impl
# (delegated by adapters/jira/backend.py), kept at the package root only for loader
# location-pinning; its physical relocation is out of epic bbf1's scope, tracked by
# follow-up ticket 556a-5a1f-adb3-4139 (linked discovered_from bbf1). A docstring/comment
# that MENTIONS the string in prose is not a load key, so the gate inspects string
# CONSTANTS by value (via AST), not raw text.
_VENDOR_LAZYLOAD_PREFIX = "rebar_reconciler.adapters.jira"
_SWEEP_EXEMPTIONS = {"inbound_fields.py"}


def _has_vendor_lazyload_literal(path: Path) -> bool:
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value == _VENDOR_LAZYLOAD_PREFIX or node.value.startswith(
                _VENDOR_LAZYLOAD_PREFIX + "."
            ):
                return True
    return False


def test_no_root_core_module_carries_vendor_lazyload_literal() -> None:
    """Union sweep: only inbound_fields.py may carry a vendor lazy-load literal (21ca)."""
    offenders = [
        path.name
        for path in sorted(_REC.glob("*.py"))
        if path.name not in _SWEEP_EXEMPTIONS and _has_vendor_lazyload_literal(path)
    ]
    assert not offenders, (
        f"package-root core modules must not carry a {_VENDOR_LAZYLOAD_PREFIX!r} lazy-load "
        f"literal (route rich-text/limit work through InboundMapper.normalize_rich_text / "
        f"FieldSanitizer.fit_comment); offenders: {offenders}. Only {_SWEEP_EXEMPTIONS} is "
        f"exempt (follow-up ticket 556a)."
    )


def test_sweep_exemption_is_still_needed() -> None:
    """The exemption is honest: inbound_fields.py genuinely still carries the literal."""
    assert _has_vendor_lazyload_literal(_REC / "inbound_fields.py")
