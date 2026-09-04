"""Provisioning for the e2e Node toolchain: bounded, diagnosable and race-safe.

The e2e tier drives the real bpmn-io libraries through a small Node harness, which needs a
one-time ``npm`` install plus an esbuild bundle. That work used to happen inside the first
e2e test's *fixture setup*, with no ``timeout=`` on either subprocess — so its only bound was
pytest's global ``timeout = 300`` / ``timeout_method = "thread"``. The thread method calls
``os._exit(1)``, which kills the whole xdist worker (``node down: Not properly terminated``)
instead of reporting a failure anyone can read. A cold install measured 90-120s of that 300s
budget in *passing* CI runs, so ordinary npm-registry variance was enough to cross it
(bug 9a17-e0b3-7aa6-4091).

Three things follow, and this module is where they live:

**It is a module, not a fixture.** Provisioning is the build's job, not a test's. ``make
e2e-deps`` calls it (well, calls the same two commands) before pytest starts, so in the
normal case the fixture finds the toolchain already there and pays nothing. Being importable
outside pytest is also what lets the failure paths be tested in a second with a stub ``npm``
rather than by waiting for a real cold install to go slow.

**Each step carries its own bound.** ``install_timeout``/``build_timeout`` are generous —
they exist to convert an unbounded hang into a *named* failure, not to police a slow
registry. The default install bound is deliberately larger than the pytest budget it used to
sit inside: the point is never to be the thing that fires first on a merely slow day.

**The install is locked.** Under ``-n 4 --dist worksteal`` each xdist worker runs the
session-scoped fixture independently, so several can race the same ``node_modules``/``dist``.
An advisory ``fcntl.flock`` serializes them; where ``fcntl`` is absent the lock degrades to a
no-op, which changes nothing for the single-process case that platform is in.
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

#: The browser stack, declared in package.json as an OPTIONAL dependency so `--omit=optional`
#: can leave it out. Only ``browser_runner`` needs it; the bpmn-moddle round-trip harness does
#: not, and it is 4 of the 15 installed packages and 16.8M of the 30M `node_modules` tree. A
#: run that selects no browser test should not pay for it.
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
    # An unwritable directory, a lock file owned by another user, a filesystem without
    # advisory locking: every one of those is a PROVISIONING failure and must be reported as
    # one. Letting an OSError escape would abort collection itself — the caller records a
    # named skip, it does not expect to have to survive an arbitrary exception.
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
    """Ensure ``js_dir`` has the node modules and harness bundle this caller needs.

    ``with_browser=False`` omits the optional browser stack, which only ``browser_runner``
    uses. That is the cheap half of the fix: a selection with no browser test installs 11
    packages instead of 15 and 12M instead of 30M. The structural half — provisioning at
    collection time — is what makes the budget question moot; this makes the bill smaller
    as well, which matters while the failure rate is what it is.

    A no-op when the tree already satisfies the request, so the normal case (``make
    e2e-deps`` ran first) costs nothing. Raises :class:`ToolchainProvisioningError` naming the
    step that failed; it never blocks indefinitely, and so never leaves the caller to be
    killed by an outer watchdog.
    """
    js_dir = Path(js_dir)
    bundle = js_dir / BUNDLE_RELPATH
    if _satisfied(js_dir, with_browser=with_browser):
        return

    npm = shutil.which("npm")
    if npm is None:
        raise ToolchainProvisioningError(
            "e2e toolchain: `npm` not on PATH (install Node to run the bpmn-io round-trip tier)"
        )

    # `npm ci` is lockfile-exact and reproducible; `npm install` is the fallback for a tree
    # that has no lockfile to be exact about.
    verb = "ci" if (js_dir / "package-lock.json").is_file() else "install"
    install = [verb] if with_browser else [verb, OMIT_BROWSER_FLAG]
    with _install_lock(js_dir):
        # Re-check under the lock: a peer may have finished while this caller waited. The
        # browser stack is re-checked separately, so a tree provisioned earlier WITHOUT it
        # is completed rather than mistaken for a finished install.
        if not _satisfied(js_dir, with_browser=with_browser):
            if not (js_dir / "node_modules").is_dir() or (
                with_browser and not (js_dir / "node_modules" / BROWSER_PACKAGE).is_dir()
            ):
                _run_step(" ".join(["npm", *install]), [npm, *install], js_dir, install_timeout)
            if not bundle.is_file():
                _run_step("npm run build", [npm, "run", "build"], js_dir, build_timeout)
