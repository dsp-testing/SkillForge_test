#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. All rights reserved.

"""Normalize remote Forge evidence and derive current Forge-style candidates."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

DEFAULT_USAGE_THRESHOLD = 3
DEFAULT_SUCCESS_THRESHOLD = 0.7
SCRIPT_EXTENSIONS = (".py", ".js", ".sh", ".ts")
SHELL_TOOLS = {"bash", "shell", "powershell"}
FILE_TOOLS = {"edit", "create"}
EXIT_CODE_RE = re.compile(r"(?:exited|completed) with exit code (\d+)")
SCRIPT_COMMAND_PATTERNS = (
    re.compile(r"(?:python3?\s+)((?:[a-zA-Z0-9_./-]*/)?[a-zA-Z0-9_.-]+\.py)(?:\s+.*)?"),
    re.compile(r"\./((?:[a-zA-Z0-9_./-]*/)?[a-zA-Z0-9_.-]+\.py)(?:\s+.*)?"),
    re.compile(r"node\s+((?:[a-zA-Z0-9_./-]*/)?[a-zA-Z0-9_.-]+\.js)(?:\s+.*)?"),
    re.compile(r"(?:bash|sh)\s+((?:[a-zA-Z0-9_./-]*/)?[a-zA-Z0-9_.-]+\.sh)(?:\s+.*)?"),
    re.compile(r"\./((?:[a-zA-Z0-9_./-]*/)?[a-zA-Z0-9_.-]+\.sh)(?:\s+.*)?"),
    re.compile(r"(?:tsx?|ts-node|npx\s+tsx?)\s+((?:[a-zA-Z0-9_./-]*/)?[a-zA-Z0-9_.-]+\.ts)(?:\s+.*)?"),
    re.compile(r"(?:python3?|node|bash|sh|tsx?)\s+(inline_\w+\.(?:py|js|sh|ts))(?:\s+.*)?"),
)
IMPORT_RE = re.compile(r"^(?:import |from |require\(|const .* = require)")
EXT_RE = re.compile(r"""['"]\S+\.(jsonl?|csv|txt|log|ya?ml|xml|html|py|js|sh|ts)['"]""", re.IGNORECASE)
CALL_PATTERNS = (
    re.compile(r"\bopen\s*\("),
    re.compile(r"\bread\s*\("),
    re.compile(r"\bwrite\s*\("),
    re.compile(r"\bjson\.(load|dump|parse|stringify)", re.IGNORECASE),
    re.compile(r"\bcsv\.(reader|writer|DictReader|DictWriter)", re.IGNORECASE),
    re.compile(r"\bprint\s*\("),
    re.compile(r"\bconsole\.(log|error)"),
    re.compile(r"\bargparse\b"),
    re.compile(r"\bsys\.argv"),
    re.compile(r"\bCounter\b"),
    re.compile(r"\bdefaultdict\b"),
    re.compile(r"\b(get|post|put|delete|fetch|request)\s*\(", re.IGNORECASE),
    re.compile(r"\bglob\b"),
    re.compile(r"\bos\.(walk|listdir|path)"),
    re.compile(r"\bre\.(match|search|findall|sub)"),
)


def read_json(path: str) -> Any:
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: str, value: Any) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=False)
        handle.write("\n")


def as_string(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value).lower() if isinstance(value, bool) else str(value)
    return None


def parse_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("tool arguments must be a JSON object")
    return value


def parse_apply_patch_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if isinstance(value, str):
        return {"patch": value}
    if not isinstance(value, dict):
        raise ValueError("apply_patch arguments must be a JSON string or object")
    patch = value.get("input")
    if not isinstance(patch, str):
        patch = value.get("patch")
    if not isinstance(patch, str):
        raise ValueError("apply_patch arguments must contain an input or patch string")
    return {"patch": patch}


def parse_exit_code(result: str | None) -> int | None:
    if not result:
        return None
    match = EXIT_CODE_RE.search(result)
    return int(match.group(1)) if match else None


def base_event(session_id: str, event_type: str, row: dict[str, Any]) -> dict[str, Any]:
    event: dict[str, Any] = {
        "session_id": session_id,
        "event_type": event_type,
    }
    completed_at = as_string(row.get("completed_at"))
    if completed_at:
        event["created_at"] = completed_at
    return event


