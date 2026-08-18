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
    repository: str,
    start: str,
    end: str,
    limit: int,
    cursor: dict[str, str] | None = None,
) -> str:
    agents = ",\n          ".join(f"'{agent}'" for agent in SUPPORTED_AGENTS)
    after = ""
    if cursor is not None:
        if (
            not isinstance(cursor, dict)
            or not isinstance(cursor.get("updatedAt"), str)
            or not cursor["updatedAt"]
            or not isinstance(cursor.get("sessionId"), str)
            or not cursor["sessionId"]
        ):
            raise ValueError(
                "discovery cursor requires non-empty updatedAt and sessionId"
            )
        after = (
            "\n  AND (updated_at > "
            f"TIMESTAMP '{sql_literal(cursor['updatedAt'])}'"
            "\n       OR (updated_at = "
            f"TIMESTAMP '{sql_literal(cursor['updatedAt'])}'"
            f"\n           AND id > '{sql_literal(cursor['sessionId'])}'))"
        )
    return f"""SELECT id AS session_id, updated_at
FROM sessions
WHERE repository = '{sql_literal(repository)}'
  AND updated_at >= TIMESTAMP '{sql_literal(start)}'
  AND updated_at < TIMESTAMP '{sql_literal(end)}'
  AND agent_name IN (
          {agents}
      ){after}
ORDER BY updated_at, id
LIMIT {limit + 1}"""


def build_metadata_query(*, session_ids: list[str], limit: int) -> str:
    ids = sql_values(session_ids)
    return f"""SELECT s.id AS session_id, s.agent_name, s.repository, s.branch,
       s.created_at, s.updated_at
FROM sessions s
WHERE s.id IN ({ids})
ORDER BY s.updated_at, s.id
LIMIT {limit + 1}"""


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
    return f"""WITH selected_tool_calls AS (
    SELECT tr.session_id, tr.tool_call_id, lower(tr.name) AS tool_name,
           tr.arguments_json
    FROM tool_requests tr
    WHERE tr.session_id IN ({ids})
      AND lower(tr.name) IN ({tools}){cursor}
    ORDER BY tr.session_id, tr.tool_call_id
    LIMIT {limit + 1}
),
completion_events AS (
    SELECT session_id, tool_complete_call_id, exit_code, completed_at
    FROM (
        SELECT e.session_id, e.tool_complete_call_id,
               try_cast(
                   nullif(
                       regexp_extract(
                           lower(COALESCE(e.tool_complete_result_content, '')),
                           '(?:exited|completed) with exit code ([0-9]+)',
                           1
                       ),
                       ''
                   )
                   AS INTEGER
               ) AS exit_code,
               e.timestamp AS completed_at,
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
          AND e.type = 'tool.execution_complete'
    )
    WHERE completion_rank = 1
)
SELECT tr.session_id, tr.tool_call_id, tr.tool_name, tr.arguments_json,
       ce.exit_code, ce.completed_at
FROM selected_tool_calls tr
LEFT JOIN completion_events ce
  ON ce.session_id = tr.session_id
 AND ce.tool_complete_call_id = tr.tool_call_id
ORDER BY tr.session_id, tr.tool_call_id"""


def build_event_tool_calls_query(
    *,
    session_id: str,
    start: str,
    end: str,
    limit: int,
    after_tool_call_id: str | None = None,
) -> str:
    cursor = ""
    if after_tool_call_id:
        cursor = f"\n      AND e.tool_start_call_id > '{sql_literal(after_tool_call_id)}'"
    tools = ", ".join(f"'{tool}'" for tool in RELEVANT_TOOLS)
    return f"""WITH selected_tool_calls AS (
    SELECT e.session_id, e.tool_start_call_id AS tool_call_id,
           lower(e.tool_start_name) AS tool_name,
           e.tool_start_arguments_json AS arguments_json
    FROM events e
    WHERE e.session_id = '{sql_literal(session_id)}'
      AND e.timestamp >= TIMESTAMP '{sql_literal(start)}'
      AND e.timestamp < TIMESTAMP '{sql_literal(end)}'
      AND e.type = 'tool.execution_start'
      AND lower(e.tool_start_name) IN ({tools}){cursor}
    ORDER BY e.tool_start_call_id
    LIMIT {limit + 1}
),
completion_events AS (
    SELECT session_id, tool_complete_call_id, exit_code, completed_at
    FROM (
        SELECT e.session_id, e.tool_complete_call_id,
               try_cast(
                   nullif(
                       regexp_extract(
                           lower(COALESCE(e.tool_complete_result_content, '')),
                           '(?:exited|completed) with exit code ([0-9]+)',
                           1
                       ),
                       ''
                   )
                   AS INTEGER
               ) AS exit_code,
               e.timestamp AS completed_at,
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
          AND e.type = 'tool.execution_complete'
    )
    WHERE completion_rank = 1
)
SELECT tr.session_id, tr.tool_call_id, tr.tool_name, tr.arguments_json,
       ce.exit_code, ce.completed_at
FROM selected_tool_calls tr
LEFT JOIN completion_events ce
  ON ce.session_id = tr.session_id
 AND ce.tool_complete_call_id = tr.tool_call_id
ORDER BY tr.tool_call_id"""
