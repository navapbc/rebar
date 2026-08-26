"""Parsing and statistical primitives for the rebar duration probe."""

from __future__ import annotations

import json
import math
import re
import shlex
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_LOG_ROOTS = (
    Path.home() / ".codex" / "sessions",
    Path.home() / ".codex" / "archived_sessions",
)


@dataclass(frozen=True)
class ProbeConfig:
    log_roots: tuple[Path, ...]
    tracker: Path
    since: float | None = None
    until: float | None = None
    provider_prefix: str | None = None
    current_since: float | None = None


def ts_seconds(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone offset")
    return parsed.timestamp()


def iso(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def flatten_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (flatten_text(item) for item in value)))
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        return "\n".join(filter(None, (flatten_text(item) for item in value.values())))
    return ""


def result_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict) and "wall_time_seconds" in value:
        return value
    text = flatten_text(value)
    decoder = json.JSONDecoder()
    matches: list[dict[str, Any]] = []
    for match in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text[match.start() :])
        except (ValueError, json.JSONDecodeError):
            continue
        if isinstance(obj, dict) and "wall_time_seconds" in obj:
            matches.append(obj)
    return matches[-1] if matches else None


def yielded_cell(value: Any) -> str | None:
    match = re.search(r"Script running with cell ID ([^\s]+)", flatten_text(value))
    return match.group(1) if match else None


def parse_js_literal(source: str, start: int) -> tuple[str, int] | None:
    if start >= len(source) or source[start] not in "\"'`":
        return None
    delimiter = source[start]
    out: list[str] = []
    i = start + 1
    while i < len(source):
        ch = source[i]
        if ch == delimiter:
            return "".join(out), i + 1
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        i += 1
        if i >= len(source):
            out.append("\\")
            break
        escaped = source[i]
        replacements = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}
        out.append(replacements.get(escaped, escaped))
        i += 1
    return None


def property_literals(source: str, prop: str) -> list[str]:
    found: list[str] = []
    pattern = re.compile(rf"\b{re.escape(prop)}\s*:\s*")
    for match in pattern.finditer(source):
        parsed = parse_js_literal(source, match.end())
        if parsed:
            found.append(parsed[0])
    return found


def json_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


CONTROL = {"&&", "||", ";", "|", "&"}
WRAPPERS = {
    "rtk",
    "proxy",
    "env",
    "uv",
    "run",
    "python",
    "python3",
    "time",
    "timeout",
    "command",
    "stdbuf",
    "-m",
}
SHELLS = {"sh", "bash", "zsh"}


def shell_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return []


