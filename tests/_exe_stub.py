# mechanism-ok: test_helper tests/_exe_stub.py — e062-1686-1a27-41a4 one portable tool stub
"""Install a stand-in executable that a bare-name spawn finds on POSIX and on Windows.

Tests stand in for ``git``, ``scc``, ``acli`` and similar tools by putting a stub first on
``PATH``. A ``#!/bin/sh`` stub only works on POSIX. On Windows, ``subprocess`` calls
``CreateProcess``, which resolves a bare ``git`` by appending ``.exe`` and ignores
``PATHEXT``, so neither a shebang file nor a ``git.cmd`` shim can shadow ``git.exe``
(bug transhuman-wounded-pangolin).

:func:`install_exe_stub` writes the stub body as Python and lets distlib's ``ScriptMaker``
package it, the same way pip installs console scripts. On POSIX the result is ``<name>``,
a script whose shebang names ``sys.executable``. On Windows it is ``<name>.exe``: distlib's
launcher with the script appended, which runs it under ``sys.executable``. The launcher
puts its child in a kill-on-close job, so a watchdog that kills the stub also kills the
Python process running the body.

Every body runs after a short prelude that defines ``exec_real(argv)``: on POSIX it
replaces the stub process with ``argv`` (so a watchdog kill lands on the real tool and
nothing is orphaned); on Windows, where ``os.execv`` returns to the caller before the new
program finishes, it runs ``argv`` as a child and exits with its status.
"""

from __future__ import annotations

import sys
import tempfile
import textwrap
from pathlib import Path

from distlib.scripts import ScriptMaker

_PRELUDE = """\
import os
import subprocess
import sys


def exec_real(argv):
    if os.name == "posix":
        os.execv(argv[0], argv)
    sys.stdout.flush()
    sys.exit(subprocess.call(argv))


"""


def install_exe_stub(bin_dir: Path, name: str, body: str) -> Path:
    """Write a stub named ``name`` into ``bin_dir`` whose behaviour is the Python ``body``.

    ``body`` is dedented and runs after the prelude, so it can call ``exec_real``.
    Returns the path a bare-name lookup of ``name`` resolves to on this platform.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="exe-stub-") as source_dir:
        source = Path(source_dir) / name
        source.write_text("#!python\n" + _PRELUDE + textwrap.dedent(body), encoding="utf-8")
        maker = ScriptMaker(source_dir, str(bin_dir), add_launchers=True)
        maker.executable = sys.executable
        maker.clobber = True
        maker.force = True
        written = maker.make(name)
    if len(written) != 1:
        raise RuntimeError(f"distlib wrote {written!r} for stub {name!r}; expected one file")
    return Path(written[0])
