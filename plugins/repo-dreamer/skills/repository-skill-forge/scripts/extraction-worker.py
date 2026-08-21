#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. All rights reserved.

"""Drive repository Skill Forge extraction as a deterministic worker loop.

One `advance` call owns every non-tool step around a single `session_store_sql`
wave: it harvests the previously issued wave from the current session event
log, materializes each exact result, records every outcome through
`extraction-controller.py`, checkpoints newly completed batches, generates the
next bounded wave, and asserts terminal status. The model only executes the
tool calls listed in the emitted wave and reads a bounded progress summary.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shlex
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from forge_common import parse_timestamp, read_json, stable_hash, timestamp_text, write_json

PROTOCOL_VERSION = 1
WORKER_STATE_VERSION = 1
DEFAULT_MAIN_BRANCHES = ("main", "master")
SCRIPT_DIR = Path(__file__).resolve().parent


def load_script(name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


controller = load_script("forge_extraction_controller", "extraction-controller.py")
materializer = load_script("forge_materialize_session_query", "materialize-session-query.py")
checkpointer = load_script("forge_checkpoint_completed_batches", "checkpoint-completed-batches.py")


class WorkerLayout:
    """Run-local paths owned by one extraction run."""

    def __init__(self, run_dir: str | Path) -> None:
        self.run_dir = Path(run_dir).resolve()
        self.worker_dir = self.run_dir / "worker"
        self.state = self.run_dir / "extraction-state.json"
        self.ledger = self.run_dir / "primitives.sanitized.json"
        self.worker_state = self.worker_dir / "worker-state.json"
        self.wave = self.worker_dir / "wave.json"
        self.envelope = self.worker_dir / "envelope.json"
        self.coverage = self.worker_dir / "coverage.json"


class WorkerError(RuntimeError):
    """A worker precondition failed before any state was mutated."""


def read_state(layout: WorkerLayout) -> dict[str, Any]:
    if not layout.state.is_file():
        raise WorkerError(f"extraction state not found: {layout.state}")
    state = read_json(layout.state)
    if not isinstance(state, dict):
        raise WorkerError("extraction state must be a JSON object")
    return state


def save_state(layout: WorkerLayout, state: dict[str, Any]) -> None:
    write_json(layout.state, state)


def read_worker_state(layout: WorkerLayout) -> dict[str, Any]:
    if not layout.worker_state.is_file():
        raise WorkerError(
            f"worker state not found: {layout.worker_state}; run the start command first"
        )
    worker = read_json(layout.worker_state)
    if not isinstance(worker, dict):
        raise WorkerError("worker state must be a JSON object")
    return worker


def main_branches(worker: dict[str, Any]) -> set[str]:
    values = worker.get("mainBranches")
    if isinstance(values, list) and values:
        return {str(value) for value in values}
    return set(DEFAULT_MAIN_BRANCHES)


def progress(state: dict[str, Any]) -> dict[str, Any]:
    """Bounded counters only: never session IDs, SQL, rows, or artifact bodies."""
    partitions = [
        partition
        for partition in state.get("partitions", [])
        if isinstance(partition, dict)
    ]
    discovered = {
        str(session["session_id"])
        for partition in partitions
        for session in partition.get("sessions", [])
        if isinstance(session, dict) and session.get("session_id")
    }
    batches = [
        batch
        for partition in partitions
        for batch in partition.get("batches", [])
        if isinstance(batch, dict)
    ]
    completed = {
        str(session_id)
        for batch in batches
        if batch.get("status") == "complete"
        for session_id in batch.get("sessionIds", [])
    }
    omitted = {
        str(session_id)
        for batch in batches
        if batch.get("status") == "omitted"
        for session_id in batch.get("sessionIds", [])
    }
    counters = state.get("workCounters", {})
    omitted_units = [
        unit for unit in state.get("omittedUnits", []) if isinstance(unit, dict)
    ]
    omitted_kinds = sorted(
        {str(unit["kind"]) for unit in omitted_units if unit.get("kind")}
    )
    return {
        "status": str(state.get("status")),
        "discoveryComplete": (
            bool(partitions)
            and all(
                bool(partition.get("discoveryComplete")) for partition in partitions
            )
            and "discovery" not in omitted_kinds
        ),
        "partitionCount": len(partitions),
        "discoveredSessionCount": len(discovered),
        "completedSessionCount": len(completed),
        "omittedSessionCount": len(omitted),
        "batchCount": len(batches),
        "batchesComplete": sum(1 for batch in batches if batch.get("status") == "complete"),
        "batchesOmitted": sum(1 for batch in batches if batch.get("status") == "omitted"),
        "batchesPending": sum(
            1
            for batch in batches
            if batch.get("status") not in {"complete", "omitted"}
        ),
        "queryAttempts": int(counters.get("queryAttempts", 0)),
        "successfulQueries": int(counters.get("successfulQueries", 0)),
        "failedQueries": int(counters.get("failedQueries", 0)),
        "rows": int(counters.get("rows", 0)),
        "toolCalls": int(counters.get("toolCalls", 0)),
        "omittedUnitCount": len(omitted_units),
        "omittedUnitKinds": omitted_kinds,
        "blockerCount": len(state.get("blockers", [])),
    }


def bounded_coverage(coverage: Any) -> dict[str, Any] | None:
    """Drop the unbounded omission and fallback detail from run coverage."""
    if not isinstance(coverage, dict):
        return None
    return {
        key: value
        for key, value in coverage.items()
        if key not in {"omittedUnits", "fallbacks"}
    }


def bounded_blocker(blocker: Any) -> dict[str, Any] | None:
    if not isinstance(blocker, dict):
        return None
    return {
        "kind": blocker.get("kind"),
        "errorKind": blocker.get("errorKind"),
        "actionId": blocker.get("actionId"),
        "reason": str(blocker.get("reason", ""))[:400],
    }


def wave_action(action: dict[str, Any]) -> dict[str, Any]:
    """The minimum a model needs to issue one exact `session_store_sql` call."""
    return {
        "actionId": action["actionId"],
        "description": action["description"],
        "kind": action["kind"],
        "rowLimit": action.get("limit"),
        "query": action["sql"],
    }


def probe_action(
    action: dict[str, Any],
    events_root: Path,
    exclude_call_ids: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Resolve one issued action into a deterministic recording outcome."""
    action_id = str(action.get("actionId"))
    sql = action.get("sql")
    kind = action.get("kind")
    output_path = action.get("outputPath")
    if not isinstance(sql, str) or not isinstance(kind, str) or not isinstance(
        output_path, str
    ):
        return {
            "actionId": action_id,
            "outcome": "artifact",
            "message": "issued action is missing sql, kind, or outputPath",
        }
    probe = materializer.probe_result(events_root, sql, action_id, exclude_call_ids)
    state = probe["state"]
    if state == "missing":
        return {"actionId": action_id, "outcome": "pending"}
    if state == "handoff-mismatch":
        return {
            "actionId": action_id,
            "outcome": "handoff",
            "message": str(probe["message"]),
        }
    if state == "failed":
        return {
            "actionId": action_id,
            "outcome": "query-failure",
            "message": str(probe["message"]),
        }
    if state == "error":
        return {
            "actionId": action_id,
            "outcome": "artifact",
            "message": str(probe["message"]),
        }
    try:
        rows = materializer.materialize_content(kind, str(probe["content"]), output_path)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        return {"actionId": action_id, "outcome": "artifact", "message": str(error)}
    return {
        "actionId": action_id,
        "outcome": "success",
        "rowCount": rows,
        "resultPath": output_path,
    }


