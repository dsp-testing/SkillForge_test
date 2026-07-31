#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. All rights reserved.

"""Generate a bounded repository-scoped session evidence query."""

from __future__ import annotations

import argparse


def sql_literal(value: str) -> str:
    return value.replace("'", "''")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="Inclusive UTC timestamp")
    parser.add_argument("--end", required=True, help="Exclusive UTC timestamp")
    parser.add_argument("--limit-sessions", type=int, default=500)
    args = parser.parse_args()
    if not 1 <= args.limit_sessions <= 5000:
        raise SystemExit("--limit-sessions must be between 1 and 5000")

    start = sql_literal(args.start)
    end = sql_literal(args.end)
    query_limit = args.limit_sessions + 1
    print(
        f"""WITH selected_sessions AS (
    SELECT id, agent_name, repository, branch, created_at, updated_at,
           row_number() OVER (ORDER BY updated_at, id) AS session_rank
    FROM sessions
    WHERE updated_at >= TIMESTAMP '{start}'
      AND updated_at < TIMESTAMP '{end}'
      AND agent_name IN (
          'Copilot Coding Agent',
          'Copilot CLI',
          'Copilot Code Review'
      )
    ORDER BY updated_at, id
    LIMIT {query_limit}
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
        JOIN selected_sessions s ON s.id = e.session_id
        WHERE e.tool_complete_call_id IS NOT NULL
          AND e.timestamp >= TIMESTAMP '{start}'
          AND e.timestamp < TIMESTAMP '{end}'
    )
    WHERE completion_rank = 1
)
SELECT s.id AS session_id, s.agent_name, s.repository, s.branch,
       s.created_at, s.updated_at, s.session_rank,
       (SELECT to_json(list(r)) FROM (
           SELECT ref_type, ref_value, turn_index
           FROM session_refs
           WHERE session_id = s.id
           ORDER BY turn_index, ref_type, ref_value
       ) r) AS refs_json,
       (SELECT to_json(list(f)) FROM (
           SELECT file_path, tool_name, turn_index
           FROM session_files
           WHERE session_id = s.id
           ORDER BY turn_index, file_path
       ) f) AS files_json,
       tr.tool_call_id, tr.name AS tool_name, tr.arguments_json,
       ce.tool_complete_success, ce.result_content, ce.completed_at
FROM selected_sessions s
LEFT JOIN tool_requests tr ON tr.session_id = s.id
LEFT JOIN completion_events ce
  ON ce.session_id = s.id
 AND ce.tool_complete_call_id = tr.tool_call_id
ORDER BY s.session_rank, ce.completed_at NULLS LAST, tr.tool_call_id"""
    )


if __name__ == "__main__":
    main()
