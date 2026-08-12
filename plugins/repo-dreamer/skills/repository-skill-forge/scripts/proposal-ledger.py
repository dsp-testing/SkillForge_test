#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. All rights reserved.

"""Check and record idempotent repository Forge proposal publication."""

from __future__ import annotations

import argparse
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

from forge_common import parse_timestamp, read_json, timestamp_text, write_json

PUBLISHED_STATUSES = {"open", "published", "closed", "merged", "rejected"}
RECORD_STATUSES = PUBLISHED_STATUSES | {"blocked", "held", "skipped"}
DECISIONS = {"create_skill", "improve_existing_skill", "merge_skills", "hold_as_pattern_only"}
LIVE_PR_STATUSES = {"none", "open", "closed", "merged"}
PROPOSAL_KEY_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def proposal_history(state: dict[str, Any]) -> dict[str, Any]:
    history = state.get("proposalHistory")
    if not isinstance(history, dict):
        raise ValueError("state proposalHistory must be an object")
    return history


def queue_sort_key(item: dict[str, Any]) -> tuple[int, str]:
    rank = item.get("rank")
    if not isinstance(rank, int) or rank < 0:
        raise ValueError("queued proposal rank must be a non-negative integer")
    return rank, str(item.get("proposalKey") or "")


def validate_queue_entry(item: dict[str, Any]) -> None:
    proposal_key = item.get("proposalKey")
    if not isinstance(proposal_key, str) or not PROPOSAL_KEY_PATTERN.fullmatch(proposal_key):
        raise ValueError("queued proposal has an invalid proposalKey")
    candidate_ids = item.get("candidateIds")
    if (
        not isinstance(candidate_ids, list)
        or not candidate_ids
        or any(not isinstance(value, str) or not value for value in candidate_ids)
    ):
        raise ValueError("queued proposal requires candidateIds")
    if len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError("queued proposal candidateIds must be unique")
    if not isinstance(item.get("proposalVersion"), str) or not item["proposalVersion"]:
        raise ValueError("queued proposal requires proposalVersion")
    if item.get("decision") not in DECISIONS:
        raise ValueError("queued proposal has an invalid decision")
    queue_sort_key(item)


def proposal_queue(state: dict[str, Any]) -> list[dict[str, Any]]:
    queue = state.get("proposalQueue", [])
    if not isinstance(queue, list) or any(not isinstance(item, dict) for item in queue):
        raise ValueError("state proposalQueue must be an array of objects")
    proposal_keys: set[str] = set()
    prior: tuple[int, str] | None = None
    for item in queue:
        validate_queue_entry(item)
        proposal_key = str(item["proposalKey"])
        if proposal_key in proposal_keys:
            raise ValueError("state proposalQueue contains duplicate proposal keys")
        proposal_keys.add(proposal_key)
        current = queue_sort_key(item)
        if prior is not None and current < prior:
            raise ValueError("state proposalQueue is not deterministically ordered")
        prior = current
    return queue


def enqueue(
    state: dict[str, Any],
    proposals: list[dict[str, Any]],
    *,
    now: str,
) -> dict[str, Any]:
    queued = {str(item["proposalKey"]): dict(item) for item in proposal_queue(state)}
    for proposal in proposals:
        validate_queue_entry(proposal)
        proposal_key = str(proposal["proposalKey"])
        current = queued.get(proposal_key)
        normalized = {
            "proposalKey": proposal_key,
            "candidateIds": sorted(set(str(value) for value in proposal["candidateIds"])),
            "proposalVersion": str(proposal["proposalVersion"]),
            "decision": str(proposal["decision"]),
            "rank": int(proposal["rank"]),
            "enqueuedAt": (
                current.get("enqueuedAt")
                if current and current.get("proposalVersion") == proposal["proposalVersion"]
                else now
            ),
        }
        if current and current.get("proposalVersion") == proposal["proposalVersion"]:
            normalized["reconciliation"] = current.get("reconciliation")
        queued[proposal_key] = normalized
    return {
        **state,
        "proposalQueue": sorted(queued.values(), key=queue_sort_key),
        "updatedAt": now,
    }


