"""The per-agent-call Terraform structural grounding SESSION (REB-640, slice
forcible-diminished-lamb).

``open_session`` returns a :class:`TerraformSession` that owns an IMMUTABLE parse
cache + a query ledger over a bounded, frozen snapshot. Each query
(:meth:`~TerraformSession.lookup_declaration`,
:meth:`~TerraformSession.resolve_reference`) returns a three-valued grounding
:class:`Result` — ``refuted`` (a real declaration disproves an asserted absence) or
``abstain`` (a closed reason) — plus a canonical, credential-redacting receipt.
The session NEVER emits ``match`` and NEVER asserts an absence, and it NEVER runs
Terraform/OpenTofu/a provider/any external process: the ONLY subprocess is the
grounding worker boundary that runs the pure ``python-hcl2`` parse fail-open.

``hcl2``/``lark`` are imported LAZILY (only inside the worker, via
:mod:`rebar.grounding.terraform_parse`) so ``import rebar`` and non-Terraform
reviews never pull the HCL parser. When the ``grounding-terraform`` extra is
absent, :func:`available` is False and every query returns a closed
``no_tool``/``missing_extra`` abstention — never a raise.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import terraform_index as tfi
from . import terraform_receipt as tr

_EXTRA = "grounding-terraform"
_LANGUAGE = "terraform"


@dataclass(frozen=True)
class Result:
    """One query's grounding evidence + its canonical receipt."""

    evidence: dict[str, Any]
    receipt: dict[str, Any]


@dataclass(frozen=True)
class Usage:
    """The finalized read ledger: concrete file reads + membership globs."""

    concrete_reads: list[str]
    membership_globs: list[str]


def available() -> bool:
    """True iff the optional ``grounding-terraform`` (``hcl2``) extra is importable.

    Detection only — uses :func:`rebar._capabilities.is_available` (``find_spec``),
    so the HCL parser is NEVER imported here.
    """
    from rebar import _capabilities

    return _capabilities.is_available("grounding_terraform")


def open_session(repo_root: str, selected: list[str]) -> TerraformSession:
    """Open a per-call Terraform grounding session over ``selected`` under ``repo_root``.

    Always returns a session (never raises): a path/limit breach or a missing extra
    is captured as a session-wide closed abstention that every query returns.
    """
    root = Path(repo_root)
    pending: str | None = None
    snapshot: tfi.Snapshot
    try:
        snapshot = tfi.build_snapshot(str(root), selected)
    except tfi.TerraformPathError:
        pending = "path_outside_snapshot"
        snapshot = tfi.Snapshot(repo_root=str(root), modules={})
    except tfi.TerraformLimitError as exc:
        pending = exc.detail
        snapshot = tfi.Snapshot(repo_root=str(root), modules={})
    if not available():
        pending = "missing_extra"
    return TerraformSession(root=root, snapshot=snapshot, pending=pending)


