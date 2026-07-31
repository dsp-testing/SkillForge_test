---
name: session-search-strategies
description: Searches the current user's cloud sessions within the requested time window.
user-invocable: false
---

## Scope

Use `session_store_sql` with `source: "cloud"` and its default personal scope.

## Discovery

Use the time window requested in the current user message as the boundary
throughout the search. Extract relevant anchors before querying.

Run one user-scoped discovery query, replacing `<window-start-expression>` with
the derived expression:

```sql
SELECT id, summary, repository, branch, agent_name, updated_at
FROM sessions
WHERE updated_at >= <window-start-expression>
  AND COALESCE(agent_name, '') NOT ILIKE '%session-dreamer%'
ORDER BY updated_at DESC
LIMIT 100
```

Group candidate sessions by topic and select relevant sessions for detailed
inspection. Exclude the current run and prior user-dreaming runs when they can
be identified.

## Detailed inspection

Inspect one candidate session at a time. Start with `tool_requests`:

```sql
SELECT session_id, tool_call_id, name,
       substr(COALESCE(arguments_json, ''), 1, 600) AS args
FROM tool_requests
WHERE session_id = '<session-id>'
  AND (
    COALESCE(arguments_json, '') ILIKE '%<anchor-1>%'
    OR COALESCE(arguments_json, '') ILIKE '%<anchor-2>%'
  )
LIMIT 50
```

Retrieve completion snippets only for matching call IDs:

```sql
SELECT session_id, timestamp, tool_complete_call_id,
       substr(COALESCE(tool_complete_result_content, ''), 1, 1200) AS result
FROM events
WHERE session_id = '<session-id>'
  AND type = 'tool.execution_complete'
  AND timestamp >= <window-start-expression>
  AND tool_complete_call_id IN ('<tool-call-id-1>', '<tool-call-id-2>')
ORDER BY timestamp DESC
LIMIT 20
```

Use `turns` only for selected session IDs when tool evidence does not contain
enough context:

```sql
SELECT session_id, turn_index,
       substr(COALESCE(user_message, ''), 1, 500) AS user_msg,
       substr(COALESCE(assistant_response, ''), 1, 700) AS assistant_msg
FROM turns
WHERE session_id = '<session-id>'
  AND timestamp >= <window-start-expression>
ORDER BY session_id, turn_index
LIMIT 50
```

For noisy or slow queries, narrow by session ID, time, tool name, event type, or
an exact anchor. Select fewer columns and split complex queries.