def harvest(
    actions: list[dict[str, Any]],
    events_root: Path,
    *,
    wait_seconds: float,
    poll_interval: float,
    excluded: dict[str, frozenset[str]] | None = None,
    sleep: Any = time.sleep,
    monotonic: Any = time.monotonic,
) -> list[dict[str, Any]]:
    """Probe every issued action, optionally waiting for in-flight tool calls."""
    boundary = excluded or {}
    resolved: dict[str, dict[str, Any]] = {}
    deadline = monotonic() + max(wait_seconds, 0.0)
    while True:
        for action in actions:
            action_id = str(action.get("actionId"))
            if action_id in resolved:
                continue
            outcome = probe_action(
                action,
                events_root,
                boundary.get(action_id, frozenset()),
            )
            if outcome["outcome"] != "pending":
                resolved[action_id] = outcome
        if len(resolved) == len(actions) or monotonic() >= deadline:
            break
        sleep(max(poll_interval, 0.0))
    return [
        resolved.get(
            str(action.get("actionId")),
            {"actionId": str(action.get("actionId")), "outcome": "pending"},
        )
        for action in actions
    ]


def wave_boundary(
    actions: list[dict[str, Any]],
    events_root: Path,
) -> dict[str, list[str]]:
    """Tool calls already completed per action when this wave is emitted.

    Recording this boundary is what stops a re-issued action, which keeps its
    exact action ID and SQL, from harvesting its own previous attempt.
    """
    return {
        str(action["actionId"]): materializer.observed_call_ids(
            events_root,
            str(action["sql"]),
            str(action["actionId"]),
        )
        for action in actions
        if action.get("actionId") and action.get("sql")
    }


