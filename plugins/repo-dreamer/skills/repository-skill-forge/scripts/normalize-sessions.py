#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. All rights reserved.

"""Normalize repository-scoped session query rows into stable session bundles."""

from __future__ import annotations

import argparse
import json
from typing import Any

from forge_common import as_string, parse_json_value, read_json, write_json

SURFACES = {
    "Copilot CLI": "cli",
    "Copilot Coding Agent": "cca",
    "Copilot Code Review": "ccr",
}


def normalize_collection(value: Any, name: str) -> list[dict[str, Any]]:
    parsed = parse_json_value(value, [])
    if parsed is None:
        return []
    if not isinstance(parsed, list) or any(not isinstance(item, dict) for item in parsed):
        raise ValueError(f"{name} must be a JSON array of objects")
    return parsed


def normalize_rows(
    rows: list[dict[str, Any]],
    *,
    repository: str,
    window_start: str,
    window_end: str,
    limit_sessions: int,
) -> dict[str, Any]:
    sessions: dict[str, dict[str, Any]] = {}
    extraction_errors: list[str] = []
    max_rank = 0

    for row in rows:
        session_id = as_string(row.get("session_id"))
        agent_name = as_string(row.get("agent_name"))
        if not session_id or agent_name not in SURFACES:
            extraction_errors.append("row missing supported session_id or agent_name")
            continue
        row_repository = as_string(row.get("repository"))
        if row_repository and row_repository != repository:
            extraction_errors.append(
                f"{session_id}: row repository {row_repository} does not match {repository}"
            )
            continue
        rank_value = row.get("session_rank")
        rank = int(rank_value) if isinstance(rank_value, (int, float, str)) and str(rank_value).isdigit() else 0
        max_rank = max(max_rank, rank)
        if rank > limit_sessions:
            continue

        session = sessions.get(session_id)
        if session is None:
            try:
                refs = normalize_collection(row.get("refs_json"), "refs_json")
                files = normalize_collection(row.get("files_json"), "files_json")
            except (json.JSONDecodeError, TypeError, ValueError) as error:
                extraction_errors.append(f"{session_id}: {error}")
                continue
            session = {
                "sessionId": session_id,
                "surface": SURFACES[agent_name],
                "agentName": agent_name,
                "repository": row_repository or repository,
                "branch": as_string(row.get("branch")),
                "createdAt": as_string(row.get("created_at")),
                "updatedAt": as_string(row.get("updated_at")),
                "refs": refs,
                "files": files,
                "toolCalls": [],
            }
            sessions[session_id] = session

        tool_name = as_string(row.get("tool_name"))
        if not tool_name:
            continue
        arguments = row.get("arguments_json")
        try:
            parsed_arguments = parse_json_value(arguments, {})
        except json.JSONDecodeError as error:
            extraction_errors.append(f"{session_id}/{tool_name}: invalid arguments JSON: {error}")
            continue
        if not isinstance(parsed_arguments, (dict, str)):
            extraction_errors.append(f"{session_id}/{tool_name}: unsupported arguments shape")
            continue
        session["toolCalls"].append(
            {
                "toolCallId": as_string(row.get("tool_call_id")),
                "name": tool_name,
                "arguments": parsed_arguments,
                "completionSuccess": row.get("tool_complete_success"),
                "resultContent": as_string(row.get("result_content")),
                "completedAt": as_string(row.get("completed_at")),
            }
        )

    ordered = sorted(
        sessions.values(),
        key=lambda session: (session.get("updatedAt") or "", session["sessionId"]),
    )
    return {
        "schemaVersion": 1,
        "scope": {
            "kind": "repository",
            "repository": repository,
            "windowStart": window_start,
            "windowEnd": window_end,
            "limitSessions": limit_sessions,
        },
        "coverage": {
            "rowCount": len(rows),
            "sessionCount": len(ordered),
            "toolCallCount": sum(len(session["toolCalls"]) for session in ordered),
            "extractionErrorCount": len(extraction_errors),
            "extractionErrors": extraction_errors,
            "truncated": max_rank > limit_sessions,
        },
        "userDiversity": {
            "status": "unknown",
            "reason": "Repository session evidence has no trusted actor identity",
        },
        "sessions": ordered,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="input", required=True)
    parser.add_argument("--out", dest="output", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--window-start", required=True)
    parser.add_argument("--window-end", required=True)
    parser.add_argument("--limit-sessions", type=int, default=500)
    args = parser.parse_args()
    rows = read_json(args.input)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise SystemExit("input must be a JSON array of query rows")
    document = normalize_rows(
        rows,
        repository=args.repository,
        window_start=args.window_start,
        window_end=args.window_end,
        limit_sessions=args.limit_sessions,
    )
    write_json(args.output, document)
    if document["coverage"]["truncated"]:
        raise SystemExit("session evidence is truncated; increase the limit or narrow the window")
    if document["coverage"]["extractionErrorCount"]:
        raise SystemExit("session evidence contains extraction errors")


if __name__ == "__main__":
    main()
