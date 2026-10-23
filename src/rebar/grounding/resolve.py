"""Confirm-only T1 resolver for asserted-absent references.

Existing files and unique bare symbols/imports may be refuted. Dotted members
abstain unless an installed module attribute resolves in the environment;
colliding names, dependencies, and missing names abstain. Dependencies belong
to T0; unresolved member binding belongs to T2. Universal-ctags builds a repo
index while missing tools, unsupported languages, parse errors, and timeouts
fail open.
The core stays import-clean and emits normalized evidence."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import evidence as ev
from . import harness
from .environment import BACKEND_ENV as BACKEND_ENV
from .environment import refute_via_environment as _refute_via_environment
from .environment import resolve_in_environment as resolve_in_environment

# ── Backend identity ─────────────────────────────────────────────────────────

#: The repo-wide-index backend name recorded in coverage.
BACKEND_CTAGS = "ctags"
#: The plain-existence backend for ``kind=file`` (no external tool).
BACKEND_FS = "filesystem"

#: ctags binary name; resolved on PATH by the harness.
_CTAGS_BIN = os.environ.get("REBAR_CTAGS_BIN", "ctags")

# ── Reference-in schema (the closed `kind` contract) ─────────────────────────

#: Closed reference kinds: symbol/import/file can refute; dependency routes to
#: T0; member abstains at T1 pending semantic resolution.
REFERENCE_KINDS: frozenset[str] = frozenset({"symbol", "import", "dependency", "file", "member"})

#: A bare (single-segment) identifier — no dots, no path separators.
_BARE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ReferenceError(ValueError):
    """A reference-in dict is malformed or outside the closed ``kind`` set.

    Raised by :func:`validate_reference` (the input-boundary check); the
    *resolution* path never raises (it fails open to ``abstain``).
    """


def validate_reference(ref: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and trim a reference-in dict with a closed kind and nonempty name.

    Optional fields are in_file, container, language, and ecosystem. Return a
    shallow copy or raise ReferenceError for malformed programmer input."""
    if not isinstance(ref, Mapping):
        raise ReferenceError(f"reference must be a mapping, got {type(ref).__name__}")
    kind = ref.get("kind")
    if kind not in REFERENCE_KINDS:
        raise ReferenceError(
            f"reference kind {kind!r} not in the closed set {sorted(REFERENCE_KINDS)}"
        )
    name = ref.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ReferenceError("reference requires a non-empty string 'name'")
    out: dict[str, Any] = {"kind": kind, "name": name.strip()}
    for opt in ("in_file", "container", "language", "ecosystem"):
        val = ref.get(opt)
        if isinstance(val, str) and val.strip():
            out[opt] = val.strip()
    return out


def is_member_name(name: str) -> bool:
    """True iff ``name`` is a dotted / member reference (``recv.attr``).

    A dotted name can't be bound to a single definition at T1 (it's T2 semantic
    territory), so it always abstains. A bare single-segment identifier is False.
    """
    return not bool(_BARE_NAME_RE.match(name.strip()))


# ── ctags repo-wide index ────────────────────────────────────────────────────


@dataclass
class CtagsIndex:
    """Repo-wide definitions keyed by bare name.

    Zero definitions are unknown, one is refute-eligible, and multiple are
    ambiguous. Language counts report index coverage."""

    defs: dict[str, list[Definition]] = field(default_factory=dict)
    languages: dict[str, int] = field(default_factory=dict)
    version: str | None = None

    def lookup(self, name: str) -> list[Definition]:
        return self.defs.get(name, [])


@dataclass(frozen=True)
class Definition:
    """One definition site from the ctags index (a ``_type=tag`` line)."""

    name: str
    path: str  # repo-relative
    line: int | None
    kind: str | None
    language: str | None


