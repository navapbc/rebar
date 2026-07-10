"""Engine A — the T1 **refutation resolver** (epic 8f6c / story 1532).

Job 1 of the code-grounding oracle: **refute an asserted absence**. When a
reviewer (plan or code) claims an import/symbol is hallucinated, this lane tries
to DISPROVE the claim by resolving the reference against the repo. It is
*confirm-only*: it emits ``refuted`` (we found the thing the reviewer said was
absent) or ``abstain`` (with a closed reason) — it **never** asserts an absence.
So the lane only ever *reduces* false positives; it never manufactures one.

The deterministic T1 floor is a **universal-ctags** repo-wide tags index (164
languages, no server, no build) plus plain file-path existence — both always
available, both fast. (LSP/SCIP semantic resolution is T2, deferred to epic
``850f``; member/attribute binding is T2 territory and abstains here.)

The ``refuted`` verdict is SCOPED by an ambiguity+member guard the spike proved
necessary (``docs/experiments/code-grounding-spike/`` E2: naive bare
name-existence false-refutes a common-name collision; the guard restores
0 false-refute). The resolution rule:

* a **dotted / member** reference (``recv.attr``) → ``abstain`` — a member can't
  be bound at T1 (T2 territory);
* a name with **>1 definition** in the index (a collision / common name) →
  ``abstain(ambiguous)`` — we can't pick the reviewer's intended one;
* a **unique, bare, non-member** symbol/import name, OR an existing **file** path
  → ``refuted`` (with the def-site / file location when known);
* ``kind=dependency`` → ``abstain`` — dependencies route to the **T0 deps lane**
  (story ``S3``), not resolved here;
* a name simply **not found** → ``abstain`` (NOT ``refuted``, NOT "absent"): the
  lane confirms presence, it never asserts absence.

Everything fails open through S1's harness: no ctags binary / parse error /
timeout / unsupported language → ``abstain``, never a raise.

This module is stdlib-only in its core (ctags is an external CLI invoked through
:func:`rebar.grounding.harness.run_tool`); it builds plain dicts to the S1
contract and is import-clean for non-adopting clients.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import evidence as ev
from . import harness

# ── Backend identity ─────────────────────────────────────────────────────────

#: The repo-wide-index backend name recorded in coverage.
BACKEND_CTAGS = "ctags"
#: The plain-existence backend for ``kind=file`` (no external tool).
BACKEND_FS = "filesystem"
#: The installed-environment (importlib) refute backend for symbol/import/member
#: references — resolves a third-party/stdlib name the repo-scoped index can't see.
BACKEND_ENV = "environment"

#: ctags binary name; resolved on PATH by the harness.
_CTAGS_BIN = os.environ.get("REBAR_CTAGS_BIN", "ctags")

# ── Reference-in schema (the closed `kind` contract) ─────────────────────────

#: The CLOSED set of reference kinds Engine A understands. This is the
#: integration contract S5 EXPOSES; the JSON Schema's ``reference.kind`` enum
#: matches this 5-value set. ``symbol``/``import``/``file`` are refute-eligible by
#: name/path existence, ``dependency`` routes to the T0 deps lane, and ``member``
#: (a dotted ``recv.attr``) is T1-abstain (T2 territory).
REFERENCE_KINDS: frozenset[str] = frozenset({"symbol", "import", "dependency", "file", "member"})

#: Kinds resolved (refute-eligible) by this T1 lane.
_REFUTE_ELIGIBLE_KINDS: frozenset[str] = frozenset({"symbol", "import", "file"})

#: A bare (single-segment) identifier — no dots, no path separators.
_BARE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: A syntactically valid (possibly dotted) Python import path. Guards importlib
#: against being handed an arbitrary string as a module name.
_IMPORTABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")


class ReferenceError(ValueError):
    """A reference-in dict is malformed or outside the closed ``kind`` set.

    Raised by :func:`validate_reference` (the input-boundary check); the
    *resolution* path never raises (it fails open to ``abstain``).
    """


def validate_reference(ref: Mapping[str, Any]) -> dict[str, Any]:
    """Validate + normalize a reference-in dict against the closed ``kind`` set.

    The reference-in contract is ``{kind, name, in_file?, container?, language?,
    ecosystem?}`` where ``kind ∈`` :data:`REFERENCE_KINDS` (closed) and ``name``
    is a non-empty string. Returns a shallow-copied, trimmed dict; raises
    :class:`ReferenceError` on a malformed reference.

    This is rebar's OWN 5-value validator (independent of the JSON Schema, whose
    ``reference.kind`` enum is the 3-value subset until the integration patch
    lands). Resolution callers run this at the boundary so a bad reference is a
    loud programmer error, distinct from a fail-open ``abstain``.
    """
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
    """A repo-wide universal-ctags tags index keyed by bare symbol name.

    ``defs[name]`` is the list of definition sites for that name across the repo;
    a name with len 0 is unknown, len 1 is unique (refute-eligible), len >1 is a
    collision (ambiguous → abstain). ``languages`` records the per-language def
    count so coverage can report what was actually indexed.
    """

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
    """True iff ``language`` is parseable by ctags OR declared in project config.

    The project extensibility slot wins: a language listed in
    ``supported_languages`` (backed by a configured optlib/grammar) is treated as
    supported even if the stock ctags build doesn't know it.
    """
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
    """Assemble the universal-ctags repo-wide JSON-index command.

    ``--fields=+lK`` adds the language (``l``) and long kind (``K``) fields so the
    index can report per-language coverage and distinguish kinds. Project
    extensibility (``optlib_dirs`` / ``options``) is threaded through ``--optlib-dir``
    / ``--options`` so a project-supplied ctags optlib (a custom ``--langdef``)
    indexes an otherwise-unsupported language with no recompile.
    """
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
    """Build a repo-wide ctags index, fail-open.

    Returns ``(index, run_result)``. On any fail-open condition (no ctags binary,
    timeout, spawn error) the index is ``None`` and ``run_result.abstained`` is
    True with the closed reason — the caller turns that into an ``abstain``. A
    non-zero ctags exit with usable stdout is still parsed (ctags often warns on
    one file yet indexes the rest); only an empty/garbage parse with a non-zero
    exit is treated as ``parse_error``.
    """
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
    """Project grounding config read from ``.rebar/grounding.toml``.

    Convention (minimal, real):

    .. code-block:: toml

        [grounding]
        # extra universal-ctags optlib dirs (custom --langdef regex grammars)
        ctags_optlib_dirs = ["tools/ctags-optlibs"]
        # explicit ctags --options files (an optlib .ctags file)
        ctags_options = ["tools/cobol.ctags"]
        # languages the project declares it can resolve via the optlibs above
        # (used to decide unsupported_lang vs a real abstain for exotic langs)
        supported_languages = ["COBOL"]

    Paths are resolved relative to ``repo_root``. An absent / unreadable / invalid
    config yields the empty default (no extensibility) — never a raise.
    """

    ctags_optlib_dirs: tuple[str, ...] = ()
    ctags_options: tuple[str, ...] = ()
    supported_languages: frozenset[str] = frozenset()

    # T2 semantic-resolution seam (epic 850f). All default-off: with ``t2_enabled``
    # false the oracle is byte-identical to the T0+T1 floor. Malformed values fail
    # open to these defaults (never a raise), like every key above.
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
    """Try to DISPROVE an asserted-absent reference; emit one evidence record.

    The single public entry point of Engine A. ``reference`` is a reference-in
    dict (see :func:`validate_reference`). Returns ONE evidence record (always
    valid against the S1 contract): ``refuted`` (the reference exists — claim
    disproved) or ``abstain`` (a closed reason). NEVER asserts an absence; NEVER
    raises on a resolution failure (fail-open through the harness).

    ``index`` lets a caller resolving many references against one repo build the
    ctags index once and pass it in; omitted, it's built per call (fail-open).

    The guard (spike E2, the 0-false-refute property):

    * ``kind=dependency`` → ``abstain`` routed to the T0 deps lane (story S3);
    * ``kind=member`` or a dotted ``name`` → ``abstain(ambiguous)`` (member is T2);
    * ``kind=file`` → ``refuted`` iff the path exists under ``repo_root``;
    * a bare symbol/import name: ``refuted`` iff exactly ONE def in the index,
      ``abstain(ambiguous)`` if >1, ``abstain`` (not found) if 0.
    """
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

    # `file` → plain path existence (a path is a path, not a member ref — so this
    # MUST precede the dotted-name gate, which would otherwise see the `.` in an
    # extension / the `/` in a path and mis-route it to the member abstain).
    if kind == "file":
        return _refute_file(name, ref, repo_root=repo_root)

    # `member` / dotted name → T2 territory for the ctags lane; never refute a
    # member at T1 by name-collision. But an installed third-party `module.attr`
    # (bug 406f) IS deterministically resolvable by importing it, so consult the
    # environment first — a real library member is CONFIRMED, not left unresolved.
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
    """Refute a bare ``symbol``/``import`` name via the ctags repo-wide index."""
    cfg = config if config is not None else load_config(repo_root)

    # Unsupported-language gate: if the reference declares a language ctags can't
    # parse AND the project hasn't supplied an optlib/grammar for it, abstain
    # with `unsupported_lang` (an exotic language fails open, never a false refute).
    lang = ref.get("language")
    if isinstance(lang, str) and lang and not _language_supported(lang, cfg, timeout=timeout):
        return ev.abstain(
            "unsupported_lang",
            job=ev.JOB_REFUTE,
            provenance_tier=ev.TIER_T1,
            backend=BACKEND_CTAGS,
            reference=_schema_safe_reference(ref),
            detail=f"language {lang!r} is not supported by ctags and no project optlib/grammar is configured for it",  # noqa: E501
        )

    if index is None:
        index, result = build_index(
            repo_root,
            timeout=timeout,
            optlib_dirs=cfg.ctags_optlib_dirs,
            options=cfg.ctags_options,
        )
        if index is None:
            # Fail-open: no tool / timeout / parse error → abstain (harness reason).
            return result.as_abstain(
                job=ev.JOB_REFUTE,
                provenance_tier=ev.TIER_T1,
                reference=_schema_safe_reference(ref),
            )

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

    # Zero defs in the repo index. Before abstaining, consult the INSTALLED
    # environment (bug 406f): a symbol/import that resolves from an installed
    # third-party dependency (site-packages) or the stdlib DOES exist — the
    # repo-scoped index simply cannot see it. Refute the asserted absence.
    env = _refute_via_environment(ref)
    if env is not None:
        return env

    # Still unresolved → confirm-only: abstain, never assert absence.
    return ev.abstain(
        "other",
        job=ev.JOB_REFUTE,
        provenance_tier=ev.TIER_T1,
        backend=BACKEND_CTAGS,
        version=version,
        reference=_schema_safe_reference(ref),
        detail=f"no definition of {name!r} in the repo index or the installed environment — cannot disprove absence (confirm-only, never asserts absent)",  # noqa: E501
    )


# ── Environment-aware resolution (installed site-packages / stdlib) ───────────
# A reviewer's repo-scoped file tools (and the ctags repo index) cannot see a
# THIRD-PARTY dependency that lives in site-packages, so a symbol imported from an
# installed library reads as "not found in the repo" and gets wrongly asserted
# non-existent (bug 406f). This closes that gap DETERMINISTICALLY by consulting the
# SAME Python environment the code runs against: `importlib.util.find_spec` proves a
# module exists (without executing it), and an import + `getattr` proves a
# `module.attr` member exists. Confirm-only: it can only upgrade a not-found ABSTAIN
# to a `refuted`; an unresolvable name still abstains (never a false absence).


def resolve_in_environment(
    name: str, *, container: str | None = None, language: str | None = None
) -> dict[str, Any] | None:
    """Resolve a Python reference against the installed environment; ``None`` if not.

    Returns a location dict — ``{"module": …, "origin": …}`` for a module, or
    ``{"module": …, "attr": …, "origin": …}`` for a bound member — when the
    reference is importable, else ``None``. Tries the most specific interpretation
    first: an explicit ``container`` (``from container import name``), then a dotted
    ``name`` split into ``module.attr`` (and ``name`` itself as a submodule path),
    then a bare ``name`` as a top-level module.

    Python-only (a declared non-Python language returns ``None``). Bounded side
    effects: ``find_spec`` never executes the target module; an attribute bind
    imports the module (running its package ``__init__``) but only when an attribute
    is actually requested. Every failure is swallowed to ``None`` — it NEVER raises
    and NEVER reports a false resolution.
    """
    if language is not None and language.strip().lower() not in ("", "python"):
        return None
    nm = (name or "").strip()
    if not nm:
        return None
    candidates: list[tuple[str, str | None]] = []
    ctr = (container or "").strip()
    if ctr:
        candidates.append((ctr, nm))
    if "." in nm:
        head, _, leaf = nm.rpartition(".")
        if head and leaf:
            candidates.append((head, leaf))
        candidates.append((nm, None))  # nm may itself be a dotted submodule path
    else:
        candidates.append((nm, None))  # a bare top-level module name
    for mod, attr in candidates:
        origin = _module_origin(mod)
        if origin is None:
            continue
        if attr is None:
            return {"module": mod, "origin": origin}
        if _attribute_exists(mod, attr):
            return {"module": mod, "attr": attr, "origin": origin}
    return None


def _module_origin(module: str) -> str | None:
    """Return ``module``'s spec origin if importable in the environment, else ``None``.

    Uses :func:`importlib.util.find_spec`, which LOCATES (does not execute) the
    target module. Fail-closed: any resolution error → ``None`` (a name we cannot
    import is simply "not confirmed", never a claimed absence)."""
    if not _IMPORTABLE_NAME_RE.match(module):
        return None
    try:
        spec = importlib.util.find_spec(module)
    except Exception:  # noqa: BLE001 — find_spec imports parent packages and can raise anything; a failure is just "unresolved"
        return None
    if spec is None:
        return None
    return spec.origin or "namespace"


def _attribute_exists(module: str, attr: str) -> bool:
    """True iff ``module.attr`` binds after importing ``module`` (fail-closed to False)."""
    if not _IMPORTABLE_NAME_RE.match(attr):
        return False
    try:
        mod = importlib.import_module(module)
    except Exception:  # noqa: BLE001 — a third-party module can raise anything at import; a failed import is "unresolved", never a raise
        return False
    return hasattr(mod, attr)


def _refute_via_environment(ref: Mapping[str, Any]) -> dict[str, Any] | None:
    """Build a ``refuted`` evidence record iff ``ref`` resolves in the environment.

    The bridge between :func:`resolve_in_environment` and the evidence contract: on
    a hit it emits a ``refuted`` record at ``TIER_T1`` with the ``environment``
    backend (the external origin is carried in ``detail``, not ``location`` — a
    site-packages path is not a repo-relative def-site); on a miss it returns
    ``None`` so the caller keeps its confirm-only abstain."""
    loc = resolve_in_environment(
        str(ref.get("name", "")),
        container=ref.get("container") if isinstance(ref.get("container"), str) else None,
        language=ref.get("language") if isinstance(ref.get("language"), str) else None,
    )
    if loc is None:
        return None
    qualified = loc["module"] + (f".{loc['attr']}" if loc.get("attr") else "")
    cov = ev.coverage(backend=BACKEND_ENV, status=ev.STATUS_RAN)
    return ev.refuted(
        provenance_tier=ev.TIER_T1,
        coverage=cov,
        reference=_schema_safe_reference(ref),
        detail=f"{qualified!r} is importable from the installed environment "
        f"(origin={loc.get('origin')}) — a third-party/stdlib symbol the repo index "
        "cannot see; asserted absence disproved",
    )


# ── schema-safe reference attachment ─────────────────────────────────────────

_CACHED_SCHEMA_KINDS: frozenset[str] | None = None


def _schema_reference_kinds() -> frozenset[str]:
    """The ``reference.kind`` enum the *current* JSON Schema actually accepts.

    Read once from ``grounding.schema.json``. The live schema enum is the full
    5-value set ``{import,symbol,dependency,file,member}``, so a ``file``/``member``
    reference attaches to emitted records today. This indirection is retained as a
    safety net: we attach the ``reference`` field only when its kind is
    schema-accepted (see :func:`_schema_safe_reference`), so the resolver can never
    emit a record that fails ``ev.validate`` even if the schema and this module ever
    drift. Fails open to the 3-value subset if the schema can't be read.
    """
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
    """Return ``ref`` only if its kind is accepted by the current schema, else None.

    Keeps every emitted record valid against the live ``ev.validate`` (the
    3-value enum) today, and AUTO-ATTACHES all 5 kinds the moment the integration
    patch widens the schema enum — no code change here. When the kind is not yet
    schema-accepted, the reference identity is still carried in the record's
    ``detail`` (set by every caller), so no information is lost.
    """
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
    """Deterministically extract ``import`` references from source ``text``.

    Returns reference-in dicts (``kind=import``) for the imported NAMES of Python
    ``import`` / ``from … import …`` statements — the bare names a reviewer would
    flag as hallucinated. Deterministic and AST-free (a regex over import lines);
    **prose extraction is out of scope** (the oracle verifies references, it does
    not mine them from natural language). Only ``language='python'`` is wired
    today; an unknown language yields ``[]`` (no extraction, never a raise).
    """
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
    """Extract ``import`` references from the ADDED lines of a unified diff.

    Collects the ``+``-prefixed (added, non-header) lines and runs
    :func:`extract_references` over them — so a review extracts the imports a diff
    *introduces*. Removed/context lines are ignored. Prose is out of scope.
    """
    added = "\n".join(m.group(1) for m in _DIFF_ADDED_RE.finditer(diff))
    return extract_references(added, language=language, in_file=in_file)


__all__ = [
    "BACKEND_CTAGS",
    "BACKEND_FS",
    "BACKEND_ENV",
    "REFERENCE_KINDS",
    "resolve_in_environment",
    "ReferenceError",
    "validate_reference",
    "is_member_name",
    "CtagsIndex",
    "Definition",
    "build_index",
    "GroundingConfig",
    "load_config",
    "CONFIG_REL_PATH",
    "refute_absence",
    "extract_references",
    "extract_references_from_diff",
]
