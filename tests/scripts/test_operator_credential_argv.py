"""Operator Gerrit tooling keeps credentials out of argv (bug immoral-heated-doctorfish).

A process's argv is readable by every local user through ``ps`` and ``/proc/<pid>/cmdline``
while it runs. These tests run ``infra/gerrit/service-user.sh`` (the review-bot token
rotation tool) and the ``lib-credentials.sh`` helpers against PATH stubs that record every
argv they are started with, and assert the credential is in none of them while it still
reaches its consumer (curl's stdin config, aws's stdin, git's environment, the rendered
webhooks template).
"""

from __future__ import annotations

import base64
import http.server
import os
import shutil
import stat
import subprocess
import threading
from pathlib import Path

import pytest
from _sandbox_capabilities import requires_posix_shell
from _subprocess_env import SubprocessEnv, subprocess_env

pytestmark = requires_posix_shell

REPO_ROOT = Path(__file__).resolve().parents[2]
GERRIT_DIR = REPO_ROOT / "infra" / "gerrit"
GENERATED = "Gen3rated+Bot/Token0123456789abcdefABCDEF"

# Every stub appends "<name> <argv...>" to $REC/argv, so the log is exactly what ps shows.
_RECORD = 'printf "%s\\n" "$(basename "$0") $*" >> "$REC/argv"\n'


