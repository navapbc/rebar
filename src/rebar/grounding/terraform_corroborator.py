"""Optional terraform-config-inspect corroboration for Terraform diagnostics.

This backend is positive-only: a schema-valid ``terraform-config-inspect --json``
entry can corroborate a structural diagnostic at T1, while every missing,
malformed, unequal, dynamic, or execution-fault case abstains with a closed
reason. Child streams, environment values, and Terraform literals are never
returned.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from rebar import schemas

from . import terraform_index as tfi

TOOL_NAME = "terraform-config-inspect"
DEADLINE_SECONDS = 60.0
MAX_STDOUT = 4 * 1024 * 1024
MAX_STDERR = 64 * 1024
_OPERATION = "corroborate_diagnostic"
_LANGUAGE = "terraform"
_SUPPORTED = frozenset({"declaration_present", "module_source_equals", "required_provider_present"})
_CLASS_TO_MAP = {
    "variable": "variables",
    "output": "outputs",
    "managed_resource": "managed_resources",
    "data_resource": "data_resources",
    "module_call": "module_calls",
    "required_provider": "required_providers",
}
_DROP_ENV_PREFIXES = (
    "TF_",
    "TOFU_",
    "AWS_",
    "AZURE_",
    "ARM_",
    "GOOGLE_",
    "GCP_",
    "CLOUDSDK_",
    "HCLOUD_",
    "DIGITALOCEAN_",
    "DO_",
    "KUBE",
    "HTTP_",
    "HTTPS_",
    "NO_PROXY",
    "ALL_PROXY",
    "TERRAFORM_",
    "CHECKPOINT_",
    "REBAR_",
    "GIT_",
)
_KEEP_ENV = frozenset({"LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"})


@dataclass(frozen=True)
class _Subject:
    diagnostic: str
    klass: str
    name: str
    expected_digest: str | None = None
    expected_value: str | None = None


@dataclass(frozen=True)
class _Execution:
    code: int
    stdout: bytes
    stderr: bytes


class _Abstain(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def corroborate(
    *,
    session: Any,
    module: str,
    diagnostic: str,
    subject: Any,
    expected: str | None = None,
) -> Any:
    """Corroborate one closed Terraform diagnostic through ``session`` helpers."""
    module_dir = _normalize_module(session, module)
    parsed = _parse_subject(diagnostic, subject, expected)
    query = _query(parsed, module_dir or ".")
    if session._pending is not None:
        return _abstain(session, query, session._pending, module_dir or ".")
    if module_dir is None:
        return _abstain(session, query, "path_outside_snapshot", ".")
    if parsed is None:
        return _abstain(session, query, "invalid_detector", module_dir or ".")
    if parsed.klass == "provider_attribute":
        return _abstain(session, query, "provider_attribute", module_dir or ".")
    if parsed.klass == "computed":
        return _abstain(session, query, "computed_value", module_dir or ".")
    if not session._has_terraform([module_dir]):
        return _abstain(session, query, "not_terraform", module_dir or ".")
    try:
        exe = _resolve_executable(session._root)
        before = _sha256_file(exe)
        with _snapshot_dir(session, module_dir) as snap:
            result = _run(exe, snap)
            after = _sha256_file(exe)
            if before != after:
                raise _Abstain("binary_replaced")
            data = _load_module_json(result)
            _validate_positions(data, snap, module_dir)
            location, detail = _match(data, parsed, module_dir)
            if detail is not None:
                raise _Abstain(detail)
            return _match_result(session, query, module_dir, parsed, location, exe, before, snap)
    except _Abstain as exc:
        return _abstain(session, query, exc.detail, module_dir or ".")


def _abstain(session: Any, query: dict[str, Any], reason_detail: str, module_dir: str) -> Any:
    return session._abstain_from_corroborator(
        query=query, reason_detail=reason_detail, module_dir=module_dir
    )


def _normalize_module(session: Any, module: str) -> str | None:
    try:
        rel = tfi._norm_rel(session._root, (module or ".").strip() or ".")
    except tfi.TerraformPathError:
        return None
    return "." if rel in ("", ".") else rel


def _parse_subject(diagnostic: str, subject: Any, expected: str | None) -> _Subject | None:
    diag = unicodedata.normalize("NFC", str(diagnostic).strip())
    if diag not in _SUPPORTED:
        return None
    klass, name = _subject_class_name(diag, subject)
    if not klass or not name:
        return None
    normalized_expected = unicodedata.normalize("NFC", (expected or "").strip())
    if diag == "module_source_equals" and not normalized_expected:
        klass = "computed"
    expected_digest = _safe_literal_digest(normalized_expected) if normalized_expected else None
    return _Subject(diag, klass, name, expected_digest, normalized_expected or None)


def _subject_class_name(diagnostic: str, subject: Any) -> tuple[str, str]:
    if isinstance(subject, dict):
        klass = str(subject.get("class") or subject.get("kind") or "").strip()
        name = str(subject.get("name") or "").strip()
        return _canonical_class(klass), unicodedata.normalize("NFC", name)
    text = unicodedata.normalize("NFC", str(subject).strip())
    if any(ch.isspace() for ch in text) or "*" in text or "[" in text or "]" in text:
        return "computed", text
    parts = text.split(".")
    if diagnostic == "required_provider_present":
        return "required_provider", parts[-1] if len(parts) in (1, 2) else ""
    if diagnostic == "module_source_equals":
        if parts[:1] == ["module"] and len(parts) == 2:
            return "module_call", parts[1]
        return "", ""
    if len(parts) == 2 and parts[0] in {"variable", "var"}:
        return "variable", parts[1]
    if len(parts) == 2 and parts[0] == "output":
        return "output", parts[1]
    if len(parts) == 2 and parts[0] == "module":
        return "module_call", parts[1]
    if len(parts) == 2 and parts[0] == "required_provider":
        return "required_provider", parts[1]
    if len(parts) == 2:
        return "managed_resource", text
    if len(parts) == 3 and parts[0] == "data":
        return "data_resource", ".".join(parts[1:])
    if len(parts) >= 3:
        return "provider_attribute", text
    return "", ""


def _canonical_class(klass: str) -> str:
    return {
        "module": "module_call",
        "module_call": "module_call",
        "managed": "managed_resource",
        "resource": "managed_resource",
        "data": "data_resource",
        "provider": "required_provider",
    }.get(klass, klass)


def _query(subject: _Subject | None, module_dir: str) -> dict[str, Any]:
    if subject is None:
        return {"diagnostic": "invalid", "module": module_dir}
    query: dict[str, Any] = {
        "diagnostic": subject.diagnostic,
        "module": module_dir,
        "subject": {"class": subject.klass, "name": subject.name},
    }
    if subject.expected_digest is not None:
        query["expected_digest"] = subject.expected_digest
    return query


def _resolve_executable(repo_root: Path) -> Path:
    found = shutil.which(TOOL_NAME)
    if not found:
        raise _Abstain("executable_not_resolvable")
    raw = Path(found)
    if raw.name != TOOL_NAME:
        raise _Abstain("rejected_executable")
    try:
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise _Abstain("rejected_executable") from exc
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise _Abstain("rejected_executable")
    try:
        resolved.relative_to(repo_root.resolve())
    except ValueError:
        return resolved
    raise _Abstain("rejected_executable")


class _snapshot_dir:
    def __init__(self, session: Any, module_dir: str) -> None:
        self._session = session
        self._module_dir = module_dir
        self.path: Path | None = None

    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix=".rebar-tfci-"))
        files = self._session._module_files(self._module_dir)
        for rel in files:
            source = self._session._root / rel
            relative = PurePosixPath(rel)
            if self._module_dir != ".":
                relative = PurePosixPath(rel).relative_to(PurePosixPath(self._module_dir))
            target = self.path / relative.as_posix()
            if source.is_symlink() or not source.is_file():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        _chmod_tree_readonly(self.path)
        return self.path

    def __exit__(self, *_exc: object) -> None:
        if self.path is not None:
            _chmod_tree_writable(self.path)
            shutil.rmtree(self.path, ignore_errors=True)


def _chmod_tree_readonly(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_symlink():
            continue
        path.chmod(stat.S_IRUSR | (stat.S_IXUSR if path.is_dir() else 0))
    root.chmod(stat.S_IRUSR | stat.S_IXUSR)


def _chmod_tree_writable(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        try:
            if not path.is_symlink():
                path.chmod(stat.S_IRUSR | stat.S_IWUSR | (stat.S_IXUSR if path.is_dir() else 0))
        except OSError:
            pass


def _run(exe: Path, snapshot: Path) -> _Execution:
    home = Path(tempfile.mkdtemp(prefix=".rebar-tfci-home-"))
    try:
        proc = subprocess.Popen(
            # "." not str(snapshot): upstream stamps pos.filename as
            # filepath.Join(dir, name) (tfconfig/load.go dirFiles), so an ABSOLUTE dir
            # argument comes back as an absolute filename that _validate_positions then
            # rejects as path_outside_snapshot. cwd is already the snapshot, so "." names
            # the same directory and keeps filenames relative — and keeps the snapshot
            # path out of argv entirely (bug f95d-19f6-7e58-4a8e).
            [str(exe), "--json", "."],
            cwd=str(snapshot),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_minimal_env(home),
            shell=False,
            start_new_session=(os.name != "nt"),
        )
        try:
            stdout, stderr = proc.communicate(timeout=DEADLINE_SECONDS)
        except subprocess.TimeoutExpired as exc:
            _terminate(proc)
            raise _Abstain("worker_timeout") from exc
        if len(stdout) > MAX_STDOUT or len(stderr) > MAX_STDERR:
            raise _Abstain("worker_failure")
        if proc.returncode != 0:
            raise _Abstain("nonzero_exit")
        return _Execution(proc.returncode, stdout, stderr)
    except OSError as exc:
        raise _Abstain("worker_failure") from exc
    finally:
        shutil.rmtree(home, ignore_errors=True)


def _minimal_env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in _KEEP_ENV and not _drop_env(k)}
    env["HOME"] = str(home)
    env["TMPDIR"] = str(home)
    return env


def _drop_env(name: str) -> bool:
    upper = name.upper()
    return any(upper.startswith(prefix) for prefix in _DROP_ENV_PREFIXES)


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        try:
            if os.name != "nt":
                os.killpg(proc.pid, signal.SIGTERM)
            else:
                proc.terminate()
        except OSError:
            pass
    try:
        proc.communicate(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            if os.name != "nt":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except OSError:
            pass
        proc.wait()


def _load_module_json(result: _Execution) -> dict[str, Any]:
    try:
        data = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _Abstain("config_inspect_schema_skew") from exc
    try:
        schemas.validator("terraform_config_inspect").validate(data)
    except Exception as exc:
        raise _Abstain("config_inspect_schema_skew") from exc
    diagnostics = data.get("diagnostics") if isinstance(data, dict) else None
    if diagnostics:
        raise _Abstain("upstream_diagnostics")
    return data


def _validate_positions(data: dict[str, Any], snapshot: Path, module_dir: str) -> None:
    for item in _iter_positioned(data):
        pos = item.get("pos")
        if not isinstance(pos, dict):
            continue
        filename = str(pos.get("filename") or "")
        rel = PurePosixPath(filename)
        if rel.is_absolute() or ".." in rel.parts or not filename:
            raise _Abstain("path_outside_snapshot")
        if not (snapshot / rel.as_posix()).is_file():
            raise _Abstain("path_outside_snapshot")


def _iter_positioned(data: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for key in ("variables", "outputs", "managed_resources", "data_resources", "module_calls"):
        value = data.get(key)
        if isinstance(value, dict):
            out.extend(item for item in value.values() if isinstance(item, dict))
    return out


def _match(
    data: dict[str, Any], subject: _Subject, module_dir: str
) -> tuple[dict[str, Any] | None, str | None]:
    if subject.klass not in _CLASS_TO_MAP:
        return None, "invalid_detector"
    entries = data.get(_CLASS_TO_MAP[subject.klass])
    if not isinstance(entries, dict):
        return None, "config_inspect_schema_skew"
    entry = entries.get(subject.name)
    if not isinstance(entry, dict):
        return None, "no_unique_address"
    if subject.diagnostic == "module_source_equals":
        source = entry.get("source")
        if not isinstance(source, str):
            return None, "computed_value"
        if unicodedata.normalize("NFC", source.strip()) != subject.expected_value:
            return None, "computed_value"
    return _location(entry.get("pos"), module_dir), None


def _location(pos: Any, module_dir: str) -> dict[str, Any] | None:
    if not isinstance(pos, dict):
        return None
    filename = str(pos.get("filename") or "")
    file = filename if module_dir == "." else f"{module_dir}/{filename}"
    line = int(pos.get("line") or 1)
    return {"file": file, "line_start": line, "line_end": line}


def _match_result(
    session: Any,
    query: dict[str, Any],
    module_dir: str,
    subject: _Subject,
    location: dict[str, Any] | None,
    exe: Path,
    exe_hash: str,
    snapshot: Path,
) -> Any:
    detail = f"class={subject.klass} name={subject.name}"
    executable = {
        "path": exe.name,
        "sha256": "sha256:" + exe_hash,
        "version": "unknown",
    }
    invocation = {
        "argv": [TOOL_NAME, "--json", "."],
        "shell": False,
        "stdin": "closed",
        "start_new_session": os.name != "nt",
        "deadline_seconds": int(DEADLINE_SECONDS),
        "stdout_limit": MAX_STDOUT,
        "stderr_limit": MAX_STDERR,
        "snapshot_path_digest": _safe_literal_digest(str(snapshot)),
    }
    return session._result_from_corroborator(
        query=query,
        module_dir=module_dir,
        subject={"class": subject.klass, "name": subject.name, "language": _LANGUAGE},
        location=location,
        detail=detail,
        executable=executable,
        invocation=invocation,
    )


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_literal_digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()