def _parse_ctags_json(stdout: str, repo_root: str) -> CtagsIndex:
    """Parse ``ctags --output-format=json`` lines into a :class:`CtagsIndex`.

    Each line is a JSON object; ``_type=="tag"`` lines are definitions. Non-tag
    lines (``_type=="ptag"`` metadata) and unparseable lines are skipped — a
    partial parse never raises (fail-open: a malformed line just doesn't index).
    """
    idx = CtagsIndex()
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            tag = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(tag, dict) or tag.get("_type") != "tag":
            continue
        name = tag.get("name")
        if not isinstance(name, str) or not name:
            continue
        raw_path = tag.get("path") or ""
        rel = os.path.relpath(raw_path, repo_root) if raw_path else raw_path
        line_no = tag.get("line")
        language = tag.get("language")
        definition = Definition(
            name=name,
            path=rel,
            line=line_no if isinstance(line_no, int) else None,
            kind=tag.get("kind"),
            language=language if isinstance(language, str) else None,
        )
        idx.defs.setdefault(name, []).append(definition)
        if definition.language:
            idx.languages[definition.language] = idx.languages.get(definition.language, 0) + 1
    return idx


_CTAGS_VERSION_RE = re.compile(r"Universal Ctags\s+([0-9][\w.\-]*)")


def ctags_version(timeout: float | None = 10) -> str | None:
    """Best-effort universal-ctags version (``"6.2.1"``), or None if unavailable.

    Recorded in coverage (version skew is the #1 real failure). Fail-open: a
    missing binary / parse miss returns None, never raises.
    """
    result = harness.run_tool([_CTAGS_BIN, "--version"], backend=BACKEND_CTAGS, timeout=timeout)
    if result.abstained or not result.stdout:
        return None
    m = _CTAGS_VERSION_RE.search(result.stdout)
    return m.group(1) if m else None


_CACHED_CTAGS_LANGS: frozenset[str] | None = None


def ctags_languages(timeout: float | None = 10) -> frozenset[str]:
    """The set of languages this universal-ctags build can parse (lowercased).

    From ``ctags --list-languages``. Cached. Fail-open: an unavailable binary
    returns the empty set (so every declared language reads as unsupported and
    abstains — never a false refute).
    """
    global _CACHED_CTAGS_LANGS
    if _CACHED_CTAGS_LANGS is not None:
        return _CACHED_CTAGS_LANGS
    result = harness.run_tool(
        [_CTAGS_BIN, "--list-languages"], backend=BACKEND_CTAGS, timeout=timeout
    )
    if result.abstained or not result.stdout:
        _CACHED_CTAGS_LANGS = frozenset()
        return _CACHED_CTAGS_LANGS
    langs: set[str] = set()
    for line in result.stdout.splitlines():
        # lines look like `Python` or `C++  [disabled]`; take the leading token.
        token = line.strip().split()[0] if line.strip() else ""
        if token:
            langs.add(token.lower())
    _CACHED_CTAGS_LANGS = frozenset(langs)
    return _CACHED_CTAGS_LANGS


def _language_supported(
    language: str, config: GroundingConfig, *, timeout: float | None = None
) -> bool:
    """Return whether stock ctags or configured project optlibs support language."""
    norm = language.strip().lower()
    if norm in {s.lower() for s in config.supported_languages}:
        return True
    # If the project supplies optlibs/options at all, be permissive (we can't
    # enumerate optlib-defined langs without invoking; the index attempt will
    # simply yield no defs → a benign not-found abstain rather than unsupported).
    if config.ctags_optlib_dirs or config.ctags_options:
        return True
    return norm in ctags_languages(timeout=timeout)


def _ctags_cmd(
    repo_root: str, *, optlib_dirs: Sequence[str] = (), options: Sequence[str] = ()
) -> list[str]:
    """Build the repo-wide ctags JSON command with language and long-kind fields.

    Pass project optlib directories and options through for custom grammars."""
    cmd = [_CTAGS_BIN, "-R", "--output-format=json", "--fields=+lK"]
    for d in optlib_dirs:
        cmd.append(f"--optlib-dir={d}")
    for opt in options:
        cmd.append(f"--options={opt}")
    cmd += ["-f", "-", repo_root]
    return cmd