def classify_command(command: str, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 2:
        return []
    tokens = shell_tokens(command)
    if not tokens:
        return []

    # Explicit shell -c/-lc wrappers hide the real command in one argument.
    for index, token in enumerate(tokens):
        if Path(token).name in SHELLS:
            for flag_index in range(index + 1, min(len(tokens), index + 4)):
                if tokens[flag_index] in {"-c", "-lc"} and flag_index + 1 < len(tokens):
                    return classify_command(tokens[flag_index + 1], depth + 1)

    classified: list[dict[str, Any]] = []
    for index, token in enumerate(tokens):
        if Path(token).name != "rebar" or index + 1 >= len(tokens):
            continue
        left = index
        while left > 0 and tokens[left - 1] not in CONTROL:
            left -= 1
        prefix = tokens[left:index]
        meaningful = [
            Path(item).name
            for item in prefix
            if not item.startswith("-")
            and "=" not in item
            and not item.isdigit()
            and item not in {"PATH"}
        ]
        if meaningful and any(item not in WRAPPERS for item in meaningful):
            continue
        right = index + 1
        while right < len(tokens) and tokens[right] not in CONTROL:
            right += 1
        args = tokens[index + 1 : right]
        op = args[0] if args else ""
        if op == "review-plan":
            target = next((arg for arg in args[1:] if not arg.startswith("-")), None)
            excluded = next(
                (
                    reason
                    for reason, present in (
                        ("status", "--status" in args),
                        ("check", "--check" in args),
                        ("help", "--help" in args or "-h" in args),
                        (
                            "force",
                            any(arg == "--force" or arg.startswith("--force=") for arg in args),
                        ),
                    )
                    if present
                ),
                None,
            )
            classified.append(
                {"operation": "plan", "target": target, "args": args, "excluded": excluded}
            )
        elif op == "transition":
            try:
                progress = args.index("in_progress", 1)
                args.index("closed", progress + 1)
            except ValueError:
                continue
            target = next((arg for arg in args[1:progress] if not arg.startswith("-")), None)
            excluded = next(
                (
                    reason
                    for reason, present in (
                        ("help", "--help" in args or "-h" in args),
                        (
                            "force",
                            any(arg == "--force" or arg.startswith("--force=") for arg in args),
                        ),
                    )
                    if present
                ),
                None,
            )
            classified.append(
                {"operation": "close", "target": target, "args": args, "excluded": excluded}
            )
    return classified


@dataclass
class Invocation:
    operation: str
    target: str | None
    command: str
    workdir: str | None
    log: str
    start: float
    args: list[str]
    excluded: str | None = None
    multi: bool = False
    compound: bool = False
    session_id: int | None = None
    last_seen: float | None = None
    end: float | None = None
    exit_code: int | None = None
    output: list[str] = field(default_factory=list)
    canonical_id: str | None = None
    event: dict[str, Any] | None = None
    event_matches: int = 0

    @property
    def duration(self) -> float | None:
        return None if self.end is None else self.end - self.start

    @property
    def observed_elapsed(self) -> float | None:
        terminal = self.end if self.end is not None else self.last_seen
        return None if terminal is None else terminal - self.start

    @property
    def all_output(self) -> str:
        return "\n".join(self.output)


@dataclass
class Wrapper:
    kind: str
    invocation: Invocation | None = None
    session_id: int | None = None
    source: str = ""


def discover_invocation(
    command: str,
    workdir: str | None,
    log: Path,
    start: float,
    multi: bool,
) -> list[Invocation]:
    classified = classify_command(command)
    compound = any(token in CONTROL for token in shell_tokens(command))
    return [
        Invocation(
            operation=item["operation"],
            target=item["target"],
            command=command,
            workdir=workdir,
            log=str(log),
            start=start,
            args=item["args"],
            excluded=item["excluded"],
            multi=multi,
            compound=compound,
        )
        for item in classified
    ]


def parse_logs(log_roots: Iterable[Path]) -> tuple[list[Invocation], Counter[str]]:
    invocations: list[Invocation] = []
    audit: Counter[str] = Counter()
    log_paths = sorted(path for root in log_roots for path in root.expanduser().rglob("*.jsonl"))
    audit["log_files"] = len(log_paths)
    for path in log_paths:
        calls: dict[str, Wrapper] = {}
        cells: dict[str, Wrapper] = {}
        sessions: dict[int, Invocation] = {}

        def finish(
            wrapper: Wrapper,
            value: Any,
            when: float,
            session_map: dict[int, Invocation],
        ) -> None:
            text = flatten_text(value)
            if wrapper.invocation is not None and text:
                wrapper.invocation.output.append(text[-30000:])
            obj = result_object(value)
            if obj is None:
                audit[f"no_result_{wrapper.kind}"] += 1
                if wrapper.kind == "initial" and wrapper.invocation is not None:
                    terminal = (
                        wrapper.invocation.operation == "plan"
                        and any(
                            marker in text
                            for marker in ('"verdict"', "INDETERMINATE", "BLOCK", "PASS")
                        )
                    ) or (
                        wrapper.invocation.operation == "close"
                        and any(
                            marker in text
                            for marker in ("transitioned ", "completion verification", '"verdict"')
                        )
                    )
                    if terminal:
                        wrapper.invocation.end = when
                        audit["terminal_text_fallback"] += 1
                return
            invocation: Invocation | None
            if wrapper.kind == "initial" and wrapper.invocation is not None:
                invocation = wrapper.invocation
            elif wrapper.kind == "poll" and wrapper.session_id is not None:
                invocation = session_map.get(wrapper.session_id)
            else:
                invocation = None
            if invocation is None:
                return
            invocation.last_seen = when
            if isinstance(obj.get("session_id"), int):
                sid = obj["session_id"]
                invocation.session_id = sid
                session_map[sid] = invocation
            if isinstance(obj.get("exit_code"), int):
                invocation.exit_code = obj["exit_code"]
                invocation.end = when

        try:
            lines = path.open()
        except OSError:
            audit["unreadable_log"] += 1
            continue
        with lines:
            for raw in lines:
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError:
                    audit["malformed_line"] += 1
                    continue
                stamp = record.get("timestamp")
                payload = record.get("payload")
                if not isinstance(stamp, str) or not isinstance(payload, dict):
                    continue
                when = ts_seconds(stamp)
                ptype = payload.get("type")
                call_id = payload.get("call_id")

                if ptype in {"custom_tool_call", "function_call"}:
                    name = payload.get("name")
                    source = (
                        payload.get("input")
                        if ptype == "custom_tool_call"
                        else payload.get("arguments")
                    )
                    if name == "exec" and isinstance(source, str):
                        poll = re.search(
                            r"tools\.write_stdin\s*\(\s*\{[^}]*session_id\s*:\s*(\d+)", source, re.S
                        )
                        if poll:
                            wrapper = Wrapper("poll", session_id=int(poll.group(1)), source=source)
                        else:
                            commands = property_literals(source, "cmd")
                            workdirs = property_literals(source, "workdir")
                            targets: list[Invocation] = []
                            for command in commands:
                                targets.extend(
                                    discover_invocation(
                                        command,
                                        workdirs[0] if workdirs else None,
                                        path,
                                        when,
                                        multi=len(commands) != 1,
                                    )
                                )
                            if targets:
                                invocations.extend(targets)
                                # A long-running target should be the wrapper's only exec.
                                wrapper = Wrapper("initial", invocation=targets[0], source=source)
                                if len(targets) != 1:
                                    audit["multi_target_wrapper"] += 1
                            else:
                                wrapper = Wrapper("other", source=source)
                                if "rebar review-plan" in source or "in_progress closed" in source:
                                    audit["target_text_unparsed"] += 1
                    elif name == "exec_command":
                        args = json_arguments(source)
                        command_value = args.get("cmd")
                        targets = (
                            discover_invocation(
                                command_value, args.get("workdir"), path, when, False
                            )
                            if isinstance(command_value, str)
                            else []
                        )
                        if targets:
                            invocations.extend(targets)
                            wrapper = Wrapper("initial", invocation=targets[0])
                        else:
                            wrapper = Wrapper("other")
                    elif name == "write_stdin":
                        args = json_arguments(source)
                        sid = args.get("session_id")
                        wrapper = Wrapper("poll", session_id=sid if isinstance(sid, int) else None)
                    elif name == "wait":
                        args = json_arguments(source)
                        cell = str(args.get("cell_id", ""))
                        matched_wrapper = cells.get(cell)
                        if matched_wrapper is None:
                            audit["wait_cell_miss"] += 1
                            matched_wrapper = Wrapper("other")
                        wrapper = matched_wrapper
                    else:
                        continue
                    if isinstance(call_id, str):
                        calls[call_id] = wrapper
                    continue

                if ptype not in {"custom_tool_call_output", "function_call_output"}:
                    continue
                if not isinstance(call_id, str) or call_id not in calls:
                    continue
                wrapper = calls.pop(call_id)
                value = payload.get("output")
                yielded = yielded_cell(value)
                if yielded:
                    cells[yielded] = wrapper
                else:
                    finish(wrapper, value, when, sessions)
    return invocations, audit


def _completion_phase_timestamps(
    ticket_dir: Path,
    verdict_at: float,
    upper: float,
    audit: Counter[str],
) -> dict[str, float | None]:
    status_at: float | None = None
    signature_at: float | None = None
    for path in ticket_dir.glob("*.json"):
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            audit["bad_phase_event"] += 1
            continue
        data = raw.get("data")
        if not isinstance(data, dict):
            continue
        event_type = raw.get("event_type")
        is_closed_status = event_type == "STATUS" and data.get("status") == "closed"
        is_completion_signature = (
            event_type == "SIGNATURE" and data.get("kind") == "completion-verifier"
        )
        if not (is_closed_status or is_completion_signature):
            continue
        timestamp_ns = raw.get("timestamp")
        if not isinstance(timestamp_ns, int):
            audit["bad_phase_timestamp"] += 1
            continue
        timestamp = timestamp_ns / 1_000_000_000
        if timestamp <= verdict_at or timestamp >= upper:
            continue
        if is_closed_status:
            status_at = timestamp if status_at is None else min(status_at, timestamp)
        elif is_completion_signature:
            signature_at = timestamp if signature_at is None else min(signature_at, timestamp)
    return {"status_at": status_at, "signature_at": signature_at}


def load_tracker(config: ProbeConfig) -> tuple[dict[str, str], list[dict[str, Any]], Counter[str]]:
    aliases: dict[str, str] = {}
    ids: set[str] = set()
    audit: Counter[str] = Counter()
    tracker = config.tracker.expanduser()
    for cache in tracker.glob("*/.cache.json"):
        try:
            state = json.loads(cache.read_text()).get("state", {})
        except (OSError, json.JSONDecodeError):
            continue
        ticket_id = state.get("ticket_id")
        alias = state.get("alias")
        if isinstance(ticket_id, str):
            ids.add(ticket_id)
            aliases[ticket_id] = ticket_id
            aliases[ticket_id[:9]] = ticket_id
            if isinstance(alias, str):
                aliases[alias] = ticket_id

    events: list[dict[str, Any]] = []
    for operation, suffix in (("plan", "REVIEW_RESULT"), ("close", "COMPLETION_VERDICT")):
        for path in tracker.glob(f"*/*-{suffix}.json"):
            try:
                raw = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                audit["bad_event"] += 1
                continue
            data = raw.get("data")
            if not isinstance(data, dict):
                continue
            metrics = data.get("metrics")
            if not isinstance(metrics, dict):
                coverage = data.get("coverage")
                metrics = coverage.get("metrics") if isinstance(coverage, dict) else None
            provenance = data.get("provider_provenance")
            ran_model = provenance.get("ran_model") if isinstance(provenance, dict) else None
            ticket_id = data.get("ticket_id")
            timestamp_ns = raw.get("timestamp")
            if not isinstance(ticket_id, str) or not isinstance(timestamp_ns, int):
                continue
            timestamp = timestamp_ns / 1_000_000_000
            if config.until is not None and timestamp > config.until:
                continue
            events.append(
                {
                    "operation": operation,
                    "ticket_id": aliases.get(ticket_id, ticket_id),
                    "timestamp": timestamp,
                    "llm_calls": metrics.get("llm_calls") if isinstance(metrics, dict) else None,
                    "total_ms": metrics.get("total_ms") if isinstance(metrics, dict) else None,
                    "det_ms": metrics.get("det_ms") if isinstance(metrics, dict) else None,
                    "llm_ms": metrics.get("llm_ms") if isinstance(metrics, dict) else None,
                    **{
                        name: metrics.get(name) if isinstance(metrics, dict) else None
                        for name in (
                            "pre_verifier_total_ms",
                            "structural_scan_ms",
                            "material_policy_ms",
                            "descendant_scope_ms",
                            "landing_check_ms",
                            "verifier_call_ms",
                            "git_history_read_ms",
                            "alias_index_build_ms",
                            "ticket_ref_resolution_ms",
                            "diff_validation_ms",
                            "verifier_wrapper_setup_ms",
                            "verifier_reusable_lookup_ms",
                            "verifier_resume_config_ms",
                            "verifier_attempts_ms",
                            "verifier_between_attempts_ms",
                            "verifier_wrapper_finalization_ms",
                            "verifier_wrapper_total_ms",
                            "verifier_attempt_setup_ms",
                            "verifier_handle_resolution_ms",
                            "verifier_snapshot_enter_ms",
                            "verifier_handle_apply_ms",
                            "verifier_inner_setup_ms",
                            "verifier_dispatch_ms",
                            "verifier_annotation_ms",
                            "verifier_snapshot_exit_ms",
                            "verifier_handle_defaults_ms",
                            "verifier_code_snapshot_ms",
                            "verifier_build_drift_ms",
                            "verifier_ticket_snapshot_ms",
                            "verifier_snapshot_gc_ms",
                            "verifier_dispatch_setup_ms",
                            "verifier_workflow_ms",
                            "verifier_precheck_context_ms",
                            "verifier_completion_agent_ms",
                            "verifier_verdict_reconcile_ms",
                            "verifier_no_llm_passthrough_ms",
                            "verifier_unclassified_workflow_steps_ms",
                            "verifier_workflow_residual_ms",
                            "verifier_dispatch_finalization_ms",
                            "commits_inspected",
                            "distinct_references",
                            "descendant_ids",
                            "referencing_commits_found",
                            "verifier_attempt_count",
                            "verifier_resume_count",
                            "verifier_workflow_step_count",
                        )
                    },
                    "ran_model": ran_model,
                    "verdict": data.get("verdict"),
                    "schema": data.get("schema"),
                    "path": str(path),
                }
            )
    events.sort(key=lambda item: item["timestamp"])
    close_by_ticket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        if event["operation"] == "close":
            close_by_ticket[event["ticket_id"]].append(event)
    for ticket_events in close_by_ticket.values():
        for index, event in enumerate(ticket_events):
            next_verdict = (
                ticket_events[index + 1]["timestamp"]
                if index + 1 < len(ticket_events)
                else math.inf
            )
            upper = next_verdict
            if config.until is not None:
                upper = min(upper, config.until + 1e-6)
            event.update(
                _completion_phase_timestamps(
                    Path(event["path"]).parent,
                    event["timestamp"],
                    upper,
                    audit,
                )
            )
    audit["aliases"] = len(aliases)
    audit["events"] = len(events)
    return aliases, events, audit


def resolve_target(invocation: Invocation, aliases: dict[str, str]) -> str | None:
    candidates = [invocation.target]
    output = invocation.all_output
    candidates.extend(re.findall(r'"ticket_id"\s*:\s*"([^"\\]+)"', output))
    candidates.extend(re.findall(r"transitioned\s+([A-Za-z0-9_-]+):", output))
    for candidate in candidates:
        if not candidate:
            continue
        if candidate in aliases:
            return aliases[candidate]
        if re.fullmatch(r"[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}", candidate):
            return candidate
    return None


def attach_events(
    invocations: list[Invocation],
    aliases: dict[str, str],
    events: list[dict[str, Any]],
    config: ProbeConfig,
) -> None:
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        by_key[(event["operation"], event["ticket_id"])].append(event)
    for invocation in invocations:
        invocation.canonical_id = resolve_target(invocation, aliases)
        if invocation.canonical_id is None:
            continue
        candidates = by_key.get((invocation.operation, invocation.canonical_id), [])
        upper = invocation.end if invocation.end is not None else invocation.start + 4 * 3600
        if config.until is not None:
            upper = min(upper, config.until)
        matches = [
            event for event in candidates if invocation.start - 3 <= event["timestamp"] <= upper + 3
        ]
        invocation.event_matches = len(matches)
        if matches:
            # Prefer the event nearest command completion; the operation emits one sidecar.
            anchor = invocation.end if invocation.end is not None else upper
            invocation.event = min(matches, key=lambda event: abs(anchor - event["timestamp"]))


def percentile(values: Iterable[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return math.nan
    h = (len(ordered) - 1) * quantile
    lower = math.floor(h)
    upper = math.ceil(h)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (h - lower) * (ordered[upper] - ordered[lower])


def serializable(invocation: Invocation) -> dict[str, Any]:
    return {
        "operation": invocation.operation,
        "target": invocation.target,
        "canonical_id": invocation.canonical_id,
        "start": iso(invocation.start),
        "end": iso(invocation.end),
        "duration_seconds": invocation.duration,
        "observed_elapsed_seconds": invocation.observed_elapsed,
        "exit_code": invocation.exit_code,
        "excluded": invocation.excluded,
        "multi": invocation.multi,
        "compound": invocation.compound,
        "event_matches": invocation.event_matches,
        "event": invocation.event,
        "workdir": invocation.workdir,
        "command": invocation.command,
        "log": invocation.log,
        "output_tail": invocation.all_output[-1500:],
    }


def _eligible_event(invocation: Invocation, config: ProbeConfig) -> bool:
    event = invocation.event
    if event is None:
        return False
    if config.since is not None and invocation.start < config.since:
        return False
    if config.until is not None and invocation.start > config.until:
        return False
    if not isinstance(event.get("llm_calls"), (int, float)) or event["llm_calls"] <= 0:
        return False
    ran_model = event.get("ran_model")
    if config.provider_prefix is not None and (
        not isinstance(ran_model, str) or not ran_model.startswith(config.provider_prefix)
    ):
        return False
    return True


def cohort(invocations: list[Invocation], operation: str, config: ProbeConfig) -> list[Invocation]:
    selected: list[Invocation] = []
    for invocation in invocations:
        event = invocation.event
        if invocation.operation != operation or invocation.excluded or invocation.duration is None:
            continue
        if invocation.exit_code in {130, 137, 143}:
            continue
        if event is None or not _eligible_event(invocation, config):
            continue
        selected.append(invocation)
    return selected


def eligible_with_censoring(
    invocations: list[Invocation], operation: str, config: ProbeConfig
) -> list[Invocation]:
    selected: list[Invocation] = []
    for invocation in invocations:
        event = invocation.event
        if (
            invocation.operation != operation
            or invocation.excluded
            or invocation.observed_elapsed is None
        ):
            continue
        if event is None or not _eligible_event(invocation, config):
            continue
        selected.append(invocation)
    return selected


def kaplan_meier_quantiles(items: list[Invocation]) -> dict[str, float | None]:
    observations = sorted(
        (
            item.observed_elapsed,
            item.duration is not None and item.exit_code not in {130, 137, 143},
        )
        for item in items
        if item.observed_elapsed is not None
    )
    at_risk = len(observations)
    survival = 1.0
    targets: dict[float, float | None] = {0.5: None, 0.9: None, 0.99: None}
    index = 0
    while index < len(observations):
        elapsed = observations[index][0]
        deaths = 0
        censored = 0
        while index < len(observations) and observations[index][0] == elapsed:
            if observations[index][1]:
                deaths += 1
            else:
                censored += 1
            index += 1
        if at_risk and deaths:
            survival *= 1 - deaths / at_risk
            for quantile in targets:
                if targets[quantile] is None and 1 - survival >= quantile:
                    targets[quantile] = elapsed
        at_risk -= deaths + censored
    return {f"p{int(quantile * 100)}": value for quantile, value in targets.items()}
