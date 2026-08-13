#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. All rights reserved.

"""Plan and checkpoint adaptive repository Skill Forge extraction."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from forge_common import parse_timestamp, read_json, stable_hash, timestamp_text, write_json
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


def partition_id(start: str, end: str) -> str:
    return stable_hash({"start": start, "end": end}, 12)


def batch_id(session_ids: list[str]) -> str:
    return stable_hash(session_ids, 12)


def session_hash(state: dict[str, Any], session_id: str) -> str:
    return stable_hash(
        {
            "repository": state["scope"]["repository"],
            "sessionId": session_id,
        },
        20,
    )


def initialize(args: argparse.Namespace) -> dict[str, Any]:
    if parse_timestamp(args.start) >= parse_timestamp(args.end):
        raise ValueError("--start must be before --end")
    if min(
        args.discovery_page_size,
        args.session_batch_size,
        args.tool_page_size,
        args.max_rows,
        args.max_artifact_bytes,
        args.min_window_minutes,
        args.max_discovery_failures,
        args.max_discovery_minutes,
    ) < 1:
        raise ValueError(
            "page, batch, row, artifact, window, failure, and time limits must be positive"
        )
    if args.max_query_retries < 0:
        raise ValueError("--max-query-retries must be non-negative")
    if args.tool_page_size > args.max_rows or args.discovery_page_size > args.max_rows:
        raise ValueError("page sizes must not exceed --max-rows")
    return {
        "schemaVersion": 1,
        "scope": {
            "kind": "repository",
            "repository": args.repository,
            "windowStart": args.start,
            "windowEnd": args.end,
        },
        "runDir": str(Path(args.run_dir).resolve()),
        "limits": {
            "discoveryPageSize": args.discovery_page_size,
            "sessionBatchSize": args.session_batch_size,
            "toolPageSize": args.tool_page_size,
            "maxRows": args.max_rows,
            "maxArtifactBytes": args.max_artifact_bytes,
            "minWindowMinutes": args.min_window_minutes,
            "maxQueryRetries": args.max_query_retries,
            "maxDiscoveryFailures": args.max_discovery_failures,
            "maxDiscoveryMinutes": args.max_discovery_minutes,
            "allowPartial": args.allow_partial,
            "enableTargetedFallback": args.enable_targeted_fallback,
        },
        "discoveryStartedAt": timestamp_text(datetime.now(timezone.utc)),
        "partitions": [
            {
                "partitionId": partition_id(args.start, args.end),
                "start": args.start,
                "end": args.end,
                "status": "discovering",
                "discoveryComplete": False,
                "discoveryCursor": None,
                "sessions": [],
                "batches": [],
            }
        ],
        "retryHistory": [],
        "strategyHistory": [],
        "blockers": [],
        "omittedUnits": [],
        "handledActionIds": [],
        "workCounters": {
            "queryAttempts": 0,
            "successfulQueries": 0,
            "failedQueries": 0,
            "discoveryFailures": 0,
            "rows": 0,
            "toolCalls": 0,
            "artifactBytes": 0,
        },
        "coverage": None,
        "status": "running",
    }


def artifact_path(state: dict[str, Any], action_id: str) -> str:
    return str(Path(state["runDir"]) / "extraction" / f"{action_id}.json")


def action_generation(batch: dict[str, Any], stage: str) -> int:
    return int(batch.get("retryGenerations", {}).get(stage, 0))


def paged_action_id(kind: str, batch: dict[str, Any], page: int) -> str:
    return (
        f"{kind}-{batch['batchId']}-{page}"
        f"-p{batch['pageSize']}-r{action_generation(batch, kind)}"
    )


def validate_state_invariants(state: dict[str, Any]) -> None:
    status = state.get("status")
    blockers = state.get("blockers", [])
    if status == "blocked" and not blockers:
        raise ValueError("blocked extraction state requires a terminal blocker")
    if blockers and status != "blocked":
        raise ValueError("terminal blockers require blocked extraction status")
    if status == "partial" and not state.get("omittedUnits"):
        raise ValueError("partial extraction state requires omitted units")


def work_counters(state: dict[str, Any]) -> dict[str, int]:
    return state.setdefault(
        "workCounters",
        {
            "queryAttempts": 0,
            "successfulQueries": 0,
            "failedQueries": 0,
            "discoveryFailures": 0,
            "rows": 0,
            "toolCalls": 0,
            "artifactBytes": 0,
        },
    )


def extraction_coverage(state: dict[str, Any]) -> dict[str, Any]:
    discovered_ids = {
        str(session["session_id"])
        for partition in state["partitions"]
        for session in partition["sessions"]
        if isinstance(session, dict) and session.get("session_id")
    }
    completed_ids = {
        str(session_id)
        for partition in state["partitions"]
        for batch in partition["batches"]
        if batch["status"] == "complete"
        for session_id in batch["sessionIds"]
    }
    omitted_unit_kinds = sorted(
        {
            str(unit["kind"])
            for unit in state["omittedUnits"]
            if isinstance(unit, dict) and unit.get("kind")
        }
    )
    return {
        "discoveredSessionCount": len(discovered_ids),
        "completedSessionCount": len(completed_ids),
        "omittedUnitCount": len(state["omittedUnits"]),
        "omittedUnitKinds": omitted_unit_kinds,
        "sessionCoverage": (
            len(completed_ids) / len(discovered_ids) if discovered_ids else 1.0
        ),
        "primaryStrategy": "sessions_updated_at_tool_requests",
        "targetedFallbackEnabled": state["limits"]["enableTargetedFallback"],
        "fallbackCount": len(state["strategyHistory"]),
        "fallbacks": state["strategyHistory"],
        "omittedUnits": state["omittedUnits"],
    }


def finalize_extraction(state: dict[str, Any]) -> None:
    state["coverage"] = extraction_coverage(state)
    state["status"] = "partial" if state["omittedUnits"] else "complete"


def discovery_budget_exhausted(state: dict[str, Any]) -> bool:
    limits = state["limits"]
    failures = work_counters(state).setdefault("discoveryFailures", 0)
    if failures >= limits.get("maxDiscoveryFailures", 8):
        return True
    started_at = parse_timestamp(
        state.setdefault(
            "discoveryStartedAt",
            timestamp_text(datetime.now(timezone.utc)),
        )
    )
    elapsed = datetime.now(timezone.utc) - started_at
    return elapsed >= timedelta(minutes=limits.get("maxDiscoveryMinutes", 10))


def block_discovery_budget(
    state: dict[str, Any],
    *,
    action_id: str = "discovery-budget",
    last_reason: str | None = None,
) -> None:
    if state["status"] == "blocked":
        return
    blocker = {
        "actionId": action_id,
        "kind": "discovery",
        "reason": "discovery_budget_exhausted",
        "failureCount": work_counters(state).setdefault("discoveryFailures", 0),
        "maxFailures": state["limits"].get("maxDiscoveryFailures", 8),
        "maxMinutes": state["limits"].get("maxDiscoveryMinutes", 10),
    }
    if last_reason:
        blocker["lastReason"] = last_reason
    state["blockers"].append(blocker)
    state["status"] = "blocked"
    validate_state_invariants(state)


def next_action(
    state: dict[str, Any],
    *,
    enforce_discovery_budget: bool = True,
) -> dict[str, Any] | None:
    validate_state_invariants(state)
    if state.get("status") != "running":
        return None
    if enforce_discovery_budget and any(
        not partition["discoveryComplete"] for partition in state["partitions"]
    ):
        if discovery_budget_exhausted(state):
            block_discovery_budget(state)
            return None
    limits = state["limits"]
    for partition in state["partitions"]:
        if not partition["discoveryComplete"]:
            action_id = f"discover-{partition['partitionId']}-{len(partition['sessions'])}"
            return {
                "actionId": action_id,
                "kind": "discovery",
                "partitionId": partition["partitionId"],
                "limit": limits["discoveryPageSize"],
                "outputPath": artifact_path(state, action_id),
                "sql": build_discovery_query(
                    repository=state["scope"]["repository"],
                    start=partition["start"],
                    end=partition["end"],
                    limit=limits["discoveryPageSize"],
                ),
            }
    for partition in state["partitions"]:
        for batch in partition["batches"]:
            if batch["status"] == "metadata":
                action_id = f"metadata-{batch['batchId']}"
                return {
                    "actionId": action_id,
                    "kind": "metadata",
                    "partitionId": partition["partitionId"],
                    "batchId": batch["batchId"],
                    "limit": len(batch["sessionIds"]),
                    "outputPath": artifact_path(state, action_id),
                    "sql": build_metadata_query(
                        session_ids=batch["sessionIds"],
                        limit=len(batch["sessionIds"]),
                    ),
                }
            if batch["status"] == "metadata-shutdown":
                action_id = f"metadata-shutdown-{batch['batchId']}"
                return {
                    "actionId": action_id,
                    "kind": "metadata-shutdown",
                    "partitionId": partition["partitionId"],
                    "batchId": batch["batchId"],
                    "limit": 1,
                    "outputPath": artifact_path(state, action_id),
                    "sql": build_shutdown_query(
                        session_id=batch["sessionIds"][0],
                        start=partition["start"],
                        end=partition["end"],
                    ),
                }
            if batch["status"] == "metadata-events":
                action_id = f"metadata-events-{batch['batchId']}"
                return {
                    "actionId": action_id,
                    "kind": "metadata-events",
                    "partitionId": partition["partitionId"],
                    "batchId": batch["batchId"],
                    "limit": 1,
                    "outputPath": artifact_path(state, action_id),
                    "sql": build_event_metadata_query(
                        session_id=batch["sessionIds"][0],
                        start=batch["fallbackStart"],
                        end=batch["fallbackEnd"],
                    ),
                }
            if batch["status"] == "refs":
                page = len(batch["refsArtifacts"])
                action_id = paged_action_id("refs", batch, page)
                return {
                    "actionId": action_id,
                    "kind": "refs",
                    "partitionId": partition["partitionId"],
                    "batchId": batch["batchId"],
                    "limit": batch["pageSize"],
                    "outputPath": artifact_path(state, action_id),
                    "sql": build_refs_query(
                        session_ids=batch["sessionIds"],
                        start=batch["sourceStart"],
                        end=batch["sourceEnd"],
                        limit=batch["pageSize"],
                        cursor=batch.get("refsCursor"),
                    ),
                }
            if batch["status"] == "files":
                page = len(batch["filesArtifacts"])
                action_id = paged_action_id("files", batch, page)
                return {
                    "actionId": action_id,
                    "kind": "files",
                    "partitionId": partition["partitionId"],
                    "batchId": batch["batchId"],
                    "limit": batch["pageSize"],
                    "outputPath": artifact_path(state, action_id),
                    "sql": build_files_query(
                        session_ids=batch["sessionIds"],
                        start=batch["sourceStart"],
                        end=batch["sourceEnd"],
                        limit=batch["pageSize"],
                        cursor=batch.get("filesCursor"),
                    ),
                }
            if batch["status"] == "tools":
                cursor = batch.get("toolCursor") or {}
                page = len(batch["toolArtifacts"])
                action_id = paged_action_id("tools", batch, page)
                return {
                    "actionId": action_id,
                    "kind": "tool-calls",
                    "partitionId": partition["partitionId"],
                    "batchId": batch["batchId"],
                    "limit": batch["pageSize"],
                    "outputPath": artifact_path(state, action_id),
                    "strategy": batch["toolStrategy"],
                    "sql": (
                        build_event_tool_calls_query(
                            session_id=batch["sessionIds"][0],
                            start=batch["sourceStart"],
                            end=batch["sourceEnd"],
                            limit=batch["pageSize"],
                            after_tool_call_id=cursor.get("toolCallId"),
                        )
                        if batch["toolStrategy"] == "events"
                        else build_tool_calls_query(
                            session_ids=batch["sessionIds"],
                            start=batch["sourceStart"],
                            end=batch["sourceEnd"],
                            limit=batch["pageSize"],
                            after_session_id=cursor.get("sessionId"),
                            after_tool_call_id=cursor.get("toolCallId"),
                        )
                    ),
                }
        partition["status"] = "complete"
    finalize_extraction(state)
    return None


def find_partition(state: dict[str, Any], partition_id_value: str) -> dict[str, Any]:
    for partition in state["partitions"]:
        if partition["partitionId"] == partition_id_value:
            return partition
    raise ValueError(f"unknown partition: {partition_id_value}")


def find_batch(partition: dict[str, Any], batch_id_value: str) -> dict[str, Any]:
    for batch in partition["batches"]:
        if batch["batchId"] == batch_id_value:
            return batch
    raise ValueError(f"unknown batch: {batch_id_value}")


def make_batches(session_ids: list[str], size: int, tool_page_size: int) -> list[dict[str, Any]]:
    return [
        {
            "batchId": batch_id(group),
            "sessionIds": group,
            "status": "metadata",
            "metadataArtifact": None,
            "metadataStrategy": "sessions",
            "fallbackStart": None,
            "fallbackEnd": None,
            "sourceStart": None,
            "sourceEnd": None,
            "refsArtifacts": [],
            "refsCursor": None,
            "filesArtifacts": [],
            "filesCursor": None,
            "toolArtifacts": [],
            "toolCursor": None,
            "toolStrategy": "tool_requests",
            "pageSize": tool_page_size,
            "retryGenerations": {
                "refs": 0,
                "files": 0,
                "tools": 0,
            },
        }
        for offset in range(0, len(session_ids), size)
        if (group := session_ids[offset : offset + size])
    ]


def validate_result(
    state: dict[str, Any],
    action: dict[str, Any],
    result_path: str,
) -> tuple[list[Any], int]:
    path = Path(result_path)
    artifact_bytes = path.stat().st_size
    if artifact_bytes > state["limits"]["maxArtifactBytes"]:
        raise OverflowError("artifact_bytes_exceeded")
    rows = read_json(path)
    if not isinstance(rows, list):
        raise ValueError("query result must be a JSON array")
    if len(rows) > state["limits"]["maxRows"]:
        raise OverflowError("row_limit_exceeded")
    if action["kind"] in {
        "discovery",
        "metadata",
        "metadata-shutdown",
        "metadata-events",
        "refs",
        "files",
        "tool-calls",
    } and len(rows) > action["limit"] + 1:
        raise ValueError("query returned more rows than its limit sentinel")
    return rows, artifact_bytes


def record_attempt(
    state: dict[str, Any],
    *,
    rows: int = 0,
    tool_calls: int = 0,
    artifact_bytes: int = 0,
) -> None:
    counters = work_counters(state)
    counters["queryAttempts"] += 1
    counters["rows"] += rows
    counters["toolCalls"] += tool_calls
    counters["artifactBytes"] += artifact_bytes


def record_success(
    state: dict[str, Any],
    action: dict[str, Any],
    result_path: str,
) -> None:
    if action["actionId"] in state["handledActionIds"]:
        return
    artifact_bytes = Path(result_path).stat().st_size
    record_attempt(state, artifact_bytes=artifact_bytes)
    rows, _ = validate_result(state, action, result_path)
    counters = work_counters(state)
    counters["rows"] += len(rows)
    if action["kind"] == "tool-calls":
        counters["toolCalls"] += len(rows)
    counters["successfulQueries"] += 1
    partition = find_partition(state, action["partitionId"])
    if action["kind"] == "discovery":
        if len(rows) > action["limit"]:
            if split_partition(state, partition, "discovery_partition_overflow"):
                state["handledActionIds"].append(action["actionId"])
                return
            state["blockers"].append(
                {
                    "actionId": action["actionId"],
                    "kind": action["kind"],
                    "reason": "discovery_partition_too_dense",
                }
            )
            state["handledActionIds"].append(action["actionId"])
            state["status"] = "blocked"
            validate_state_invariants(state)
            return
        existing_session_ids = {
            str(session["session_id"])
            for existing_partition in state["partitions"]
            for session in existing_partition["sessions"]
            if isinstance(session, dict) and session.get("session_id")
        }
        accepted = [
            row
            for row in rows
            if isinstance(row, dict)
            and row.get("session_id")
            and str(row["session_id"]) not in existing_session_ids
        ]
        partition["sessions"].extend(accepted)
        session_ids = [str(row["session_id"]) for row in accepted]
        partition["batches"].extend(
            make_batches(
                session_ids,
                state["limits"]["sessionBatchSize"],
                state["limits"]["toolPageSize"],
            )
        )
        partition["discoveryComplete"] = True
        partition["status"] = "extracting" if partition["batches"] else "complete"
        state["handledActionIds"].append(action["actionId"])
        return

    batch = find_batch(partition, action["batchId"])
    if action["kind"] == "metadata-shutdown":
        if not rows:
            raise ValueError("metadata shutdown fallback found no shutdown")
        completed_at = rows[0].get("completed_at") if isinstance(rows[0], dict) else None
        if not isinstance(completed_at, str):
            raise ValueError("metadata shutdown fallback requires completed_at")
        completed = parse_timestamp(completed_at)
        fallback_start = completed.replace(hour=0, minute=0, second=0, microsecond=0)
        if fallback_start == completed:
            fallback_start -= timedelta(days=1)
        batch["fallbackStart"] = timestamp_text(fallback_start)
        batch["fallbackEnd"] = completed_at
        batch["status"] = "metadata-events"
        state["handledActionIds"].append(action["actionId"])
        return

    if action["kind"] in {"metadata", "metadata-events"}:
        expected = set(batch["sessionIds"])
        actual = {str(row.get("session_id")) for row in rows if isinstance(row, dict)}
        if actual != expected:
            raise ValueError("metadata result does not exactly cover the session batch")
        created_at_values = [
            str(row.get("created_at"))
            for row in rows
            if isinstance(row, dict) and row.get("created_at")
        ]
        updated_at_values = [
            str(row.get("updated_at"))
            for row in rows
            if isinstance(row, dict) and row.get("updated_at")
        ]
        if len(created_at_values) != len(rows) or len(updated_at_values) != len(rows):
            raise ValueError("metadata result requires created_at and updated_at for every session")
        batch["metadataArtifact"] = result_path
        batch["metadataStrategy"] = (
            "shutdown_day_events" if action["kind"] == "metadata-events" else "sessions"
        )
        batch["sourceStart"] = min(created_at_values, key=parse_timestamp)
        latest_update = max(updated_at_values, key=parse_timestamp)
        batch["sourceEnd"] = timestamp_text(
            min(
                parse_timestamp(partition["end"]),
                parse_timestamp(latest_update) + timedelta(minutes=1),
            )
        )
        batch["status"] = "refs"
        state["handledActionIds"].append(action["actionId"])
        return

    accepted = rows[: action["limit"]]
    if accepted:
        accepted_path = str(
            Path(state["runDir"]) / "extraction" / f"{action['actionId']}.accepted.json"
        )
        write_json(accepted_path, accepted)
        artifact_key = {
            "refs": "refsArtifacts",
            "files": "filesArtifacts",
            "tool-calls": "toolArtifacts",
        }[action["kind"]]
        batch[artifact_key].append(accepted_path)
    if len(rows) > action["limit"]:
        last = accepted[-1]
        if action["kind"] == "refs":
            batch["refsCursor"] = {
                "sessionId": last["session_id"],
                "turnIndex": last["turn_index"],
                "refType": last["ref_type"],
                "refValue": last["ref_value"],
            }
        elif action["kind"] == "files":
            batch["filesCursor"] = {
                "sessionId": last["session_id"],
                "turnIndex": last["turn_index"],
                "filePath": last["file_path"],
                "toolName": last["tool_name"],
            }
        else:
            batch["toolCursor"] = {
                "sessionId": str(last["session_id"]),
                "toolCallId": str(last["tool_call_id"]),
            }
        state["handledActionIds"].append(action["actionId"])
        return
    if action["kind"] == "refs":
        batch["status"] = "files"
    elif action["kind"] == "files":
        batch["status"] = "tools"
    else:
        batch["status"] = "complete"
    if partition["discoveryComplete"] and all(
        item["status"] == "complete" for item in partition["batches"]
    ):
        partition["status"] = "complete"
    state["handledActionIds"].append(action["actionId"])


def split_partition(state: dict[str, Any], partition: dict[str, Any], reason: str) -> bool:
    start = parse_timestamp(partition["start"])
    end = parse_timestamp(partition["end"])
    if end - start < timedelta(minutes=state["limits"]["minWindowMinutes"]) * 2:
        return False
    midpoint = start + (end - start) / 2
    midpoint_text = timestamp_text(midpoint)
    children = [
        {
            "partitionId": partition_id(partition["start"], midpoint_text),
            "start": partition["start"],
            "end": midpoint_text,
            "status": "discovering",
            "discoveryComplete": False,
            "discoveryCursor": None,
            "sessions": [],
            "batches": [],
        },
        {
            "partitionId": partition_id(midpoint_text, partition["end"]),
            "start": midpoint_text,
            "end": partition["end"],
            "status": "discovering",
            "discoveryComplete": False,
            "discoveryCursor": None,
            "sessions": [],
            "batches": [],
        },
    ]
    index = state["partitions"].index(partition)
    state["partitions"][index : index + 1] = children
    state["retryHistory"].append(
        {"kind": "split_time", "partitionId": partition["partitionId"], "reason": reason}
    )
    return True


def split_batch(state: dict[str, Any], partition: dict[str, Any], batch: dict[str, Any], reason: str) -> bool:
    session_ids = batch["sessionIds"]
    if len(session_ids) > 1:
        midpoint = len(session_ids) // 2
        replacements = make_batches(
            session_ids[:midpoint],
            len(session_ids),
            batch["pageSize"],
        ) + make_batches(
            session_ids[midpoint:],
            len(session_ids),
            batch["pageSize"],
        )
        index = partition["batches"].index(batch)
        partition["batches"][index : index + 1] = replacements
        state["retryHistory"].append(
            {"kind": "split_batch", "batchId": batch["batchId"], "reason": reason}
        )
        return True
    if batch["status"] in {"refs", "files", "tools"} and batch["pageSize"] > 1:
        batch["pageSize"] = max(1, batch["pageSize"] // 2)
        cursor_key = f"{batch['status']}Cursor" if batch["status"] != "tools" else "toolCursor"
        artifacts_key = (
            f"{batch['status']}Artifacts" if batch["status"] != "tools" else "toolArtifacts"
        )
        batch[cursor_key] = None
        batch[artifacts_key] = []
        generations = batch.setdefault(
            "retryGenerations",
            {"refs": 0, "files": 0, "tools": 0},
        )
        generations[batch["status"]] += 1
        state["retryHistory"].append(
            {
                "kind": "reduce_evidence_page",
                "batchId": batch["batchId"],
                "stage": batch["status"],
                "pageSize": batch["pageSize"],
                "retryGeneration": generations[batch["status"]],
                "reason": reason,
            }
        )
        return True
    if (
        state["limits"]["enableTargetedFallback"]
        and batch["status"] == "tools"
        and batch["toolStrategy"] == "tool_requests"
    ):
        batch["toolStrategy"] = "events"
        batch["toolCursor"] = None
        batch["toolArtifacts"] = []
        batch["retryGenerations"]["tools"] += 1
        fallback = {
            "kind": "tool_events_fallback",
            "batchId": batch["batchId"],
            "sessionHash": session_hash(state, batch["sessionIds"][0]),
            "reason": reason,
        }
        state["strategyHistory"].append(fallback)
        state["retryHistory"].append(fallback)
        return True
    if state["limits"]["enableTargetedFallback"] and batch["status"] == "metadata":
        batch["status"] = "metadata-shutdown"
        fallback = {
            "kind": "metadata_shutdown_day_fallback",
            "batchId": batch["batchId"],
            "sessionHash": session_hash(state, batch["sessionIds"][0]),
            "reason": reason,
        }
        state["strategyHistory"].append(fallback)
        state["retryHistory"].append(fallback)
        return True
    return False


def record_failure(
    state: dict[str, Any],
    action: dict[str, Any],
    reason: str,
    *,
    count_attempt: bool = True,
    allow_retry: bool = True,
    error_kind: str = "auto",
) -> None:
    if action["actionId"] in state["handledActionIds"]:
        return
    if count_attempt:
        record_attempt(state)
    work_counters(state)["failedQueries"] += 1
    partition = find_partition(state, action["partitionId"])
    resolved_error_kind = classify_error(reason) if error_kind == "auto" else error_kind
    if action["kind"] == "discovery":
        work_counters(state).setdefault("discoveryFailures", 0)
        work_counters(state)["discoveryFailures"] += 1
        if discovery_budget_exhausted(state):
            state["handledActionIds"].append(action["actionId"])
            block_discovery_budget(
                state,
                action_id=action["actionId"],
                last_reason=reason,
            )
            return
    retries = [
        item
        for item in state["retryHistory"]
        if item.get("kind") == "retry_same_unit"
        and item.get("actionId") == action["actionId"]
    ]
    retry_limit = retry_limit_for(state, action, resolved_error_kind)
    if allow_retry and len(retries) < retry_limit:
        state["retryHistory"].append(
            {
                "kind": "retry_same_unit",
                "actionId": action["actionId"],
                "partitionId": action["partitionId"],
                "retryAttempt": len(retries) + 1,
                "errorKind": resolved_error_kind,
                "reason": reason,
            }
        )
        return
    if action["kind"] == "discovery" and resolved_error_kind in {
        "authorization",
        "syntax",
        "schema",
        "validation",
        "other",
    }:
        state["blockers"].append(
            {
                "actionId": action["actionId"],
                "kind": action["kind"],
                "errorKind": resolved_error_kind,
                "reason": reason,
            }
        )
        state["handledActionIds"].append(action["actionId"])
        state["status"] = "blocked"
        validate_state_invariants(state)
        return
    recovered = False
    if action["kind"] == "discovery":
        recovered = split_partition(state, partition, reason)
    else:
        recovered = split_batch(
            state,
            partition,
            find_batch(partition, action["batchId"]),
            reason,
        )
    if recovered:
        state["handledActionIds"].append(action["actionId"])
        return
    if state["limits"]["allowPartial"] and action["kind"] != "discovery":
        batch = find_batch(partition, action["batchId"])
        batch["status"] = "omitted"
        state["omittedUnits"].append(
            {
                "actionId": action["actionId"],
                "kind": action["kind"],
                "partitionId": action["partitionId"],
                "batchId": batch["batchId"],
                "sessionHashes": [
                    session_hash(state, str(session_id))
                    for session_id in batch["sessionIds"]
                ],
                "reason": reason,
            }
        )
        state["handledActionIds"].append(action["actionId"])
        return
    blocker = {
        "actionId": action["actionId"],
        "kind": action["kind"],
        "reason": reason,
    }
    state["blockers"].append(blocker)
    state["handledActionIds"].append(action["actionId"])
    state["status"] = "blocked"
    validate_state_invariants(state)


def classify_error(reason: str) -> str:
    normalized = reason.lower()
    if any(token in normalized for token in ("unauthorized", "forbidden", "permission", "access denied")):
        return "authorization"
    if any(token in normalized for token in ("rate limit", "too many requests", "429")):
        return "rate-limit"
    if any(
        token in normalized
        for token in (
            "timed out",
            "timeout",
            "deadline exceeded",
            "maximum allowed runtime",
            "runtime limit",
        )
    ):
        return "timeout"
    if any(token in normalized for token in ("connection", "network", "temporarily unavailable")):
        return "network"
    if any(token in normalized for token in ("500", "502", "503", "504", "server error")):
        return "server"
    if any(token in normalized for token in ("syntax", "parser error", "parse error")):
        return "syntax"
    if any(token in normalized for token in ("schema", "column", "table", "catalog")):
        return "schema"
    if any(token in normalized for token in ("validation", "invalid", "malformed")):
        return "validation"
    return "other"


def retry_limit_for(
    state: dict[str, Any],
    action: dict[str, Any],
    error_kind: str,
) -> int:
    if error_kind in {"authorization", "syntax", "schema", "validation", "other"}:
        return 0
    if action["kind"] == "discovery" and error_kind == "timeout":
        return 0
    configured_limit = state["limits"].get("maxQueryRetries", 1)
    if action["kind"] == "discovery":
        partition_retries = sum(
            1
            for item in state["retryHistory"]
            if item.get("kind") == "retry_same_unit"
            and item.get("partitionId") == action["partitionId"]
        )
        if partition_retries >= 1:
            return 0
    return min(configured_limit, 1)


def load_action(value: str, state: dict[str, Any]) -> dict[str, Any]:
    path = Path(value)
    if path.is_file():
        action = read_json(path)
        if not isinstance(action, dict):
            raise ValueError("action must be a JSON object")
        return action
    action = next_action(state, enforce_discovery_budget=False)
    if action is not None and action.get("actionId") == value:
        return action
    raise ValueError(f"action file or current action ID not found: {value}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init")
    init.add_argument("--out", required=True)
    init.add_argument("--repository", required=True)
    init.add_argument("--start", required=True)
    init.add_argument("--end", required=True)
    init.add_argument("--run-dir", required=True)
    init.add_argument("--discovery-page-size", type=int, default=100)
    init.add_argument("--session-batch-size", type=int, default=100)
    init.add_argument("--tool-page-size", type=int, default=500)
    init.add_argument("--max-rows", type=int, default=1000)
    init.add_argument("--max-artifact-bytes", type=int, default=10_000_000)
    init.add_argument("--min-window-minutes", type=int, default=15)
    init.add_argument("--max-query-retries", type=int, default=1)
    init.add_argument("--max-discovery-failures", type=int, default=4)
    init.add_argument("--max-discovery-minutes", type=int, default=5)
    init.add_argument("--enable-targeted-fallback", action="store_true")
    partial_mode = init.add_mutually_exclusive_group()
    partial_mode.add_argument("--allow-partial", dest="allow_partial", action="store_true")
    partial_mode.add_argument("--fail-on-omission", dest="allow_partial", action="store_false")
    init.set_defaults(allow_partial=True)

    next_parser = subparsers.add_parser("next")
    next_parser.add_argument("--state", required=True)
    next_parser.add_argument("--out")

    success = subparsers.add_parser("record-success")
    success.add_argument("--state", required=True)
    success.add_argument("--action", required=True)
    success.add_argument("--result", required=True)
    success.add_argument("--out", required=True)

    failure = subparsers.add_parser("record-failure")
    failure.add_argument("--state", required=True)
    failure.add_argument("--action", required=True)
    failure.add_argument("--reason", required=True)
    failure.add_argument(
        "--error-kind",
        choices=(
            "auto",
            "timeout",
            "network",
            "rate-limit",
            "server",
            "syntax",
            "schema",
            "validation",
            "authorization",
            "other",
        ),
        default="auto",
    )
    failure.add_argument("--out", required=True)

    args = parser.parse_args()
    try:
        if args.command == "init":
            write_json(args.out, initialize(args))
            return
        state = read_json(args.state)
        if not isinstance(state, dict):
            raise ValueError("state must be a JSON object")
        if args.command == "next":
            action = next_action(state)
            write_json(args.state, state)
            done = {"kind": "done", "status": state["status"]}
            if state["status"] == "blocked":
                done["blocker"] = state["blockers"][-1]
            if state["coverage"] is not None:
                done["coverage"] = state["coverage"]
            if state["status"] == "partial":
                done["omittedUnits"] = state["omittedUnits"]
            payload = action or done
            if args.out:
                write_json(args.out, payload)
            else:
                print(json.dumps(payload, indent=2))
            return
        action = load_action(args.action, state)
        if args.command == "record-success":
            try:
                record_success(state, action, args.result)
            except OverflowError as error:
                record_failure(
                    state,
                    action,
                    str(error),
                    count_attempt=False,
                    allow_retry=False,
                )
        else:
            record_failure(state, action, args.reason, error_kind=args.error_kind)
        write_json(args.out, state)
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
