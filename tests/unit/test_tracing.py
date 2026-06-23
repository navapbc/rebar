"""Optional OTel tracing (story d6d1): no-op without keys, import-light, never raises."""

from __future__ import annotations

import subprocess
import sys


def test_setup_tracing_is_noop_without_keys() -> None:
    # No Langfuse keys configured → tracing must be a clean no-op (False), never raising,
    # and must not require the [tracing] extra to be reached.
    from rebar.llm.config import LangfuseConfig
    from rebar.llm.tracing import setup_tracing

    cfg = LangfuseConfig(public_key=None, secret_key=None, host=None)
    assert cfg.enabled is False
    assert setup_tracing(cfg) is False


def test_importing_tracing_pulls_no_opentelemetry() -> None:
    # `import rebar.llm.tracing` must stay dependency-free (opentelemetry/pydantic_ai are
    # imported INSIDE setup_tracing) — checked in a clean subprocess.
    code = (
        "import sys, rebar.llm.tracing;"
        "heavy=[m for m in ('opentelemetry','pydantic_ai') if m in sys.modules];"
        "print('CLEAN' if not heavy else 'HEAVY', heavy)"
    )
    cp = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": "src", "PATH": __import__("os").environ.get("PATH", "")},
    )
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.startswith("CLEAN"), cp.stdout
