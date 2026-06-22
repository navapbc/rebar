/**
 * Faithful E2E harness for the rebar workflow visual editor.
 *
 * The Python unit tests round-trip BPMN through xml.etree, which is a PERMISSIVE
 * parser: it preserves anything, including ids that are illegal in real BPMN. The
 * browser editor does NOT use xml.etree — it uses bpmn-io's `bpmn-moddle` to read and
 * write the diagram, and `bpmn-auto-layout` to position it. This harness drives THOSE
 * libraries so the round-trip is tested against the same code the editor runs, which
 * is what surfaces faithfulness bugs (e.g. an `@` in an element id is a legal XML
 * attribute but an ILLEGAL BPMN id — bpmn-moddle drops the element, xml.etree keeps it).
 *
 * Protocol: read one JSON request from stdin, write one JSON response to stdout.
 *   request : { "mode": "serialize"|"layout", "bpmn": "<xml>", "moddle": {<descriptor>} }
 *   response: { "ok": bool, "xml": "<xml>", "warnings": [..], "error": "..." }
 *
 * mode "serialize" — fromXML -> toXML via bpmn-moddle (the editor's read+write layer);
 *                    `warnings` carries every parse complaint (e.g. "illegal ID").
 * mode "layout"    — bpmn-auto-layout.layoutProcess(xml): replaces our hand-rolled DI
 *                    with the bpmn-io auto-layout the editor uses, so geometry asserts
 *                    test the REAL layout, not our deprecated `_emit_di`.
 */
import BpmnModdle from "bpmn-moddle";
import { layoutProcess } from "bpmn-auto-layout";

function readStdin() {
  return new Promise((resolve, reject) => {
    let buf = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (c) => (buf += c));
    process.stdin.on("end", () => resolve(buf));
    process.stdin.on("error", reject);
  });
}

async function serialize(req) {
  const moddle = new BpmnModdle(req.moddle ? { rebar: req.moddle } : {});
  const { rootElement, warnings } = await moddle.fromXML(req.bpmn);
  const { xml } = await moddle.toXML(rootElement, { format: true });
  return { ok: true, xml, warnings: (warnings || []).map((w) => w.message) };
}

async function layout(req) {
  // bpmn-auto-layout takes BPMN (DI optional/ignored) and returns it with fresh DI.
  const xml = await layoutProcess(req.bpmn);
  return { ok: true, xml, warnings: [] };
}

async function main() {
  const req = JSON.parse(await readStdin());
  const handler = req.mode === "layout" ? layout : serialize;
  try {
    process.stdout.write(JSON.stringify(await handler(req)));
  } catch (e) {
    process.stdout.write(JSON.stringify({ ok: false, error: String(e && e.message || e) }));
  }
}

main();
