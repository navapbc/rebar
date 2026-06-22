"""IR <-> BPMN 2.0 serializer — the thin bespoke piece behind visual editing (WS / 00da).

Human visual editing rides an off-the-shelf, CONSTRAINED editor (bpmn-js); the only
custom code is this serializer, which projects the v2 workflow IR onto BPMN 2.0 XML
and round-trips edits back. The projection is BOTH structural and lossless:

  * **Structure → BPMN element types** (so it is a real, editable diagram and a visual
    flow edit maps back to the IR):
      scripted step  -> ``bpmn:scriptTask``
      agent step     -> ``bpmn:serviceTask`` + a typed ``<rebar:Agent>`` (prompt/…)
      branch         -> ``bpmn:exclusiveGateway`` + a ``then``/``else`` sub-process arm
      loop           -> ``bpmn:subProcess`` + ``standardLoopCharacteristics``
      map            -> ``bpmn:subProcess`` + ``multiInstanceLoopCharacteristics``
    ``needs`` edges become ``bpmn:sequenceFlow``s within each frame, and nesting
    (loop/map/branch bodies) becomes sub-process containment. Stable step ids are
    preserved verbatim.
  * **Exact config → a ``rebar`` extension** (the bounded part BPMN's vocabulary can't
    express): each element carries ``<rebar:Config value="…json…">`` with the step's
    non-structural config (``with``/``mode``/``max_iterations``/``while``/``over``/…).
    Reconstruction takes STRUCTURE from BPMN (id, kind, ``needs``, nesting) and CONFIG
    from the extension, so a visual flow edit is honoured while exact semantics
    survive. The de-risk POC proved extension elements are SILENTLY STRIPPED unless a
    moddle descriptor is registered — :data:`REBAR_MODDLE_DESCRIPTOR` is that
    descriptor (the JS editor side), shipped as package data.

A deterministic left-to-right auto-layout (BPMN DI) is generated on serialize so the
diagram opens reproducibly; the DI is NOT parsed back (the IR is the source of truth,
the visual format is never committed). Pure stdlib (``xml.etree`` + ``json``).
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from typing import Any

from .schema import step_kind

# ── Namespaces ────────────────────────────────────────────────────────────────

BPMN = "http://www.omg.org/spec/BPMN/20100524/MODEL"
BPMNDI = "http://www.omg.org/spec/BPMN/20100524/DI"
DC = "http://www.omg.org/spec/DD/20100524/DC"
DI = "http://www.omg.org/spec/DD/20100524/DI"
REBAR = "http://rebar.dev/schema/workflow/1.0"

_NS = {"bpmn": BPMN, "bpmndi": BPMNDI, "dc": DC, "di": DI, "rebar": REBAR}
for _p, _u in _NS.items():
    ET.register_namespace(_p, _u)


def _q(prefix: str, tag: str) -> str:
    return f"{{{_NS[prefix]}}}{tag}"


# The moddle descriptor the bpmn-js editor MUST register so the rebar extension
# elements survive save/copy cycles (POC T1/T2). Shipped to the JS side verbatim.
REBAR_MODDLE_DESCRIPTOR: dict[str, Any] = {
    "name": "Rebar",
    "uri": REBAR,
    "prefix": "rebar",
    "types": [
        {
            "name": "Config",
            "superClass": ["Element"],
            "properties": [{"name": "value", "isAttr": True, "type": "String"}],
        },
        {
            "name": "Agent",
            "superClass": ["Element"],
            "properties": [
                {"name": "prompt", "isAttr": True, "type": "String"},
                {"name": "provider", "isAttr": True, "type": "String"},
                {"name": "tools", "isAttr": True, "type": "String"},
            ],
        },
        {
            "name": "Workflow",
            "superClass": ["Element"],
            "properties": [{"name": "value", "isAttr": True, "type": "String"}],
        },
    ],
}

# Structural keys taken from BPMN structure (NOT stored in the Config extension): the
# id, the discriminator/nested bodies, and ``needs`` (= sequence flows). An explicit
# ``type`` discriminator IS kept in Config so a schema-valid input round-trips exactly.
_STRUCTURAL = {"id", "uses", "prompt", "branch", "loop", "map", "needs"}

# The role marker stamped (via ``<rebar:Config>``) on a branch arm sub-process so
# reconstruction recovers then/else WITHOUT parsing the arm's id — the id is not
# round-trip-stable through bpmn-js, but a registered extension attribute is.
_ROLE_KEY = "_role"

# Process-child element tags that are NOT rebar steps and are legitimately ignored on
# read (structural plumbing / diagram annotations). Anything else that isn't a known
# step kind is a hard error (no silent drops) — see :func:`_read_frame`.
_IGNORED_TAGS = frozenset(
    {
        "sequenceFlow",
        "extensionElements",
        "startEvent",
        "endEvent",
        "association",
        "textAnnotation",
        "group",
        "laneSet",
        "documentation",
    }
)


# ── IR -> BPMN ────────────────────────────────────────────────────────────────


def ir_to_bpmn(doc: dict[str, Any]) -> str:
    """Serialize a v2 workflow IR ``doc`` to a BPMN 2.0 XML string (with a
    deterministic auto-layout). The inverse is :func:`bpmn_to_ir`."""
    defs = ET.Element(
        _q("bpmn", "definitions"),
        {"id": "defs_rebar", "targetNamespace": "http://rebar.dev"},
    )
    proc = ET.SubElement(defs, _q("bpmn", "process"), {"id": _proc_id(doc), "isExecutable": "true"})
    # Workflow-level fields (schema_version/name/description/model/inputs) ride on the
    # process so the whole document round-trips, not just the steps.
    meta = {
        k: doc[k] for k in ("schema_version", "name", "description", "model", "inputs") if k in doc
    }
    _ext(proc, "Workflow", value=json.dumps(meta, sort_keys=True))

    # 1. Emit the BPMN model (elements + sequence flows); collect the flow edges.
    edges: list[tuple[str, str, str]] = []  # (flow_id, source, target)
    steps = doc.get("steps", [])
    _emit_frame(proc, steps, edges)
    # 2. A start event -> the root step(s) and the terminal step(s) -> an end event, so the
    #    diagram shows WHERE the workflow begins/ends (round-trip drops these: _read_frame
    #    excludes start/end events and their flows).
    roots, sinks = _emit_terminals(proc, steps, edges)
    # 3. Compute a real layered layout (boxes + which sub-processes are expanded).
    expanded: set[str] = set()
    boxes, _w, _h = _layout_frame(steps, expanded)
    _place_terminals(boxes, roots, sinks)
    # 4. Emit the DI from the layout (shapes, expanded flags, docked edge waypoints).
    _emit_di(defs, _proc_id(doc), boxes, expanded, edges)
    ET.indent(defs, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(defs, encoding="unicode")


_START_ID = "StartEvent_1"
_END_ID = "EndEvent_1"


def _emit_terminals(proc: ET.Element, steps, edges) -> tuple[list[str], list[str]]:
    """Emit a top-frame ``startEvent`` -> each root step and each sink step -> an
    ``endEvent``, recording the flows in ``edges``. Returns ``(roots, sinks)``."""
    top = [s for s in steps if isinstance(s, dict) and "id" in s]
    ids = {s["id"] for s in top}
    needed: set[str] = set()
    for s in top:
        needed |= {n for n in (s.get("needs") or []) if n in ids}
    roots = [s["id"] for s in top if not any(n in ids for n in (s.get("needs") or []))]
    sinks = [s["id"] for s in top if s["id"] not in needed]
    if not top:
        return roots, sinks
    ET.SubElement(proc, _q("bpmn", "startEvent"), {"id": _START_ID, "name": "start"})
    sf = _q("bpmn", "sequenceFlow")
    for r in roots:
        fid = f"flow_start__{r}"
        ET.SubElement(proc, sf, {"id": fid, "sourceRef": _START_ID, "targetRef": r})
        edges.append((fid, _START_ID, r))
    ET.SubElement(proc, _q("bpmn", "endEvent"), {"id": _END_ID, "name": "end"})
    for k in sinks:
        fid = f"flow_{k}__end"
        ET.SubElement(proc, sf, {"id": fid, "sourceRef": k, "targetRef": _END_ID})
        edges.append((fid, k, _END_ID))
    return roots, sinks


def _place_terminals(boxes: dict[str, list[float]], roots: list[str], sinks: list[str]) -> None:
    """Add boxes for the start/end events: shift the laid-out steps right to make room for
    the start event on the left, then place start (centred on the roots) and end (centred
    on the sinks, at the far right)."""
    if not boxes:
        return
    ev = 36.0
    dx = ev + _HGAP
    for b in boxes.values():
        b[0] += dx

    def _centre(ids: list[str]) -> float:
        bs = [boxes[i] for i in ids if i in boxes]
        if not bs:
            return 0.0
        return (min(b[1] for b in bs) + max(b[1] + b[3] for b in bs)) / 2 - ev / 2

    boxes[_START_ID] = [0.0, _centre(roots), ev, ev]
    end_x = max(b[0] + b[2] for b in boxes.values()) + _HGAP
    boxes[_END_ID] = [end_x, _centre(sinks), ev, ev]


def _proc_id(doc: dict[str, Any]) -> str:
    name = doc.get("name")
    return f"wf_{name}" if isinstance(name, str) and name else "wf_workflow"


def _ext(parent: ET.Element, kind: str, **attrs: str) -> None:
    """Attach a ``<bpmn:extensionElements><rebar:KIND …/></…>`` to ``parent``."""
    holder = parent.find(_q("bpmn", "extensionElements"))
    if holder is None:
        holder = ET.SubElement(parent, _q("bpmn", "extensionElements"))
    ET.SubElement(holder, _q("rebar", kind), {k: v for k, v in attrs.items() if v is not None})


def _config_json(step: dict[str, Any]) -> str:
    """The step's non-structural config (everything BPMN structure can't carry)."""
    cfg = {k: v for k, v in step.items() if k not in _STRUCTURAL}
    for disc in ("loop", "map", "branch"):
        block = step.get(disc)
        if isinstance(block, dict):
            cfg.update({k: v for k, v in block.items() if k not in ("body", "then", "else")})
    return json.dumps(cfg, sort_keys=True)


def _emit_frame(container: ET.Element, steps, edges) -> None:
    """Emit one frame's steps as flow elements under ``container``; wire ``needs`` as
    sequence flows (collecting each as ``(flow_id, src, tgt)`` in ``edges`` for the DI);
    recurse into control bodies. Geometry is computed separately by :func:`_layout_frame`."""
    ids = [s["id"] for s in steps if isinstance(s, dict) and "id" in s]
    branch_ids = {s["id"] for s in steps if isinstance(s, dict) and step_kind(s) == "branch"}
    for s in steps:
        if not isinstance(s, dict) or "id" not in s:
            continue
        _emit_step(container, s, step_kind(s), edges)
    # sequence flows from needs (same frame only)
    for s in steps:
        if not isinstance(s, dict) or "id" not in s:
            continue
        for n in s.get("needs") or []:
            if n in ids:
                fid = f"flow_{n}__{s['id']}"
                attrs = {"id": fid, "sourceRef": n, "targetRef": s["id"]}
                # A continuation OUT of a branch gateway (the step runs after the branch
                # completes, regardless of arm) is labelled "after" — so it reads as the
                # post-branch step, not a third decision outcome.
                if n in branch_ids:
                    attrs["name"] = "after"
                ET.SubElement(container, _q("bpmn", "sequenceFlow"), attrs)
                edges.append((fid, n, s["id"]))


def _emit_step(container, s, kind, edges) -> ET.Element:
    # NB: ``extensionElements`` (Config) is attached FIRST, before any
    # loopCharacteristics / child flowElements, to honour the BPMN 2.0 XSD child
    # ordering (extensionElements precedes loopCharacteristics precedes flowElement).
    sid = s["id"]
    if kind == "scripted":
        el = ET.SubElement(container, _q("bpmn", "scriptTask"), {"id": sid, "name": s["uses"]})
        _ext(el, "Config", value=_config_json(s))
        return el
    if kind == "agent":
        el = ET.SubElement(container, _q("bpmn", "serviceTask"), {"id": sid, "name": s["prompt"]})
        _ext(el, "Config", value=_config_json(s))
        # The typed <rebar:Agent> is WRITE-ONLY/editor-facing (the POC's
        # extension-survival proof + display); reconstruction reads prompt from
        # ``name``, never from here, so provider/tools are display fixtures.
        _ext(el, "Agent", prompt=s.get("prompt"), provider="anthropic", tools="fs,mcp,rebar")
        return el
    if kind == "loop":
        sp = ET.SubElement(container, _q("bpmn", "subProcess"), {"id": sid, "name": "loop"})
        _ext(sp, "Config", value=_config_json(s))
        ET.SubElement(sp, _q("bpmn", "standardLoopCharacteristics"))
        _emit_frame(sp, s["loop"].get("body", []), edges)
        return sp
    if kind == "map":
        sp = ET.SubElement(container, _q("bpmn", "subProcess"), {"id": sid, "name": "map"})
        _ext(sp, "Config", value=_config_json(s))
        seq = "false" if (s["map"].get("max_concurrency") or 1) > 1 else "true"
        ET.SubElement(sp, _q("bpmn", "multiInstanceLoopCharacteristics"), {"isSequential": seq})
        _emit_frame(sp, s["map"].get("body", []), edges)
        return sp
    # branch -> exclusiveGateway + a then/else sub-process arm each. The arm's ROLE
    # (then/else) is carried two ways that both survive a real bpmn-js edit: a
    # ``<rebar:Config _role=…>`` marker AND the gateway->arm sequence flow. Reconstruction
    # reads the role from those, NOT from the arm's id — so it is robust to bpmn-js
    # regenerating ids on Save. The id itself uses ``.`` as a readable separator (a legal
    # BPMN NCName char that the schema's step-id pattern forbids, so it can't collide with
    # a real step id). The old ``@`` separator was an ILLEGAL BPMN id: bpmn-moddle dropped
    # the whole arm on Save, silently deleting the branch (see tests/e2e).
    # The gateway is LABELLED with its condition (so the decision logic is visible on the
    # canvas, not buried in config) and each outgoing flow is labelled then/else (so you can
    # see which branch is taken when). Names are display-only — reconstruction reads the
    # condition from <rebar:Config> and the arm role from the _role marker, never the labels.
    when = s["branch"].get("when") if isinstance(s.get("branch"), dict) else None
    gw_name = when if isinstance(when, str) and when else "branch?"
    gw = ET.SubElement(container, _q("bpmn", "exclusiveGateway"), {"id": sid, "name": gw_name})
    _ext(gw, "Config", value=_config_json(s))
    else_flow_id: str | None = None
    for arm, label in (("then", "true"), ("else", "false")):
        body = s["branch"].get(arm)
        if not isinstance(body, list):
            continue
        arm_id = f"{sid}.{arm}"
        # Name the arm with its gateway id so the label is UNIQUE and visibly tied to its
        # branch (e.g. ``decide ▸ then``) — not a bare ``then`` that repeats across every
        # branch and can't be told apart. (Display only; role comes from the _role marker.)
        sp = ET.SubElement(
            container, _q("bpmn", "subProcess"), {"id": arm_id, "name": f"{sid} ▸ {arm}"}
        )
        _ext(sp, "Config", value=json.dumps({_ROLE_KEY: arm}))  # role survives id rewrites
        _emit_frame(sp, body, edges)
        fid = f"flow_{sid}.{arm}"
        ET.SubElement(
            container,
            _q("bpmn", "sequenceFlow"),
            {"id": fid, "name": label, "sourceRef": sid, "targetRef": arm_id},
        )
        edges.append((fid, sid, arm_id))
        if arm == "else":
            else_flow_id = fid
    # Mark the else arm as the gateway's DEFAULT flow → bpmn-js draws the default-flow slash
    # on it, so it reads unambiguously as the fallback ("when the condition is false"). The
    # `default` attribute is display-only here (reconstruction reads then/else from the role
    # markers), so it does not affect the round-trip.
    if else_flow_id is not None:
        gw.set("default", else_flow_id)
    return gw


# ── Layout (a real layered DAG layout, generated; never committed) ──────────────
#
# Each frame is laid out left-to-right by longest-path rank, with same-rank nodes stacked
# vertically (so PARALLEL siblings never collide — the old lane-only layout drew them on
# one row, on top of each other). Control bodies are laid out recursively and their parent
# sub-process is sized to CONTAIN them and marked expanded, so loop/map/branch bodies show
# inline on the canvas instead of as a collapsed drill-down box. bpmn-auto-layout was
# evaluated and rejected: for our coordinate-free, start/end-event-free, nested IR it
# stacks nodes in a single column and emits no edges.

_LEAF = (100, 80)  # task shape (bpmn-js default size)
_GW = (50, 50)  # gateway shape
_PAD = 30  # inner padding of an expanded sub-process
_HDR = 30  # header band at the top of an expanded sub-process (label/marker room)
_HGAP = 90  # horizontal gap between ranks (columns)
_VGAP = 40  # vertical gap between nodes in the same rank
_MARGIN = 40  # diagram margin


def _frame_rank(ids: set[str], edges: list[tuple[str, str]]) -> dict[str, int]:
    """Longest-path rank over a frame's edge set (`(src, tgt)`), so a node sits to the
    right of everything that must precede it."""
    preds: dict[str, list[str]] = {i: [] for i in ids}
    for a, b in edges:
        if a in ids and b in ids:
            preds[b].append(a)
    rank: dict[str, int] = {}

    def depth(n: str, seen: frozenset[str]) -> int:
        if n in rank:
            return rank[n]
        r = 0 if (n in seen or not preds[n]) else 1 + max(depth(p, seen | {n}) for p in preds[n])
        rank[n] = r
        return r

    for i in ids:
        depth(i, frozenset())
    return rank


def _layout_frame(steps, expanded: set[str]) -> tuple[dict[str, list[float]], float, float]:
    """Lay out one frame. Returns ``(boxes, width, height)`` where ``boxes`` maps every
    element id in this frame (and, recursively, inside its control bodies) to a LOCAL
    ``[x, y, w, h]`` with the frame's content starting at ``(0, 0)``. Container (loop/map/
    branch-arm) ids are added to ``expanded``."""
    sizes: dict[str, tuple[float, float]] = {}
    order: list[str] = []
    children: dict[str, tuple[dict[str, list[float]], float, float]] = {}
    frame_edges: list[tuple[str, str]] = []  # gateway -> arm (structural, for ranking)

    def add(nid: str, size: tuple[float, float]) -> None:
        sizes[nid] = size
        order.append(nid)

    for s in steps:
        if not isinstance(s, dict) or "id" not in s:
            continue
        sid, kind = s["id"], step_kind(s)
        if kind in ("scripted", "agent"):
            add(sid, _LEAF)
        elif kind in ("loop", "map"):
            cb, cw, ch = _layout_frame(s[kind].get("body", []) or [], expanded)
            children[sid] = (cb, cw, ch)
            add(sid, (cw + 2 * _PAD, ch + _HDR + _PAD))
            expanded.add(sid)
        elif kind == "branch":
            add(sid, _GW)
            for arm in ("then", "else"):
                body = s["branch"].get(arm)
                if isinstance(body, list):
                    aid = f"{sid}.{arm}"
                    cb, cw, ch = _layout_frame(body, expanded)
                    children[aid] = (cb, cw, ch)
                    add(aid, (cw + 2 * _PAD, ch + _HDR + _PAD))
                    expanded.add(aid)
                    frame_edges.append((sid, aid))

    ids = set(sizes)
    rank_edges = list(frame_edges)
    for s in steps:
        if isinstance(s, dict) and "id" in s:
            for n in s.get("needs") or []:
                if n in ids and s["id"] in ids:
                    rank_edges.append((n, s["id"]))
    rank = _frame_rank(ids, rank_edges)

    by_rank: dict[int, list[str]] = {}
    for nid in order:  # document order within a rank → stable vertical stacking
        by_rank.setdefault(rank[nid], []).append(nid)
    rank_w = {r: max(sizes[i][0] for i in ns) for r, ns in by_rank.items()}
    x_of = {}
    acc = 0.0
    for r in sorted(by_rank):
        x_of[r] = acc
        acc += rank_w[r] + _HGAP

    boxes: dict[str, list[float]] = {}
    for r in sorted(by_rank):
        y = 0.0
        for nid in by_rank[r]:
            w, h = sizes[nid]
            boxes[nid] = [x_of[r] + (rank_w[r] - w) / 2, y, w, h]
            y += h + _VGAP

    for cid, (cb, _cw, _ch) in children.items():
        cx, cy = boxes[cid][0], boxes[cid][1]
        ox, oy = cx + _PAD, cy + _HDR
        for k, (x, y, w, h) in cb.items():
            boxes[k] = [x + ox, y + oy, w, h]

    width = max((b[0] + b[2] for b in boxes.values()), default=0.0)
    height = max((b[1] + b[3] for b in boxes.values()), default=0.0)
    return boxes, width, height


def _emit_di(defs, proc_id, boxes, expanded, edges) -> None:
    """Emit the BPMN DI from the computed layout: one ``BPMNShape`` per element (flagged
    ``isExpanded`` for sub-processes so their bodies render inline) and one ``BPMNEdge``
    per sequence flow, with waypoints DOCKED to the source's right edge and the target's
    left edge (not centre-to-centre, so arrows don't cut through node labels)."""
    plane_owner = ET.SubElement(defs, _q("bpmndi", "BPMNDiagram"), {"id": "di"})
    plane = ET.SubElement(
        plane_owner, _q("bpmndi", "BPMNPlane"), {"id": "plane", "bpmnElement": proc_id}
    )

    def n(v: float) -> str:
        return str(int(round(v)))

    for sid, (x, y, w, h) in boxes.items():
        attrs = {"id": f"di_{sid}", "bpmnElement": sid}
        if sid in expanded:
            attrs["isExpanded"] = "true"
        shp = ET.SubElement(plane, _q("bpmndi", "BPMNShape"), attrs)
        ET.SubElement(
            shp,
            _q("dc", "Bounds"),
            {"x": n(x + _MARGIN), "y": n(y + _MARGIN), "width": n(w), "height": n(h)},
        )
    for fid, src, tgt in edges:
        if src not in boxes or tgt not in boxes:
            continue
        sx, sy, sw, sh = boxes[src]
        tx, ty, _tw, th = boxes[tgt]
        ed = ET.SubElement(plane, _q("bpmndi", "BPMNEdge"), {"id": f"di_{fid}", "bpmnElement": fid})
        wp = ET.SubElement
        wp(ed, _q("di", "waypoint"), {"x": n(sx + sw + _MARGIN), "y": n(sy + sh / 2 + _MARGIN)})
        wp(ed, _q("di", "waypoint"), {"x": n(tx + _MARGIN), "y": n(ty + th / 2 + _MARGIN)})


# ── BPMN -> IR ────────────────────────────────────────────────────────────────


def bpmn_to_ir(xml: str) -> dict[str, Any]:
    """Reconstruct the v2 workflow IR from BPMN XML (the inverse of
    :func:`ir_to_bpmn`). Structure (id, kind, ``needs``, nesting) is read from BPMN;
    exact config from the ``rebar`` extension. The DI layout is ignored."""
    root = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    proc = root.find(_q("bpmn", "process"))
    if proc is None:
        raise ValueError("no <bpmn:process> in document")
    meta = _read_ext_json(proc, "Workflow") or {}
    doc: dict[str, Any] = {}
    for k in ("schema_version", "name", "description", "model", "inputs"):
        if k in meta:
            doc[k] = meta[k]
    doc.setdefault("schema_version", "2")
    doc["steps"] = _read_frame(proc)
    return doc


def _read_ext_json(el: ET.Element, kind: str) -> dict[str, Any] | None:
    holder = el.find(_q("bpmn", "extensionElements"))
    if holder is None:
        return None
    node = holder.find(_q("rebar", kind))
    if node is None or node.get("value") is None:
        return None
    return json.loads(node.get("value"))


def _arm_ids(container: ET.Element) -> set[str]:
    """Ids of the branch-arm sub-processes in this frame. An arm is a SUB-PROCESS wired to
    an exclusiveGateway by a sequence flow AND carrying a then/else role (the same role-
    aware test :func:`_gateway_arms` uses) — so a gateway's flow to an ordinary downstream
    step (a real ``needs`` edge, e.g. ``decide -> notify``) is NOT mistaken for an arm."""
    gw_ids = [
        el.get("id")
        for el in container
        if el.tag.split("}")[-1] == "exclusiveGateway" and el.get("id")
    ]
    arms: set[str] = set()
    for gw_id in gw_ids:
        for arm_el, _role in _gateway_arms(container, gw_id):
            if arm_el.get("id"):
                arms.add(arm_el.get("id"))
    return arms


def _gateway_arms(container: ET.Element, gw_id: str) -> list[tuple[ET.Element, str]]:
    """[(arm_el, role)] for the branch gateway ``gw_id``. Arms are found via the gateway's
    outgoing sequence flows (structural); each role (``then``/``else``) is read from the
    ``<rebar:Config _role=…>`` marker, falling back to the arm's @name then an id suffix.
    Both the flow and the extension marker survive a bpmn-js edit, so this does not rely on
    the arm id (which bpmn-js may regenerate)."""
    by_id = {el.get("id"): el for el in container if el.get("id")}
    out: list[tuple[ET.Element, str]] = []
    seen: set[str] = set()
    for flow in container.findall(_q("bpmn", "sequenceFlow")):
        if flow.get("sourceRef") != gw_id:
            continue
        tgt = by_id.get(flow.get("targetRef"))
        if tgt is None or tgt.tag.split("}")[-1] != "subProcess":
            continue
        cfg = _read_ext_json(tgt, "Config") or {}
        role = cfg.get(_ROLE_KEY)
        if role not in ("then", "else") and tgt.get("name") in ("then", "else"):
            role = tgt.get("name")
        if role not in ("then", "else"):  # last resort: an id suffix
            tid = tgt.get("id") or ""
            for sep in (".", "@"):
                tail = tid.rsplit(sep, 1)[-1] if sep in tid else ""
                if tail in ("then", "else"):
                    role = tail
        if role in ("then", "else") and role not in seen:
            out.append((tgt, role))
            seen.add(role)
    return out


def _read_frame(container: ET.Element) -> list[dict[str, Any]]:
    """Reconstruct one frame's steps (in document order) with their ``needs`` from the
    sequence flows whose target is in this frame.

    Robust to an EDITED file from bpmn-js, which adds ``startEvent``/``endEvent`` and
    wires them with sequence flows: those event ids — and the branch-arm sub-processes
    (consumed by their gateway, never standalone steps) — are excluded from ``needs`` and
    from the step list, so a real editor save round-trips without fabricating a dependency
    on a non-step element (the visual format is never committed; only this reconstruction
    feeds the IR).
    """
    # Ids that are NOT first-class steps of this frame: events bpmn-js adds, and branch
    # arm sub-processes. A flow touching one of these is structural plumbing (e.g. a
    # gateway->arm edge), never a ``needs`` edge.
    non_step_ids = {
        el.get("id")
        for el in container
        if el.tag.split("}")[-1] in ("startEvent", "endEvent") and el.get("id")
    }
    non_step_ids |= _arm_ids(container)

    # needs: target id -> [source ids] in document (= original) order, so an unsorted
    # ``needs`` round-trips exactly.
    needs: dict[str, list[str]] = {}
    for flow in container.findall(_q("bpmn", "sequenceFlow")):
        src, tgt = flow.get("sourceRef"), flow.get("targetRef")
        if not (src and tgt):
            continue
        if src in non_step_ids or tgt in non_step_ids:
            continue  # gateway->arm plumbing or an event flow, not a needs edge
        needs.setdefault(tgt, []).append(src)

    steps: list[dict[str, Any]] = []
    for el in list(container):
        tag = el.tag.split("}")[-1]
        eid = el.get("id")
        if tag in _IGNORED_TAGS or eid is None:
            continue
        if eid in non_step_ids:  # a branch arm or an event — not a top step of this frame
            continue
        step = _read_step(container, el, tag)
        if step is None:
            # An element that is neither a known rebar step nor ignorable structural
            # plumbing. FAIL LOUDLY rather than silently dropping it — a silent drop lets
            # a user save and close the editor believing their node was kept when it was
            # discarded (the data-loss footgun). They must map it to a real step kind.
            raise ValueError(
                f"the diagram contains a {tag!r} element (id {eid!r}) that does not map to "
                f"a rebar step. Change it to a Script Task (scripted), a Service Task "
                f"(agent), a loop/map sub-process, or a branch gateway — or remove it."
            )
        if eid in needs:
            step["needs"] = needs[eid]
        steps.append(step)
    return steps


def _read_step(container: ET.Element, el: ET.Element, tag: str) -> dict[str, Any] | None:
    eid = el.get("id")
    cfg = _read_ext_json(el, "Config") or {}
    step: dict[str, Any] = {"id": eid}
    if tag in ("scriptTask", "task"):
        # A plain ``bpmn:task`` is what the bpmn-js palette draws by default; map it to a
        # scripted step (rather than silently dropping the user's new node) so their work
        # is never lost — they pick the real `uses`/kind in the panel.
        step["uses"] = el.get("name") or cfg.get("uses") or eid
        _merge_cfg(step, cfg)
        return step
    if tag == "serviceTask":
        step["prompt"] = cfg.get("prompt") or el.get("name") or eid
        _merge_cfg(step, cfg, skip={"prompt"})
        return step
    if tag == "subProcess":
        body = _read_frame(el)
        if el.find(_q("bpmn", "multiInstanceLoopCharacteristics")) is not None:
            step["map"] = {**cfg, "body": body}
        elif el.find(_q("bpmn", "standardLoopCharacteristics")) is not None:
            step["loop"] = {**cfg, "body": body}
        else:
            # A bare sub-process (no loop characteristics) is not a rebar construct —
            # the editor must mark it as a loop or a multi-instance map. Fail loudly
            # rather than silently fabricate a loop without max_iterations (invalid v2).
            raise ValueError(
                f"sub-process {eid!r} has no loop/multiInstance characteristics — "
                f"a rebar loop needs standardLoopCharacteristics, a map needs "
                f"multiInstanceLoopCharacteristics"
            )
        return step
    if tag == "exclusiveGateway":
        branch: dict[str, Any] = dict(cfg)
        for arm_el, role in _gateway_arms(container, eid):
            branch[role] = _read_frame(arm_el)
        step["branch"] = branch
        return step
    return None


def _merge_cfg(step: dict[str, Any], cfg: dict[str, Any], skip: set[str] = frozenset()) -> None:
    for k, v in cfg.items():
        if k not in skip and k != "uses":
            step[k] = v


def _find_by_id(container: ET.Element, eid: str) -> ET.Element | None:
    for el in list(container):
        if el.get("id") == eid:
            return el
    return None
