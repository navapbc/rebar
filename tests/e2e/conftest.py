"""E2E tier: drive the REAL bpmn-io libraries the browser editor uses.

These tests round-trip BPMN through `bpmn-moddle` (the editor's read/write layer) and
`bpmn-auto-layout` (its layout) via a small Node harness (``js/roundtrip.mjs``), instead
of the permissive ``xml.etree`` the unit tests use. That is the only way to catch
*faithfulness* bugs — e.g. an id that is a legal XML attribute but an illegal BPMN id,
which ``xml.etree`` keeps and ``bpmn-moddle`` drops.

The tier is **opt-in and self-skipping**: it needs Node + a one-time ``npm ci`` +
esbuild bundle. Provision that AHEAD of pytest with ``make e2e-deps`` — a toolchain install
is the build's work, not a test's, and charging it to the first e2e test's setup is what
bug 9a17-e0b3-7aa6-4091 was. ``tests/e2e/_toolchain.py`` remains the in-fixture fallback for
a checkout that never ran the target. When Node is absent or provisioning fails (offline CI,
etc.) the whole tier skips with a clear reason rather than failing — the Python unit tests
remain the always-on floor.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parents[1]
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from _child_diag import child_failure_detail  # noqa: E402
from _toolchain import JS_DIR as _JS_DIR  # noqa: E402
from _toolchain import ToolchainProvisioningError, provision_toolchain  # noqa: E402

_BUNDLE = _JS_DIR / "dist" / "roundtrip.mjs"

# Fixtures whose tests need the Node toolchain. Provisioning is triggered by their
# PRESENCE IN THE SELECTION, so a run that selects none of them (the toolchain's own
# tests, the macOS platform_compat subset) never pays for it.
_TOOLCHAIN_FIXTURES = frozenset({"bpmn_harness", "browser_runner"})

# Set once by the collection hook below: ``None`` once provisioning has been attempted and
# succeeded, a message when it failed. ``_PROVISION_ATTEMPTED`` keeps the fixture from
# re-running a provisioning step that already failed slowly once.
_PROVISION_ATTEMPTED = False
_PROVISION_ERROR: str | None = None


def _have_node() -> str | None:
    return shutil.which("node")


def pytest_collection_modifyitems(config, items) -> None:
    """Provision the Node toolchain at COLLECTION time, not inside a test.

    This is the load-bearing half of bug 9a17-e0b3-7aa6-4091. pytest-timeout bounds test
    ITEMS: with ``timeout = 300`` / ``timeout_method = "thread"`` (pyproject.toml), a
    session-scoped fixture that installs the toolchain spends that install inside the first
    e2e test's setup, and crossing the budget makes the thread method ``os._exit(1)`` — the
    whole xdist worker dies as ``node down: Not properly terminated`` with no traceback. A
    lock alone does not fix it: under ``-n 4 --dist worksteal`` every worker runs the
    fixture, so serializing them merely converts the race into a queue that is *still* being
    charged to a test. Collection is not an item, so nothing here is on any test's clock.
    """
    global _PROVISION_ATTEMPTED, _PROVISION_ERROR
    if _PROVISION_ATTEMPTED:
        return
    here = Path(__file__).parent
    wanted = any(
        _TOOLCHAIN_FIXTURES.intersection(getattr(item, "fixturenames", ()))
        and Path(str(item.fspath)).is_relative_to(here)
        for item in items
    )
    if not wanted or not _have_node():
        return  # the fixtures self-skip on a missing Node; nothing to provision for.
    _PROVISION_ATTEMPTED = True
    try:
        provision_toolchain(_JS_DIR)
    except ToolchainProvisioningError as exc:
        _PROVISION_ERROR = str(exc)


@pytest.fixture(scope="session")
def bpmn_harness():
    """A callable ``run(bpmn_xml, *, mode="serialize", moddle=None) -> dict`` that drives
    the real bpmn-io libraries through the Node harness. Skips the test if Node or the
    JS toolchain is unavailable. The bundle is built once per session."""
    node = _have_node()
    if not node:
        pytest.skip("e2e: `node` not on PATH (install Node to run the bpmn-io round-trip tier)")
    # By here the toolchain has normally been provisioned twice over: by `make e2e-deps`
    # before pytest started, and failing that by the collection hook above. This is the last
    # fallback, for a session that reached the fixture without either — and it re-reports
    # rather than re-runs a provisioning that already failed, so a slow failure is paid once.
    if _PROVISION_ERROR is not None:
        pytest.skip(f"e2e: {_PROVISION_ERROR}")
    if not _PROVISION_ATTEMPTED:
        try:
            provision_toolchain(_JS_DIR)
        except ToolchainProvisioningError as exc:
            pytest.skip(f"e2e: {exc}")

    def run(bpmn_xml: str, *, mode: str = "serialize", moddle: dict | None = None) -> dict:
        req = {"mode": mode, "bpmn": bpmn_xml, "moddle": moddle}
        proc = subprocess.run(
            [node, str(_BUNDLE)],
            input=json.dumps(req),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if not proc.stdout.strip():
            raise AssertionError(f"harness produced no output; {child_failure_detail(proc)}")
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
        check=False,
    )
    if check.returncode != 0:
        pytest.skip("e2e(browser): Chromium unavailable (run `npx playwright install chromium`)")

    def run(script_name: str, url: str) -> dict:
        proc = subprocess.run(
            [node, str(_JS_DIR / script_name), url],
            capture_output=True,
            text=True,
            timeout=150,
            check=False,
        )
        if not proc.stdout.strip():
            raise AssertionError(f"{script_name} produced no output; {child_failure_detail(proc)}")
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


@pytest.fixture
def editor_server_batch(tmp_path):
    """Like :func:`editor_server` but serves the v3 ``batch-demo`` fixture, so the browser
    tier can select a `batch` step and exercise the criteria-list add/remove/edit UI (A4)."""
    import shutil

    from rebar.llm.workflow import editor as _editor

    sample = Path(__file__).parent / "fixtures" / "batch-demo.yaml"
    if not sample.is_file() or not _editor.assets_available():
        pytest.skip("e2e(browser): batch fixture workflow or built editor bundle missing")
    ir = tmp_path / "batch-demo.yaml"
    shutil.copy(sample, ir)
    server, host, port, _token = _editor.edit_workflow(
        ir, open_browser=False, serve_forever=False, host="127.0.0.1"
    )
    try:
        yield f"http://{host}:{port}/", ir
    finally:
        server.shutdown()
        server.server_close()
