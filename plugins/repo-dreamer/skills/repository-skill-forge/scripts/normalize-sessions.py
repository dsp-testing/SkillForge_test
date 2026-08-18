#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. All rights reserved.

"""Normalize bounded repository-scoped extraction artifacts into session bundles."""

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
                "exitCode": row.get("exit_code"),
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


def normalize_batched_rows(
    metadata_rows: list[dict[str, Any]],
    ref_rows: list[dict[str, Any]],
    file_rows: list[dict[str, Any]],
    tool_rows: list[dict[str, Any]],
    *,
    repository: str,
    window_start: str,
    window_end: str,
    limit_sessions: int,
) -> dict[str, Any]:
    rows_by_session: dict[str, list[dict[str, Any]]] = {}
    for tool_row in tool_rows:
        session_id = as_string(tool_row.get("session_id"))
        if session_id:
            rows_by_session.setdefault(session_id, []).append(tool_row)

    refs_by_session: dict[str, list[dict[str, Any]]] = {}
    for ref_row in ref_rows:
        session_id = as_string(ref_row.get("session_id"))
        if session_id:
            refs_by_session.setdefault(session_id, []).append(
                {
                    key: ref_row.get(key)
                    for key in ("ref_type", "ref_value", "turn_index")
                }
            )
    files_by_session: dict[str, list[dict[str, Any]]] = {}
    for file_row in file_rows:
        session_id = as_string(file_row.get("session_id"))
        if session_id:
            files_by_session.setdefault(session_id, []).append(
                {
                    key: file_row.get(key)
                    for key in ("file_path", "tool_name", "turn_index")
                }
            )

    flattened: list[dict[str, Any]] = []
    for rank, metadata in enumerate(metadata_rows, start=1):
        session_id = as_string(metadata.get("session_id"))
        if not session_id:
            flattened.append(metadata)
            continue
        calls = rows_by_session.pop(session_id, [])
        common = {
            **metadata,
            "session_rank": rank,
            "refs_json": refs_by_session.pop(
                session_id,
                normalize_collection(metadata.get("refs_json"), "refs_json"),
            ),
            "files_json": files_by_session.pop(
                session_id,
                normalize_collection(metadata.get("files_json"), "files_json"),
            ),
        }
        if calls:
            flattened.extend({**common, **call} for call in calls)
        else:
            flattened.append(common)
    for session_id in sorted(rows_by_session):
        for call in rows_by_session[session_id]:
            flattened.append(call)
    if refs_by_session or files_by_session:
        flattened.append({"session_id": None, "agent_name": None})

    document = normalize_rows(
        flattened,
        repository=repository,
        window_start=window_start,
        window_end=window_end,
        limit_sessions=limit_sessions,
    )
    for session in document["sessions"]:
        session["toolCalls"].sort(
            key=lambda call: (
                call.get("completedAt") is None,
                call.get("completedAt") or "",
                call.get("toolCallId") or "",
            )
        )
    document["coverage"]["metadataRowCount"] = len(metadata_rows)
    document["coverage"]["refRowCount"] = len(ref_rows)
    document["coverage"]["fileRowCount"] = len(file_rows)
    document["coverage"]["toolRowCount"] = len(tool_rows)
    return document


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="input")
    parser.add_argument("--metadata-in")
    parser.add_argument("--refs-in", action="append", default=[])
    parser.add_argument("--files-in", action="append", default=[])
    parser.add_argument("--tool-in", action="append", default=[])
    parser.add_argument("--out", dest="output", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--window-start", required=True)
    parser.add_argument("--window-end", required=True)
    parser.add_argument("--limit-sessions", type=int, default=500)
    args = parser.parse_args()
    if args.input and (args.metadata_in or args.refs_in or args.files_in or args.tool_in):
        raise SystemExit("use either --in or the batched --metadata-in/--tool-in inputs")
    if args.input:
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
    else:
        if not args.metadata_in:
            raise SystemExit("batched input requires --metadata-in")
        metadata_rows = read_json(args.metadata_in)
        ref_pages = [read_json(path) for path in args.refs_in]
        file_pages = [read_json(path) for path in args.files_in]
        tool_pages = [read_json(path) for path in args.tool_in]
        if not isinstance(metadata_rows, list) or any(
            not isinstance(row, dict) for row in metadata_rows
        ):
            raise SystemExit("metadata input must be a JSON array of objects")
        if any(
            not isinstance(page, list) or any(not isinstance(row, dict) for row in page)
            for page in [*ref_pages, *file_pages, *tool_pages]
        ):
            raise SystemExit("each paged input must be a JSON array of objects")
        document = normalize_batched_rows(
            metadata_rows,
            [row for page in ref_pages for row in page],
            [row for page in file_pages for row in page],
            [row for page in tool_pages for row in page],
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