def issued_boundary(worker: dict[str, Any]) -> dict[str, frozenset[str]]:
    recorded = worker.get("waveBoundary")
    if not isinstance(recorded, dict):
        return {}
    return {
        str(action_id): frozenset(str(value) for value in call_ids)
        for action_id, call_ids in recorded.items()
        if isinstance(call_ids, list)
    }


def record_outcome(
    state: dict[str, Any],
    action: dict[str, Any],
    outcome: dict[str, Any],
) -> dict[str, Any]:
    """Record one outcome and report what the controller actually recorded."""
    kind = outcome["outcome"]
    if kind == "success":
        try:
            controller.record_success(state, action, outcome["resultPath"])
        except OverflowError as error:
            controller.record_failure(
                state,
                action,
                str(error),
                count_attempt=False,
                allow_retry=False,
            )
            return {
                "actionId": outcome["actionId"],
                "outcome": "query-failure",
                "message": str(error),
            }
        except (KeyError, TypeError, ValueError) as error:
            controller.record_artifact_failure(state, action, str(error))
            return {
                "actionId": outcome["actionId"],
                "outcome": "artifact",
                "message": str(error),
            }
        return outcome
    if kind == "query-failure":
        controller.record_failure(state, action, outcome["message"], error_kind="auto")
    elif kind == "handoff":
        controller.record_failure(
            state,
            action,
            outcome["message"],
            error_kind="handoff",
        )
    elif kind == "artifact":
        controller.record_artifact_failure(state, action, outcome["message"])
    return outcome