def reconcile_queue_entry(
    state: dict[str, Any],
    *,
    proposal_key: str,
    now: str,
    cooldown_hours: float,
    matching_pr_status: str,
    matching_pr_ref: str | None,
    matching_pr_draft: bool,
    materially_new_evidence: bool,
) -> dict[str, Any]:
    queue = [dict(item) for item in proposal_queue(state)]
    entry = next((item for item in queue if item.get("proposalKey") == proposal_key), None)
    if entry is None:
        raise ValueError("proposal key is not queued")
    entry["reconciliation"] = publication_check(
        state,
        proposal_key=proposal_key,
        candidate_ids=list(entry["candidateIds"]),
        proposal_version=str(entry["proposalVersion"]),
        decision=str(entry["decision"]),
        now=now,
        cooldown_hours=cooldown_hours,
        matching_pr_status=matching_pr_status,
        matching_pr_ref=matching_pr_ref,
        matching_pr_draft=matching_pr_draft,
        materially_new_evidence=materially_new_evidence,
    )
    return {**state, "proposalQueue": sorted(queue, key=queue_sort_key), "updatedAt": now}


def select_queue_mutation(state: dict[str, Any]) -> dict[str, Any]:
    queue = sorted((dict(item) for item in proposal_queue(state)), key=queue_sort_key)
    selected = next(
        (
            item
            for item in queue
            if isinstance(item.get("reconciliation"), dict)
            and item["reconciliation"].get("action") in {"create", "update"}
            and item["reconciliation"].get("allowed") is True
            and item.get("decision") != "hold_as_pattern_only"
        ),
        None,
    )
    return {
        "selection": selected,
        "mutationCount": 1 if selected else 0,
        "remainingProposalKeys": [
            str(item["proposalKey"])
            for item in queue
            if selected is None or item["proposalKey"] != selected["proposalKey"]
        ],
    }


def history_entry(
    history: dict[str, Any],
    *,
    proposal_key: str,
    candidate_ids: list[str],
) -> dict[str, Any] | None:
    current = history.get(proposal_key)
    if isinstance(current, dict):
        return current
    for candidate_id in candidate_ids:
        legacy = history.get(candidate_id)
        if isinstance(legacy, dict):
            return legacy
    return None


def cooldown_check(
    current: dict[str, Any] | None,
    *,
    now: str,
    cooldown_hours: float,
) -> dict[str, Any] | None:
    if not current or cooldown_hours <= 0:
        return None
    published_at = current.get("publishedAt")
    if not isinstance(published_at, str):
        return None
    next_allowed = parse_timestamp(published_at) + timedelta(hours=cooldown_hours)
    if parse_timestamp(now) >= next_allowed:
        return None
    return {
        "allowed": False,
        "reason": "proposal_cooldown",
        "action": "hold",
        "nextAllowedAt": timestamp_text(next_allowed),
    }


def publication_check(
    state: dict[str, Any],
    *,
    proposal_key: str,
    candidate_ids: list[str],
    proposal_version: str,
    decision: str,
    now: str,
    cooldown_hours: float,
    matching_pr_status: str,
    matching_pr_ref: str | None,
    matching_pr_draft: bool,
    materially_new_evidence: bool,
) -> dict[str, Any]:
    if not PROPOSAL_KEY_PATTERN.fullmatch(proposal_key):
        raise ValueError("proposal key must be lowercase letters, numbers, and single hyphens")
    if decision not in DECISIONS:
        raise ValueError("invalid proposal decision")
    if not candidate_ids or any(not candidate_id for candidate_id in candidate_ids):
        raise ValueError("at least one candidate ID is required")
    if not proposal_version:
        raise ValueError("proposal version is required")
    if matching_pr_status not in LIVE_PR_STATUSES:
        raise ValueError("invalid matching PR status")
    if decision == "hold_as_pattern_only":
        return {
            "allowed": False,
            "reason": "pattern_only_decision",
            "action": "hold",
        }
    history = proposal_history(state)
    current = history_entry(history, proposal_key=proposal_key, candidate_ids=candidate_ids)
    current_version = current.get("proposalVersion", current.get("candidateVersion")) if current else None
    current_pr_ref = matching_pr_ref or (current.get("prRef") if current else None)
    same_version = current_version == proposal_version

    if matching_pr_status == "none" and current_pr_ref:
        return {
            "allowed": False,
            "reason": "recorded_pr_requires_live_status",
            "action": "hold",
            "existingPrRef": current_pr_ref,
        }

    if matching_pr_status == "open":
        if not current_pr_ref:
            raise ValueError("an open matching PR requires --matching-pr-ref or a recorded PR reference")
        if same_version:
            return {
                "allowed": False,
                "reason": "unchanged_open_proposal",
                "action": "skip",
                "existingPrRef": current_pr_ref,
                "existingStatus": "open",
                "existingDraft": matching_pr_draft,
            }
        return {
            "allowed": True,
            "reason": "update_open_proposal",
            "action": "update",
            "existingPrRef": current_pr_ref,
            "existingStatus": "open",
            "existingDraft": matching_pr_draft,
        }

    if matching_pr_status == "closed":
        if same_version:
            return {
                "allowed": False,
                "reason": "unchanged_closed_proposal",
                "action": "skip",
                "existingPrRef": current_pr_ref,
            }
        if not materially_new_evidence:
            return {
                "allowed": False,
                "reason": "closed_proposal_requires_materially_new_evidence",
                "action": "hold",
                "existingPrRef": current_pr_ref,
            }

    if matching_pr_status == "merged":
        if same_version:
            return {
                "allowed": False,
                "reason": "unchanged_merged_proposal",
                "action": "skip",
                "existingPrRef": current_pr_ref,
            }
        if decision not in {"improve_existing_skill", "merge_skills"}:
            return {
                "allowed": False,
                "reason": "merged_proposal_requires_improvement_decision",
                "action": "hold",
                "existingPrRef": current_pr_ref,
            }
        if not materially_new_evidence:
            return {
                "allowed": False,
                "reason": "merged_proposal_requires_materially_new_evidence",
                "action": "hold",
                "existingPrRef": current_pr_ref,
            }

    if matching_pr_status in {"closed", "merged"}:
        cooldown = cooldown_check(current, now=now, cooldown_hours=cooldown_hours)
        if cooldown:
            return cooldown
    reason = {
        "none": "eligible_new_proposal",
        "closed": "eligible_replacement_proposal",
        "merged": "eligible_skill_improvement",
    }[matching_pr_status]
    return {"allowed": True, "reason": reason, "action": "create"}