def build_index(
    repo_root: str,
    *,
    timeout: float | None = None,
    optlib_dirs: Sequence[str] = (),
    options: Sequence[str] = (),
) -> tuple[CtagsIndex | None, harness.RunResult]:
    """Build a ctags index and return (index, run_result).

    Missing tools, timeout, or spawn failures return None with closed abstention.
    Parse usable stdout despite a nonzero exit; empty or invalid output with a
    nonzero exit is a parse error."""
    cmd = _ctags_cmd(repo_root, optlib_dirs=optlib_dirs, options=options)
    version = ctags_version()
    result = harness.run_tool(cmd, backend=BACKEND_CTAGS, timeout=timeout, version=version)
    if result.abstained:
        return None, result
    idx = _parse_ctags_json(result.stdout, repo_root)
    idx.version = version
    if not idx.defs and result.returncode not in (0, None):
        # ctags ran but produced no parseable tags AND failed — treat as parse_error.
        result.abstain_reason = "parse_error"
        result.detail = (
            f"ctags exited {result.returncode} with no parseable tags: {result.stderr[:200]!r}"
        )
        return None, result
    return idx, result


# ── Project language-extensibility config (`.rebar/grounding.toml`) ───────────

#: The project config slot the resolver reads for language extensibility.
CONFIG_REL_PATH = os.path.join(".rebar", "grounding.toml")


@dataclass(frozen=True)
class GroundingConfig:
    """Optional .rebar/grounding.toml settings for ctags optlib dirs, options,
    and supported languages. Paths are relative to repo_root; missing or invalid
    config yields empty defaults without raising."""

    ctags_optlib_dirs: tuple[str, ...] = ()
    ctags_options: tuple[str, ...] = ()
    supported_languages: frozenset[str] = frozenset()

    # T2 defaults off; invalid config fails open without changing the T0+T1 floor.
    t2_enabled: bool = False
    t2_backend: str | None = None
    t2_timeout_seconds: float = 30.0


def load_config(repo_root: str) -> GroundingConfig:
    """Read ``.rebar/grounding.toml`` from ``repo_root``, fail-open to defaults.

    Uses the stdlib ``tomllib`` (3.11+). A missing file, a parse error, or a
    missing ``tomllib`` all return the empty :class:`GroundingConfig` — config is
    an optional extensibility slot, never a hard dependency.
    """
    path = os.path.join(repo_root, CONFIG_REL_PATH)
    if not os.path.isfile(path):
        return GroundingConfig()
    try:
        import tomllib  # py3.11+ stdlib
    except ImportError:  # pragma: no cover - <3.11 fallback
        return GroundingConfig()
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError):
        return GroundingConfig()
    section = data.get("grounding", {})
    if not isinstance(section, dict):
        return GroundingConfig()

    def _abs_strs(key: str) -> tuple[str, ...]:
        raw = section.get(key, [])
        if not isinstance(raw, list):
            return ()
        out: list[str] = []
        for item in raw:
            if isinstance(item, str) and item.strip():
                p = item.strip()
                out.append(p if os.path.isabs(p) else os.path.join(repo_root, p))
        return tuple(out)

    langs_raw = section.get("supported_languages", [])
    langs = (
        frozenset(s for s in langs_raw if isinstance(s, str) and s.strip())
        if isinstance(langs_raw, list)
        else frozenset()
    )
    # ── T2 seam keys (epic 850f) — each fails open to its default ──────────────
    t2_enabled_raw = section.get("t2_enabled", False)
    t2_enabled = t2_enabled_raw if isinstance(t2_enabled_raw, bool) else False
    t2_backend_raw = section.get("t2_backend")
    t2_backend = (
        t2_backend_raw.strip()
        if isinstance(t2_backend_raw, str) and t2_backend_raw.strip()
        else None
    )
    t2_timeout_raw = section.get("t2_timeout_seconds", 30.0)
    # bool is a subclass of int — reject it; require a positive number.
    t2_timeout_seconds = (
        float(t2_timeout_raw)
        if isinstance(t2_timeout_raw, (int, float))
        and not isinstance(t2_timeout_raw, bool)
        and t2_timeout_raw > 0
        else 30.0
    )

    return GroundingConfig(
        ctags_optlib_dirs=_abs_strs("ctags_optlib_dirs"),
        ctags_options=_abs_strs("ctags_options"),
        supported_languages=langs,
        t2_enabled=t2_enabled,
        t2_backend=t2_backend,
        t2_timeout_seconds=t2_timeout_seconds,
    )