class TerraformSession:
    """A per-agent-call session: immutable parse cache + query ledger over a snapshot."""

    def __init__(self, *, root: Path, snapshot: tfi.Snapshot, pending: str | None) -> None:
        self._root = root
        self._snapshot = snapshot
        self._pending = pending
        self._cache: dict[str, dict[str, Any]] = {}
        self._concrete_reads: list[str] = []
        self._membership: list[str] = []
        self._finalized = False

    # ── public queries ──────────────────────────────────────────────────────

    def lookup_declaration(self, address: str, module_path: str = "") -> Result:
        """Refute the asserted absence of a Terraform declaration ``address``."""
        module_dir = self._normalize_module(module_path)
        query = {"address": address.strip(), "module": module_dir if module_dir else ""}
        if self._pending is not None:
            return self._abstain("lookup_declaration", query, self._pending, module_dir or ".")
        if module_dir is None:
            return self._abstain("lookup_declaration", query, "path_outside_snapshot", ".")
        targets = self._targets(module_dir)
        if not self._has_terraform(targets):
            return self._abstain("lookup_declaration", query, "not_terraform", module_dir or ".")
        kind, canonical, detail = _classify_lookup(address)
        if detail is not None:
            return self._abstain("lookup_declaration", query, detail, module_dir or ".")
        query["address"] = canonical
        matches, parse_detail = self._find_declaration(targets, canonical)
        if parse_detail is not None:
            return self._abstain("lookup_declaration", query, parse_detail, module_dir or ".")
        return self._decide_lookup(query, kind, canonical, matches, module_dir)

    def resolve_reference(self, reference: str, from_file: str) -> Result:
        """Refute the asserted absence of a dotted traversal ``reference``."""
        rel_from = self._safe_rel(from_file)
        query = {"reference": reference.strip(), "from_file": rel_from or from_file}
        if self._pending is not None:
            return self._abstain("resolve_reference", query, self._pending, ".")
        if rel_from is None:
            return self._abstain("resolve_reference", query, "path_outside_snapshot", ".")
        from_dir = tfi._module_dir_of(rel_from)
        if not self._has_terraform([from_dir]):
            return self._abstain("resolve_reference", query, "not_terraform", from_dir)
        action, data = _resolve_plan(reference)
        if action == "abstain":
            return self._abstain("resolve_reference", query, str(data), from_dir)
        if action == "module_output":
            return self._resolve_module_output(query, from_dir, data)
        return self._resolve_decl_base(query, from_dir, data)

    def finalize(self) -> Usage:
        """Free the cache/ledger and return the deterministic read :class:`Usage`."""
        usage = Usage(
            concrete_reads=sorted(set(self._concrete_reads)),
            membership_globs=sorted(set(self._membership)),
        )
        self._cache = {}
        self._concrete_reads = []
        self._membership = []
        self._finalized = True
        return usage

    # ── resolution helpers ──────────────────────────────────────────────────

    def _decide_lookup(
        self,
        query: dict[str, Any],
        kind: str,
        canonical: str,
        matches: list[tuple[str, dict[str, Any]]],
        module_dir: str,
    ) -> Result:
        if not matches:
            return self._abstain(
                "lookup_declaration", query, "no_unique_address", module_dir or "."
            )
        if len(matches) > 1:
            return self._abstain(
                "lookup_declaration", query, "duplicate_address", module_dir or "."
            )
        found_dir, decl = matches[0]
        reference = {
            "kind": "symbol",
            "name": canonical,
            "language": _LANGUAGE,
            "terraform_kind": kind,
        }
        location = _location(found_dir, decl)
        return self._refuted("lookup_declaration", query, reference, location, found_dir)

    def _resolve_decl_base(
        self, query: dict[str, Any], from_dir: str, data: tuple[str, str]
    ) -> Result:
        _kind, canonical = data
        matches, detail = self._find_declaration([from_dir], canonical)
        if detail is not None:
            return self._abstain("resolve_reference", query, detail, from_dir)
        if not matches:
            return self._abstain("resolve_reference", query, "no_unique_address", from_dir)
        if len(matches) > 1:
            return self._abstain("resolve_reference", query, "duplicate_address", from_dir)
        found_dir, decl = matches[0]
        reference = {"kind": "member", "name": query["reference"], "language": _LANGUAGE}
        return self._refuted(
            "resolve_reference", query, reference, _location(found_dir, decl), found_dir
        )

    def _resolve_module_output(
        self, query: dict[str, Any], from_dir: str, data: tuple[str, str]
    ) -> Result:
        module_name, output_name = data
        facts, detail = self._parse_module(from_dir)
        if detail is not None:
            return self._abstain("resolve_reference", query, detail, from_dir)
        call = _module_call(facts, module_name)
        if call is None:
            return self._abstain("resolve_reference", query, "no_unique_address", from_dir)
        if call.get("dynamic"):
            return self._abstain("resolve_reference", query, "dynamic_source", from_dir)
        child_dir = tfi._resolve_child_dir(self._root, from_dir, call["source"])
        if child_dir is None:
            return self._abstain("resolve_reference", query, "no_unique_address", from_dir)
        matches, cdetail = self._find_declaration([child_dir], f"output.{output_name}")
        if cdetail is not None:
            return self._abstain("resolve_reference", query, cdetail, from_dir)
        if not matches:
            return self._abstain("resolve_reference", query, "no_unique_address", from_dir)
        found_dir, decl = matches[0]
        reference = {"kind": "member", "name": query["reference"], "language": _LANGUAGE}
        return self._refuted(
            "resolve_reference", query, reference, _location(found_dir, decl), from_dir
        )

    def _find_declaration(
        self, module_dirs: list[str], canonical: str
    ) -> tuple[list[tuple[str, dict[str, Any]]], str | None]:
        """All ``(module_dir, decl)`` whose address == ``canonical`` across modules."""
        matches: list[tuple[str, dict[str, Any]]] = []
        for module_dir in module_dirs:
            facts, detail = self._parse_module(module_dir)
            if detail is not None:
                return [], detail
            for decl in facts.get("declarations", []):
                if decl.get("address") == canonical:
                    matches.append((module_dir, decl))
        return matches, None

    # ── snapshot / parse plumbing ───────────────────────────────────────────

    def _targets(self, module_dir: str) -> list[str]:
        if module_dir and module_dir in self._snapshot.modules:
            return [module_dir]
        if module_dir:
            return sorted(self._snapshot.modules)
        return sorted(self._snapshot.modules)

    def _module_files(self, module_dir: str) -> list[str]:
        module = self._snapshot.modules.get(module_dir)
        if module is not None:
            return module.files
        return tfi._tf_files_in_dir(self._root, module_dir)

    def _has_terraform(self, module_dirs: list[str]) -> bool:
        """True iff any target module holds a ``.tf``/``.tf.json`` file in scope.

        Zero Terraform files across the query scope is a decisive "this isn't
        Terraform" (``unsupported_lang``/``not_terraform``), distinct from the
        ``ambiguous``/``no_unique_address`` case where Terraform IS in scope but no
        single declaration matches the address.
        """
        return any(self._module_files(module_dir) for module_dir in module_dirs)

    def _parse_module(self, module_dir: str) -> tuple[dict[str, Any], str | None]:
        """Parse (and merge) every ``.tf`` file of ``module_dir`` fail-open."""
        self._record_membership(module_dir)
        declarations: list[dict[str, Any]] = []
        module_calls: list[dict[str, Any]] = []
        for rel_file in self._module_files(module_dir):
            facts, detail = self._parse_file(rel_file)
            if detail is not None:
                return {}, detail
            for decl in facts.get("declarations", []):
                declarations.append({**decl, "file": rel_file})
            module_calls.extend(facts.get("module_calls", []))
        return {"declarations": declarations, "module_calls": module_calls}, None

    def _parse_file(self, rel_file: str) -> tuple[dict[str, Any], str | None]:
        if rel_file in self._cache:
            self._record_read(rel_file)
            return self._cache[rel_file], None
        try:
            data = (self._root / rel_file).read_bytes()
        except OSError:
            return {}, "unreadable_file"
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return {}, "invalid_input"
        facts, detail = self._run_parse(text)
        if detail is not None:
            return {}, detail
        self._cache[rel_file] = facts
        self._record_read(rel_file)
        return facts, None

    def _run_parse(self, text: str) -> tuple[dict[str, Any], str | None]:
        from . import terraform_parse as tp
        from .harness import run_in_worker

        rr = run_in_worker(
            tp.parse_document_safe,
            text,
            backend=tr.PARSER,
            version=_installed_version(),
            expected_version=tr.PARSER_VERSION,
        )
        if rr.abstained:
            return {}, tr.WORKER_REASON_DETAIL.get(rr.abstain_reason or "other", "worker_failure")
        value = rr.value
        if not isinstance(value, dict) or not value.get("ok"):
            return {}, "invalid_input"
        return {
            "declarations": list(value.get("declarations", [])),
            "module_calls": list(value.get("module_calls", [])),
        }, None

    # ── ledger + result construction ────────────────────────────────────────

    def _record_read(self, rel_file: str) -> None:
        if rel_file not in self._concrete_reads:
            self._concrete_reads.append(rel_file)

    def _record_membership(self, module_dir: str) -> None:
        prefix = "" if module_dir in ("", ".") else f"{module_dir}/"
        for glob in (f"{prefix}**/*.tf", f"{prefix}**/*.tf.json"):
            if glob not in self._membership:
                self._membership.append(glob)

    def _refuted(
        self,
        operation: str,
        query: dict[str, Any],
        reference: dict[str, Any],
        location: dict[str, Any],
        module_dir: str,
    ) -> Result:
        return Result(
            evidence=tr.refuted_evidence(reference, location),
            receipt=tr.refuted_receipt(
                operation,
                query,
                tfi.snapshot_digest(self._snapshot),
                tfi.module_digest(self._snapshot, module_dir),
                reference,
                location,
            ),
        )

    def _abstain(
        self, operation: str, query: dict[str, Any], reason_detail: str, module_dir: str
    ) -> Result:
        reason = tr.ABSTENTIONS[reason_detail][0]
        return Result(
            evidence=tr.abstain_evidence(reason),
            receipt=tr.abstain_receipt(
                operation,
                query,
                tfi.snapshot_digest(self._snapshot),
                tfi.module_digest(self._snapshot, module_dir),
                reason_detail,
            ),
        )

    # ── path normalization ──────────────────────────────────────────────────

    def _normalize_module(self, module_path: str) -> str | None:
        """Normalize a module-dir hint to repo-relative POSIX (``""`` → search all)."""
        raw = (module_path or "").strip()
        if not raw:
            return ""
        try:
            rel = tfi._norm_rel(self._root, raw)
        except tfi.TerraformPathError:
            return None
        return "." if rel in ("", ".") else rel

    def _safe_rel(self, target: str) -> str | None:
        try:
            return tfi._norm_rel(self._root, target)
        except tfi.TerraformPathError:
            return None


