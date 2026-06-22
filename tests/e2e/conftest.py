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
