"""Criteria authoring guide rendering (R-5, epic cite-stone-sea / WS10).

A GENERATED, section-keyed Markdown guide (``docs/plan-review-criteria-guide.md``): one
``## <id>`` section per criterion, DERIVED from the registry (the rubric a plan author must
satisfy). Regenerated in place (``... registry regenerate-criteria-guide``) and kept honest by
:func:`validate_criteria_guide` (folded into the validate-routing gate) — the same
regenerate-in-place + parity-diff contract as ``reviewers/index.json``.
:func:`explain_criterion` is the ONE shared lookup that ``rebar explain``, the MCP read tool,
and the library all wrap.

Extracted from :mod:`.registry` along the guide call-graph seam (registry sits at the
module-size cap). Every :mod:`.registry` dependency (``load_criteria`` / ``CANONICAL_LLM`` /
``ExplainError``) is imported LAZILY inside the function that needs it, so importing this module
from ``registry`` (re-export) is never circular AND a ``monkeypatch.setattr(registry, ...)`` is
resolved off the live module attribute at call time. ``registry`` re-exports these names, so
every ``registry.<name>`` call site (``guide_parity``, the MCP tool, the CLI, the library) is
unchanged.
"""

from __future__ import annotations

import re
from typing import Any

_GUIDE_RELPATH = ("docs", "plan-review-criteria-guide.md")


def _guide_path(repo_root_path: str | None = None):  # -> Path
    from rebar import config

    return config.repo_root(repo_root_path).joinpath(*_GUIDE_RELPATH)


def _guide_section_body(criterion: dict[str, Any]) -> str:
    posture = criterion.get("default_posture", "advisory")
    header = f"**{criterion.get('name', '')}** — exec:{criterion.get('exec', '1-TURN')}, {posture}"
    facet = criterion.get("facet", "")
    if facet:
        header += f", facet:{facet}"
    lines = [f"## {criterion['id']}", header, "", (criterion.get("scenario") or "").strip()]
    checklist = criterion.get("checklist") or []
    if checklist:
        lines += ["", "Checklist:"]
        lines += [f"- {c.get('check', c) if isinstance(c, dict) else c}" for c in checklist]
    return "\n".join(lines).rstrip()


def regenerate_criteria_guide(repo_root_path: str | None = None) -> str:
    """Generate docs/plan-review-criteria-guide.md for the canonical built-in registry.

    ``repo_root_path`` selects the output checkout only.  Project-local overlays remain
    available through :func:`explain_criterion`, but are intentionally excluded from this
    tracked, package-wide guide so regeneration inside a configured project stays parity-clean.
    """
    from .registry import load_criteria

    criteria = sorted(load_criteria(repo_root=""), key=lambda c: c["id"])
    header = (
        "# Plan-review criteria authoring guide\n\n"
        "GENERATED from the criteria registry (`python -m rebar.llm.plan_review.registry "
        "regenerate-criteria-guide`) — do not hand-edit. One `## <criterion-id>` section per "
        "criterion; `rebar explain <criterion-id>` prints a section, and coach deep-links anchor "
        "to `#<criterion-id lower-cased>` (the heading slug).\n"
    )
    body = "\n\n".join(_guide_section_body(c) for c in criteria)
    path = _guide_path(repo_root_path)
    path.write_text(header + "\n" + body + "\n", encoding="utf-8")
    return str(path)


def _guide_sections(text: str) -> dict[str, str]:
    """Parse a guide into ``{criterion-id: section-text}`` keyed by ``## <id>`` headings."""
    out: dict[str, str] = {}
    cur_id: str | None = None
    buf: list[str] = []
    for line in text.split("\n"):
        m = re.match(r"^## (\S+)\s*$", line)
        if m:
            if cur_id is not None:
                out[cur_id] = "\n".join(buf).strip()
            cur_id, buf = m.group(1), [line]
        elif cur_id is not None:
            buf.append(line)
    if cur_id is not None:
        out[cur_id] = "\n".join(buf).strip()
    return out


def validate_criteria_guide(repo_root_path: str | None = None) -> list[str]:
    """Parity: every ``CANONICAL_LLM`` criterion has a ``## <id>`` guide section and the guide
    has no ORPHAN section. Returns problems (empty == in sync). Folded into the routing gate so a
    removed/renamed section fails ``validate-routing``."""
    from .registry import CANONICAL_LLM

    path = _guide_path(repo_root_path)
    if not path.exists():
        return [f"criteria guide missing at {path} (run regenerate-criteria-guide)"]
    sections = set(_guide_sections(path.read_text(encoding="utf-8")))
    problems = [
        f"criterion {cid!r} has no `## {cid}` section in the criteria guide"
        for cid in sorted(CANONICAL_LLM - sections)
    ]
    problems += [
        f"criteria guide has an ORPHAN section `## {cid}` (not in CANONICAL_LLM)"
        for cid in sorted(sections - CANONICAL_LLM)
    ]
    return problems


def explain_criterion(criterion_id: str, *, repo_root_path: str | None = None) -> str:
    """The ONE shared lookup behind ``rebar explain``, the MCP ``explain_criterion`` tool, and the
    library — returns a criterion's authoring-guide section, RENDERED from the packaged registry
    (via :func:`_guide_section_body`, the same content ``regenerate_criteria_guide`` writes into
    the docs guide). Rendering from the registry rather than reading ``docs/`` makes the lookup
    work from any installation (an installed rebar has no ``docs/`` tree). ``repo_root_path`` still
    flows to :func:`load_criteria` so a project overlay's ``project.<name>`` criteria resolve.
    Raises :class:`ExplainError` with a ``kind`` of ``malformed-registry`` / ``unknown-id``."""
    from .registry import ExplainError, load_criteria

    try:
        by_id = {c["id"]: c for c in load_criteria(repo_root=repo_root_path)}
    except Exception as exc:
        raise ExplainError("malformed-registry", f"criteria registry is malformed: {exc}") from exc
    criterion = by_id.get(criterion_id)
    if criterion is None:
        raise ExplainError(
            "unknown-id",
            f"unknown criterion {criterion_id!r}; known: {', '.join(sorted(by_id))}",
        )
    return _guide_section_body(criterion)
