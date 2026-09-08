"""Provide E2E fixtures for bpmn-moddle serialization and browser editor tests.

The Node harness parses and serializes BPMN with bpmn-moddle. Toolchain provisioning follows
selected fixtures and normally runs through ``make e2e-deps`` before pytest. The fixture
fallback stores one named provisioning failure for both toolchain fixtures.

Browser fixture failures pass through ``tier_unavailable``. The recorded browser opt-out
permits a reported skip. Without that record, unavailable browser dependencies fail the tier.
Python unit tests remain the always-on floor.
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

from _browser_tier import tier_unavailable  # noqa: E402
from _child_diag import child_failure_detail  # noqa: E402
from _toolchain import JS_DIR as _JS_DIR  # noqa: E402
from _toolchain import ToolchainProvisioningError, provision_toolchain  # noqa: E402

_BUNDLE = _JS_DIR / "dist" / "roundtrip.mjs"

# Fixtures whose tests need the Node toolchain. Provisioning is triggered by their
# PRESENCE IN THE SELECTION, so a run that selects none of them (the toolchain's own
# tests, the macOS platform_compat subset) never pays for it.
_BROWSER_FIXTURE = "browser_runner"
_TOOLCHAIN_FIXTURES = frozenset({"bpmn_harness", _BROWSER_FIXTURE})

# Store one collection-time provisioning error for both fixtures to report without retrying.
_PROVISION_ERROR: str | None = None


def _have_node() -> str | None:
    return shutil.which("node")


def pytest_collection_modifyitems(config, items) -> None:
    """Provision selected Node dependencies during collection.

    Collection runs outside each test item's timeout. Provisioning before selected fixtures
    prevents serialized session workers from charging installation time to a test.
    """
    global _PROVISION_ERROR
    here = Path(__file__).parent
    selected = {
        name
        for item in items
        if Path(str(item.fspath)).is_relative_to(here)
        for name in _TOOLCHAIN_FIXTURES.intersection(getattr(item, "fixturenames", ()))
    }
    if not selected or not _have_node():
        return  # the fixtures self-skip on a missing Node; nothing to provision for.
    # Install the browser stack only when a browser test is actually in the selection: it is
    # 4 of the 15 packages and over half the tree, and the round-trip harness never uses it.
    try:
        provision_toolchain(_JS_DIR, with_browser=_BROWSER_FIXTURE in selected)
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
    # A failure at collection time is re-REPORTED, never re-run: retrying a slow failure
    # once per test is how the original defect burned the budget in the first place.
    if _PROVISION_ERROR is not None:
        pytest.skip(f"e2e: {_PROVISION_ERROR}")
    # Otherwise the last fallback, for a session that reached the fixture without either
    # `make e2e-deps` or the hook having run. A no-op once the toolchain is on disk.
    try:
        provision_toolchain(_JS_DIR, with_browser=False)
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
    (``js/browser_*.mjs``) against a running editor URL in real headless Chromium — the real
    browser is the only place the bundle's runtime behavior (rendering, panel, selection) can
    be checked.

    When Node, Playwright, the Chromium download or the built bundle is unavailable the tier
    cannot run, and that is routed through ``tier_unavailable`` rather than a bare
    ``pytest.skip``: with the recorded opt-out present it is a LOUD skip that names itself as a
    deliberate non-execution, and without it a hard failure. A bare skip here is what made a
    green build indistinguishable from one that actually exercised the browser
    (bug 337e-b558-17a2-49bd)."""
    node = _have_node()
    if not node:
        tier_unavailable("`node` is not on PATH")
    # Report the stored collection-time error through the browser-tier guard.
    if _PROVISION_ERROR is not None:
        tier_unavailable(f"the Node toolchain failed to provision — {_PROVISION_ERROR}")
    try:
        provision_toolchain(_JS_DIR, with_browser=True)
    except ToolchainProvisioningError as exc:
        tier_unavailable(f"the Node toolchain failed to provision — {exc}")
    if not (_JS_DIR / "node_modules" / "playwright").is_dir():
        tier_unavailable("playwright is not installed (npm install in tests/e2e/js)")
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
        tier_unavailable("Chromium will not launch (run `npx playwright install chromium`)")

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

    # A TRACKED fixture (not the gitignored .rebar/workflows copy) so the tier depends on
    # nothing a checkout might lack; the only non-execution here is a genuinely absent
    # editor bundle, and that is routed through the guard like every other one.
    sample = Path(__file__).parent / "fixtures" / "roundtrip-demo.yaml"
    if not sample.is_file() or not _editor.assets_available():
        tier_unavailable("the fixture workflow or the built editor bundle is missing")
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
        tier_unavailable("the batch fixture workflow or the built editor bundle is missing")
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