def synthetic_name(interpreter: str, content: str) -> str:
    extension = "py" if interpreter.startswith("python") else "js" if interpreter == "node" else "sh"
    digest = hashlib.sha256(content.strip().encode()).hexdigest()[:12]
    return f"inline_{interpreter}_{digest}.{extension}"


def synthetic_script_events(
    session_id: str,
    interpreter: str,
    content: str,
    exit_code: int | None,
    row: dict[str, Any],
) -> list[dict[str, Any]]:
    if not content.strip():
        return []
    name = synthetic_name(interpreter, content)
    command = f"{interpreter} {name}"
    cache = base_event(session_id, "cache", row)
    cache.update({"event_key": name, "event_value": content})
    create = base_event(session_id, "command", row)
    create["command"] = f"cat > {name} << EOF"
    execute = base_event(session_id, "command", row)
    execute.update({"command": command, "tool_call_id": as_string(row.get("tool_call_id"))})
    events = [cache, create, execute]
    if exit_code is not None:
        result = base_event(session_id, "result", row)
        result.update(
            {
                "command": command,
                "exit_code": exit_code,
                "tool_call_id": as_string(row.get("tool_call_id")),
            }
        )
        events.append(result)
    return events


def split_shell_segments(value: str) -> list[str]:
    segments: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(value):
        char = value[index]
        next_char = value[index + 1] if index + 1 < len(value) else None
        if escaped:
            current.append(char)
            escaped = False
            index += 1
            continue
        if char == "\\":
            current.append(char)
            escaped = True
            index += 1
            continue
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            current.append(char)
            quote = char
            index += 1
            continue
        if char in {";", "|"}:
            segment = "".join(current).strip()
            if segment:
                segments.append(segment)
            current = []
            index += 1
            continue
        if char == "&" and next_char == "&":
            segment = "".join(current).strip()
            if segment:
                segments.append(segment)
            current = []
            index += 2
            continue
        current.append(char)
        index += 1
    segment = "".join(current).strip()
    if segment:
        segments.append(segment)
    return segments


def is_env_assignment(token: str) -> bool:
    if "=" not in token:
        return False
    name, _value = token.split("=", 1)
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name))


def leading_interpreter(segment: str, allowed: set[str]) -> tuple[str, str] | None:
    remaining = segment.lstrip()
    while remaining:
        match = re.match(r"(\S+)(?:\s+|$)", remaining)
        if not match:
            return None
        token = match.group(1)
        remaining = remaining[match.end() :].lstrip()
        if is_env_assignment(token):
            continue
        return (token, remaining) if token in allowed else None
    return None


def extract_script_heredoc(command: str) -> tuple[str, str, str] | None:
    patterns = (
        re.compile(
            r"""cat\s*<<\s*['"]?(\w+)['"]?\s*>\s*(?:\S+/)?([a-zA-Z0-9_.-]+\.(?:py|js|sh|ts))\n([\s\S]*?)\n\1"""
        ),
        re.compile(
            r"""cat\s*>\s*(?:\S+/)?([a-zA-Z0-9_.-]+\.(?:py|js|sh|ts))\s*<<\s*['"]?(\w+)['"]?\n([\s\S]*?)\n\2"""
        ),
    )
    first = patterns[0].search(command)
    if first:
        return first.group(2), first.group(3), "file"
    second = patterns[1].search(command)
    if second:
        return second.group(1), second.group(3), "file"
    return None


def extract_interpreter_heredoc(command: str) -> tuple[str, str] | None:
    lines = command.splitlines()
    for index, line in enumerate(lines):
        operator = line.find("<<")
        if operator == -1:
            continue
        before = line[:operator]
        segments = split_shell_segments(before)
        parsed = leading_interpreter(
            segments[-1] if segments else "",
            {"python", "python3", "node", "bash", "sh", "ts", "tsx"},
        )
        if not parsed:
            continue
        interpreter, _remaining = parsed
        delimiter_text = line[operator + 2 :].lstrip()
        if delimiter_text.startswith("-"):
            delimiter_text = delimiter_text[1:].lstrip()
        raw_delimiter = delimiter_text.split(maxsplit=1)[0] if delimiter_text else ""
        delimiter = raw_delimiter.strip("'\"")
        if not delimiter:
            continue
        for end in range(index + 1, len(lines)):
            if lines[end].strip() == delimiter:
                return interpreter, "\n".join(lines[index + 1 : end])
        return None
    return None


