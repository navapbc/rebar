"""Task 932d — the inline-PEM op-cert key source is CONSUMED from ``os.environ``.

``OpcertServiceConfig.from_env`` is the only reader of ``REBAR_OPCERT_PRIVATE_KEY`` in the
tree, and it is invoked exactly once per process (module scope in ``opcert_service/app.py``).
It now TRANSFERS the inline PEM into the config object rather than copying it: the variable is
popped once its value is captured, so a later ``os.environ`` dump (a crash handler, an error
reporter, a traceback that renders the environment) and every child process spawned after
config load (``ssh-keygen`` in ``keyprov``, ``git`` in ``workspace``, neither of which passes
an explicit ``env=``) no longer see the raw key.

That is the whole of the guarantee. The key is STILL in process memory — ``cfg.private_key``
stays reachable through ``app.state.config``, and ``compose_signer`` writes a 0600 copy to a
process-owned dir — so a core dump or heap scrape is unaffected, as is anything that read the
variable earlier, and (on Linux) ``/proc/<pid>/environ``, which reflects the exec-time
environment block that ``unsetenv`` does not rewrite.

No fixture here contains a plausible real credential: the value is an obvious dummy string,
except where a signer must actually compose, which uses a throwaway ``ssh-keygen`` key
generated into ``tmp_path``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from rebar.opcert_service.config import OpcertServiceConfig
from rebar.opcert_service.keyprov import compose_signer

pytestmark = pytest.mark.unit

_ENV = "REBAR_OPCERT_PRIVATE_KEY"

#: Deliberately NOT PEM-shaped — nothing here should ever look like a real key.
_DUMMY = "DUMMY-NOT-A-REAL-KEY"


def _env_present() -> bool:
    """Is the inline variable set? Bound to a bool so a FAILING assertion never renders
    ``os.environ`` — whose repr would dump every variable, the secret under test included."""
    return _ENV in os.environ


def test_from_env_consumes_inline_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The value reaches the config AND leaves the environment (the task's whole point)."""
    monkeypatch.setenv(_ENV, _DUMMY)

    cfg = OpcertServiceConfig.from_env()

    assert cfg.private_key == _DUMMY, "the inline PEM must still reach the config object"
    assert not _env_present(), f"{_ENV} must be popped once from_env has captured it"


def test_second_from_env_call_has_no_inline_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Consume-once is the CONTRACT, not an accident.

    There is no config-reload path today (``from_env``'s only caller runs once at module
    scope, and the container runs uvicorn with neither ``--workers`` nor ``--reload``), so a
    second call is not a supported re-read. Pinning it here means a future reload path fails
    loudly in this test rather than silently composing with no key.
    """
    monkeypatch.setenv(_ENV, _DUMMY)

    first = OpcertServiceConfig.from_env()
    second = OpcertServiceConfig.from_env()

    assert first.private_key == _DUMMY
    assert second.private_key is None, "the inline source is consumed, so a re-read sees none"


def test_unset_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the variable absent the pop is a no-op — no KeyError, no key."""
    monkeypatch.delenv(_ENV, raising=False)

    cfg = OpcertServiceConfig.from_env()

    assert cfg.private_key is None
    assert not _env_present()


def test_blank_value_is_consumed_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """A whitespace-only value yields no key AND is still removed — the variable is consumed
    on read regardless of whether its content qualified."""
    monkeypatch.setenv(_ENV, "   ")

    cfg = OpcertServiceConfig.from_env()

    assert cfg.private_key is None
    assert not _env_present()


def test_key_path_source_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the inline SECRET is consumed; the preferred file-path source is left alone."""
    monkeypatch.delenv(_ENV, raising=False)
    key_file = tmp_path / "opcert-key"
    key_file.write_text("not-read-here\n", encoding="utf-8")
    monkeypatch.setenv("REBAR_OPCERT_KEY_PATH", str(key_file))

    cfg = OpcertServiceConfig.from_env()

    assert cfg.key_path == str(key_file)
    assert os.environ.get("REBAR_OPCERT_KEY_PATH") == str(key_file), (
        "the file-path source is not a secret and must remain readable in the environment"
    )


def test_compose_still_works_after_pop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pop happens AFTER the value is captured: compose_signer still gets a usable key.

    Uses a throwaway Ed25519 key generated into ``tmp_path`` (never a committed PEM), because
    ``compose_signer`` validates the material with ``ssh-keygen -y``.
    """
    monkeypatch.delenv("REBAR_OPCERT_KEY_PATH", raising=False)
    source = tmp_path / "throwaway-ed25519"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(source), "-N", "", "-q", "-C", "932d-test"],
        check=True,
        capture_output=True,
    )
    monkeypatch.setenv(_ENV, source.read_text(encoding="utf-8"))
    monkeypatch.setenv("REBAR_OPCERT_ENV_ID", "nava-opcert-test-932d")

    cfg = OpcertServiceConfig.from_env()
    assert not _env_present()

    signer = compose_signer(cfg)
    try:
        copy = Path(signer.key_path)
        assert copy.is_file(), "compose_signer must have written its process-owned copy"
        assert copy.stat().st_mode & 0o777 == 0o600
        assert signer.principal == "nava-opcert-test-932d"
    finally:
        signer.cleanup()