def record(
    state: dict[str, Any],
    *,
    proposal_key: str,
    candidate_ids: list[str],
    proposal_version: str,
    decision: str,
    status: str,
    published_at: str,
    pr_ref: str | None,
    draft: bool,
) -> dict[str, Any]:
    if not PROPOSAL_KEY_PATTERN.fullmatch(proposal_key):
        raise ValueError("proposal key must be lowercase letters, numbers, and single hyphens")
    if decision not in DECISIONS:
        raise ValueError("invalid proposal decision")
    if not candidate_ids or any(not candidate_id for candidate_id in candidate_ids):
        raise ValueError("at least one candidate ID is required")
    if not proposal_version:
        raise ValueError("proposal version is required")
    if status not in RECORD_STATUSES:
        raise ValueError("invalid proposal status")
    history = dict(proposal_history(state))
    current = history_entry(history, proposal_key=proposal_key, candidate_ids=candidate_ids)
    for candidate_id in candidate_ids:
        history.pop(candidate_id, None)
    if (
        isinstance(current, dict)
        and current.get("status") in PUBLISHED_STATUSES
        and status in {"blocked", "held", "skipped"}
    ):
        current_version = current.get("proposalVersion", current.get("candidateVersion"))
        current_candidate_ids = current.get("candidateIds")
        history[proposal_key] = {
            "proposalKey": proposal_key,
            "candidateIds": (
                current_candidate_ids if isinstance(current_candidate_ids, list) else candidate_ids
            ),
            "proposalVersion": current_version,
            "decision": current.get("decision"),
            "status": current.get("status"),
            "publishedAt": current.get("publishedAt"),
            "prRef": current.get("prRef"),
            "draft": current.get("draft", draft),
            "lastEvaluation": {
                "candidateIds": candidate_ids,
                "proposalVersion": proposal_version,
                "decision": decision,
                "status": status,
                "evaluatedAt": published_at,
            },
        }
        return {
            **state,
            "proposalHistory": history,
            "proposalQueue": (
                [item for item in proposal_queue(state) if item.get("proposalKey") != proposal_key]
                if status == "skipped"
                else proposal_queue(state)
            ),
            "updatedAt": published_at,
        }
    if status in {"open", "published", "closed", "merged"} and not pr_ref:
        raise ValueError(f"proposal status {status} requires --pr-ref")
    history[proposal_key] = {
        "proposalKey": proposal_key,
        "candidateIds": candidate_ids,
        "proposalVersion": proposal_version,
        "decision": decision,
        "status": status,
        "publishedAt": published_at,
        "prRef": pr_ref,
        "draft": draft if status == "open" else False,
    }
    remove_from_queue = status in PUBLISHED_STATUSES or status == "skipped"
    return {
        **state,
        "proposalHistory": history,
        "proposalQueue": (
            [item for item in proposal_queue(state) if item.get("proposalKey") != proposal_key]
            if remove_from_queue
            else proposal_queue(state)
        ),
        "updatedAt": published_at,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    check = subcommands.add_parser("check")
    check.add_argument("--state", required=True, type=Path)
    check.add_argument("--proposal-key", required=True)
    check.add_argument("--candidate-id", action="append", required=True)
    check.add_argument("--proposal-version", required=True)
    check.add_argument("--decision", required=True)
    check.add_argument("--now", required=True)
    check.add_argument("--cooldown-hours", type=float, required=True)
    check.add_argument("--matching-pr-status", choices=sorted(LIVE_PR_STATUSES), required=True)
    check.add_argument("--matching-pr-ref")
    check.add_argument("--matching-pr-draft", action="store_true")
    check.add_argument("--materially-new-evidence", action="store_true")
    check.add_argument("--out", required=True, type=Path)

    record_command = subcommands.add_parser("record")
    record_command.add_argument("--state", required=True, type=Path)
    record_command.add_argument("--proposal-key", required=True)
    record_command.add_argument("--candidate-id", action="append", required=True)
    record_command.add_argument("--proposal-version", required=True)
    record_command.add_argument("--decision", required=True)
    record_command.add_argument("--status", required=True)
    record_command.add_argument("--published-at", required=True)
    record_command.add_argument("--pr-ref")
    record_command.add_argument("--draft", action="store_true")
    record_command.add_argument("--out", required=True, type=Path)

    enqueue_command = subcommands.add_parser("enqueue")
    enqueue_command.add_argument("--state", required=True, type=Path)
    enqueue_command.add_argument("--proposals", required=True, type=Path)
    enqueue_command.add_argument("--now", required=True)
    enqueue_command.add_argument("--out", required=True, type=Path)

    reconcile_command = subcommands.add_parser("reconcile")
    reconcile_command.add_argument("--state", required=True, type=Path)
    reconcile_command.add_argument("--proposal-key", required=True)
    reconcile_command.add_argument("--now", required=True)
    reconcile_command.add_argument("--cooldown-hours", type=float, required=True)
    reconcile_command.add_argument("--matching-pr-status", choices=sorted(LIVE_PR_STATUSES), required=True)
    reconcile_command.add_argument("--matching-pr-ref")
    reconcile_command.add_argument("--matching-pr-draft", action="store_true")
    reconcile_command.add_argument("--materially-new-evidence", action="store_true")
    reconcile_command.add_argument("--out", required=True, type=Path)

    select_command = subcommands.add_parser("select")
    select_command.add_argument("--state", required=True, type=Path)
    select_command.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    state = read_json(args.state)
    if not isinstance(state, dict):
        raise SystemExit("state must be a JSON object")
    try:
        if args.command == "enqueue":
            proposal_document = read_json(args.proposals)
            proposals = (
                proposal_document.get("proposals")
                if isinstance(proposal_document, dict)
                else proposal_document
            )
            if not isinstance(proposals, list) or any(not isinstance(item, dict) for item in proposals):
                raise ValueError("proposals must be an array of objects")
            write_json(args.out, enqueue(state, proposals, now=args.now))
            return
        if args.command == "reconcile":
            write_json(
                args.out,
                reconcile_queue_entry(
                    state,
                    proposal_key=args.proposal_key,
                    now=args.now,
                    cooldown_hours=args.cooldown_hours,
                    matching_pr_status=args.matching_pr_status,
                    matching_pr_ref=args.matching_pr_ref,
                    matching_pr_draft=args.matching_pr_draft,
                    materially_new_evidence=args.materially_new_evidence,
                ),
            )
            return
        if args.command == "select":
            write_json(args.out, select_queue_mutation(state))
            return
        if args.command == "check":
            result = publication_check(
                state,
                proposal_key=args.proposal_key,
                candidate_ids=args.candidate_id,
                proposal_version=args.proposal_version,
                decision=args.decision,
                now=args.now,
                cooldown_hours=args.cooldown_hours,
                matching_pr_status=args.matching_pr_status,
                matching_pr_ref=args.matching_pr_ref,
                matching_pr_draft=args.matching_pr_draft,
                materially_new_evidence=args.materially_new_evidence,
            )
            write_json(args.out, result)
            raise SystemExit(0 if result["allowed"] else 1)
        next_state = record(
            state,
            proposal_key=args.proposal_key,
            candidate_ids=args.candidate_id,
            proposal_version=args.proposal_version,
            decision=args.decision,
            status=args.status,
            published_at=args.published_at,
            pr_ref=args.pr_ref,
            draft=args.draft,
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error
    write_json(args.out, next_state)


if __name__ == "__main__":
    main()