def _installed_version() -> str | None:
    import importlib.metadata

    try:
        return importlib.metadata.version("python-hcl2")
    except importlib.metadata.PackageNotFoundError:
        return None


def _module_call(facts: dict[str, Any], name: str) -> dict[str, Any] | None:
    for call in facts.get("module_calls", []):
        if call.get("name") == name:
            return call
    return None


def _location(module_dir: str, decl: dict[str, Any]) -> dict[str, Any]:
    return {
        "file": decl["file"],
        "line_start": decl["line_start"],
        "line_end": decl["line_end"],
    }


def _split_address(address: str) -> tuple[list[str] | None, str | None]:
    s = unicodedata.normalize("NFC", address.strip())
    if not s:
        return None, "no_unique_address"
    if any(ch.isspace() for ch in s):
        return None, "no_unique_address"
    if "*" in s or "[" in s or "]" in s:
        return None, "splat_index"
    parts = s.split(".")
    if any(not part for part in parts):
        return None, "no_unique_address"
    return parts, None


def _classify_lookup(address: str) -> tuple[str, str, str | None]:
    """``(terraform_kind, canonical_address, None)`` or ``("", "", reason_detail)``."""
    parts, detail = _split_address(address)
    if parts is None:
        return "", "", detail
    head, n = parts[0], len(parts)
    canonical = ".".join(parts)
    simple = {
        "variable": "variable",
        "local": "local",
        "output": "output",
        "module": "module",
        "provider": "provider",
    }
    if head in simple and n == 2:
        return simple[head], canonical, None
    if head == "data" and n == 3:
        return "data_resource", canonical, None
    if head not in simple and head != "data" and n == 2:
        return "managed_resource", canonical, None
    return "", "", "no_unique_address"


def _resolve_plan(reference: str) -> tuple[str, Any]:
    """Plan a resolve: ``("abstain", detail)`` | ``("module_output", (name, out))`` |
    ``("decl_base", (kind, canonical))``."""
    parts, detail = _split_address(reference)
    if parts is None:
        return "abstain", detail
    head, n = parts[0], len(parts)
    if head == "module":
        if n == 3:
            return "module_output", (parts[1], parts[2])
        return "abstain", "no_unique_address"
    if head == "var" and n == 2:
        return "decl_base", ("variable", f"variable.{parts[1]}")
    if head == "local" and n == 2:
        return "decl_base", ("local", f"local.{parts[1]}")
    if head == "data":
        if n == 3:
            return "decl_base", ("data_resource", ".".join(parts))
        if n >= 4:
            return "abstain", "provider_attribute"
        return "abstain", "no_unique_address"
    if n == 2:
        return "decl_base", ("managed_resource", ".".join(parts))
    if n >= 3:
        return "abstain", "provider_attribute"
    return "abstain", "no_unique_address"
