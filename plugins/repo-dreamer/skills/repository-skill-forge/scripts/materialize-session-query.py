#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. All rights reserved.

"""Materialize a session_store_sql result from the current session event log."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from forge_common import read_json, write_json


ROW_COUNT_RE = re.compile(r"^(\d+) row\(s\) returned:$")
SPILL_PATH_RE = re.compile(r"Saved to: (.+)")
DETAILED_SQL_RE = re.compile(
    r"^SQL \(session_store(?:/[^)]*)?\): (.*)$",
    re.DOTALL,
)


def load_action(path: str, action_id: str) -> dict[str, Any]:
    payload = read_json(path)
    if not isinstance(payload, dict):
        raise ValueError("action payload must be a JSON object")
    if payload.get("kind") == "action-batch":
        actions = payload.get("actions")
        if not isinstance(actions, list):
            raise ValueError("action batch must contain an actions array")
        for action in actions:
            if isinstance(action, dict) and action.get("actionId") == action_id:
                return action
        raise ValueError(f"action ID not found in action batch: {action_id}")
    if payload.get("actionId") != action_id:
        raise ValueError(f"action payload does not match action ID: {action_id}")
    return payload


def result_content(
    events_root: Path,
    sql: str,
) -> str:
    event_files = sorted(
        events_root.glob("*/events.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for event_file in event_files:
        matches: list[tuple[bool | None, dict[str, Any]]] = []
        with event_file.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") != "tool.execution_complete":
                    continue
                data = event.get("data")
                if not isinstance(data, dict):
                    continue
                result = data.get("result")
                if not isinstance(result, dict):
                    continue
                detailed = result.get("detailedContent")
                sql_match = (
                    DETAILED_SQL_RE.fullmatch(detailed)
                    if isinstance(detailed, str)
                    else None
                )
                detailed_payload = sql_match.group(1) if sql_match else None
                if detailed_payload == sql or (
                    isinstance(detailed_payload, str)
                    and detailed_payload.startswith(f"{sql}\n\n")
                ):
                    matches.append((data.get("success"), result))
        if not matches:
            continue
        success, result = matches[-1]
        if success is not True:
            raise ValueError(
                "matching session_store_sql call was not explicitly successful"
            )
        content = result.get("content")
        if not isinstance(content, str):
            raise ValueError("matching session_store_sql result has no content")
        spill_match = SPILL_PATH_RE.search(content)
        if spill_match:
            spill_path = Path(spill_match.group(1).strip())
            if not spill_path.is_file():
                raise ValueError(f"session_store_sql spill file not found: {spill_path}")
            return spill_path.read_text(encoding="utf-8")
        return content
    raise ValueError("matching session_store_sql result not found in session event logs")


def table_rows(content: str) -> tuple[list[str] | None, list[str]]:
    stripped = content.strip()
    if stripped == "Query returned 0 rows.":
        return None, []
    lines = content.splitlines()
    count_index = next(
        (
            index
            for index, line in enumerate(lines)
            if ROW_COUNT_RE.fullmatch(line.strip())
        ),
        None,
    )
    if count_index is None:
        raise ValueError("session_store_sql row count not found")
    expected = int(ROW_COUNT_RE.fullmatch(lines[count_index].strip()).group(1))
    table = [
        line[2:-2]
        for line in lines[count_index + 1 :]
        if line.startswith("| ") and line.endswith(" |")
    ]
    if len(table) < 2:
        raise ValueError("session_store_sql Markdown table not found")
    header = table[0].split(" | ")
    rows = table[2:]
    if len(rows) != expected:
        raise ValueError(
            f"session_store_sql row count mismatch: expected {expected}, found {len(rows)}"
        )
    return header, rows


def nullable(value: str) -> str | None:
    return None if value == "NULL" else value


def parse_rows(
    kind: str,
    header: list[str] | None,
    rows: list[str],
) -> list[dict[str, Any]]:
    expected_headers = {
        "discovery": ["session_id", "updated_at"],
        "metadata": [
            "session_id",
            "agent_name",
            "repository",
            "branch",
            "created_at",
            "updated_at",
        ],
        "refs": ["session_id", "ref_type", "ref_value", "turn_index"],
        "files": ["session_id", "file_path", "tool_name", "turn_index"],
        "tools": [
            "session_id",
            "tool_call_id",
            "tool_name",
            "arguments_json",
            "exit_code",
            "completed_at",
        ],
    }
    expected_header = expected_headers.get(kind)
    if expected_header is None:
        raise ValueError(f"unsupported action kind: {kind}")
    if header is not None and header != expected_header:
        raise ValueError(
            f"unexpected {kind} result columns: {', '.join(header)}"
        )
    parsed: list[dict[str, Any]] = []
    for row in rows:
        if kind == "discovery":
            session_id, updated_at = row.split(" | ", 1)
            parsed.append({"session_id": session_id, "updated_at": updated_at})
        elif kind == "metadata":
            session_id, agent_name, repository, remainder = row.split(" | ", 3)
            branch, created_at, updated_at = remainder.rsplit(" | ", 2)
            parsed.append(
                {
                    "session_id": session_id,
                    "agent_name": agent_name,
                    "repository": repository,
                    "branch": nullable(branch),
                    "created_at": created_at,
                    "updated_at": updated_at,
                }
            )
        elif kind == "refs":
            session_id, ref_type, remainder = row.split(" | ", 2)
            ref_value, turn_index = remainder.rsplit(" | ", 1)
            parsed.append(
                {
                    "session_id": session_id,
                    "ref_type": ref_type,
                    "ref_value": ref_value,
                    "turn_index": int(turn_index),
                }
            )
        elif kind == "files":
            session_id, remainder = row.split(" | ", 1)
            file_path, tool_name, turn_index = remainder.rsplit(" | ", 2)
            parsed.append(
                {
                    "session_id": session_id,
                    "file_path": file_path,
                    "tool_name": tool_name,
                    "turn_index": int(turn_index),
                }
            )
        elif kind == "tools":
            session_id, tool_call_id, tool_name, remainder = row.split(" | ", 3)
            arguments_json, exit_code, completed_at = remainder.rsplit(" | ", 2)
            json.loads(arguments_json)
            parsed.append(
                {
                    "session_id": session_id,
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "arguments_json": arguments_json,
                    "exit_code": int(exit_code) if exit_code != "NULL" else None,
                    "completed_at": nullable(completed_at),
                }
            )
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actions", required=True)
    parser.add_argument("--action", required=True)
    parser.add_argument(
        "--events-root",
        default=str(Path.home() / ".copilot" / "session-state"),
    )
    args = parser.parse_args()

    action = load_action(args.actions, args.action)
    sql = action.get("sql")
    output_path = action.get("outputPath")
    kind = action.get("kind")
    if not isinstance(sql, str) or not sql:
        raise SystemExit("action is missing SQL")
    if not isinstance(output_path, str) or not output_path:
        raise SystemExit("action is missing outputPath")
    if not isinstance(kind, str):
        raise SystemExit("action is missing kind")
    try:
        content = result_content(Path(args.events_root), sql)
        header, raw_rows = table_rows(content)
        rows = parse_rows(kind, header, raw_rows)
        write_json(output_path, rows)
        print(output_path)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
