"""Deterministically import the installed ``rebar`` package and top-level scripts.

The clean-wheel probe runs this command, which is equally usable without CI::

    python scripts/check_import_walk.py            # both legs
    make import-walk                               # the same, via the venv

Both legs collect every failure instead of failing fast:

* The installed-package leg imports the package root and every module found by
  ``pkgutil.walk_packages``. It catches broken subpackages and module-scope optional
  dependencies without a static module allowlist.
* **scripts/ leg** — imports each top-level ``scripts/*.py`` in an ISOLATED child process
  after stripping the scripts directory, CWD, and empty path from ``sys.path``. Each script
  must therefore resolve bare sibling imports through its own ``__file__``-derived insert,
  and one script cannot leak a path adjustment into the next.

Skip policy — every skip is EXPLICIT and carries a recorded reason:

* ``EXPECTED_OPTIONAL`` names the one dependency each sanctioned lazy-boundary module may
  lack. A skip requires that exact absent dependency and matching ``ModuleNotFoundError``;
  every other failure is real. Each entry requires a reason.
* ``SCRIPTS_SKIPS`` (scripts leg) — scripts whose import-time side effects make importing
  them unsafe. Additions require a reason.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import pkgutil
import subprocess
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent

_CHILD_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class ExpectedOptional:
    """One sanctioned lazy-boundary module: the dep it may lack, its extra, and why."""

    dep: str
    extra: str
    reason: str


@dataclass(frozen=True)
class Failure:
    name: str
    error: str


@dataclass(frozen=True)
class Skip:
    name: str
    reason: str


# Installed-leg exceptions: each module may lack only its named dependency on the base wheel.
EXPECTED_OPTIONAL: dict[str, ExpectedOptional] = {
    "rebar._mcp_auth": ExpectedOptional(
        dep="mcp",
        extra="mcp",
        reason="the MCP auth seam imports the mcp SDK at module scope; only building the "
        "MCP-over-HTTP server needs it (rebar-mcp --help degrades without it)",
    ),
    "rebar.opcert_service.app": ExpectedOptional(
        dep="fastapi",
        extra="reviewbot",
        reason="FastAPI ASGI shell over the FastAPI-free job core (rebar.opcert_service.jobs); "
        "nothing in core imports it — only running the op-cert gate service needs the extra",
    ),
    "rebar.review_bot.app": ExpectedOptional(
        dep="fastapi",
        extra="reviewbot",
        reason="FastAPI ASGI webhook receiver (ADR-0007); nothing in core imports it — only "
        "running the review-bot receiver needs the extra",
    ),
}

# Scripts-leg skip table: script filename -> why importing it is unsafe.
SCRIPTS_SKIPS: dict[str, str] = {
    "t5c_calibrate.py": "one-off calibration script with UNGUARDED top-level execution "
    "(module-scope for/with loops running real LLM criterion-preview calls); importing it "
    "would execute the calibration",
}


def _format_error(exc: BaseException) -> str:
    """One compact line per failure: the exception, plus the deepest originating frame."""
    summary = "".join(traceback.format_exception_only(exc)).strip()
    tb = traceback.extract_tb(exc.__traceback__)
    if tb:
        last = tb[-1]
        summary += f" (at {last.filename}:{last.lineno})"
    return summary


def _classify_package_failure(
    name: str, exc: BaseException, expected_optional: dict[str, ExpectedOptional]
) -> Failure | Skip:
    """An expected-optional module missing exactly its declared dep is a skip, else failure."""
    entry = expected_optional.get(name)
    if (
        entry is not None
        and isinstance(exc, ModuleNotFoundError)
        and exc.name == entry.dep
        and importlib.util.find_spec(entry.dep) is None
    ):
        return Skip(
            name, f"expected-optional: needs {entry.dep} ([{entry.extra}] extra) — {entry.reason}"
        )
    return Failure(name, _format_error(exc))


def walk_package(
    package: str, expected_optional: dict[str, ExpectedOptional]
) -> tuple[list[Failure], list[Skip], int]:
    """Import every module under *package*, collecting ALL failures (never fail-fast).

    Returns ``(failures, skips, attempted)`` where *attempted* counts every import tried
    (the package root included).
    """
    failures: list[Failure] = []
    skips: list[Skip] = []
    attempted = 1
    try:
        root = importlib.import_module(package)
    except BaseException as exc:  # noqa: BLE001 — a report, not control flow
        return [Failure(package, _format_error(exc))], [], attempted

    def _on_walk_error(name: str) -> None:
        # walk_packages imports packages itself to descend; record what it could not.
        exc = sys.exc_info()[1]
        if exc is not None:
            result = _classify_package_failure(name, exc, expected_optional)
            (skips if isinstance(result, Skip) else failures).append(result)  # type: ignore[arg-type]

    prefix = package + "."
    for info in pkgutil.walk_packages(root.__path__, prefix=prefix, onerror=_on_walk_error):
        name = info.name
        if name.rsplit(".", 1)[-1] == "__main__":
            continue  # runpy entry shims; executing them is `python -m`'s job, not the walk's
        attempted += 1
        try:
            importlib.import_module(name)
        except BaseException as exc:  # noqa: BLE001 — a report, not control flow
            result = _classify_package_failure(name, exc, expected_optional)
            (skips if isinstance(result, Skip) else failures).append(result)  # type: ignore[arg-type]
    return failures, skips, attempted


def _import_one_script(path: Path) -> int:
    """``--child`` mode: import *path* standalone, with no ambient path entries.

    Strips this file's own directory (``python scripts/check_import_walk.py`` puts
    ``scripts/`` at ``sys.path[0]``), the CWD, and ``""`` so the target resolves bare
    sibling imports ONLY through its own documented ``__file__``-derived insert.
    """
    ambient = {"", str(Path.cwd()), str(SCRIPTS_DIR)}
    sys.path[:] = [entry for entry in sys.path if entry not in ambient]
    module_name = f"_import_walk_target_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        print(f"cannot build an import spec for {path}", file=sys.stderr)
        return 1
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:  # noqa: BLE001 — a report, not control flow
        traceback.print_exc()
        return 1
    return 0


def walk_scripts(scripts_dir: Path, skips: dict[str, str]) -> tuple[list[Failure], list[Skip], int]:
    """Import each top-level ``scripts_dir/*.py`` in an isolated child process."""
    failures: list[Failure] = []
    skipped: list[Skip] = []
    attempted = 0
    for path in sorted(scripts_dir.glob("*.py")):
        attempted += 1
        if path.name in skips:
            skipped.append(Skip(path.name, skips[path.name]))
            continue
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--child", str(path)],
            capture_output=True,
            text=True,
            timeout=_CHILD_TIMEOUT_SECONDS,
            check=False,
        )
        if proc.returncode != 0:
            detail = (proc.stderr.strip() or proc.stdout.strip()).splitlines()
            failures.append(Failure(path.name, detail[-1] if detail else "child import failed"))
    return failures, skipped, attempted


def _report(leg: str, total: int, failures: list[Failure], skips: list[Skip]) -> None:
    passed = total - len(failures) - len(skips)
    print(f"{leg}: {len(failures)} failed, {len(skips)} skipped (expected), {passed} passed")
    for skip in skips:
        print(f"  SKIP {skip.name}: {skip.reason}")
    for failure in failures:
        print(f"  FAIL {failure.name}: {failure.error}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--leg", choices=("installed", "scripts", "all"), default="all")
    parser.add_argument("--package", default="rebar", help="package for the installed leg")
    parser.add_argument("--scripts-dir", type=Path, default=SCRIPTS_DIR)
    parser.add_argument("--child", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child is not None:
        return _import_one_script(args.child)

    failed = False
    if args.leg in ("installed", "all"):
        failures, skips, attempted = walk_package(args.package, EXPECTED_OPTIONAL)
        spec = importlib.util.find_spec(args.package)
        origin = spec.origin if spec is not None else "?"
        print(f"installed leg: walked {args.package} (root: {origin})")
        _report("installed leg", attempted, failures, skips)
        failed |= bool(failures)
    if args.leg in ("scripts", "all"):
        failures, skips, attempted = walk_scripts(args.scripts_dir, SCRIPTS_SKIPS)
        _report("scripts leg", attempted, failures, skips)
        failed |= bool(failures)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
