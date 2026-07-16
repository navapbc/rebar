#!/usr/bin/env python3
"""Ambient-package guard for the hash-locked `--no-isolation` release build (story 08a8).

`python -m build --no-isolation` reuses the *current* interpreter's environment instead of
building in a fresh, isolated one. That is deliberate here — the build env is pinned by
`.github/release-requirements.txt` (installed `--require-hashes --no-deps`) so the toolchain
is reproducible. But `--no-isolation` means any package that leaked into the interpreter
(a pre-baked runner image dep, a stray `pip install`) is *also* visible to the build backend
and could influence the produced artifact. This guard closes that hole: it asserts the
installed set (`pip freeze`) is a subset of the lock (modulo a documented base allowlist),
and FAILs the release otherwise.

Usage:
    python scripts/check_build_env_locked.py --lock <lock> --freeze <freeze>

`--lock`   path to the hash-locked requirements file (pip-compile --generate-hashes output).
`--freeze` path to a `pip freeze` capture (the workflow passes `<(pip freeze)`).

Exit 0 when every installed package is present in the lock at the same version (allowlist
aside); non-zero (with the offending packages on stderr) otherwise.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Base packages that are part of every interpreter/venv bootstrap and are NOT pinned in the
# release lock. They are not a supply-chain risk for the build backend and pip refuses to
# operate without them, so they are exempt from the subset check.
BASE_ALLOWLIST = {"pip", "setuptools", "wheel"}


def _canonical(name: str) -> str:
    """PEP 503-canonicalize a distribution name: collapse runs of `-_.` to a single `-`.

    `pip freeze` reports a distribution by its metadata name (e.g. `jaraco.classes`,
    `readme_renderer`), while a pip-compile lock pins the normalized form
    (`jaraco-classes`, `readme-renderer`). Per PEP 503 these are the SAME package, so both
    sides must be canonicalized before comparison — otherwise a lock-pinned dependency is
    falsely reported "absent" purely because of `.`/`_`-vs-`-` spelling.
    """
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _parse_pins(text: str) -> dict[str, str]:
    """Parse `name==version` lines into a {lowercased-name: version} map.

    Skips blank lines, comments (`#…`), hash-continuation lines (`--hash=…` and the trailing
    `\\` continuation they hang off), and options. Tolerates inline `# comment` and the
    trailing ` \\` line-continuation that pip-compile emits.
    """
    pins: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        # Drop an inline comment and a trailing line-continuation backslash.
        line = line.split("#", 1)[0].strip().rstrip("\\").strip()
        if "==" not in line:
            continue
        name, _, version = line.partition("==")
        # A version may carry an environment marker (`; python_version …`) or an extras
        # suffix on the name (`pkg[extra]`) — normalise both away, then PEP 503-canonicalize
        # the name so `.`/`_`/`-` spellings from `pip freeze` vs the lock compare equal.
        version = version.split(";", 1)[0].strip()
        name = _canonical(name.split("[", 1)[0])
        if name and version:
            pins[name] = version
    return pins


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", required=True, type=Path, help="hash-locked requirements file")
    parser.add_argument("--freeze", required=True, type=Path, help="pip freeze capture")
    args = parser.parse_args(argv)

    lock = _parse_pins(args.lock.read_text(encoding="utf-8"))
    freeze = _parse_pins(args.freeze.read_text(encoding="utf-8"))

    offenders: list[str] = []
    for name, version in sorted(freeze.items()):
        if name in BASE_ALLOWLIST:
            continue
        if name not in lock:
            offenders.append(f"{name}=={version}: installed but ABSENT from the lock")
        elif lock[name] != version:
            offenders.append(
                f"{name}: installed {version} but lock pins {lock[name]} (version mismatch)"
            )

    if offenders:
        print(
            "check_build_env_locked: the --no-isolation build environment is NOT lock-consistent.\n"
            "The following installed package(s) are not pinned by the release lock "
            f"({args.lock}):",
            file=sys.stderr,
        )
        for line in offenders:
            print(f"  - {line}", file=sys.stderr)
        print(
            "Rebuild the release env from the lock "
            "(pip install --require-hashes --no-deps -r .github/release-requirements.txt) "
            "in a fresh venv before building.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
