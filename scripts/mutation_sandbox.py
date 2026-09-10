#!/usr/bin/env python3
"""Apply OS write isolation to mutation-test subprocesses.

Archiving the source tree does not contain effects from mutated code. This module keeps
the enforcement layer outside the artifact under mutation. On macOS it generates a
Seatbelt profile and proves that the child ran while an external write was denied. On
Linux it uses ``bwrap`` with a read-only root and explicitly writable paths. It does not
use ``unshare`` because a private mount namespace alone denies no writes and may be
unavailable on hardened hosts.

The caller aborts when neither mechanism enforces isolation. Only
:data:`ALLOW_UNSANDBOXED_ENV` permits an explicit, logged waiver. See
``rebar:e668-b496-e264-4283`` for the destructive-mutation incident.
"""

from __future__ import annotations

import functools
import logging
import os
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

logger = logging.getLogger(__name__)

#: Explicit operator waiver for running mutations without an OS sandbox.
ALLOW_UNSANDBOXED_ENV = "REBAR_MUTATION_ALLOW_UNSANDBOXED"

#: Missing HOME path that prevents writes to operator dotfiles.
HOMELESS = "/nonexistent-rebar-mutation-home"
# Prove that the Seatbelt child reached its denied write attempt.
_PROBE_MARKER = "seatbelt-probe-ran"

SEATBELT = "seatbelt"
BWRAP = "bwrap"


class SandboxUnavailable(RuntimeError):
    """No OS sandbox mechanism is available and the opt-out was not set."""


@functools.lru_cache(maxsize=1)
def probe() -> str | None:
    """Return the first mechanism that passes an enforcement probe.

    An installed ``bwrap`` may lack namespace permission, so path presence is
    insufficient. The cached result avoids repeating subprocess probes during one run.
    """
    if shutil.which("sandbox-exec") and _seatbelt_works():
        return SEATBELT
    if shutil.which("bwrap") and _bwrap_works():
        return BWRAP
    return None


def _seatbelt_works() -> bool:
    """Require both child execution and denial of its attempted external write.

    File absence alone cannot distinguish enforcement from a child that never started.
    This two-signal probe also rejects a non-enforcing ``sandbox-exec`` replacement.
    """
    exe = shutil.which("sandbox-exec")
    if exe is None:
        return False
    marker = _PROBE_MARKER
    try:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "probe.txt"
            profile = Path(tmp) / "probe.sb"
            # An empty allowlist denies file writes while stdout carries the marker.
            profile.write_text(build_seatbelt_profile(()), encoding="utf-8")
            proc = subprocess.run(
                # Quote the target so shell parsing cannot imitate a denied write.
                [
                    exe,
                    "-f",
                    str(profile),
                    "/bin/sh",
                    "-c",
                    f"echo {marker}; echo x > {shlex.quote(str(target))}",
                ],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            ran = marker in (proc.stdout or "")
            denied = not target.exists()
    except (OSError, subprocess.SubprocessError):
        return False
    if not ran:
        logger.warning(
            "sandbox-exec is installed but the probe child never ran (%s); treating "
            "the sandbox as unavailable rather than trusting an unverified mechanism.",
            (proc.stderr or "").strip() or f"exit {proc.returncode}",
        )
        return False
    if not denied:
        logger.warning(
            "sandbox-exec is installed but did NOT deny a write; treating the sandbox "
            "as unavailable rather than trusting a mechanism that does not enforce."
        )
        return False
    return True


def _bwrap_works() -> bool:
    """True when bwrap can actually create the namespace it needs."""
    try:
        proc = subprocess.run(
            ["bwrap", "--ro-bind", "/", "/", "--", "/bin/true"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        logger.warning(
            "bwrap is installed but cannot create a namespace (%s); treating the "
            "sandbox as unavailable rather than trusting a mechanism that does not "
            "enforce.",
            (proc.stderr or "").strip() or f"exit {proc.returncode}",
        )
        return False
    return True


def opt_out_enabled(env: Mapping[str, str] | None = None) -> bool:
    raw = (env if env is not None else os.environ).get(ALLOW_UNSANDBOXED_ENV, "")
    return raw.strip().lower() not in {"", "0", "false", "no"}


def _sb_quote(path: Path) -> str:
    """Escape quotes and backslashes in a Seatbelt string literal."""
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def build_seatbelt_profile(allow: Sequence[Path]) -> str:
    """Seatbelt profile: deny all writes, then re-permit the allow-list subpaths."""
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        "(allow file-write*",
    ]
    for path in allow:
        lines.append(f'  (subpath "{_sb_quote(Path(path).resolve())}")')
    lines.extend(
        [
            '  (literal "/dev/null")',
            '  (literal "/dev/stdout")',
            '  (literal "/dev/stderr")',
            '  (subpath "/dev/fd")',
            ")",
        ]
    )
    return "\n".join(lines) + "\n"


def _bwrap_argv(argv: Sequence[str], allow: Sequence[Path]) -> list[str]:
    """Read-only root, then re-bind each allow-list path writable."""
    # ``--dev`` avoids exposing the host ``/dev`` as a writable bind mount.
    out = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
    for path in allow:
        resolved = Path(path).resolve()
        # Missing bind sources stay read-only and produce an explicit warning.
        if not resolved.exists():
            logger.warning(
                "sandbox allow-list path %s does not exist; it will NOT be writable "
                "inside the sandbox. Create it before wrapping if the child needs it.",
                resolved,
            )
            continue
        out += ["--bind", str(resolved), str(resolved)]
    out.append("--")
    out.extend(argv)
    return out


def wrap(
    argv: Sequence[str],
    *,
    allow: Sequence[Path],
    profile_dir: Path,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Wrap ``argv`` or raise unless the explicit unsandboxed waiver is set."""
    mechanism = probe()
    if mechanism is None:
        # Ambient CI state never waives isolation. Only the named operator setting does.
        if opt_out_enabled(env):
            logger.warning(
                "%s is set: running mutation tests UNSANDBOXED. A mutation that reaches "
                "a destructive code path can delete files outside the scratch tree.",
                ALLOW_UNSANDBOXED_ENV,
            )
            return list(argv)
        raise SandboxUnavailable(
            "no OS sandbox available (need `sandbox-exec` on macOS or `bwrap` on Linux); "
            f"refusing to run mutation tests unsandboxed. Set {ALLOW_UNSANDBOXED_ENV}=1 "
            "to override, accepting that a destructive mutant can delete real files."
        )
    if mechanism == SEATBELT:
        profile_dir.mkdir(parents=True, exist_ok=True)
        profile = profile_dir / "mutation-sandbox.sb"
        profile.write_text(build_seatbelt_profile(allow), encoding="utf-8")
        return ["sandbox-exec", "-f", str(profile), *argv]
    return _bwrap_argv(argv, allow)


def sandbox_env(env: Mapping[str, str]) -> dict[str, str]:
    """Copy ``env`` with ``HOME`` pointed at a path that does not exist."""
    out = dict(env)
    out["HOME"] = HOMELESS
    # Keep the venv read-only and suppress bytecode writes into site-packages.
    out["PYTHONDONTWRITEBYTECODE"] = "1"
    return out