def _write_exec(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + _RECORD + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _without_git_config() -> SubprocessEnv:
    env = subprocess_env()
    for name in [k for k in env if k.startswith("GIT_CONFIG")]:
        del env[name]
    return env


def _clean_env(tmp_path: Path, fakebin: Path, rec: Path, **extra: str) -> SubprocessEnv:
    env = _without_git_config()
    for name in ("BASH_ENV", "ENV"):
        env.pop(name, None)
    env.update(PATH=f"{fakebin}:{os.environ['PATH']}", REC=str(rec), HOME=str(tmp_path))
    env.update(extra)
    return env


# ── service-user.sh ──────────────────────────────────────────────────────────────


def _service_user_stubs(fakebin: Path, *, account_exists: bool, generated: str) -> None:
    create = 'echo "fatal: username already exists" >&2; exit 1' if account_exists else "exit 0"
    _write_exec(
        fakebin / "ssh",
        f"""case "$*" in
  *create-account*) {create} ;;
  *--generate-http-password*) printf '%s\\n' {generated!r} ;;
  *) exit 0 ;;
esac
""",
    )
    _write_exec(fakebin / "aws", 'cat > "$REC/aws-stdin"\n')
    _write_exec(
        fakebin / "git",
        """case "$1" in
  clone) mkdir -p "${@: -1}" ;;
  add) cp webhooks.config "$REC/rendered" ;;
  diff) exit 1 ;;
esac
exit 0
""",
    )


def _run_service_user(
    tmp_path: Path, *, account_exists: bool = True, generated: str = f"New password: {GENERATED}"
) -> tuple[subprocess.CompletedProcess[str], Path]:
    fakebin, rec, gerrit = tmp_path / "bin", tmp_path / "rec", tmp_path / "gerrit"
    for d in (fakebin, rec, gerrit):
        d.mkdir()
    _service_user_stubs(fakebin, account_exists=account_exists, generated=generated)
    # A copy of the tool next to a template that still carries the legacy placeholder,
    # so the render path that substitutes the token is exercised.
    shutil.copy(GERRIT_DIR / "service-user.sh", gerrit / "service-user.sh")
    (gerrit / "webhooks.config").write_text(
        '[remote "review-bot"]\n\turl = http://review-bot:8000/webhook?token=__BOT_TOKEN__\n',
        encoding="utf-8",
    )
    key = tmp_path / "admin_key"
    key.write_text("not a key\n", encoding="utf-8")
    env = _clean_env(tmp_path, fakebin, rec, GERRIT_ADMIN_SSH_KEY=str(key))
    result = subprocess.run(
        ["bash", str(gerrit / "service-user.sh")],
        env=env,
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, rec


@pytest.mark.parametrize("account_exists", [True, False], ids=["rotate", "create"])
def test_service_user_keeps_the_token_out_of_every_argv(tmp_path, account_exists):
    result, rec = _run_service_user(tmp_path, account_exists=account_exists)
    assert result.returncode == 0, result.stderr
    argv = (rec / "argv").read_text(encoding="utf-8")
    assert GENERATED not in argv
    assert GENERATED not in result.stdout + result.stderr
    assert "--http-password" not in argv
    assert "--generate-http-password" in argv
    # SSM gets exactly the generated token, on stdin.
    assert "--value file:///dev/stdin" in argv
    assert (rec / "aws-stdin").read_text(encoding="utf-8") == GENERATED
    # The template render still substitutes the token, with no awk -v.
    assert f"?token={GENERATED}\n" in (rec / "rendered").read_text(encoding="utf-8")
    assert "awk" not in argv


@pytest.mark.parametrize(
    "generated",
    ["", "New password:", f"New password: {GENERATED} extra", "New password: a&b", "oops\\x"],
)
def test_service_user_fails_closed_on_unexpected_generator_output(tmp_path, generated):
    result, rec = _run_service_user(tmp_path, generated=generated)
    assert result.returncode != 0
    assert "unexpected --generate-http-password output" in result.stderr
    assert not (rec / "aws-stdin").exists(), "SSM must not be written"
    payload = generated.removeprefix("New password:").strip()
    if payload:
        assert payload not in result.stdout + result.stderr


# ── lib-credentials.sh ───────────────────────────────────────────────────────────


def _run_lib(
    tmp_path: Path, snippet: str, **extra: str
) -> tuple[subprocess.CompletedProcess[str], Path]:
    fakebin, rec = tmp_path / "bin", tmp_path / "rec"
    fakebin.mkdir()
    rec.mkdir()
    _write_exec(fakebin / "curl", 'cat > "$REC/curl-stdin"\n')
    _write_exec(fakebin / "git", 'env | grep "^GIT_" | sort > "$REC/git-env"\n')
    script = f'set -euo pipefail\n. "{GERRIT_DIR / "lib-credentials.sh"}"\n{snippet}\n'
    result = subprocess.run(
        ["bash", "-c", script],
        env=_clean_env(tmp_path, fakebin, rec, **extra),
        text=True,
        capture_output=True,
        check=False,
    )
    return result, rec


def test_curl_auth_sends_the_credential_on_stdin_only(tmp_path):
    pw = 'p"a\\ss+/w=rd'
    result, rec = _run_lib(
        tmp_path, 'curl_auth "$U" "$P" -sS https://gerrit.example.test/a/x', U="bot", P=pw
    )
    assert result.returncode == 0, result.stderr
    argv = (rec / "argv").read_text(encoding="utf-8")
    assert pw not in argv and "-u" not in argv.split()
    assert argv.split()[:3] == ["curl", "-K", "-"]
    # curl's config syntax escapes \ and " inside a quoted value.
    assert (rec / "curl-stdin").read_text(encoding="utf-8") == 'user = "bot:p\\"a\\\\ss+/w=rd"\n'


def test_curl_auth_config_round_trips_through_real_curl(tmp_path):
    """Real curl parses the escaped config and sends the original password."""
    if shutil.which("curl") is None:
        pytest.skip("curl not installed")
    seen: list[str | None] = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(self.headers.get("Authorization"))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    pw = 'p"a\\ss+/w=rd'
    script = (
        f'. "{GERRIT_DIR / "lib-credentials.sh"}"\ncurl_auth "$U" "$P" -sS -o /dev/null "$URL"\n'
    )
    env = subprocess_env(U="bot", P=pw, URL=f"http://127.0.0.1:{server.server_port}/a/x")
    for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        env.pop(name, None)
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1"
    try:
        result = subprocess.run(
            ["bash", "-c", script], env=env, text=True, capture_output=True, check=False
        )
        thread.join(10)
    finally:
        server.server_close()
    assert result.returncode == 0, result.stderr
    assert seen == ["Basic " + base64.b64encode(f"bot:{pw}".encode()).decode()]


def _config_entries(env_text: str) -> list[tuple[str, str]]:
    env = dict(line.split("=", 1) for line in env_text.splitlines() if "=" in line)
    count = int(env["GIT_CONFIG_COUNT"])
    return [
        (env[f"GIT_CONFIG_KEY_{i}"], env.get(f"GIT_CONFIG_VALUE_{i}", "")) for i in range(count)
    ]


def test_git_auth_carries_a_url_scoped_header_in_the_environment(tmp_path):
    pw = "s3cr3t/bot+token=="
    result, rec = _run_lib(
        tmp_path,
        'git_auth "https://gerrit.example.test/" "$U" "$P" clone -q "$URL" d',
        U="bot",
        P=pw,
        URL="https://gerrit.example.test/a/p",
        GIT_CONFIG_COUNT="1",
        GIT_CONFIG_KEY_0="safe.directory",
        GIT_CONFIG_VALUE_0="/srv/x",
    )
    assert result.returncode == 0, result.stderr
    argv = (rec / "argv").read_text(encoding="utf-8")
    basic = base64.b64encode(f"bot:{pw}".encode()).decode()
    assert pw not in argv and basic not in argv
    env_text = (rec / "git-env").read_text(encoding="utf-8")
    assert _config_entries(env_text) == [
        ("safe.directory", "/srv/x"),
        ("credential.helper", ""),
        ("http.https://gerrit.example.test/.extraHeader", f"Authorization: Basic {basic}"),
    ]
    assert "GIT_TERMINAL_PROMPT=0" in env_text.splitlines()


def test_git_auth_ignores_a_malformed_inherited_count(tmp_path):
    result, rec = _run_lib(
        tmp_path, 'git_auth https://g.test "$U" "$P" fetch', U="u", P="p", GIT_CONFIG_COUNT="x"
    )
    assert result.returncode == 0, result.stderr
    entries = _config_entries((rec / "git-env").read_text(encoding="utf-8"))
    assert [k for k, _ in entries] == ["credential.helper", "http.https://g.test/.extraHeader"]


def test_git_auth_header_is_scoped_to_gerrit_by_real_git(tmp_path):
    """Real git resolves the header for the Gerrit URL and for no other host, and sees
    an empty helper list. Global/system config are off so a CI checkout's own
    extraheader is neither read nor printed."""
    script = (
        f'. "{GERRIT_DIR / "lib-credentials.sh"}"\n'
        'git_auth https://gerrit.example.test u p config --get-urlmatch http.extraheader "$1"\n'
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    env = _without_git_config()
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_CEILING_DIRECTORIES=str(tmp_path)
    )

    def lookup(url: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-c", script, "lookup", url],
            cwd=outside,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    assert lookup("https://gerrit.example.test/a/rebar").stdout.startswith("Authorization: Basic ")
    assert lookup("https://github.com/navapbc/rebar").returncode == 1
