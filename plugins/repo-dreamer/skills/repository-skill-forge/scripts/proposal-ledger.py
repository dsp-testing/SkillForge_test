#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. All rights reserved.

"""Check and record idempotent repository Forge proposal publication."""

from __future__ import annotations

import argparse
from datetime import timedelta
from pathlib import Path
from typing import Any

from forge_common import parse_timestamp, read_json, timestamp_text, write_json

PUBLISHED_STATUSES = {"open", "published", "merged", "rejected"}
RECORD_STATUSES = PUBLISHED_STATUSES | {"blocked", "held"}
DECISIONS = {"create_skill", "improve_existing_skill", "merge_skills", "hold_as_pattern_only"}


def proposal_history(state: dict[str, Any]) -> dict[str, Any]:
    history = state.get("proposalHistory")
    if not isinstance(history, dict):
        raise ValueError("state proposalHistory must be an object")
    return history


def publication_check(
    state: dict[str, Any],
    *,
    candidate_id: str,
    candidate_version: str,
    now: str,
    cooldown_hours: float,
) -> dict[str, Any]:
    history = proposal_history(state)
    current = history.get(candidate_id)
    if isinstance(current, dict):
        current_status = current.get("status")
        current_version = current.get("candidateVersion")
        current_pr_ref = current.get("prRef")
        if current_version == candidate_version and current_status in PUBLISHED_STATUSES:
            return {"allowed": False, "reason": "duplicate_candidate_version"}
        if current_status == "open" and current_pr_ref:
            return {
                "allowed": True,
                "reason": "update_open_proposal",
                "action": "update",
                "existingPrRef": current_pr_ref,
                "existingStatus": current_status,
            }

    latest_publication = None
    for entry in history.values():
        if not isinstance(entry, dict) or entry.get("status") not in PUBLISHED_STATUSES:
            continue
        published_at = entry.get("publishedAt")
        if not isinstance(published_at, str):
            continue
        parsed = parse_timestamp(published_at)
        latest_publication = parsed if latest_publication is None else max(latest_publication, parsed)
    current_time = parse_timestamp(now)
    if latest_publication is not None and current_time < latest_publication + timedelta(hours=cooldown_hours):
        return {
            "allowed": False,
            "reason": "publication_cooldown",
            "nextAllowedAt": timestamp_text(latest_publication + timedelta(hours=cooldown_hours)),
        }
    return {"allowed": True, "reason": "eligible", "action": "create"}


def record(
    state: dict[str, Any],
    *,
    candidate_id: str,
    candidate_version: str,
    decision: str,
    status: str,
    published_at: str,
    pr_ref: str | None,
) -> dict[str, Any]:
    if decision not in DECISIONS:
        raise ValueError("invalid proposal decision")
    if status not in RECORD_STATUSES:
        raise ValueError("invalid proposal status")
    history = dict(proposal_history(state))
    current = history.get(candidate_id)
    if (
        isinstance(current, dict)
        and current.get("status") in PUBLISHED_STATUSES
        and status in {"blocked", "held"}
    ):
        history[candidate_id] = {
            **current,
            "lastEvaluation": {
                "candidateVersion": candidate_version,
                "decision": decision,
                "status": status,
                "evaluatedAt": published_at,
            },
        }
        return {**state, "proposalHistory": history, "updatedAt": published_at}
    if status in {"open", "published"} and not pr_ref:
        raise ValueError(f"proposal status {status} requires --pr-ref")
    history[candidate_id] = {
        "candidateVersion": candidate_version,
        "decision": decision,
        "status": status,
        "publishedAt": published_at,
        "prRef": pr_ref,
    }
    return {**state, "proposalHistory": history, "updatedAt": published_at}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    check = subcommands.add_parser("check")
    check.add_argument("--state", required=True, type=Path)
    check.add_argument("--candidate-id", required=True)
    check.add_argument("--candidate-version", required=True)
    check.add_argument("--now", required=True)
    check.add_argument("--cooldown-hours", type=float, required=True)
    check.add_argument("--out", required=True, type=Path)

    record_command = subcommands.add_parser("record")
    record_command.add_argument("--state", required=True, type=Path)
    record_command.add_argument("--candidate-id", required=True)
    record_command.add_argument("--candidate-version", required=True)
    record_command.add_argument("--decision", required=True)
    record_command.add_argument("--status", required=True)
    record_command.add_argument("--published-at", required=True)
    record_command.add_argument("--pr-ref")
    record_command.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    state = read_json(args.state)
    if not isinstance(state, dict):
        raise SystemExit("state must be a JSON object")
    try:
        if args.command == "check":
            result = publication_check(
                state,
                candidate_id=args.candidate_id,
                candidate_version=args.candidate_version,
                now=args.now,
                cooldown_hours=args.cooldown_hours,
            )
            write_json(args.out, result)
            raise SystemExit(0 if result["allowed"] else 1)
        next_state = record(
            state,
            candidate_id=args.candidate_id,
            candidate_version=args.candidate_version,
            decision=args.decision,
            status=args.status,
            published_at=args.published_at,
            pr_ref=args.pr_ref,
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error
    write_json(args.out, next_state)


if __name__ == "__main__":
    main()