def extract_inline_script(command: str) -> tuple[str, str] | None:
    for segment in split_shell_segments(command):
        parsed = leading_interpreter(segment, {"python", "python3", "node", "ts", "tsx"})
        if not parsed:
            continue
        interpreter, remaining = parsed
        match = re.search(r"(?:^|\s)(?:-c|-e)\s+(['\"])([\s\S]*?)\1", remaining)
        if match:
            return interpreter, match.group(2)
    return None


def shell_events(session_id: str, arguments: dict[str, Any], row: dict[str, Any]) -> list[dict[str, Any]]:
    command = arguments.get("command")
    if not isinstance(command, str) or not command:
        return []
    result_text = as_string(row.get("result_content"))
    exit_code = parse_exit_code(result_text)
    tool_call_id = as_string(row.get("tool_call_id"))

    command_event = base_event(session_id, "command", row)
    command_event.update({"command": command, "tool_call_id": tool_call_id})
    events = [command_event]
    if exit_code is not None:
        result_event = base_event(session_id, "result", row)
        result_event.update(
            {
                "command": command,
                "exit_code": exit_code,
                "tool_call_id": tool_call_id,
            }
        )
        events.append(result_event)

    file_heredoc = extract_script_heredoc(command)
    if file_heredoc:
        name, content, _kind = file_heredoc
        cache = base_event(session_id, "cache", row)
        cache.update({"event_key": name, "event_value": content})
        events.append(cache)

    interpreter_heredoc = extract_interpreter_heredoc(command)
    if interpreter_heredoc:
        events.extend(synthetic_script_events(session_id, *interpreter_heredoc, exit_code, row))

    inline_script = extract_inline_script(command)
    if inline_script:
        events.extend(synthetic_script_events(session_id, *inline_script, exit_code, row))
    return events


def file_events(session_id: str, arguments: dict[str, Any], row: dict[str, Any]) -> list[dict[str, Any]]:
    path = arguments.get("path")
    if not isinstance(path, str) or not path.endswith(SCRIPT_EXTENSIONS):
        return []
    name = Path(path).name
    events: list[dict[str, Any]] = []
    content = arguments.get("file_text")
    if isinstance(content, str) and content:
        cache = base_event(session_id, "cache", row)
        cache.update({"event_key": name, "event_value": content})
        events.append(cache)
    command = base_event(session_id, "command", row)
    command["command"] = f"cat > {name} << EOF"
    events.append(command)
    return events


def apply_patch_events(session_id: str, arguments: dict[str, Any], row: dict[str, Any]) -> list[dict[str, Any]]:
    patch = arguments.get("patch")
    if not isinstance(patch, str):
        return []
    events: list[dict[str, Any]] = []
    add_re = re.compile(
        r"^\*\*\* Add File: (.+?)\n((?:(?:\+.*)?\n)*)",
        re.MULTILINE,
    )
    for match in add_re.finditer(patch):
        path = match.group(1).strip()
        if not path.endswith(SCRIPT_EXTENSIONS):
            continue
        content = "\n".join(
            line[1:] for line in match.group(2).splitlines() if line.startswith("+")
        )
        name = Path(path).name
        if content:
            cache = base_event(session_id, "cache", row)
            cache.update({"event_key": name, "event_value": content})
            events.append(cache)
        command = base_event(session_id, "command", row)
        command["command"] = f"cat > {name} << EOF"
        events.append(command)
    return events