# ── The resolution lane ──────────────────────────────────────────────────────


def refute_absence(
    reference: Mapping[str, Any],
    *,
    repo_root: str,
    index: CtagsIndex | None = None,
    config: GroundingConfig | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Return one normalized refuted or closed-reason abstain record.

    Dependencies route to T0; members and dotted names abstain at T1; existing
    files and unique bare symbols/imports refute absence. Collisions and missing
    names abstain. Reuse a supplied ctags index or build one fail-open."""
    ref = validate_reference(reference)
    kind = ref["kind"]
    name = ref["name"]

    # `dependency` is not ours: route to the T0 deps lane (S3). Abstain, signal it.
    if kind == "dependency":
        return ev.abstain(
            "other",
            job=ev.JOB_REFUTE,
            provenance_tier=ev.TIER_T0,
            backend="registry",
            reference=_schema_safe_reference(ref),
            detail="kind=dependency routes to the T0 deps lane (story S3); not resolved by the T1 ctags lane",  # noqa: E501
        )

    # Check files before dotted-name routing; extensions contain dots.
    if kind == "file":
        return _refute_file(name, ref, repo_root=repo_root)

    # Members need T2, except installed module attributes confirmed by environment lookup.
    if kind == "member" or is_member_name(name):
        env = _refute_via_environment(ref)
        if env is not None:
            return env
        return ev.abstain(
            "ambiguous",
            job=ev.JOB_REFUTE,
            provenance_tier=ev.TIER_T1,
            backend=BACKEND_CTAGS,
            version=(index.version if index else None),
            reference=_schema_safe_reference(ref),
            detail=f"member/dotted reference {name!r} cannot be bound at T1 (member binding is T2)",
        )

    # `symbol` / `import` → the ctags repo-wide name-existence guard.
    return _refute_symbol(
        name, ref, repo_root=repo_root, index=index, config=config, timeout=timeout
    )


def _refute_file(name: str, ref: dict[str, Any], *, repo_root: str) -> dict[str, Any]:
    """Refute a ``kind=file`` reference by plain path existence under the repo."""
    rel = name.lstrip("/")
    candidate = os.path.normpath(os.path.join(repo_root, rel))
    # Guard against path-escape (`../`): only refute paths inside the repo.
    inside = os.path.commonpath(
        [os.path.abspath(candidate), os.path.abspath(repo_root)]
    ) == os.path.abspath(repo_root)
    if inside and os.path.exists(candidate):
        cov = ev.coverage(backend=BACKEND_FS, status=ev.STATUS_RAN)
        return ev.refuted(
            provenance_tier=ev.TIER_T1,
            coverage=cov,
            reference=_schema_safe_reference(ref),
            location={"file": rel},
            detail=f"file path {rel!r} exists under the repo — asserted absence disproved",
        )
    # Not found → abstain (confirm-only: we never assert the file is absent).
    return ev.abstain(
        "other",
        job=ev.JOB_REFUTE,
        provenance_tier=ev.TIER_T1,
        backend=BACKEND_FS,
        reference=_schema_safe_reference(ref),
        detail=f"file path {rel!r} not found under the repo — cannot disprove absence (confirm-only)",  # noqa: E501
    )


def _refute_symbol(
    name: str,
    ref: dict[str, Any],
    *,
    repo_root: str,
    index: CtagsIndex | None,
    config: GroundingConfig | None,
    timeout: float | None,
) -> dict[str, Any]:
    """Resolve a bare symbol/import through ctags, then the installed environment.

    Unsupported languages, index failures, and zero repo definitions all reach
    the environment fallback. Repo collisions remain ambiguous."""
    cfg = config if config is not None else load_config(repo_root)

    # A ctags-blind language goes directly to the installed-environment fallback.
    lang = ref.get("language")
    lang_unsupported = bool(
        isinstance(lang, str) and lang and not _language_supported(lang, cfg, timeout=timeout)
    )

    version: str | None = None
    build_result: harness.RunResult | None = None
    if not lang_unsupported:
        if index is None:
            index, build_result = build_index(
                repo_root,
                timeout=timeout,
                optlib_dirs=cfg.ctags_optlib_dirs,
                options=cfg.ctags_options,
            )
        if index is not None:
            defs = index.lookup(name)
            version = index.version
            if len(defs) == 1:
                d = defs[0]
                cov = ev.coverage(backend=BACKEND_CTAGS, status=ev.STATUS_RAN, version=version)
                location: dict[str, Any] = {"file": d.path}
                if d.line:
                    location["line_start"] = d.line
                return ev.refuted(
                    provenance_tier=ev.TIER_T1,
                    coverage=cov,
                    reference=_schema_safe_reference(ref),
                    location=location,
                    detail=f"unique definition of {name!r} at {d.path}"
                    + (f":{d.line}" if d.line else "")
                    + " — asserted absence disproved",
                )
            if len(defs) > 1:
                sites = ", ".join(sorted({d.path for d in defs}))
                return ev.abstain(
                    "ambiguous",
                    job=ev.JOB_REFUTE,
                    provenance_tier=ev.TIER_T1,
                    backend=BACKEND_CTAGS,
                    version=version,
                    reference=_schema_safe_reference(ref),
                    detail=f"{len(defs)} definitions of {name!r} ({sites}) — cannot bind the intended one at T1",  # noqa: E501
                )
            # Zero defs in the repo index → fall through to the environment fallback.

    # Ctags misses, failures, or unsupported languages can still resolve through an
    # actual installed dependency or stdlib import without false refutation.
    env = _refute_via_environment(ref)
    if env is not None:
        return env

    # Unresolved everywhere → confirm-only abstain (never asserts an absence),
    # carrying the reason that fits why the ctags lane could not confirm.
    if lang_unsupported:
        return ev.abstain(
            "unsupported_lang",
            job=ev.JOB_REFUTE,
            provenance_tier=ev.TIER_T1,
            backend=BACKEND_CTAGS,
            reference=_schema_safe_reference(ref),
            detail=f"language {lang!r} is not supported by ctags and no project optlib/grammar is configured for it",  # noqa: E501
        )
    if index is None and build_result is not None:
        # Fail-open: no tool / timeout / parse error → the harness's closed reason.
        return build_result.as_abstain(
            job=ev.JOB_REFUTE,
            provenance_tier=ev.TIER_T1,
            reference=_schema_safe_reference(ref),
        )
    return ev.abstain(
        "other",
        job=ev.JOB_REFUTE,
        provenance_tier=ev.TIER_T1,
        backend=BACKEND_CTAGS,
        version=version,
        reference=_schema_safe_reference(ref),
        detail=f"no definition of {name!r} in the repo index or the installed environment — cannot disprove absence (confirm-only, never asserts absent)",  # noqa: E501
    )


# Installed-environment fallback lives in .environment; re-export its public
# symbols here for existing callers. It can refute ctags-blind references.


# ── schema-safe reference attachment ─────────────────────────────────────────

_CACHED_SCHEMA_KINDS: frozenset[str] | None = None


def _schema_reference_kinds() -> frozenset[str]:
    """Read accepted reference kinds from the live grounding schema.

    The full set includes import, symbol, dependency, file, and member. Fall back
    to the three-kind subset if unreadable so emitted records stay valid."""
    global _CACHED_SCHEMA_KINDS
    if _CACHED_SCHEMA_KINDS is not None:
        return _CACHED_SCHEMA_KINDS
    fallback = frozenset({"import", "symbol", "dependency"})
    try:
        from rebar import schemas

        schema = schemas.load(schemas.GROUNDING)
        enum = (
            schema.get("$defs", {})
            .get("reference", {})
            .get("properties", {})
            .get("kind", {})
            .get("enum")
        )
        _CACHED_SCHEMA_KINDS = frozenset(enum) if isinstance(enum, list) and enum else fallback
    except Exception:  # noqa: BLE001 — never let schema introspection break resolution
        _CACHED_SCHEMA_KINDS = fallback
    return _CACHED_SCHEMA_KINDS


def _schema_safe_reference(ref: Mapping[str, Any]) -> dict[str, Any] | None:
    """Attach a reference only when its kind passes the live schema.

    Other references remain represented in record detail, preserving identity
    without emitting invalid evidence."""
    kind = ref.get("kind")
    if kind in _schema_reference_kinds():
        return dict(ref)
    return None


# ── Deterministic code/diff reference extractor (optional, in scope) ──────────

# `from X import a, b as c` / `import X, Y as Z` — Python. Deterministic, AST-free
# (a regex over import statements); prose extraction is explicitly OUT of scope.
_PY_FROM_RE = re.compile(r"^\s*from\s+([.\w]+)\s+import\s+(.+?)(?:#.*)?$", re.MULTILINE)
_PY_IMPORT_RE = re.compile(r"^\s*import\s+(.+?)(?:#.*)?$", re.MULTILINE)
# A unified-diff added line (`+...`), minus the leading `+` and not the `+++` header.
_DIFF_ADDED_RE = re.compile(r"^\+(?!\+\+)(.*)$", re.MULTILINE)


def extract_references(
    text: str, *, language: str = "python", in_file: str | None = None
) -> list[dict[str, Any]]:
    """Extract Python import names deterministically from source lines.

    Return reference-in dicts; unknown languages return []. Prose extraction is
    outside the oracle's scope."""
    if language != "python":
        return []
    refs: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(nm: str) -> None:
        nm = nm.strip()
        # honor `a as b` (the bound name is what's referenced) and drop wildcards.
        if " as " in nm:
            nm = nm.split(" as ", 1)[1].strip()
        nm = nm.strip("() ").split(".", 1)[0].strip()
        if not nm or nm == "*" or not _BARE_NAME_RE.match(nm) or nm in seen:
            return
        seen.add(nm)
        ref: dict[str, Any] = {"kind": "import", "name": nm, "language": "python"}
        if in_file:
            ref["in_file"] = in_file
        refs.append(ref)

    for m in _PY_FROM_RE.finditer(text):
        for part in m.group(2).split(","):
            _add(part)
    for m in _PY_IMPORT_RE.finditer(text):
        # skip the `from … import …` already handled (this RE also matches it loosely)
        if " import " in m.group(0):
            continue
        for part in m.group(1).split(","):
            _add(part)
    return refs


def extract_references_from_diff(
    diff: str, *, language: str = "python", in_file: str | None = None
) -> list[dict[str, Any]]:
    """Extract imports from added non-header diff lines only.

    Ignore removed/context lines and prose."""
    added = "\n".join(m.group(1) for m in _DIFF_ADDED_RE.finditer(diff))
    return extract_references(added, language=language, in_file=in_file)


__all__ = [
    "BACKEND_CTAGS",
    "BACKEND_ENV",
    "BACKEND_FS",
    "CONFIG_REL_PATH",
    "REFERENCE_KINDS",
    "CtagsIndex",
    "Definition",
    "GroundingConfig",
    "ReferenceError",
    "build_index",
    "extract_references",
    "extract_references_from_diff",
    "is_member_name",
    "load_config",
    "refute_absence",
    "resolve_in_environment",
    "validate_reference",
]
