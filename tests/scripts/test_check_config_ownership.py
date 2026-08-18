"""Happy-path contract for the config-ownership gate (RP-04 S7.1, ticket 29a9).

The gate proves that ONLY approved composition roots / provider boundaries read ambient
configuration and credentials: every prohibited ambient access (env read, `load_config`
call, credential read, backend reload, configurable default) BELOW an approved seam is an
error unless it is a recorded legacy exception or carries a `# read-via:` marker.

Env-read detection REUSES `scripts/gen_env_registry.py` (`scan` / `KNOWN_ENV_HELPERS`);
the gate layers seam/ownership classification on top.

API contract (scripts/check_config_ownership.py):
  - check(root: Path) -> list[str]      # error strings, [] == clean
  - main(argv: list[str] | None) -> int  # 0 clean, 1 failures

Edge shapes (import/local-aliased callees, receiver-object aliasing, getattr-env,
helper-shim reads, unresolved fail-closed, credential reads, backend reload, structural
exception rejection, the drift/registry-completeness check, and real-tree cleanliness)
are held out in test_check_config_ownership_heldout.py.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.scripts

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_config_ownership.py"


@pytest.fixture(scope="module")
def gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_config_ownership", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _root(tmp_path: Path, module_body: str, relname: str = "belowseam.py") -> Path:
    """A one-module synthetic source root. `relname` is the module's path RELATIVE to the
    root — an arbitrary name is never an approved seam, so any prohibited access in it
    fires."""
    root = tmp_path / "srcroot"
    target = root / relname
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(module_body, encoding="utf-8")
    return root


def test_clean_module_passes(gate, tmp_path):
    root = _root(tmp_path, "def f():\n    return 1 + 1\n")
    assert gate.check(root) == []


def test_direct_env_read_below_seam_fails(gate, tmp_path):
    root = _root(tmp_path, "import os\n\n\ndef f():\n    return os.getenv('SOME_KNOB')\n")
    errors = gate.check(root)
    assert len(errors) == 1, errors
    msg = errors[0]
    assert "belowseam.py" in msg, "the reading module path must be named"
    assert "SOME_KNOB" in msg, "the env-name symbol must be named"
    assert "env-read" in msg, "the access kind must be reported"


def test_environ_subscript_below_seam_fails(gate, tmp_path):
    root = _root(tmp_path, "import os\n\n\ndef f():\n    return os.environ['SOME_KNOB']\n")
    errors = gate.check(root)
    assert len(errors) == 1, errors
    assert "SOME_KNOB" in errors[0]


def test_load_config_call_below_seam_fails(gate, tmp_path):
    body = "from rebar.config import load_config\n\n\ndef f():\n    return load_config()\n"
    root = _root(tmp_path, body)
    errors = gate.check(root)
    assert len(errors) == 1, errors
    assert "load_config" in errors[0]


def test_read_via_marker_suppresses_a_below_seam_read(gate, tmp_path):
    marker = "# read-via: test-only knob, ticket abcd-1234"
    body = f"import os\n\n\ndef f():\n    return os.getenv('SOME_KNOB')  {marker}\n"
    root = _root(tmp_path, body)
    assert gate.check(root) == []


def test_main_returns_zero_on_clean_root(gate, tmp_path, capsys):
    root = _root(tmp_path, "def f():\n    return None\n")
    assert gate.main(["--root", str(root)]) == 0


def test_main_returns_one_on_violation(gate, tmp_path, capsys):
    root = _root(tmp_path, "import os\n\n\ndef f():\n    return os.getenv('SOME_KNOB')\n")
    assert gate.main(["--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "SOME_KNOB" in out