def normalize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    coverage = {
        "toolRequestCount": 0,
        "shellToolRequestCount": 0,
        "fileToolRequestCount": 0,
        "applyPatchToolRequestCount": 0,
        "completionLinkedCount": 0,
        "missingCompletionCount": 0,
        "extractionErrorCount": 0,
        "commandEventCount": 0,
        "resultEventCount": 0,
        "cacheEventCount": 0,
    }
    events: list[dict[str, Any]] = []

    def sort_key(row: dict[str, Any]) -> tuple[str, bool, str, str]:
        completed_at = as_string(row.get("completed_at"))
        return (
            as_string(row.get("session_id")) or "",
            completed_at is None,
            completed_at or "",
            as_string(row.get("tool_call_id")) or "",
        )

    for row in sorted(rows, key=sort_key):
        session_id = as_string(row.get("session_id"))
        tool_name = as_string(row.get("tool_name"))
        if not session_id or not tool_name:
            coverage["extractionErrorCount"] += 1
            continue
        coverage["toolRequestCount"] += 1
        if tool_name in SHELL_TOOLS:
            coverage["shellToolRequestCount"] += 1
        elif tool_name == "apply_patch":
            coverage["applyPatchToolRequestCount"] += 1
        else:
            coverage["fileToolRequestCount"] += 1
        if row.get("tool_complete_success") is None:
            coverage["missingCompletionCount"] += 1
        else:
            coverage["completionLinkedCount"] += 1

        try:
            arguments = (
                parse_apply_patch_arguments(row.get("arguments_json"))
                if tool_name == "apply_patch"
                else parse_arguments(row.get("arguments_json"))
            )
            if tool_name in SHELL_TOOLS:
                extracted = shell_events(session_id, arguments, row)
            elif tool_name in FILE_TOOLS:
                extracted = file_events(session_id, arguments, row)
            elif tool_name == "apply_patch":
                extracted = apply_patch_events(session_id, arguments, row)
            else:
                extracted = []
            events.extend(extracted)
            for event in extracted:
                key = f"{event['event_type']}EventCount"
                coverage[key] += 1
        except (json.JSONDecodeError, TypeError, ValueError):
            coverage["extractionErrorCount"] += 1

    return {"schemaVersion": 1, "events": events, "coverage": coverage}


