#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. All rights reserved.

"""Generate bounded repository-scoped Forge extraction queries."""

from __future__ import annotations

import argparse
import json

from session_queries import (
    build_discovery_query,
    build_event_metadata_query,
    build_event_tool_calls_query,
    build_files_query,
    build_metadata_query,
    build_refs_query,
    build_shutdown_query,
    build_tool_calls_query,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--kind",
        choices=(
            "discovery",
            "metadata",
            "metadata-events",
            "shutdown",
            "refs",
            "files",
            "tool-calls",
            "tool-calls-events",
        ),
        default="discovery",
    )
    parser.add_argument("--start", help="Inclusive UTC timestamp")
    parser.add_argument("--end", help="Exclusive UTC timestamp")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--after-session-id")
    parser.add_argument("--after-tool-call-id")
    parser.add_argument("--after-turn-index", type=int)
    parser.add_argument("--after-ref-type")
    parser.add_argument("--after-ref-value")
    parser.add_argument("--after-file-path")
    parser.add_argument("--after-tool-name")
    parser.add_argument("--repository")
    parser.add_argument("--session-id", action="append", default=[])
    parser.add_argument("--session-ids-json")
    args = parser.parse_args()
    if not 1 <= args.limit <= 5000:
        raise SystemExit("--limit must be between 1 and 5000")

    session_ids = list(args.session_id)
    if args.session_ids_json:
        values = json.loads(args.session_ids_json)
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise SystemExit("--session-ids-json must be a JSON array of strings")
        session_ids.extend(values)

    try:
        if args.kind == "discovery":
            if not args.repository or not args.start or not args.end:
                raise ValueError("discovery requires --repository, --start, and --end")
            if args.after_session_id:
                raise ValueError("discovery does not support --after-session-id")
            query = build_discovery_query(
                repository=args.repository,
                start=args.start,
                end=args.end,
                limit=args.limit,
            )
        elif args.kind == "metadata":
            query = build_metadata_query(session_ids=session_ids, limit=args.limit)
        elif args.kind == "shutdown":
            if len(session_ids) != 1 or not args.start or not args.end:
                raise ValueError("shutdown requires one --session-id, --start, and --end")
            query = build_shutdown_query(
                session_id=session_ids[0],
                start=args.start,
                end=args.end,
            )
        elif args.kind == "metadata-events":
            if len(session_ids) != 1 or not args.start or not args.end:
                raise ValueError("metadata-events requires one --session-id, --start, and --end")
            query = build_event_metadata_query(
                session_id=session_ids[0],
                start=args.start,
                end=args.end,
            )
        elif args.kind == "refs":
            if not args.start or not args.end:
                raise ValueError("refs requires --start and --end")
            cursor = None
            if args.after_session_id:
                cursor = {
                    "sessionId": args.after_session_id,
                    "turnIndex": args.after_turn_index,
                    "refType": args.after_ref_type,
                    "refValue": args.after_ref_value,
                }
            query = build_refs_query(
                session_ids=session_ids,
                start=args.start,
                end=args.end,
                limit=args.limit,
                cursor=cursor,
            )
        elif args.kind == "files":
            if not args.start or not args.end:
                raise ValueError("files requires --start and --end")
            cursor = None
            if args.after_session_id:
                cursor = {
                    "sessionId": args.after_session_id,
                    "turnIndex": args.after_turn_index,
                    "filePath": args.after_file_path,
                    "toolName": args.after_tool_name,
                }
            query = build_files_query(
                session_ids=session_ids,
                start=args.start,
                end=args.end,
                limit=args.limit,
                cursor=cursor,
            )
        elif args.kind == "tool-calls":
            if not args.start or not args.end:
                raise ValueError("tool-calls requires --start and --end")
            query = build_tool_calls_query(
                session_ids=session_ids,
                start=args.start,
                end=args.end,
                limit=args.limit,
                after_session_id=args.after_session_id,
                after_tool_call_id=args.after_tool_call_id,
            )
        else:
            if len(session_ids) != 1 or not args.start or not args.end:
                raise ValueError("tool-calls-events requires one --session-id, --start, and --end")
            query = build_event_tool_calls_query(
                session_id=session_ids[0],
                start=args.start,
                end=args.end,
                limit=args.limit,
                after_tool_call_id=args.after_tool_call_id,
            )
    except ValueError as error:
        raise SystemExit(str(error)) from error
    print(query)


if __name__ == "__main__":
    main()
