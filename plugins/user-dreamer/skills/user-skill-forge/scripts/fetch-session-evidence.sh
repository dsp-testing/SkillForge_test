#!/usr/bin/env bash
# Copyright (c) Microsoft Corporation. All rights reserved.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  fetch-session-evidence.sh query --repository OWNER/REPO --branch BRANCH [--limit-sessions N] [--days N]
  fetch-session-evidence.sh convert --in ROWS.json --out EVENTS.json

Commands:
  query    Print the user-scoped DuckDB SQL for session_store_sql.
  convert  Normalize saved remote tool rows into Forge trajectory events.

The query relies on session_store_sql personal scope for user isolation.
EOF
}

die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

sql_literal() {
    printf "%s" "$1" | sed "s/'/''/g"
}

command_name="${1:-}"
if [[ -z "$command_name" || "$command_name" == "--help" || "$command_name" == "-h" ]]; then
    usage
    exit 0
fi
shift

case "$command_name" in
    query)
        repository=""
        branch=""
        limit_sessions=100
        days=36500

        while (($# > 0)); do
            case "$1" in
                --repository)
                    repository="${2:-}"
                    shift 2
                    ;;
                --branch)
                    branch="${2:-}"
                    shift 2
                    ;;
                --limit-sessions)
                    limit_sessions="${2:-}"
                    shift 2
                    ;;
                --days)
                    days="${2:-}"
                    shift 2
                    ;;
                --help|-h)
                    usage
                    exit 0
                    ;;
                *)
                    die "unknown query argument: $1"
                    ;;
            esac
        done

        [[ -n "$repository" ]] || die "--repository is required"
        [[ -n "$branch" ]] || die "--branch is required"
        [[ "$limit_sessions" =~ ^[0-9]+$ ]] || die "--limit-sessions must be an integer"
        [[ "$days" =~ ^[0-9]+$ ]] || die "--days must be an integer"
        ((limit_sessions >= 1 && limit_sessions <= 1000)) || die "--limit-sessions must be between 1 and 1000"
        ((days >= 1)) || die "--days must be at least 1"

        repository_sql="$(sql_literal "$repository")"
        branch_sql="$(sql_literal "$branch")"

        cat <<EOF
WITH recent_sessions AS (
    SELECT id
    FROM sessions
    WHERE repository = '$repository_sql'
      AND branch = '$branch_sql'
      AND agent_name = 'Copilot CLI'
      AND updated_at > now() - INTERVAL '$days days'
    ORDER BY updated_at DESC
    LIMIT $limit_sessions
),
completion_events AS (
    SELECT session_id, tool_complete_call_id, tool_complete_success,
           tool_complete_result_content AS result_content, timestamp AS completed_at
    FROM (
        SELECT e.session_id, e.tool_complete_call_id, e.tool_complete_success,
               e.tool_complete_result_content, e.timestamp,
               row_number() OVER (
                   PARTITION BY e.session_id, e.tool_complete_call_id
                   ORDER BY e.timestamp DESC
               ) AS completion_rank
        FROM events e
        JOIN recent_sessions rs ON rs.id = e.session_id
        WHERE e.tool_complete_call_id IS NOT NULL
          AND e.timestamp > now() - INTERVAL '$days days'
    )
    WHERE completion_rank = 1
)
SELECT tr.session_id, tr.tool_call_id, tr.name AS tool_name, tr.arguments_json,
       ce.tool_complete_success, ce.result_content, ce.completed_at
FROM tool_requests tr
JOIN recent_sessions rs ON rs.id = tr.session_id
LEFT JOIN completion_events ce
  ON ce.session_id = tr.session_id
 AND ce.tool_complete_call_id = tr.tool_call_id
WHERE tr.name IN ('bash', 'shell', 'powershell', 'edit', 'create', 'apply_patch')
ORDER BY tr.session_id, ce.completed_at NULLS LAST, tr.tool_call_id
EOF
        ;;
    convert)
        input=""
        output=""

        while (($# > 0)); do
            case "$1" in
                --in)
                    input="${2:-}"
                    shift 2
                    ;;
                --out)
                    output="${2:-}"
                    shift 2
                    ;;
                --help|-h)
                    usage
                    exit 0
                    ;;
                *)
                    die "unknown convert argument: $1"
                    ;;
            esac
        done

        [[ -n "$input" ]] || die "--in is required"
        [[ -n "$output" ]] || die "--out is required"
        [[ -f "$input" ]] || die "input file does not exist: $input"

        script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
        python3 "$script_dir/derive-candidates.py" normalize --in "$input" --out "$output"
        ;;
    *)
        die "unknown command: $command_name"
        ;;
esac