def comparable_event(event: dict[str, Any]) -> str:
    return json.dumps(
        {
            key: event.get(key)
            for key in (
                "event_type",
                "command",
                "output",
                "exit_code",
                "event_key",
                "event_value",
            )
            if event.get(key) is not None
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def supplement_events(
    remote_document: dict[str, Any],
    current_document: dict[str, Any],
) -> dict[str, Any]:
    remote_events = remote_document.get("events")
    current_events = current_document.get("events")
    if not isinstance(remote_events, list) or not isinstance(current_events, list):
        raise ValueError("supplement inputs must contain events arrays")
    if not current_events:
        return {
            **remote_document,
            "supplementation": {
                "supplementedEventCount": 0,
                "replacedRemoteEventCount": 0,
            },
        }

    current_signatures = [comparable_event(event) for event in current_events]
    remote_by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in remote_events:
        session_id = as_string(event.get("session_id"))
        if session_id:
            remote_by_session[session_id].append(event)

    matching_session_id: str | None = None
    matching_event_count = -1
    for session_id, session_events in remote_by_session.items():
        if (
            len(session_events) <= len(current_signatures)
            and all(
                comparable_event(event) == current_signatures[index]
                for index, event in enumerate(session_events)
            )
            and len(session_events) > matching_event_count
        ):
            matching_session_id = session_id
            matching_event_count = len(session_events)

    retained_remote = (
        [
            event
            for event in remote_events
            if as_string(event.get("session_id")) != matching_session_id
        ]
        if matching_session_id
        else remote_events
    )
    merged_events = [*retained_remote, *current_events]
    coverage = dict(remote_document.get("coverage", {}))
    for key in ("commandEventCount", "resultEventCount", "cacheEventCount"):
        event_type = key.removesuffix("EventCount").lower()
        coverage[key] = sum(event.get("event_type") == event_type for event in merged_events)
    return {
        **remote_document,
        "events": merged_events,
        "coverage": coverage,
        "supplementation": {
            "supplementedEventCount": len(current_events),
            "replacedRemoteEventCount": max(0, matching_event_count),
        },
    }


def classify_tool_type(content: str) -> str:
    lower = content.lower()
    if any(token in lower for token in ("request", "http", "api", "curl")):
        return "api_interaction"
    if any(token in lower for token in ("json", "csv", "pandas", "parse")):
        return "data_processing"
    if any(token in lower for token in ("file", "directory", "path", "glob")):
        return "file_operations"
    if any(token in lower for token in ("test", "assert", "pytest", "unittest")):
        return "testing"
    if any(token in lower for token in ("debug", "log", "trace")):
        return "debugging"
    if any(token in lower for token in ("cron", "schedule", "batch")):
        return "automation"
    return "utility"


def structural_fingerprint(content: str, tool_type: str) -> str:
    lines = [
        line.strip()
        for line in content.splitlines()
        if line.strip() and not line.strip().startswith(("#", "//"))
    ]
    utf16_key = lambda value: value.encode("utf-16-be", errors="surrogatepass")
    imports = sorted(
        (re.sub(r"\s+", " ", line) for line in lines if IMPORT_RE.search(line)),
        key=utf16_key,
    )
    joined = "\n".join(lines)
    calls = sorted(
        {
            re.sub(r"\s+", "", match.group(0).lower())
            for pattern in CALL_PATTERNS
            for match in pattern.finditer(joined)
        },
        key=utf16_key,
    )
    extensions = sorted(
        {
            re.search(r"\.([a-z0-9]+)['\"]$", match.group(0), re.IGNORECASE).group(1).lower()
            for match in EXT_RE.finditer(joined)
        },
        key=utf16_key,
    )
    fingerprint = (
        f"imports:{'|'.join(imports)};"
        f"calls:{'|'.join(calls)};"
        f"fileExts:{'|'.join(extensions)};"
        f"type:{tool_type}"
    )
    return hashlib.sha256(fingerprint.encode()).hexdigest()[:12]


def script_path(command: str) -> str | None:
    for pattern in SCRIPT_COMMAND_PATTERNS:
        match = pattern.search(command)
        if match:
            return Path(match.group(1)).name
    return None


def derive_patterns(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cache: dict[str, str] = {}
    result_by_tool_call: dict[str, int | None] = {}
    result_by_command: dict[tuple[str, str], deque[int | None]] = defaultdict(deque)

    for event in events:
        if event.get("event_type") != "result":
            continue
        exit_code = event.get("exit_code")
        tool_call_id = as_string(event.get("tool_call_id"))
        if tool_call_id:
            result_by_tool_call[tool_call_id] = exit_code if isinstance(exit_code, int) else None
        command = event.get("command")
        session_id = as_string(event.get("session_id"))
        if isinstance(command, str) and session_id:
            result_by_command[(session_id, command)].append(
                exit_code if isinstance(exit_code, int) else None
            )

    patterns: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for event in events:
        event_type = event.get("event_type")
        if event_type == "cache":
            event_key = event.get("event_key")
            event_value = event.get("event_value")
            if isinstance(event_key, str) and isinstance(event_value, str) and event_value:
                cache[Path(event_key).name] = event_value
            continue
        if event_type != "command":
            continue
        command = event.get("command")
        if not isinstance(command, str) or not command:
            continue
        path = script_path(command)
        if not path or path not in cache:
            continue
        content = cache[path]
        tool_type = classify_tool_type(content)
        tool_hash = structural_fingerprint(content, tool_type)
        if tool_hash not in patterns:
            order.append(tool_hash)
            patterns[tool_hash] = {
                "tool_hash": tool_hash,
                "script_path": path,
                "script_content": content,
                "usage_count": 0,
                "success_count": 0,
                "failure_count": 0,
                "first_used": event.get("created_at"),
                "last_used": event.get("created_at"),
                "tool_type": tool_type,
                "contexts": [],
                "session_ids": [],
            }
        pattern = patterns[tool_hash]
        pattern["usage_count"] += 1
        tool_call_id = as_string(event.get("tool_call_id"))
        session_id = as_string(event.get("session_id")) or "undefined"
        exit_code = result_by_tool_call.get(tool_call_id) if tool_call_id else None
        if exit_code is None:
            queue = result_by_command.get((session_id, command))
            exit_code = queue.popleft() if queue else None
        if exit_code == 0:
            pattern["success_count"] += 1
        elif exit_code is not None:
            pattern["failure_count"] += 1
        context = f"session-{session_id}"
        if context not in pattern["contexts"]:
            pattern["contexts"].append(context)
        if session_id not in pattern["session_ids"]:
            pattern["session_ids"].append(session_id)
        created_at = event.get("created_at")
        if isinstance(created_at, str):
            first = pattern.get("first_used")
            last = pattern.get("last_used")
            pattern["first_used"] = created_at if not isinstance(first, str) else min(first, created_at)
            pattern["last_used"] = created_at if not isinstance(last, str) else max(last, created_at)

    ranked = [patterns[tool_hash] for tool_hash in order]
    ranked.sort(key=lambda item: item["usage_count"], reverse=True)
    return ranked


def candidate_document(
    events_document: dict[str, Any],
    *,
    user: str,
    repository: str,
    branch: str,
    usage_threshold: int,
    success_threshold: float,
) -> dict[str, Any]:
    events = events_document.get("events")
    if not isinstance(events, list):
        raise ValueError("normalized evidence must contain an events array")
    patterns = derive_patterns(events)
    candidates = []
    for pattern in patterns:
        total = pattern["success_count"] + pattern["failure_count"]
        success_rate = pattern["success_count"] / total if total else 0.0
        candidate = {**pattern, "success_rate": success_rate}
        if candidate["usage_count"] >= usage_threshold and success_rate >= success_threshold:
            candidates.append(candidate)
    candidates.sort(key=lambda item: (-item["usage_count"], -item["success_rate"]))
    return {
        "schemaVersion": 1,
        "scope": {
            "kind": "user_repo_branch",
            "user": user,
            "repository": repository,
            "branch": branch,
            "limitSessions": 100,
        },
        "thresholds": {
            "usageThreshold": usage_threshold,
            "successThreshold": success_threshold,
        },
        "coverage": events_document.get("coverage", {}),
        "usagePatterns": patterns,
        "candidates": candidates,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subcommands = result.add_subparsers(dest="command", required=True)

    normalize = subcommands.add_parser("normalize", help="Normalize remote tool rows")
    normalize.add_argument("--in", dest="input", required=True)
    normalize.add_argument("--out", dest="output", required=True)

    supplement = subcommands.add_parser(
        "supplement",
        help="Merge a complete current-session trajectory into remote evidence",
    )
    supplement.add_argument("--remote", required=True)
    supplement.add_argument("--current", required=True)
    supplement.add_argument("--out", dest="output", required=True)

    derive = subcommands.add_parser("derive", help="Derive and rank Forge candidates")
    derive.add_argument("--in", dest="input", required=True)
    derive.add_argument("--out", dest="output", required=True)
    derive.add_argument("--user", required=True)
    derive.add_argument("--repository", required=True)
    derive.add_argument("--branch", required=True)
    derive.add_argument("--usage-threshold", type=int, default=DEFAULT_USAGE_THRESHOLD)
    derive.add_argument("--success-threshold", type=float, default=DEFAULT_SUCCESS_THRESHOLD)

    run = subcommands.add_parser("run", help="Normalize rows and derive candidates")
    run.add_argument("--in", dest="input", required=True)
    run.add_argument("--out", dest="output", required=True)
    run.add_argument("--events-out")
    run.add_argument("--user", required=True)
    run.add_argument("--repository", required=True)
    run.add_argument("--branch", required=True)
    run.add_argument("--usage-threshold", type=int, default=DEFAULT_USAGE_THRESHOLD)
    run.add_argument("--success-threshold", type=float, default=DEFAULT_SUCCESS_THRESHOLD)
    return result


def main() -> None:
    args = parser().parse_args()
    if args.command == "normalize":
        rows = read_json(args.input)
        if not isinstance(rows, list):
            raise SystemExit("input must be a JSON array of remote tool rows")
        write_json(args.output, normalize_rows(rows))
        return

    if args.command == "supplement":
        write_json(
            args.output,
            supplement_events(read_json(args.remote), read_json(args.current)),
        )
        return

    if args.command == "derive":
        events_document = read_json(args.input)
    else:
        rows = read_json(args.input)
        if not isinstance(rows, list):
            raise SystemExit("input must be a JSON array of remote tool rows")
        events_document = normalize_rows(rows)
        if args.events_out:
            write_json(args.events_out, events_document)

    document = candidate_document(
        events_document,
        user=args.user,
        repository=args.repository,
        branch=args.branch,
        usage_threshold=args.usage_threshold,
        success_threshold=args.success_threshold,
    )
    write_json(args.output, document)


if __name__ == "__main__":
    main()
