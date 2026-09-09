"""Provision the E2E Node harness before pytest starts.

The harness requires installed packages and an esbuild bundle. ``make e2e-deps`` performs
normal provisioning before test execution. The importable fallback reports each install or
build failure by name and bounds each subprocess independently.

Provisioning holds one advisory lock while checking readiness, installing packages, and
building the bundle. This prevents a caller from accepting a bundle that another process is
still writing. Platforms without ``fcntl`` use the same flow without interprocess locking.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

try:  # POSIX only; the e2e tier's supported platforms all have it.
    import fcntl
except ImportError:  # pragma: no cover - exercised only on platforms without fcntl
    fcntl = None  # type: ignore[assignment]

JS_DIR = Path(__file__).parent / "js"
BUNDLE_RELPATH = Path("dist") / "roundtrip.mjs"
LOCK_NAME = ".provision.lock"

#: Playwright is optional because only ``browser_runner`` requires it.
#: Harness-only selections omit the package.
BROWSER_PACKAGE = "playwright"
OMIT_BROWSER_FLAG = "--omit=optional"

# Ceilings, not budgets: large enough that a slow-but-working registry never trips them, so
# firing means genuinely stuck rather than merely unlucky.
INSTALL_TIMEOUT_S = 900.0
BUILD_TIMEOUT_S = 300.0

_STDERR_TAIL = 500


class ToolchainProvisioningError(RuntimeError):
    """Provisioning failed. The message always NAMES the step that failed."""


@contextlib.contextmanager
def _install_lock(js_dir: Path) -> Iterator[None]:
    """Serialize provisioning across processes sharing ``js_dir``."""
    if fcntl is None:  # pragma: no cover - platforms without fcntl
        yield
        return
    # Report lock setup errors as provisioning failures so collection can name the cause.
    try:
        js_dir.mkdir(parents=True, exist_ok=True)
        handle = (js_dir / LOCK_NAME).open("a+")
    except OSError as exc:
        raise ToolchainProvisioningError(
            f"e2e toolchain: cannot open the provisioning lock in {js_dir}: {exc}"
        ) from exc
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            raise ToolchainProvisioningError(
                f"e2e toolchain: cannot acquire the provisioning lock in {js_dir}: {exc}"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _run_step(label: str, argv: list[str], cwd: Path, timeout: float) -> None:
    """Run one provisioning command, converting every failure into a NAMED error."""
    try:
        completed = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except OSError as exc:
        raise ToolchainProvisioningError(
            f"e2e toolchain: could not run `{label}` in {cwd}: {exc}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolchainProvisioningError(
            f"e2e toolchain: `{label}` timed out after {timeout:g}s in {cwd}. "
            "Provision it ahead of pytest with `make e2e-deps` (the toolchain install is not "
            "a test's work), or re-run with a warm npm cache."
        ) from exc
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "").strip()[-_STDERR_TAIL:]
        raise ToolchainProvisioningError(
            f"e2e toolchain: `{label}` failed (exit {completed.returncode}) in {cwd}:\n{tail}"
        )


def _satisfied(js_dir: Path, *, with_browser: bool) -> bool:
    """True when ``js_dir`` already has everything this caller asked for."""
    if not ((js_dir / "node_modules").is_dir() and (js_dir / BUNDLE_RELPATH).is_file()):
        return False
    return not with_browser or (js_dir / "node_modules" / BROWSER_PACKAGE).is_dir()


def provision_toolchain(
    js_dir: Path | str = JS_DIR,
    *,
    with_browser: bool = True,
    install_timeout: float = INSTALL_TIMEOUT_S,
    build_timeout: float = BUILD_TIMEOUT_S,
) -> None:
    """Ensure ``js_dir`` contains the requested packages and harness bundle.

    ``with_browser=False`` omits Playwright. A satisfied tree returns before the npm lookup.
    Each bounded failure raises :class:`ToolchainProvisioningError` with the failing step.
    """
    js_dir = Path(js_dir)
    bundle = js_dir / BUNDLE_RELPATH
    # Check readiness under the lock because esbuild writes the bundle in place.
    # An unlocked existence check could accept another process's incomplete bundle.
    with _install_lock(js_dir):
        # The browser stack is checked separately, so a tree provisioned earlier WITHOUT it
        # is completed rather than mistaken for a finished install.
        if _satisfied(js_dir, with_browser=with_browser):
            return

        npm = shutil.which("npm")
        if npm is None:
            raise ToolchainProvisioningError(
                "e2e toolchain: `npm` not on PATH (install Node to run the bpmn-io round-trip tier)"
            )

        # `npm ci` is lockfile-exact and reproducible; `npm install` is the fallback for a
        # tree that has no lockfile to be exact about.
        verb = "ci" if (js_dir / "package-lock.json").is_file() else "install"
        install = [verb] if with_browser else [verb, OMIT_BROWSER_FLAG]
        if not (js_dir / "node_modules").is_dir() or (
            with_browser and not (js_dir / "node_modules" / BROWSER_PACKAGE).is_dir()
        ):
            _run_step(" ".join(["npm", *install]), [npm, *install], js_dir, install_timeout)
        if not bundle.is_file():
            _run_step("npm run build", [npm, "run", "build"], js_dir, build_timeout)
