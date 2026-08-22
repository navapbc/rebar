from __future__ import annotations

import subprocess
import sys


def test_first_server_construction_completes_mcp_settings() -> None:
    script = (
        "import warnings\n"
        "from pydantic_settings.sources.utils import "
        "IncompleteFieldDefinitionWarning\n"
        "warnings.simplefilter('error', IncompleteFieldDefinitionWarning)\n"
        "from rebar.mcp_server import build_server\n"
        "print(build_server().name)\n"
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "rebar"
