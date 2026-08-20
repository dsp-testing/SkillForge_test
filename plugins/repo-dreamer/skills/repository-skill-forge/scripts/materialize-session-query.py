#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. All rights reserved.

"""Materialize a session_store_sql result from the current session event log."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from forge_common import read_json, write_json


ROW_COUNT_RE = re.compile(r"^(\d+) row\(s\) returned:$")
SPILL_PATH_RE = re.compile(r"Saved to: (.+)")
DETAILED_SQL_RE = re.compile(
    r"^SQL \(session_store(?:/[^)]*)?\): (.*)$",
    re.DOTALL,
)


class QueryHandoffMismatch(ValueError):
    """The action ID matched, but the submitted SQL differed."""


def normalize_sql(sql: str) -> tuple[str, ...]:
    tokens: list[str] = []
    index = 0
    while index < len(sql):
        character = sql[index]
        if character.isspace():
            index += 1
            continue
        if character in {"'", '"'}:
            quote = character
            end = index + 1
            while end < len(sql):
                if sql[end] == quote:
                    if end + 1 < len(sql) and sql[end + 1] == quote:
                        end += 2
                        continue
                    end += 1
                    break
                end += 1
            tokens.append(sql[index:end])
            index = end
            continue
        if character.isalnum() or character in {"_", "$"}:
            end = index + 1
            while end < len(sql) and (
                sql[end].isalnum() or sql[end] in {"_", "$"}
            ):
                end += 1
            tokens.append(sql[index:end])
            index = end
            continue
        operator = sql[index : index + 2]
        if operator in {">=", "<=", "<>", "!=", "||", "::"}:
            tokens.append(operator)
            index += 2
            continue
        tokens.append(character)
        index += 1
    return tuple(tokens)


def read_events(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                yield event


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


def failure_message(result: dict[str, Any]) -> str:
    for candidate in (result.get("content"), result.get("error")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return "session_store_sql call was not explicitly successful"


def probe_result(
    events_root: Path,
    sql: str,
    action_id: str | None = None,
) -> dict[str, Any]:
    """Classify the current session's result for one action without raising.

    States are ``ready`` with materializable ``content``, ``failed`` with the
    tool's own error ``message``, ``handoff-mismatch``, ``error`` for a broken
    but present result, and ``missing`` while the tool call has not completed.
    """
    event_files = sorted(
        events_root.glob("*/events.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    normalized_sql = normalize_sql(sql)
    exact_call_ids: set[str] = set()
    normalized_call_ids: set[str] = set()
    described_starts: list[tuple[int, int, str, str]] = []
    for file_index, event_file in enumerate(event_files):
        for event_index, event in enumerate(read_events(event_file)):
            if event.get("type") != "tool.execution_start":
                continue
            data = event.get("data")
            if not isinstance(data, dict):
                continue
            arguments = data.get("arguments")
            call_id = data.get("toolCallId")
            if (
                action_id is not None
                and data.get("toolName") == "session_store_sql"
                and isinstance(arguments, dict)
                and arguments.get("description") == action_id
                and isinstance(arguments.get("query"), str)
                and isinstance(call_id, str)
            ):
                described_starts.append(
                    (file_index, event_index, call_id, arguments["query"])
                )
            if (
                data.get("toolName") == "session_store_sql"
                and isinstance(arguments, dict)
                and isinstance(arguments.get("query"), str)
                and isinstance(call_id, str)
            ):
                query = arguments["query"]
                if query == sql:
                    exact_call_ids.add(call_id)
                elif normalize_sql(query) == normalized_sql:
                    normalized_call_ids.add(call_id)

    if described_starts:
        _, _, described_call_id, submitted_sql = max(
            described_starts,
            key=lambda start: (-start[0], start[1]),
        )
        if normalize_sql(submitted_sql) != normalized_sql:
            return {
                "state": "handoff-mismatch",
                "message": (
                    f"session_store_sql query handoff mismatch for action {action_id}"
                ),
            }
        exact_call_ids.add(described_call_id)

    matching_call_ids = exact_call_ids or normalized_call_ids
    match_groups: list[
        list[tuple[int, int, bool | None, dict[str, Any]]]
    ] = [[], [], []]
    for file_index, event_file in enumerate(event_files):
        for event_index, event in enumerate(read_events(event_file)):
            if event.get("type") != "tool.execution_complete":
                continue
            data = event.get("data")
            if not isinstance(data, dict):
                continue
            result = data.get("result")
            if not isinstance(result, dict):
                continue
            call_id = data.get("toolCallId")
            if isinstance(call_id, str) and call_id in matching_call_ids:
                match_groups[0].append(
                    (file_index, event_index, data.get("success"), result)
                )
                continue
            detailed = result.get("detailedContent")
            sql_match = (
                DETAILED_SQL_RE.fullmatch(detailed)
                if isinstance(detailed, str)
                else None
            )
            detailed_payload = sql_match.group(1) if sql_match else None
            rendered_sql = (
                detailed_payload.split("\n\n", 1)[0]
                if isinstance(detailed_payload, str)
                else None
            )
            if rendered_sql == sql:
                match_groups[1].append(
                    (file_index, event_index, data.get("success"), result)
                )
            elif (
                isinstance(rendered_sql, str)
                and normalize_sql(rendered_sql) == normalized_sql
            ):
                match_groups[2].append(
                    (file_index, event_index, data.get("success"), result)
                )

    selected: tuple[bool | None, dict[str, Any]] | None = None
    for matches in match_groups:
        if matches:
            _, _, success, result = max(
                matches,
                key=lambda match: (-match[0], match[1]),
            )
            selected = success, result
            break
    if selected is None:
        return {
            "state": "missing",
            "message": (
                "matching session_store_sql result not found in session event logs"
            ),
        }
    success, result = selected
    if success is not True:
        return {"state": "failed", "message": failure_message(result)}
    content = result.get("content")
    if not isinstance(content, str):
        return {
            "state": "error",
            "message": "matching session_store_sql result has no content",
        }
    spill_match = SPILL_PATH_RE.search(content)
    if spill_match:
        spill_path = Path(spill_match.group(1).strip())
        if not spill_path.is_file():
            return {
                "state": "error",
                "message": f"session_store_sql spill file not found: {spill_path}",
            }
        return {"state": "ready", "content": spill_path.read_text(encoding="utf-8")}
    return {"state": "ready", "content": content}


def result_content(
    events_root: Path,
    sql: str,
    action_id: str | None = None,
) -> str:
    probe = probe_result(events_root, sql, action_id)
    if probe["state"] == "ready":
        return str(probe["content"])
    if probe["state"] == "handoff-mismatch":
        raise QueryHandoffMismatch(probe["message"])
    if probe["state"] == "failed":
        raise ValueError(
            "matching session_store_sql call was not explicitly successful"
        )
    raise ValueError(probe["message"])


def materialize_content(kind: str, content: str, output_path: str) -> int:
    """Parse one bounded result into the controller's JSON artifact."""
    header, raw_rows = table_rows(content)
    rows = parse_rows(kind, header, raw_rows)
    write_json(output_path, rows)
    return len(rows)


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
        "tool-calls": [
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
        elif kind == "tool-calls":
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
        content = result_content(Path(args.events_root), sql, args.action)
        materialize_content(kind, content, output_path)
        print(output_path)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