def record_wave(
    state: dict[str, Any],
    actions: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Record successes before terminal failures, always by exact action ID."""
    by_id = {str(action.get("actionId")): action for action in actions}
    order = list(by_id)
    recorded: dict[str, dict[str, Any]] = {}
    for outcome in sorted(
        (outcome for outcome in outcomes if outcome["outcome"] != "pending"),
        key=lambda outcome: (
            outcome["outcome"] != "success",
            order.index(outcome["actionId"]),
        ),
    ):
        recorded[outcome["actionId"]] = record_outcome(
            state,
            by_id[outcome["actionId"]],
            outcome,
        )
    return [recorded.get(outcome["actionId"], outcome) for outcome in outcomes]


def run_checkpoint(
    layout: WorkerLayout,
    state: dict[str, Any],
    branches: set[str],
) -> dict[str, Any]:
    """Checkpoint completed batches, converting failure into a terminal blocker."""
    try:
        summary = checkpointer.checkpoint(
            state,
            ledger_path=layout.ledger,
            main_branches=branches,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        controller.record_checkpoint_failure(state, str(error))
        return {
            "checkpointedBatchCount": 0,
            "processedBatchCount": 0,
            "terminalCoverageAttached": False,
            "failed": True,
            "reason": str(error)[:400],
        }
    return {
        "checkpointedBatchCount": len(summary["checkpointedBatchIds"]),
        "processedBatchCount": len(summary["processedBatchIds"]),
        "terminalCoverageAttached": bool(summary["terminalCoverageAttached"]),
        "failed": False,
    }


def outcome_counts(outcomes: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "success": 0,
        "query-failure": 0,
        "handoff": 0,
        "artifact": 0,
        "pending": 0,
    }
    for outcome in outcomes:
        counts[outcome["outcome"]] = counts.get(outcome["outcome"], 0) + 1
    return counts


def recorded_summary(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "counts": outcome_counts(outcomes),
        "actions": [
            {
                "actionId": outcome["actionId"],
                "outcome": outcome["outcome"],
                **(
                    {"rowCount": outcome["rowCount"]}
                    if "rowCount" in outcome
                    else {}
                ),
                **(
                    {"message": str(outcome["message"])[:200]}
                    if "message" in outcome
                    else {}
                ),
            }
            for outcome in outcomes
        ],
    }


def terminal_envelope(
    layout: WorkerLayout,
    worker: dict[str, Any],
    state: dict[str, Any],
    recorded: dict[str, Any],
    checkpoint_summary: dict[str, Any],
) -> dict[str, Any]:
    summary = controller.terminal_summary(state)
    if state.get("coverage") is not None:
        write_json(layout.coverage, state["coverage"])
    envelope: dict[str, Any] = {
        "protocolVersion": PROTOCOL_VERSION,
        "kind": "terminal",
        "runId": worker["runId"],
        "cycle": int(worker["cycle"]),
        "status": summary["status"],
        "terminal": True,
        "recorded": recorded,
        "checkpoint": checkpoint_summary,
        "progress": progress(state),
        "ledgerPath": str(layout.ledger),
        "statePath": str(layout.state),
    }
    coverage = bounded_coverage(state.get("coverage"))
    if coverage is not None:
        envelope["coverage"] = coverage
        envelope["coveragePath"] = str(layout.coverage)
    if summary["status"] == "partial":
        envelope["omissionSummary"] = {
            "count": len(state["omittedUnits"]),
            "kinds": sorted(
                {
                    str(unit["kind"])
                    for unit in state["omittedUnits"]
                    if isinstance(unit, dict) and unit.get("kind")
                }
            ),
        }
    if summary["status"] == "blocked":
        envelope["blocker"] = bounded_blocker(state["blockers"][-1])
    return envelope


def wave_envelope(
    layout: WorkerLayout,
    worker: dict[str, Any],
    state: dict[str, Any],
    actions: list[dict[str, Any]],
    recorded: dict[str, Any],
    checkpoint_summary: dict[str, Any],
) -> dict[str, Any]:
    write_json(layout.wave, {"kind": "action-batch", "actions": actions})
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "kind": "wave",
        "runId": worker["runId"],
        "cycle": int(worker["cycle"]),
        "status": str(state["status"]),
        "terminal": False,
        "recorded": recorded,
        "checkpoint": checkpoint_summary,
        "progress": progress(state),
        "wave": {
            "manifestPath": str(layout.wave),
            "actionCount": len(actions),
            "actions": [wave_action(action) for action in actions],
        },
        "next": {
            "tool": "session_store_sql",
            "callCount": len(actions),
            "arguments": "wave.actions[].description and wave.actions[].query",
            "thenCommand": shlex.join(
                [
                    "python3",
                    str(SCRIPT_DIR / "extraction-worker.py"),
                    "advance",
                    "--run-dir",
                    str(layout.run_dir),
                ]
            ),
        },
    }


def emit(envelope: dict[str, Any], layout: WorkerLayout, out: str | None) -> None:
    write_json(layout.envelope, envelope)
    if out:
        write_json(out, envelope)
    print(json.dumps(envelope, indent=2))


def advance(
    layout: WorkerLayout,
    *,
    events_root: Path,
    wait_seconds: float,
    poll_interval: float,
) -> dict[str, Any]:
    """Perform every non-tool step around exactly one tool wave, atomically."""
    worker = read_worker_state(layout)
    state = read_state(layout)
    issued = [
        action
        for action in state.get("issuedActions", [])
        if isinstance(action, dict) and action.get("actionId")
    ]
    outcomes = (
        harvest(
            issued,
            events_root,
            wait_seconds=wait_seconds,
            poll_interval=poll_interval,
            excluded=issued_boundary(worker),
        )
        if issued
        else []
    )
    outcomes = record_wave(state, issued, outcomes)
    save_state(layout, state)

    branches = main_branches(worker)
    checkpoint_summary = run_checkpoint(layout, state, branches)
    save_state(layout, state)

    actions = controller.next_actions(
        state,
        int(state["limits"]["maxConcurrentBatches"]),
    )
    save_state(layout, state)

    worker["cycle"] = int(worker.get("cycle", 0)) + 1
    worker["toolCallsIssued"] = int(worker.get("toolCallsIssued", 0)) + len(actions)
    worker["waveHistory"] = [
        *worker.get("waveHistory", []),
        {"cycle": worker["cycle"], "actionCount": len(actions)},
    ][-200:]
    recorded = recorded_summary(outcomes)

    if actions:
        worker["waveBoundary"] = wave_boundary(actions, events_root)
        write_json(layout.worker_state, worker)
        return wave_envelope(
            layout,
            worker,
            state,
            actions,
            recorded,
            checkpoint_summary,
        )
    if not checkpoint_summary["failed"] and state["status"] in {"complete", "partial"}:
        checkpoint_summary = run_checkpoint(layout, state, branches)
        save_state(layout, state)
    worker["waveBoundary"] = {}
    write_json(layout.worker_state, worker)
    return terminal_envelope(layout, worker, state, recorded, checkpoint_summary)


def start(layout: WorkerLayout, args: argparse.Namespace) -> dict[str, Any]:
    if layout.state.exists():
        raise WorkerError(
            f"extraction state already exists: {layout.state}; use advance to resume"
        )
    window_end = timestamp_text(parse_timestamp(args.window_end))
    window_start = (
        timestamp_text(parse_timestamp(args.window_start))
        if args.window_start
        else timestamp_text(
            parse_timestamp(args.window_end) - timedelta(hours=args.window_hours)
        )
    )
    init = argparse.Namespace(
        repository=args.repository,
        start=window_start,
        end=window_end,
        run_dir=str(layout.run_dir),
        discovery_page_size=args.discovery_page_size,
        session_batch_size=args.session_batch_size,
        tool_page_size=args.tool_page_size,
        max_rows=args.max_rows,
        max_artifact_bytes=args.max_artifact_bytes,
        min_window_minutes=args.min_window_minutes,
        max_concurrent_batches=args.max_concurrent_batches,
        max_query_retries=args.max_query_retries,
        allow_partial=args.allow_partial,
        enable_tool_event_fallback=args.enable_tool_event_fallback,
    )
    state = controller.initialize(init)
    save_state(layout, state)
    write_json(
        layout.worker_state,
        {
            "schemaVersion": WORKER_STATE_VERSION,
            "protocolVersion": PROTOCOL_VERSION,
            "runId": args.run_id
            or stable_hash(
                {
                    "repository": args.repository,
                    "windowStart": window_start,
                    "windowEnd": window_end,
                },
                12,
            ),
            "repository": args.repository,
            "windowStart": window_start,
            "windowEnd": window_end,
            "mainBranches": sorted(set(args.main_branch)),
            "cycle": 0,
            "waveBoundary": {},
            "toolCallsIssued": 0,
            "waveHistory": [],
        },
    )
    return advance(
        layout,
        events_root=Path(args.events_root),
        wait_seconds=0.0,
        poll_interval=0.0,
    )


def status(layout: WorkerLayout) -> dict[str, Any]:
    worker = read_worker_state(layout)
    state = read_state(layout)
    envelope: dict[str, Any] = {
        "protocolVersion": PROTOCOL_VERSION,
        "kind": "status",
        "runId": worker["runId"],
        "cycle": int(worker.get("cycle", 0)),
        "status": str(state.get("status")),
        "progress": progress(state),
        "ledgerPath": str(layout.ledger),
        "statePath": str(layout.state),
        "pendingActionIds": [
            str(action["actionId"])
            for action in state.get("issuedActions", [])
            if isinstance(action, dict) and action.get("actionId")
        ],
    }
    try:
        summary = controller.terminal_summary(state)
    except ValueError as error:
        envelope["terminal"] = False
        envelope["assertion"] = {"ok": False, "reason": str(error)}
        return envelope
    envelope["terminal"] = True
    envelope["assertion"] = {"ok": True, "status": summary["status"]}
    coverage = bounded_coverage(state.get("coverage"))
    if coverage is not None:
        envelope["coverage"] = coverage
    if summary["status"] == "blocked":
        envelope["blocker"] = bounded_blocker(state["blockers"][-1])
    return envelope


def add_limit_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--discovery-page-size", type=int, default=500)
    parser.add_argument("--session-batch-size", type=int, default=25)
    parser.add_argument("--tool-page-size", type=int, default=1000)
    parser.add_argument("--max-rows", type=int, default=1000)
    parser.add_argument("--max-artifact-bytes", type=int, default=10_000_000)
    parser.add_argument("--min-window-minutes", type=int, default=15)
    parser.add_argument("--max-concurrent-batches", type=int, default=3)
    parser.add_argument("--max-query-retries", type=int, default=1)
    parser.add_argument("--enable-tool-event-fallback", action="store_true")
    partial_mode = parser.add_mutually_exclusive_group()
    partial_mode.add_argument("--allow-partial", dest="allow_partial", action="store_true")
    partial_mode.add_argument(
        "--fail-on-omission",
        dest="allow_partial",
        action="store_false",
    )
    parser.set_defaults(allow_partial=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    default_events_root = str(Path.home() / ".copilot" / "session-state")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("--run-dir", required=True)
    start_parser.add_argument("--repository", required=True)
    start_parser.add_argument("--window-end", required=True)
    start_parser.add_argument("--window-start")
    start_parser.add_argument("--window-hours", type=int, default=96)
    start_parser.add_argument("--run-id")
    start_parser.add_argument("--main-branch", action="append")
    start_parser.add_argument("--events-root", default=default_events_root)
    start_parser.add_argument("--out")
    add_limit_arguments(start_parser)

    advance_parser = subparsers.add_parser("advance")
    advance_parser.add_argument("--run-dir", required=True)
    advance_parser.add_argument("--events-root", default=default_events_root)
    advance_parser.add_argument("--wait", type=float, default=0.0)
    advance_parser.add_argument("--poll-interval", type=float, default=2.0)
    advance_parser.add_argument("--out")

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--run-dir", required=True)
    status_parser.add_argument("--assert-terminal", action="store_true")
    status_parser.add_argument("--out")

    return parser


def main() -> None:
    args = build_parser().parse_args()
    layout = WorkerLayout(args.run_dir)
    try:
        if args.command == "start":
            args.main_branch = args.main_branch or list(DEFAULT_MAIN_BRANCHES)
            envelope = start(layout, args)
        elif args.command == "advance":
            envelope = advance(
                layout,
                events_root=Path(args.events_root),
                wait_seconds=args.wait,
                poll_interval=args.poll_interval,
            )
        else:
            envelope = status(layout)
    except (
        WorkerError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        raise SystemExit(str(error)) from error
    emit(envelope, layout, args.out)
    if args.command == "status" and args.assert_terminal and not envelope["terminal"]:
        raise SystemExit(envelope["assertion"]["reason"])


if __name__ == "__main__":
    main()
