"""E2E tier: drive the REAL bpmn-io libraries the browser editor uses.

These tests round-trip BPMN through `bpmn-moddle` (the editor's read/write layer) and
`bpmn-auto-layout` (its layout) via a small Node harness (``js/roundtrip.mjs``), instead
of the permissive ``xml.etree`` the unit tests use. That is the only way to catch
*faithfulness* bugs — e.g. an id that is a legal XML attribute but an illegal BPMN id,
which ``xml.etree`` keeps and ``bpmn-moddle`` drops.

The tier is **opt-in and self-skipping**: it needs Node + a one-time ``npm install`` +
esbuild bundle. When Node is absent or the install/build fails (offline CI, etc.) the
whole tier skips with a clear reason rather than failing — the Python unit tests remain
the always-on floor.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_JS_DIR = Path(__file__).parent / "js"
_BUNDLE = _JS_DIR / "dist" / "roundtrip.mjs"


def _have_node() -> str | None:
    return shutil.which("node")


@pytest.fixture(scope="session")
def bpmn_harness():
    """A callable ``run(bpmn_xml, *, mode="serialize", moddle=None) -> dict`` that drives
    the real bpmn-io libraries through the Node harness. Skips the test if Node or the
    JS toolchain is unavailable. The bundle is built once per session."""
    node = _have_node()
    if not node:
        pytest.skip("e2e: `node` not on PATH (install Node to run the bpmn-io round-trip tier)")
    if not (_JS_DIR / "node_modules").is_dir():
        npm = shutil.which("npm")
        if not npm:
            pytest.skip("e2e: `npm` not on PATH")
        r = subprocess.run([npm, "install"], cwd=_JS_DIR, capture_output=True, text=True)
        if r.returncode != 0:
            pytest.skip(f"e2e: `npm install` failed (offline?):\n{r.stderr[-500:]}")
    if not _BUNDLE.is_file():
        r = subprocess.run(
            [shutil.which("npm"), "run", "build"], cwd=_JS_DIR, capture_output=True, text=True
        )
        if r.returncode != 0:
            pytest.skip(f"e2e: harness build failed:\n{r.stderr[-500:]}")

    def run(bpmn_xml: str, *, mode: str = "serialize", moddle: dict | None = None) -> dict:
        req = {"mode": mode, "bpmn": bpmn_xml, "moddle": moddle}
        proc = subprocess.run(
            [node, str(_BUNDLE)],
            input=json.dumps(req),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if not proc.stdout.strip():
            raise AssertionError(f"harness produced no output; stderr:\n{proc.stderr}")
        resp = json.loads(proc.stdout)
        if not resp.get("ok"):
            raise AssertionError(f"harness error: {resp.get('error')}")
        return resp

    return run


@pytest.fixture(scope="session")
def browser_runner():
    """A callable ``run(script_name, url) -> dict`` that runs a Playwright browser probe
    (``js/browser_*.mjs``) against a running editor URL in real headless Chromium. Skips if
    Node, Playwright, or the Chromium download is unavailable — the real browser is the
    only place the bundle's runtime behavior (rendering, panel, selection) can be checked."""
    node = _have_node()
    if not node:
        pytest.skip("e2e(browser): `node` not on PATH")
    if not (_JS_DIR / "node_modules" / "playwright").is_dir():
        pytest.skip("e2e(browser): playwright not installed (npm install in tests/e2e/js)")
    # Confirm a browser actually launches (the download may be absent in CI).
    check = subprocess.run(
        [
            node,
            "-e",
            "require('playwright').chromium.launch().then(b=>b.close()).then(()=>process.exit(0)).catch(()=>process.exit(3))",
        ],
        cwd=_JS_DIR,
        capture_output=True,
        text=True,
    )
    if check.returncode != 0:
        pytest.skip("e2e(browser): Chromium unavailable (run `npx playwright install chromium`)")

    def run(script_name: str, url: str) -> dict:
        proc = subprocess.run(
            [node, str(_JS_DIR / script_name), url],
            capture_output=True,
            text=True,
            timeout=150,
        )
        if not proc.stdout.strip():
            raise AssertionError(
                f"{script_name} produced no output; stderr:\n{proc.stderr[-1500:]}"
            )
        return json.loads(proc.stdout)

    return run


@pytest.fixture
def editor_server(tmp_path):
    """Start the real editor HTTP server on the round-trip demo (loopback, background
    thread) and yield ``(url, ir_path)``; tear it down after the test."""
    import shutil

    from rebar.llm.workflow import editor as _editor

    # A TRACKED fixture (not the gitignored .rebar/workflows copy) so the browser tier
    # runs in CI; only skip when the built editor bundle is genuinely absent.
    sample = Path(__file__).parent / "fixtures" / "roundtrip-demo.yaml"
    if not sample.is_file() or not _editor.assets_available():
        pytest.skip("e2e(browser): fixture workflow or built editor bundle missing")
    ir = tmp_path / "roundtrip-demo.yaml"
    shutil.copy(sample, ir)
    server, host, port, _token = _editor.edit_workflow(
        ir, open_browser=False, serve_forever=False, host="127.0.0.1"
    )
    try:
        yield f"http://{host}:{port}/", ir
    finally:
        server.shutdown()
        server.server_close()
