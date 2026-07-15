"""Data-shaping for the read-only ``/ticket/<id>`` audit page (story ff6f).

Pure functions that turn one :func:`rebar.audit.read.audit_trail` dict into a flat,
template-ready context: the three-gate status strip, the fixed-order gate sections
(plan → completion → code), per-round finding groups + convergence series, the
per-finding threshold meter, and the completion panel. NO web imports live here (so
``import rebar`` stays free of the ``[ui]`` stack); ``server.py`` renders the context
through Jinja2. Everything is best-effort — the read layer never raises and neither
does this shaping, so a malformed record degrades to an empty/"not run" section.
"""

from __future__ import annotations

from typing import Any

# The four canonical decision series. ``overflow`` (a payload BUCKET, never a per-finding
# ``decision`` value) folds into ``advisory``; any unknown decision folds into advisory too.
SERIES: tuple[str, ...] = ("block", "advisory", "dropped", "indeterminate")
SERIES_LABEL: dict[str, str] = {
    "block": "Blocking",
    "advisory": "Advisory",
    "dropped": "Dropped",
    "indeterminate": "Indeterminate",
}


def _one_decimal(value: Any) -> str | None:
    """Format a score to one decimal, or ``None`` when it is not a number."""
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return None


def _pct(value: Any) -> float | None:
    """A 0–1 score as a 0–100 percentage (1 decimal), or ``None`` when not numeric."""
    try:
        return round(float(value) * 100.0, 1)
    except (TypeError, ValueError):
        return None


def _series_of(decision: Any) -> str:
    """Map a per-finding ``decision`` to one of the four canonical series."""
    d = str(decision or "").strip().lower()
    return d if d in SERIES else "advisory"


def _coaching_for(payload: dict, finding_id: Any) -> dict | None:
    """The round's pass-4 coaching entry whose ``finding_refs`` contains ``finding_id``."""
    if not finding_id:
        return None
    for c in payload.get("coaching") or []:
        if not isinstance(c, dict):
            continue
        refs = c.get("finding_refs") or []
        if finding_id in refs:
            return {"coaching": c.get("coaching"), "move_name": c.get("move_name")}
    return None


def _plan_finding_view(f: dict, payload: dict) -> dict:
    """A template view-model for one plan-review finding (full four-pass shape + meter)."""
    decision = _series_of(f.get("decision"))
    bt = f.get("block_threshold")
    prio = f.get("priority")
    has_meter = bt is not None and prio is not None
    return {
        "is_plan": True,
        "id": f.get("id"),
        "norm_id": f.get("norm_id"),
        "finding": f.get("finding") or "",
        "criteria": f.get("criteria") or [],
        "location": f.get("location") or "",
        "evidence": f.get("evidence") or [],
        "scenarios": f.get("scenarios") or [],
        "impact": _one_decimal(f.get("impact")),
        "suggested_fix": f.get("suggested_fix") or "",
        "verification": f.get("verification") if isinstance(f.get("verification"), dict) else {},
        "decision": decision,
        "decision_label": SERIES_LABEL[decision],
        "reason": f.get("reason") or "",
        "priority": _one_decimal(prio),
        "block_threshold": _one_decimal(bt),
        "blocking_enabled": f.get("blocking_enabled"),
        "coaching": _coaching_for(payload, f.get("id")),
        # Threshold meter: fill to priority, tick at this finding's OWN block_threshold.
        "has_meter": has_meter,
        "meter_fill_pct": _pct(prio) if has_meter else None,
        "meter_tick_pct": _pct(bt) if has_meter else None,
        "meter_class": f"meter-{decision}",
        "meter_missing": not has_meter,  # v1: "threshold not recorded"
    }


def _code_finding_view(f: dict, series: str) -> dict:
    """A view-model for one code-review finding — only the fields it carries."""
    return {
        "is_plan": False,
        "id": f.get("id"),
        "norm_id": f.get("norm_id"),
        "finding": f.get("finding") or "",
        "location": f.get("location") or "",
        "suggested_fix": f.get("suggested_fix") or "",
        "verification": f.get("verification") if isinstance(f.get("verification"), dict) else {},
        "decision": series,
        "decision_label": SERIES_LABEL[series],
        "has_meter": False,
        "meter_missing": False,
        "coaching": None,
    }


def _group_and_sort(views_by_series: dict[str, list[dict]]) -> list[dict]:
    """Order the four series (block first) into render-ready groups, each sorted by
    priority descending; block is expanded (``open``), the rest collapsed."""
    groups: list[dict] = []
    for s in SERIES:
        items = views_by_series.get(s, [])
        items = sorted(items, key=lambda v: _sort_key(v), reverse=True)
        groups.append(
            {
                "series": s,
                "label": SERIES_LABEL[s],
                "count": len(items),
                "open": s == "block",
                "findings": items,
            }
        )
    return groups


