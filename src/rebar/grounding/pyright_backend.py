"""The one-shot ``pyright --outputjson`` T2 semantic backend (epic 850f, story S3).

The v1 code-grounding **T2** backend (ADR 0030): a self-contained, opt-in,
confirm-only semantic resolver for Python. It runs ``pyright`` once over the project
root, parses its structured diagnostics, and decides whether an asserted-absent
reference actually resolves.

Confirm-only mapping (for a reference in file ``in_file``):

* ``refuted`` at :data:`~rebar.grounding.evidence.TIER_T2` iff pyright ran, its JSON
  parsed, ``in_file`` has **no** import-resolution diagnostic (the "environment
  built" precondition), and **no** diagnostic in ``in_file`` names the reference's
  leaf — a trustworthy semantic confirmation the reference resolves.
* ``abstain`` (closed reason) otherwise: ``no_tool`` (pyright absent),
  ``unsupported_lang`` (not Python), ``ambiguous`` (no locatable file),
  ``parse_error`` (unparseable output), ``timeout``, or ``other`` (env-not-built /
  a diagnostic sits at the reference / an unrecognized diagnostic at the reference).

The backend NEVER asserts an absence: a pyright diagnostic saying the reference does
NOT resolve becomes an ``abstain`` (a suspected-absent), never a ``refuted``-negation.
Any diagnostic the mapping does not recognize routes to ``abstain`` (fail-safe).

Isolation: this module is imported **lazily** by :mod:`.semantic` only when the
``pyright`` backend is selected, so :mod:`rebar.grounding` stays import-clean.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from typing import Any

from . import evidence as ev
from . import harness

#: Backend name (matches the ``t2_backend`` config value + the grounding_info entry).
BACKEND = "pyright"

#: pyright rules that mean an *import* did not resolve — i.e. the environment is not
#: built (deps missing), so the checker's view of the rest of the file is unreliable.
_IMPORT_RULES = frozenset({"reportMissingImports", "reportMissingModuleSource"})

#: pyright rules that mean a *symbol/attribute* did not resolve at a use site.
_UNRESOLVED_RULES = frozenset(
    {"reportAttributeAccessIssue", "reportUndefinedVariable", "reportUndefinedName"}
)

_PY_SUFFIXES = (".py", ".pyi")
_PY_LANGS = frozenset({"python", "py", "python3"})


# ── version / availability probe ──────────────────────────────────────────────


def version() -> str | None:
    """Detected pyright version, or ``None`` when pyright is absent (fail-open)."""
    res = harness.run_tool([BACKEND, "--version"], backend=BACKEND, timeout=10)
    if res.abstained or not res.stdout:
        return None
    # pyright prints e.g. "pyright 1.1.400"
    m = re.search(r"(\d+\.\d+\.\d+)", res.stdout)
    return m.group(1) if m else None


# ── the resolver ──────────────────────────────────────────────────────────────


def _run_pyright(repo_root: str, timeout: float | None) -> harness.RunResult:
    """Invoke ``pyright --outputjson`` over the project root (a single seam to patch)."""
    return harness.run_tool(
        [BACKEND, "--outputjson", repo_root],
        backend=BACKEND,
        timeout=timeout,
    )


def _abstain(reason: str, reference: Mapping[str, Any], *, detail: str) -> dict[str, Any]:
    return ev.abstain(
        reason,
        job=ev.JOB_REFUTE,
        provenance_tier=ev.TIER_T2,
        backend=BACKEND,
        reference=dict(reference),
        detail=detail,
    )


def _is_python(reference: Mapping[str, Any], in_file: str) -> bool:
    lang = reference.get("language")
    if isinstance(lang, str) and lang.strip():
        return lang.strip().lower() in _PY_LANGS
    return in_file.endswith(_PY_SUFFIXES)


def _leaf(name: str) -> str:
    """The last dotted segment — the member/symbol a diagnostic would name."""
    return name.rsplit(".", 1)[-1] if name else name


def _names_leaf(message: str, leaf: str) -> bool:
    if not leaf:
        return False
    # pyright quotes the offending name; fall back to a word-boundary match.
    return f'"{leaf}"' in message or re.search(rf"\b{re.escape(leaf)}\b", message) is not None


def _same_file(diag_file: str, in_file: str, repo_root: str) -> bool:
    """Whether a pyright diagnostic's ``file`` is the reference's ``in_file``."""
    if not diag_file:
        return False
    ref_abs = in_file if os.path.isabs(in_file) else os.path.join(repo_root, in_file)
    try:
        return os.path.realpath(diag_file) == os.path.realpath(ref_abs)
    except OSError:  # pragma: no cover - realpath on a hostile path
        return os.path.normpath(diag_file) == os.path.normpath(ref_abs)


def _diagnostics(payload: Any) -> list[dict[str, Any]]:
    diags = payload.get("generalDiagnostics") if isinstance(payload, dict) else None
    return [d for d in diags if isinstance(d, dict)] if isinstance(diags, list) else []


def refute(
    reference: Mapping[str, Any],
    *,
    repo_root: str,
    timeout: float | None = None,
    cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Try to refute an asserted-absent ``reference`` via pyright diagnostics.

    Returns ONE evidence record (``refuted`` at ``TIER_T2`` on a trustworthy resolve,
    otherwise ``abstain`` with a closed reason). Never raises; never asserts an
    absence. ``cache`` (caller-owned) memoizes one pyright invocation per project root
    so many references in a review reuse a single run.
    """
    name = reference.get("name")
    if not isinstance(name, str) or not name:
        return _abstain("ambiguous", reference, detail="reference has no name")

    in_file = reference.get("in_file")
    if not isinstance(in_file, str) or not in_file:
        return _abstain("ambiguous", reference, detail="reference has no in_file to locate")

    if not _is_python(reference, in_file):
        return _abstain(
            "unsupported_lang", reference, detail=f"pyright backend handles Python only ({in_file})"
        )

    payload = _pyright_payload(repo_root, timeout, cache)
    if isinstance(payload, harness.RunResult):  # a fail-open abstain from the harness
        return payload.as_abstain(
            job=ev.JOB_REFUTE, provenance_tier=ev.TIER_T2, reference=dict(reference)
        )
    if payload is None:
        return _abstain("parse_error", reference, detail="pyright output was not parseable JSON")

    file_diags = [
        d for d in _diagnostics(payload) if _same_file(str(d.get("file", "")), in_file, repo_root)
    ]

    # Environment-built precondition: unresolved imports make the whole file's
    # semantic view untrustworthy — abstain rather than trust a resolve.
    if any(d.get("rule") in _IMPORT_RULES for d in file_diags):
        return _abstain(
            "other", reference, detail=f"environment not built: unresolved imports in {in_file}"
        )

    leaf = _leaf(name)
    ref_diags = [d for d in file_diags if _names_leaf(str(d.get("message", "")), leaf)]
    if any(d.get("rule") in _UNRESOLVED_RULES for d in ref_diags):
        return _abstain(
            "other",
            reference,
            detail=f"pyright reports {name!r} does not resolve (suspected absent)",
        )
    if ref_diags:
        # A diagnostic names the reference but with a rule we do not recognize as a
        # clean resolve — be conservative (fail-safe): never refute on an unknown signal.
        rules = sorted({str(d.get("rule")) for d in ref_diags})
        return _abstain(
            "other", reference, detail=f"unrecognized diagnostic at {name!r} (rules={rules})"
        )

    # pyright ran, imports resolved, nothing at the reference → it resolves.
    return ev.refuted(
        provenance_tier=ev.TIER_T2,
        coverage=ev.coverage(backend=BACKEND, status=ev.STATUS_RAN, version=version()),
        reference=dict(reference),
        detail=f"pyright resolved {name!r} with no diagnostic in {in_file}",
    )


def _pyright_payload(
    repo_root: str, timeout: float | None, cache: dict[str, Any] | None
) -> Any | harness.RunResult | None:
    """Run (or reuse) one pyright invocation per project root.

    Returns the parsed JSON payload, a :class:`~harness.RunResult` when the harness
    failed open (so the caller emits its abstain), or ``None`` when the output did not
    parse. The result is memoized in ``cache`` keyed by ``repo_root``.
    """
    key = f"{BACKEND}:{os.path.realpath(repo_root)}"
    if cache is not None and key in cache:
        return cache[key]

    res = _run_pyright(repo_root, timeout)
    payload: Any | harness.RunResult | None
    if res.abstained:
        payload = res
    else:
        try:
            payload = json.loads(res.stdout)
        except (ValueError, TypeError):
            payload = None

    if cache is not None:
        cache[key] = payload
    return payload
