#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. All rights reserved.

"""Derive deterministic workflow primitives from normalized session bundles."""

from __future__ import annotations

import argparse
import json
import re
import shlex
from pathlib import Path
from typing import Any

from forge_common import as_string, read_json, stable_hash, write_json

SCRIPT_EXTENSIONS = (".py", ".js", ".sh", ".ts")
SHELL_TOOLS = {"bash", "shell", "powershell"}
FILE_TOOLS = {"create", "edit"}
EXIT_CODE_RE = re.compile(r"(?:exited|completed) with exit code (\d+)", re.IGNORECASE)
SCRIPT_PATH_RE = re.compile(r"(?:python3?|node|bash|sh|tsx?|ts-node)\s+([^\s;&|]+\.(?:py|js|sh|ts))")
IMPORT_RE = re.compile(
    r"^(?:import\s+[^\n]+|from\s+[^\n]+|[^\n]*require\([^\n]+)",
    re.MULTILINE,
)
CALL_RE = re.compile(
    r"\b(open|read|write|fetch|request|get|post|put|delete|glob|walk|listdir|"
    r"parse|stringify|load|dump|assert|print|log)\s*\(",
    re.IGNORECASE,
)
EXT_RE = re.compile(r"\.([a-zA-Z0-9]{1,8})(?:['\"]|\b)")


def parse_exit_code(result: str | None) -> int | None:
    if not result:
        return None
    match = EXIT_CODE_RE.search(result)
    return int(match.group(1)) if match else None


def extract_patch_files(patch: str) -> list[tuple[str, str]]:
    files: list[tuple[str, str]] = []
    pattern = re.compile(r"^\*\*\* Add File: (.+?)\n((?:(?:\+.*)?\n)*)", re.MULTILINE)
    for match in pattern.finditer(patch):
        path = match.group(1).strip()
        if not path.endswith(SCRIPT_EXTENSIONS):
            continue
        content = "\n".join(line[1:] for line in match.group(2).splitlines() if line.startswith("+"))
        if content:
            files.append((path, content))
    return files


def extract_inline_script(command: str) -> tuple[str, str] | None:
    heredoc = re.search(
        r"(python3?|node|bash|sh|tsx?)\s+<<-?['\"]?(\w+)['\"]?\n([\s\S]*?)\n\2(?:\s|$)",
        command,
    )
    if heredoc:
        return heredoc.group(1), heredoc.group(3)
    inline = re.search(r"(python3?|node|tsx?)\s+(?:-c|-e)\s+(['\"])([\s\S]*?)\2", command)
    if inline:
        return inline.group(1), inline.group(3)
    return None


def script_signature(content: str) -> dict[str, Any]:
    imports = sorted({re.sub(r"\s+", " ", match.group(0).strip()) for match in IMPORT_RE.finditer(content)})
    calls = sorted({match.group(1).lower() for match in CALL_RE.finditer(content)})
    extensions = sorted({match.group(1).lower() for match in EXT_RE.finditer(content)})
    return {"imports": imports, "calls": calls, "fileExtensions": extensions}


def command_signature(command: str) -> dict[str, Any]:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    normalized: list[str] = []
    for token in tokens:
        if re.fullmatch(r"[a-f0-9]{7,40}", token, re.IGNORECASE):
            normalized.append("<sha>")
        elif re.fullmatch(r"\d+(?:\.\d+)*", token):
            normalized.append("<number>")
        elif token.startswith(("/Users/", "/home/")):
            normalized.append("<home-path>")
        elif len(token) > 80:
            normalized.append("<value>")
        else:
            normalized.append(token)
    return {"tokens": normalized[:40]}


def path_families(files: list[dict[str, Any]]) -> list[str]:
    families: set[str] = set()
    for item in files:
        path = as_string(item.get("file_path"))
        if not path:
            continue
        parts = [part for part in Path(path).parts if part not in {".", "/"}]
        if not parts:
            continue
        depth = 3 if parts[0] == ".github" else 2
        families.add("/".join(parts[:depth]))
    return sorted(families)