def _sort_key(view: dict) -> float:
    try:
        return float(view.get("priority") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _plan_round(payload: dict) -> dict:
    """One plan-review round → grouped findings + per-series counts + header."""
    by_series: dict[str, list[dict]] = {s: [] for s in SERIES}
    for f in payload.get("findings") or []:
        if not isinstance(f, dict):
            continue
        v = _plan_finding_view(f, payload)
        by_series[v["decision"]].append(v)
    counts = {s: len(by_series[s]) for s in SERIES}
    return {
        "verdict": payload.get("verdict"),
        "model": payload.get("model"),
        "impact_model_version": payload.get("impact_model_version"),
        "material_fingerprint": payload.get("material_fingerprint"),
        "groups": _group_and_sort(by_series),
        "counts": counts,
    }


def _code_round(sidecar: dict) -> dict:
    """One code-review sidecar → grouped findings + per-series counts + header."""
    by_series: dict[str, list[dict]] = {s: [] for s in SERIES}
    bucket_map = {
        "block": sidecar.get("blocking") or [],
        "advisory": (sidecar.get("advisory") or []) + (sidecar.get("overflow") or []),
        "dropped": sidecar.get("dropped") or [],
        "indeterminate": sidecar.get("indeterminate") or [],
    }
    for s, bucket in bucket_map.items():
        for f in bucket:
            if isinstance(f, dict):
                by_series[s].append(_code_finding_view(f, s))
    counts = {s: len(by_series[s]) for s in SERIES}
    return {
        "verdict": sidecar.get("verdict"),
        "model": sidecar.get("model"),
        "impact_model_version": sidecar.get("impact_model_version"),
        "material_fingerprint": sidecar.get("change_fingerprint"),
        "groups": _group_and_sort(by_series),
        "counts": counts,
    }


def select_round(param: Any, total: int) -> int:
    """Resolve a ``?<gate>_round=`` query value to a 1-based index into the newest-first
    round list. Absent / non-integer / out-of-range clamps to 1 (latest)."""
    if total <= 0:
        return 0
    try:
        n = int(str(param))
    except (TypeError, ValueError):
        return 1
    if n < 1 or n > total:
        return 1
    return n


def _qs(plan_n: int, code_n: int) -> str:
    return f"?plan_round={plan_n}&code_round={code_n}"


def _selector(gate: str, sel: int, total: int, plan_n: int, code_n: int) -> dict:
    """The ``◂ round n of N ▸`` server-rendered selector for one gate. ``older`` steps to a
    higher (older) 1-based index, ``newer`` to a lower (newer) one; the OTHER gate's round
    param is preserved so the two selectors move independently."""
    older_href = newer_href = None
    if total > 1:
        if sel < total:  # an older round exists
            o_plan, o_code = (sel + 1, code_n) if gate == "plan" else (plan_n, sel + 1)
            older_href = _qs(o_plan, o_code)
        if sel > 1:  # a newer round exists
            n_plan, n_code = (sel - 1, code_n) if gate == "plan" else (plan_n, sel - 1)
            newer_href = _qs(n_plan, n_code)
    return {"n": sel, "N": total, "older_href": older_href, "newer_href": newer_href}


def _convergence(rounds_oldest_first: list[dict]) -> dict:
    """Convergence viz context. ≤3 rounds → small-multiple bars (one per round); ≥4 rounds
    → a line graph with one polyline per NON-EMPTY series. Always ships an sr-only table of
    per-round per-series counts (one row per round, one column per series)."""
    r = len(rounds_oldest_first)
    counts = [rnd["counts"] for rnd in rounds_oldest_first]
    max_count = max((c[s] for c in counts for s in SERIES), default=0) or 1

    # sr-only table rows.
    table_rows = [
        {"round": i + 1, "cells": [{"series": s, "count": c[s]} for s in SERIES]}
        for i, c in enumerate(counts)
    ]

    form = "bars" if r <= 3 else "line"
    bars: list[dict] = []
    polylines: list[dict] = []
    if form == "bars":
        for i, c in enumerate(counts):
            total = sum(c[s] for s in SERIES)
            bars.append(
                {
                    "round": i + 1,
                    "height_pct": round(total / max_count * 100.0, 1),
                    "counts": c,
                }
            )
    else:
        for s in SERIES:
            if not any(c[s] for c in counts):
                continue  # one polyline per NON-EMPTY series only
            pts = []
            for i, c in enumerate(counts):
                x = 0.0 if r <= 1 else round(i / (r - 1) * 100.0, 2)
                y = round(100.0 - (c[s] / max_count) * 100.0, 2)
                pts.append(f"{x},{y}")
            polylines.append({"series": s, "points": " ".join(pts), "css": f"conv-{s}"})

    return {
        "form": form,
        "rounds": r,
        "bars": bars,
        "polylines": polylines,
        "table_rows": table_rows,
        "series": [{"key": s, "label": SERIES_LABEL[s]} for s in SERIES],
    }


def _plan_section(plan_reviews: list[dict], plan_n: int, code_n: int) -> dict:
    total = len(plan_reviews)
    sel = select_round(plan_n if plan_n else None, total)
    if total == 0:
        return {"ran": False}
    payload = plan_reviews[sel - 1] if 0 < sel <= total else plan_reviews[0]
    rounds_oldest_first = [_plan_round(p) for p in reversed(plan_reviews)]
    return {
        "ran": True,
        "round": _plan_round(payload),
        "selector": _selector("plan", sel, total, sel, code_n or 1),
        "convergence": _convergence(rounds_oldest_first),
    }


def _flatten_code_sidecars(code_reviews: list[dict]) -> list[dict]:
    """All code-review sidecars across every related artifact, newest-first."""
    out: list[dict] = []
    for cr in code_reviews or []:
        for sc in cr.get("sidecars") or []:
            if isinstance(sc, dict):
                out.append(sc)
    return out


def _code_section(code_reviews: list[dict], plan_n: int, code_n: int) -> dict:
    sidecars = _flatten_code_sidecars(code_reviews)
    total = len(sidecars)
    if total == 0:
        return {"ran": False}
    sel = select_round(code_n if code_n else None, total)
    sidecar = sidecars[sel - 1] if 0 < sel <= total else sidecars[0]
    rounds_oldest_first = [_code_round(sc) for sc in reversed(sidecars)]
    return {
        "ran": True,
        "round": _code_round(sidecar),
        "selector": _selector("code", sel, total, plan_n or 1, sel),
        "convergence": _convergence(rounds_oldest_first),
    }


def _completion_section(completion: dict | None) -> dict:
    if not completion:
        return {"ran": False}
    sidecar = completion.get("sidecar")
    if not isinstance(sidecar, dict):
        return {"ran": False}
    if "criteria" in sidecar:  # PASS record
        rows = []
        for c in sidecar.get("criteria") or []:
            if not isinstance(c, dict):
                continue
            met = bool(c.get("met"))
            kind = str(c.get("kind") or "codebase-verifiable")
            rows.append(
                {
                    "criterion": c.get("criterion") or "",
                    "met": met,
                    "kind": kind,
                    "citation": c.get("citation"),
                    "lacking": kind == "operator-attested" and not met,
                }
            )
        return {"ran": True, "is_pass": True, "criteria": rows}
    # FAIL verdict (failures-only findings, no criteria).
    return {
        "ran": True,
        "is_pass": False,
        "findings": [f for f in (sidecar.get("findings") or []) if isinstance(f, dict)],
    }


def _gate_strip(trail: dict) -> dict:
    plan_reviews = trail.get("plan_reviews") or []
    plan: dict[str, Any] = {"ran": False}
    if plan_reviews:
        latest = plan_reviews[0]
        blocking = sum(
            1
            for f in (latest.get("findings") or [])
            if isinstance(f, dict) and _series_of(f.get("decision")) == "block"
        )
        plan = {"ran": True, "verdict": latest.get("verdict"), "blocking": blocking}

    comp = _completion_section(trail.get("completion"))
    completion: dict[str, Any] = {"ran": comp.get("ran", False)}
    if completion["ran"]:
        if comp.get("is_pass"):
            crit = comp.get("criteria") or []
            completion.update(
                {"status": "PASS", "met": sum(1 for c in crit if c["met"]), "total": len(crit)}
            )
        else:
            completion.update({"status": "FAIL"})

    sidecars = _flatten_code_sidecars(trail.get("code_reviews") or [])
    code: dict[str, Any] = {"ran": False}
    if sidecars:
        latest = sidecars[0]
        blocking = sum(1 for f in (latest.get("blocking") or []) if isinstance(f, dict))
        code = {"ran": True, "verdict": latest.get("verdict"), "blocking": blocking}

    return {"plan": plan, "completion": completion, "code": code}


def build_context(trail: dict, *, plan_round: Any = None, code_round: Any = None) -> dict:
    """Turn an ``audit_trail`` dict into the full template context for ``/ticket/<id>``."""
    raw_ticket = trail.get("ticket")
    ticket: dict = raw_ticket if isinstance(raw_ticket, dict) else {}
    tid = str(ticket.get("ticket_id") or ticket.get("id") or "")
    plan_reviews = trail.get("plan_reviews") or []
    code_reviews = trail.get("code_reviews") or []

    # Resolve the selected rounds up front so each selector preserves the other's param.
    plan_total = len(plan_reviews)
    code_total = len(_flatten_code_sidecars(code_reviews))
    plan_sel = select_round(plan_round, plan_total) if plan_total else 1
    code_sel = select_round(code_round, code_total) if code_total else 1

    return {
        "ticket_id": tid,
        "ticket": {
            "title": ticket.get("title") or tid,
            "status": ticket.get("status") or "",
            "assignee": ticket.get("assignee") or "",
            "description": ticket.get("description") or "",
        },
        "strip": _gate_strip(trail),
        "plan": _plan_section(plan_reviews, plan_sel, code_sel),
        "completion": _completion_section(trail.get("completion")),
        "code": _code_section(code_reviews, plan_sel, code_sel),
    }
