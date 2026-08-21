#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. All rights reserved.

"""Emit the Copilot coding agent completion verdict for a Skill Forge run.

The final line on stdout is the guard contract: ``{"status": "complete"}`` or
``{"status": "incomplete", "reason": ..., "continuePrompt": ...}``. Exit status
0 means complete and 1 means incomplete. The full diagnostic snapshot is written
to stderr. This script never writes to the run directory or the repository.
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse
import copy
import importlib.util
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import forge_marker
from forge_common import read_json

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
COMPLETE = "complete"
INCOMPLETE = "incomplete"
DEFAULT_MAX_REASON_CHARS = 1200
DEFAULT_MAX_PROMPT_CHARS = 2400
DEFAULT_MAX_PENDING_IDS = 20
TRUNCATION_SUFFIX = " ...[truncated]"


def load_controller() -> Any:
    spec = importlib.util.spec_from_file_location(
        "forge_extraction_controller",
        SCRIPT_DIR / "extraction-controller.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load extraction-controller.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bounded(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    if limit <= len(TRUNCATION_SUFFIX):
        return text[:limit]
    return text[: limit - len(TRUNCATION_SUFFIX)] + TRUNCATION_SUFFIX


def pending_text(snapshot: dict[str, Any], limit: int) -> str:
    identifiers = snapshot["actions"]["pendingActionIds"]
    if not identifiers:
        return "none"
    shown = identifiers[:limit] if limit > 0 else list(identifiers)
    rendered = ", ".join(shown)
    remaining = len(identifiers) - len(shown)
    return f"{rendered} (+{remaining} more)" if remaining else rendered


def progress_text(snapshot: dict[str, Any]) -> str:
    sessions = snapshot["sessions"]
    batches = snapshot["batches"]
    counters = snapshot["counters"]
    partitions = snapshot["partitions"]
    return (
        f"sessions {sessions['completed']}/{sessions['discovered']} completed, "
        f"batches {batches['complete']}/{batches['total']} complete, "
        f"discovery {partitions['discoveryComplete']}/{partitions['total']} partitions "
        f"resolved, {counters['successfulQueries']} successful and "
        f"{counters['failedQueries']} failed of {counters['queryAttempts']} query "
        f"attempts, {counters['artifactBytes']} artifact bytes, "
        f"{sessions['omittedUnits']} omitted units"
    )


def blocker_text(snapshot: dict[str, Any]) -> str:
    blocker = snapshot.get("blocker")
    if not isinstance(blocker, dict):
        return "no blocker recorded"
    fields = ("actionId", "kind", "errorKind", "reason")
    parts = [f"{field}={blocker[field]}" for field in fields if blocker.get(field)]
    return "; ".join(parts) if parts else json.dumps(blocker, sort_keys=True)


def verdict(
    status: str,
    name: str,
    reason: str,
    continue_prompt: str,
    *,
    snapshot: dict[str, Any] | None = None,
    marker: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "verdict": name,
        "reason": reason,
        "continuePrompt": continue_prompt,
        "snapshot": snapshot,
        "marker": marker,
    }


def marker_summary(marker: dict[str, Any], age: float | None, stale: bool) -> dict[str, Any]:
    return {
        "runId": marker.get("runId"),
        "repository": marker.get("repository"),
        "runDir": marker.get("runDir"),
        "statePath": marker.get("statePath"),
        "phase": marker.get("phase"),
        "revision": marker.get("revision"),
        "updatedAt": marker.get("updatedAt"),
        "ageSeconds": age,
        "stale": stale,
    }


def skill_dir_for(marker: dict[str, Any] | None) -> str:
    if marker:
        recorded = marker.get("skillDir")
        if isinstance(recorded, str) and recorded:
            return recorded
    return str(SKILL_DIR)


def running_prompt(marker: dict[str, Any], pending: str) -> str:
    skill = skill_dir_for(marker)
    state_path = marker["statePath"]
    run_dir = marker.get("runDir") or "$RUN_DIR"
    return (
        "Do not finish. Repository Skill Forge extraction state "
        f"{state_path} is still `running`, so a final response and run-directory "
        f"cleanup are forbidden. Pending action IDs: {pending}. Continue the "
        f"controller loop: run `python3 {skill}/scripts/extraction-controller.py next "
        f"--state {state_path} --parallel --out {run_dir}/actions.json`, execute every "
        "returned action, record every outcome with record-success, record-failure, or "
        "record-artifact-failure, run checkpoint-completed-batches.py, then run "
        f"`python3 {skill}/scripts/run-marker.py refresh`. Repeat until "
        f"`python3 {skill}/scripts/extraction-controller.py assert-terminal "
        f"--state {state_path}` succeeds."
    )


def blocked_prompt(marker: dict[str, Any]) -> str:
    skill = skill_dir_for(marker)
    state_path = marker["statePath"]
    return (
        "Do not report success and do not resume extraction. Repository Skill Forge "
        f"extraction state {state_path} is terminal `blocked`. Run "
        f"`python3 {skill}/scripts/extraction-controller.py assert-terminal "
        f"--state {state_path}`, then `python3 {skill}/scripts/run-marker.py finish`, "
        "and produce the final BLOCKED report quoting the blocker above verbatim."
    )


def repair_prompt(marker: dict[str, Any] | None, detail: str) -> str:
    skill = skill_dir_for(marker)
    return (
        f"Do not finish. {detail} Re-read the run directory, confirm the controller "
        f"state file exists, run `python3 {skill}/scripts/extraction-controller.py "
        "assert-terminal --state $RUN_DIR/extraction-state.json`, and refresh the run "
        f"marker with `python3 {skill}/scripts/run-marker.py refresh`. If the run "
        "genuinely finished, run `run-marker.py finish` and then `run-marker.py clear`."
    )


def missing_marker_verdict(marker_path: Path, require_marker: bool) -> dict[str, Any]:
    if not require_marker:
        return verdict(
            COMPLETE,
            "no-active-run",
            f"no repository Skill Forge run marker at {marker_path}; nothing to guard",
            "",
        )
    return verdict(
        INCOMPLETE,
        "marker-missing",
        (
            f"no repository Skill Forge run marker at {marker_path}, so extraction "
            "completion cannot be verified"
        ),
        (
            "Do not finish. Repository Skill Forge never initialised its run marker, so "
            "extraction completion cannot be verified. Initialise the controller, then "
            f"run `python3 {SKILL_DIR}/scripts/run-marker.py init --state "
            "$RUN_DIR/extraction-state.json` and continue the documented controller loop."
        ),
    )


def evaluate_status(
    *,
    controller: Any,
    state: dict[str, Any],
    marker: dict[str, Any],
    snapshot: dict[str, Any],
    stale: bool,
    max_pending_ids: int,
) -> dict[str, Any]:
    status = snapshot["status"]
    staleness = (
        f"; run marker was last refreshed at {marker.get('updatedAt')} and is stale"
        if stale
        else ""
    )
    try:
        summary = controller.terminal_summary(copy.deepcopy(state))
    except (KeyError, TypeError, ValueError) as error:
        if status == "running":
            pending = pending_text(snapshot, max_pending_ids)
            return verdict(
                INCOMPLETE,
                "extraction-running",
                (
                    "repository Skill Forge extraction is still running: "
                    f"{progress_text(snapshot)}; pending actions: {pending}; "
                    f"controller reported: {error}{staleness}"
                ),
                running_prompt(marker, pending),
                snapshot=snapshot,
            )
        return verdict(
            INCOMPLETE,
            "state-inconsistent",
            (
                f"repository Skill Forge extraction state {marker['statePath']} is "
                f"inconsistent with status {status!r}: {error}{staleness}"
            ),
            repair_prompt(
                marker,
                "The Skill Forge controller state failed its own invariant checks.",
            ),
            snapshot=snapshot,
        )
    if status == "blocked":
        return verdict(
            INCOMPLETE,
            "extraction-blocked",
            (
                "repository Skill Forge extraction is terminal `blocked`: "
                f"{blocker_text(snapshot)}; {progress_text(snapshot)}{staleness}"
            ),
            blocked_prompt(marker),
            snapshot=snapshot,
        )
    if status == "running":
        pending = pending_text(snapshot, max_pending_ids)
        return verdict(
            INCOMPLETE,
            "extraction-running",
            (
                "repository Skill Forge extraction is still running: "
                f"{progress_text(snapshot)}; pending actions: {pending}{staleness}"
            ),
            running_prompt(marker, pending),
            snapshot=snapshot,
        )
    if not snapshot["consistent"]:
        return verdict(
            INCOMPLETE,
            "state-inconsistent",
            (
                f"repository Skill Forge extraction state {marker['statePath']} "
                f"violates a controller invariant: {snapshot['invariantError']}"
            ),
            repair_prompt(
                marker,
                "The Skill Forge controller state failed its own invariant checks.",
            ),
            snapshot=snapshot,
        )
    return verdict(
        COMPLETE,
        "extraction-terminal",
        (
            f"repository Skill Forge extraction is terminal `{summary['status']}`: "
            f"{progress_text(snapshot)}"
        ),
        "",
        snapshot=snapshot,
    )


def evaluate(
    *,
    controller: Any,
    marker_path: Path,
    max_age_seconds: float,
    require_marker: bool,
    max_pending_ids: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not os.path.lexists(marker_path):
        return missing_marker_verdict(marker_path, require_marker)
    try:
        forge_marker.assert_private_directory(marker_path.parent)
    except forge_marker.MarkerError as error:
        return verdict(
            INCOMPLETE,
            "marker-untrusted",
            (
                f"repository Skill Forge run marker directory {marker_path.parent} "
                f"cannot be trusted: {error}"
            ),
            repair_prompt(
                None,
                f"The Skill Forge marker directory {marker_path.parent} is not private.",
            ),
        )
    try:
        marker = forge_marker.read_marker(marker_path)
    except FileNotFoundError:
        return missing_marker_verdict(marker_path, require_marker)
    except forge_marker.MarkerError as error:
        return verdict(
            INCOMPLETE,
            "marker-unreadable",
            f"repository Skill Forge run marker at {marker_path} is unusable: {error}",
            repair_prompt(None, f"The Skill Forge run marker at {marker_path} is unusable."),
        )

    age = forge_marker.marker_age_seconds(marker, now=now)
    stale = forge_marker.marker_is_stale(
        marker,
        max_age_seconds=max_age_seconds,
        now=now,
    )
    summary = marker_summary(marker, age, stale)
    state_path = Path(marker["statePath"])
    try:
        state = read_json(state_path)
        if not isinstance(state, dict):
            raise ValueError("extraction state must be a JSON object")
    except FileNotFoundError:
        name = "marker-stale" if stale else "state-missing"
        result = verdict(
            INCOMPLETE,
            name,
            (
                f"repository Skill Forge run marker {marker_path} points at missing "
                f"extraction state {state_path}"
                + (
                    f"; marker is stale after {age:.0f}s"
                    if stale and age is not None
                    else ""
                )
            ),
            repair_prompt(
                marker,
                f"The Skill Forge run marker points at missing state {state_path}.",
            ),
            marker=summary,
        )
        return result
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        return verdict(
            INCOMPLETE,
            "state-unreadable",
            (
                f"repository Skill Forge extraction state {state_path} is unreadable: "
                f"{error}"
            ),
            repair_prompt(
                marker,
                f"The Skill Forge controller state {state_path} is unreadable.",
            ),
            marker=summary,
        )

    try:
        snapshot = controller.diagnostics(copy.deepcopy(state))
    except (KeyError, TypeError, ValueError) as error:
        return verdict(
            INCOMPLETE,
            "state-inconsistent",
            (
                f"repository Skill Forge extraction state {state_path} could not be "
                f"summarized: {error}"
            ),
            repair_prompt(
                marker,
                f"The Skill Forge controller state {state_path} could not be summarized.",
            ),
            marker=summary,
        )

    if snapshot["status"] not in {"running", *controller.TERMINAL_STATUSES}:
        return verdict(
            INCOMPLETE,
            "state-unsupported",
            (
                f"repository Skill Forge extraction state {state_path} has unsupported "
                f"status {snapshot['status']!r}"
            ),
            repair_prompt(
                marker,
                "The Skill Forge controller state has an unsupported status.",
            ),
            snapshot=snapshot,
            marker=summary,
        )

    result = evaluate_status(
        controller=controller,
        state=state,
        marker=marker,
        snapshot=snapshot,
        stale=stale,
        max_pending_ids=max_pending_ids,
    )
    result["marker"] = summary
    return result


def contract_line(
    result: dict[str, Any],
    max_reason_chars: int,
    max_prompt_chars: int,
) -> dict[str, Any]:
    if result["status"] == COMPLETE:
        return {"status": COMPLETE}
    return {
        "status": INCOMPLETE,
        "reason": bounded(result["reason"], max_reason_chars),
        "continuePrompt": bounded(result["continuePrompt"], max_prompt_chars),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--marker")
    parser.add_argument("--marker-dir")
    parser.add_argument(
        "--max-age-seconds",
        type=float,
        default=forge_marker.DEFAULT_MAX_AGE_SECONDS,
    )
    parser.add_argument("--require-marker", action="store_true")
    parser.add_argument("--max-reason-chars", type=int, default=DEFAULT_MAX_REASON_CHARS)
    parser.add_argument("--max-prompt-chars", type=int, default=DEFAULT_MAX_PROMPT_CHARS)
    parser.add_argument("--max-pending-ids", type=int, default=DEFAULT_MAX_PENDING_IDS)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    marker_path: Path | None = None
    try:
        marker_path = forge_marker.resolve_marker_path(args.marker, args.marker_dir)
        result = evaluate(
            controller=load_controller(),
            marker_path=marker_path,
            max_age_seconds=args.max_age_seconds,
            require_marker=args.require_marker,
            max_pending_ids=args.max_pending_ids,
        )
    except forge_marker.MarkerError as error:
        result = verdict(
            INCOMPLETE,
            "marker-unresolvable",
            (
                "repository Skill Forge run marker location could not be resolved: "
                f"{error}"
            ),
            repair_prompt(
                None,
                "The Skill Forge marker location is not a usable absolute path.",
            ),
        )
    except Exception as error:  # noqa: BLE001 - the guard must always emit a verdict
        result = verdict(
            INCOMPLETE,
            "predicate-error",
            (
                "repository Skill Forge completion predicate failed before it could "
                f"verify extraction state: {type(error).__name__}: {error}"
            ),
            repair_prompt(
                None,
                "The Skill Forge completion predicate could not evaluate the run.",
            ),
        )

    if not args.quiet:
        print(
            json.dumps(
                {"markerPath": str(marker_path) if marker_path else None, **result},
                sort_keys=False,
            ),
            file=sys.stderr,
        )
    print(json.dumps(contract_line(result, args.max_reason_chars, args.max_prompt_chars)))
    raise SystemExit(0 if result["status"] == COMPLETE else 1)


if __name__ == "__main__":
    main()
