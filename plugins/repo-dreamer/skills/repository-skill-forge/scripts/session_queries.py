#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. All rights reserved.

"""Build bounded repository Skill Forge session-store queries."""

from __future__ import annotations


SUPPORTED_AGENTS = (
    "Copilot Coding Agent",
    "Copilot CLI",
    "Copilot Code Review",
)
RELEVANT_TOOLS = ("bash", "shell", "powershell", "create", "edit", "apply_patch")


def sql_literal(value: str) -> str:
    return value.replace("'", "''")


def sql_values(values: list[str]) -> str:
    if not values:
        raise ValueError("at least one session id is required")
    return ", ".join(f"'{sql_literal(value)}'" for value in values)


def build_discovery_query(
    *,
    start: str,
    end: str,
    limit: int,
    after_updated_at: str | None = None,
    after_session_id: str | None = None,
) -> str:
    cursor = ""
    if after_updated_at or after_session_id:
        if not after_updated_at or not after_session_id:
            raise ValueError("both discovery cursor values are required")
        cursor = (
            "\n      AND (updated_at, id) > "
            f"(TIMESTAMP '{sql_literal(after_updated_at)}', '{sql_literal(after_session_id)}')"
        )
    agents = ",\n          ".join(f"'{agent}'" for agent in SUPPORTED_AGENTS)
    return f"""SELECT id AS session_id, agent_name, repository, branch, created_at, updated_at
FROM sessions
WHERE updated_at >= TIMESTAMP '{sql_literal(start)}'
  AND updated_at < TIMESTAMP '{sql_literal(end)}'
  AND agent_name IN (
          {agents}
      ){cursor}
ORDER BY updated_at, id
LIMIT {limit + 1}"""


def build_metadata_query(*, session_ids: list[str]) -> str:
    ids = sql_values(session_ids)
    return f"""SELECT s.id AS session_id, s.agent_name, s.repository, s.branch,
       s.created_at, s.updated_at
FROM sessions s
WHERE s.id IN ({ids})
ORDER BY s.updated_at, s.id"""


def build_refs_query(
    *,
    session_ids: list[str],
    start: str,
    end: str,
    limit: int,
    cursor: dict[str, str | int] | None = None,
) -> str:
    ids = sql_values(session_ids)
    after = ""
    if cursor:
        after = (
            "\n  AND (session_id, turn_index, ref_type, ref_value) > "
            f"('{sql_literal(str(cursor['sessionId']))}', {int(cursor['turnIndex'])}, "
            f"'{sql_literal(str(cursor['refType']))}', '{sql_literal(str(cursor['refValue']))}')"
        )
    return f"""SELECT session_id, ref_type, ref_value, turn_index
FROM session_refs
WHERE created_at >= TIMESTAMP '{sql_literal(start)}'
  AND created_at < TIMESTAMP '{sql_literal(end)}'
  AND session_id IN ({ids}){after}
ORDER BY session_id, turn_index, ref_type, ref_value
LIMIT {limit + 1}"""


def build_files_query(
    *,
    session_ids: list[str],
    start: str,
    end: str,
    limit: int,
    cursor: dict[str, str | int] | None = None,
) -> str:
    ids = sql_values(session_ids)
    after = ""
    if cursor:
        after = (
            "\n  AND (session_id, turn_index, file_path, tool_name) > "
            f"('{sql_literal(str(cursor['sessionId']))}', {int(cursor['turnIndex'])}, "
            f"'{sql_literal(str(cursor['filePath']))}', '{sql_literal(str(cursor['toolName']))}')"
        )
    return f"""SELECT session_id, file_path, tool_name, turn_index
FROM session_files
WHERE first_seen_at >= TIMESTAMP '{sql_literal(start)}'
  AND first_seen_at < TIMESTAMP '{sql_literal(end)}'
  AND session_id IN ({ids}){after}
ORDER BY session_id, turn_index, file_path, tool_name
LIMIT {limit + 1}"""


def build_tool_calls_query(
    *,
    session_ids: list[str],
    start: str,
    end: str,
    limit: int,
    after_session_id: str | None = None,
    after_tool_call_id: str | None = None,
) -> str:
    ids = sql_values(session_ids)
    cursor = ""
    if after_session_id or after_tool_call_id:
        if not after_session_id or not after_tool_call_id:
            raise ValueError("both tool-call cursor values are required")
        cursor = (
            "\n      AND (tr.session_id, tr.tool_call_id) > "
            f"('{sql_literal(after_session_id)}', '{sql_literal(after_tool_call_id)}')"
        )
    tools = ", ".join(f"'{tool}'" for tool in RELEVANT_TOOLS)
    return f"""WITH target_sessions AS (
    SELECT id
    FROM sessions
    WHERE created_at >= TIMESTAMP '{sql_literal(start)}'
      AND created_at < TIMESTAMP '{sql_literal(end)}'
      AND id IN ({ids})
),
selected_tool_calls AS (
    SELECT tr.session_id, tr.tool_call_id, lower(tr.name) AS tool_name,
           tr.arguments_json
    FROM tool_requests tr
    JOIN target_sessions ts ON ts.id = tr.session_id
    WHERE lower(tr.name) IN ({tools}){cursor}
    ORDER BY tr.session_id, tr.tool_call_id
    LIMIT {limit + 1}
),
completion_events AS (
    SELECT session_id, tool_complete_call_id, tool_complete_success,
           tool_complete_result_content AS result_content,
           timestamp AS completed_at
    FROM (
        SELECT e.session_id, e.tool_complete_call_id, e.tool_complete_success,
               e.tool_complete_result_content, e.timestamp,
               row_number() OVER (
                   PARTITION BY e.session_id, e.tool_complete_call_id
                   ORDER BY e.timestamp DESC
               ) AS completion_rank
        FROM events e
        JOIN selected_tool_calls tr
          ON tr.session_id = e.session_id
         AND tr.tool_call_id = e.tool_complete_call_id
        WHERE e.timestamp >= TIMESTAMP '{sql_literal(start)}'
          AND e.timestamp < TIMESTAMP '{sql_literal(end)}'
    )
    WHERE completion_rank = 1
)
SELECT tr.session_id, tr.tool_call_id, tr.tool_name, tr.arguments_json,
       ce.tool_complete_success, ce.result_content, ce.completed_at
FROM selected_tool_calls tr
LEFT JOIN completion_events ce
  ON ce.session_id = tr.session_id
 AND ce.tool_complete_call_id = tr.tool_call_id
ORDER BY tr.session_id, tr.tool_call_id"""
