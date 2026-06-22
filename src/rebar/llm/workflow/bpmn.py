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

    shapes: list[tuple[str, int, int]] = []  # (id, rank, lane) for DI
    edges: list[tuple[str, str, str]] = []  # (flow_id, source, target)
    _emit_frame(proc, doc.get("steps", []), shapes, edges, lane=0)
    _emit_di(defs, _proc_id(doc), shapes, edges)
    ET.indent(defs, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(defs, encoding="unicode")


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


def _emit_frame(container: ET.Element, steps, shapes, edges, lane: int) -> None:
    """Emit one frame's steps as flow elements under ``container``; wire ``needs`` as
    sequence flows; recurse into control bodies. ``lane`` offsets the DI rows so nested
    frames don't overlap their parent visually."""
    ids = [s["id"] for s in steps if isinstance(s, dict) and "id" in s]
    rank = _ranks(steps)
    for s in steps:
        if not isinstance(s, dict) or "id" not in s:
            continue
        sid = s["id"]
        kind = step_kind(s)
        _emit_step(container, s, kind, shapes, edges, lane)
        shapes.append((sid, rank.get(sid, 0), lane))
    # sequence flows from needs (same frame only)
    for s in steps:
        if not isinstance(s, dict) or "id" not in s:
            continue
        for n in s.get("needs") or []:
            if n in ids:
                fid = f"flow_{n}__{s['id']}"
                ET.SubElement(
                    container,
                    _q("bpmn", "sequenceFlow"),
                    {"id": fid, "sourceRef": n, "targetRef": s["id"]},
                )
                edges.append((fid, n, s["id"]))


def _emit_step(container, s, kind, shapes, edges, lane) -> ET.Element:
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
        _emit_frame(sp, s["loop"].get("body", []), shapes, edges, lane + 1)
        return sp
    if kind == "map":
        sp = ET.SubElement(container, _q("bpmn", "subProcess"), {"id": sid, "name": "map"})
        _ext(sp, "Config", value=_config_json(s))
        seq = "false" if (s["map"].get("max_concurrency") or 1) > 1 else "true"
        ET.SubElement(sp, _q("bpmn", "multiInstanceLoopCharacteristics"), {"isSequential": seq})
        _emit_frame(sp, s["map"].get("body", []), shapes, edges, lane + 1)
        return sp
    # branch -> exclusiveGateway + a then/else sub-process arm each. The arm's ROLE
    # (then/else) is carried two ways that both survive a real bpmn-js edit: a
    # ``<rebar:Config _role=…>`` marker AND the gateway->arm sequence flow. Reconstruction
    # reads the role from those, NOT from the arm's id — so it is robust to bpmn-js
    # regenerating ids on Save. The id itself uses ``.`` as a readable separator (a legal
    # BPMN NCName char that the schema's step-id pattern forbids, so it can't collide with
    # a real step id). The old ``@`` separator was an ILLEGAL BPMN id: bpmn-moddle dropped
    # the whole arm on Save, silently deleting the branch (see tests/e2e).
    gw = ET.SubElement(container, _q("bpmn", "exclusiveGateway"), {"id": sid, "name": "branch"})
    _ext(gw, "Config", value=_config_json(s))
    for arm in ("then", "else"):
        body = s["branch"].get(arm)
        if not isinstance(body, list):
            continue
        arm_id = f"{sid}.{arm}"
        sp = ET.SubElement(container, _q("bpmn", "subProcess"), {"id": arm_id, "name": arm})
        _ext(sp, "Config", value=json.dumps({_ROLE_KEY: arm}))  # role survives id rewrites
        _emit_frame(sp, body, shapes, edges, lane + 1)
        shapes.append((arm_id, _arm_rank(sid, shapes), lane + (0 if arm == "then" else 1)))
        fid = f"flow_{sid}.{arm}"
        attrs = {"id": fid, "sourceRef": sid, "targetRef": arm_id}
        ET.SubElement(container, _q("bpmn", "sequenceFlow"), attrs)
        edges.append((fid, sid, arm_id))
    return gw


def _arm_rank(sid: str, shapes) -> int:
    for s_id, rank, _lane in shapes:
        if s_id == sid:
            return rank + 1
    return 1


def _ranks(steps) -> dict[str, int]:
    """Longest-path rank per step over the frame's ``needs`` DAG (deterministic
    left-to-right layout)."""
    ids = {s["id"] for s in steps if isinstance(s, dict) and "id" in s}
    deps = {
        s["id"]: [n for n in (s.get("needs") or []) if n in ids]
        for s in steps
        if isinstance(s, dict) and "id" in s
    }
    rank: dict[str, int] = {}

    def depth(sid: str, seen: frozenset[str]) -> int:
        if sid in rank:
            return rank[sid]
        if sid in seen or not deps.get(sid):
            r = 0
        else:
            r = 1 + max((depth(d, seen | {sid}) for d in deps[sid]), default=-1)
        rank[sid] = r
        return r

    for sid in ids:
        depth(sid, frozenset())
    return rank


def _emit_di(defs, proc_id, shapes, edges) -> None:
    """A deterministic BPMN DI: x by rank, y by lane. Reproducible so the diagram
    opens the same way every time (the layout is generated, never committed)."""
    plane_owner = ET.SubElement(defs, _q("bpmndi", "BPMNDiagram"), {"id": "di"})
    plane = ET.SubElement(
        plane_owner, _q("bpmndi", "BPMNPlane"), {"id": "plane", "bpmnElement": proc_id}
    )
    pos: dict[str, tuple[int, int]] = {}
    for sid, rank, lane in shapes:
        x, y = 160 + rank * 180, 80 + lane * 120
        pos[sid] = (x + 50, y + 40)
        shp = ET.SubElement(
            plane, _q("bpmndi", "BPMNShape"), {"id": f"di_{sid}", "bpmnElement": sid}
        )
        ET.SubElement(
            shp, _q("dc", "Bounds"), {"x": str(x), "y": str(y), "width": "100", "height": "80"}
        )
    for fid, src, tgt in edges:
        ed = ET.SubElement(plane, _q("bpmndi", "BPMNEdge"), {"id": f"di_{fid}", "bpmnElement": fid})
        for end in (src, tgt):
            x, y = pos.get(end, (0, 0))
            ET.SubElement(ed, _q("di", "waypoint"), {"x": str(x), "y": str(y)})


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
        if eid is None or tag in ("sequenceFlow", "extensionElements", "startEvent", "endEvent"):
            continue
        if eid in non_step_ids:  # a branch arm or an event — not a top step of this frame
            continue
        step = _read_step(container, el, tag)
        if step is None:
            continue
        if eid in needs:
            step["needs"] = needs[eid]
        steps.append(step)
    return steps


def _read_step(container: ET.Element, el: ET.Element, tag: str) -> dict[str, Any] | None:
    eid = el.get("id")
    cfg = _read_ext_json(el, "Config") or {}
    step: dict[str, Any] = {"id": eid}
    if tag == "scriptTask":
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