def normalize_refs(refs: list[dict[str, Any]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for ref in refs:
        ref_type = as_string(ref.get("ref_type"))
        ref_value = as_string(ref.get("ref_value"))
        if ref_type in {"pr", "issue", "commit"} and ref_value:
            result.append({"type": ref_type, "value": ref_value})
    return result


def derive(document: dict[str, Any]) -> dict[str, Any]:
    sessions = document.get("sessions")
    if not isinstance(sessions, list):
        raise ValueError("normalized document must contain sessions")
    primitives: list[dict[str, Any]] = []

    for session in sessions:
        if not isinstance(session, dict):
            continue
        session_id = as_string(session.get("sessionId"))
        if not session_id:
            continue
        cache: dict[str, str] = {}
        refs = normalize_refs(session.get("refs") if isinstance(session.get("refs"), list) else [])
        families = path_families(session.get("files") if isinstance(session.get("files"), list) else [])
        tool_calls = session.get("toolCalls")
        if not isinstance(tool_calls, list):
            continue

        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            name = as_string(call.get("name"))
            arguments = call.get("arguments")
            if name in FILE_TOOLS and isinstance(arguments, dict):
                path = as_string(arguments.get("path"))
                content = as_string(arguments.get("file_text"))
                if path and content and path.endswith(SCRIPT_EXTENSIONS):
                    cache[Path(path).name] = content
                continue
            if name == "apply_patch":
                patch = arguments if isinstance(arguments, str) else (
                    as_string(arguments.get("input") or arguments.get("patch"))
                    if isinstance(arguments, dict)
                    else None
                )
                if patch:
                    for path, content in extract_patch_files(patch):
                        cache[Path(path).name] = content
                continue
            if name not in SHELL_TOOLS or not isinstance(arguments, dict):
                continue
            command = as_string(arguments.get("command"))
            if not command:
                continue

            inline = extract_inline_script(command)
            script_match = SCRIPT_PATH_RE.search(command)
            script_name = Path(script_match.group(1)).name if script_match else None
            script_content = inline[1] if inline else cache.get(script_name or "")
            if script_content:
                signature = script_signature(script_content)
                kind = "script"
                fingerprint = stable_hash({"kind": kind, "signature": signature})
            else:
                signature = command_signature(command)
                kind = "command"
                fingerprint = stable_hash({"kind": kind, "signature": signature})

            exit_code_value = call.get("exitCode")
            exit_code = (
                int(exit_code_value)
                if isinstance(exit_code_value, int)
                or (
                    isinstance(exit_code_value, str)
                    and exit_code_value.isdigit()
                )
                else parse_exit_code(as_string(call.get("resultContent")))
            )
            completed_at = as_string(call.get("completedAt")) or as_string(session.get("updatedAt"))
            tool_call_id = as_string(call.get("toolCallId"))
            evidence_key = stable_hash(
                {
                    "sessionId": session_id,
                    "toolCallId": tool_call_id,
                    "fingerprint": fingerprint,
                    "completedAt": completed_at,
                },
                24,
            )
            primitives.append(
                {
                    "evidenceKey": evidence_key,
                    "fingerprint": fingerprint,
                    "kind": kind,
                    "signature": signature,
                    "sessionId": session_id,
                    "surface": session.get("surface"),
                    "branch": session.get("branch"),
                    "completedAt": completed_at,
                    "day": completed_at[:10] if completed_at else None,
                    "refs": refs,
                    "pathFamilies": families,
                    "outcome": (
                        "success" if exit_code == 0 else "failure" if exit_code is not None else "unknown"
                    ),
                    "exitCode": exit_code,
                    "rawEvidence": {
                        "command": command,
                        "scriptContent": script_content,
                    },
                }
            )

    return {
        "schemaVersion": 1,
        "scope": document.get("scope"),
        "coverage": {
            **(document.get("coverage") if isinstance(document.get("coverage"), dict) else {}),
            "primitiveCount": len(primitives),
        },
        "userDiversity": document.get("userDiversity"),
        "primitives": primitives,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="input", required=True)
    parser.add_argument("--out", dest="output", required=True)
    args = parser.parse_args()
    document = read_json(args.input)
    if not isinstance(document, dict):
        raise SystemExit("input must be a normalized JSON object")
    try:
        result = derive(document)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(str(error)) from error
    write_json(args.output, result)


if __name__ == "__main__":
    main()
