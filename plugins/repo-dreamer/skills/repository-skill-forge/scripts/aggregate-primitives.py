#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. All rights reserved.

"""Merge incremental evidence, rebuild aggregates, and rank Forge candidates."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import timedelta
from typing import Any

from forge_common import parse_timestamp, read_json, stable_hash, timestamp_text, write_json


def reference_values(primitive: dict[str, Any], ref_type: str) -> set[str]:
    refs = primitive.get("refs")
    if not isinstance(refs, list):
        return set()
    return {
        str(ref.get("value"))
        for ref in refs
        if isinstance(ref, dict) and ref.get("type") == ref_type and ref.get("value")
    }


def merge_evidence(
    state: dict[str, Any] | None,
    document: dict[str, Any],
    repository: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    previous_scope = state.get("scope") if isinstance(state, dict) else None
    if isinstance(previous_scope, dict) and previous_scope.get("repository") != repository:
        raise ValueError("state repository does not match the current repository")
    evidence_by_key: dict[str, dict[str, Any]] = {}
    if isinstance(state, dict):
        for item in state.get("evidenceLedger", []):
            if isinstance(item, dict) and item.get("evidenceKey"):
                evidence_by_key[str(item["evidenceKey"])] = item
    for item in document.get("primitives", []):
        if isinstance(item, dict) and item.get("evidenceKey"):
            evidence_by_key[str(item["evidenceKey"])] = item
    history = state.get("proposalHistory", {}) if isinstance(state, dict) else {}
    return list(evidence_by_key.values()), history if isinstance(history, dict) else {}


def aggregate(
    evidence: list[dict[str, Any]],
    *,
    as_of: str,
    active_days: int,
    stale_days: int,
    merged_prs: set[str],
    thresholds: dict[str, float | int],
) -> list[dict[str, Any]]:
    now = parse_timestamp(as_of)
    active_start = now - timedelta(days=active_days)
    stale_start = now - timedelta(days=stale_days)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in evidence:
        completed_at = item.get("completedAt")
        if not isinstance(completed_at, str):
            continue
        if parse_timestamp(completed_at) < stale_start:
            continue
        grouped[str(item.get("fingerprint"))].append(item)

    patterns: list[dict[str, Any]] = []
    for fingerprint, items in grouped.items():
        active = [
            item
            for item in items
            if isinstance(item.get("completedAt"), str)
            and parse_timestamp(str(item["completedAt"])) >= active_start
        ]
        if not active:
            continue
        sessions = {str(item.get("sessionId")) for item in active if item.get("sessionId")}
        days = {str(item.get("day")) for item in active if item.get("day")}
        branches = {str(item.get("branchId")) for item in active if item.get("branchId")}
        branch_categories = {
            str(item.get("branchCategory")) for item in active if item.get("branchCategory")
        }
        surfaces = {str(item.get("surface")) for item in active if item.get("surface")}
        pr_refs = set().union(*(reference_values(item, "pr") for item in active))
        merged_refs = pr_refs & merged_prs
        successes = sum(item.get("outcome") == "success" for item in active)
        failures = sum(item.get("outcome") == "failure" for item in active)
        known = successes + failures
        unknown = len(active) - known
        success_rate = successes / known if known else 0.0
        scored_coverage = known / len(active) if active else 0.0
        mainline_count = sum(
            item.get("branchCategory") == "default"
            or bool(reference_values(item, "pr") & merged_prs)
            for item in active
        )
        leakage_findings = 0
        unresolved_conflicts = 0
        metrics = {
            "usageCount": len(active),
            "distinctSessionCount": len(sessions),
            "distinctDayCount": len(days),
            "distinctBranchCount": len(branches),
            "distinctSurfaceCount": len(surfaces),
            "knownOutcomeCount": known,
            "successCount": successes,
            "failureCount": failures,
            "unknownOutcomeCount": unknown,
            "successRate": success_rate,
            "scoredOutcomeCoverage": scored_coverage,
            "mergedPrCount": len(merged_refs),
            "mainlineCorroborationCount": mainline_count,
            "leakageFindingCount": leakage_findings,
            "unresolvedConflictCount": unresolved_conflicts,
        }
        reasons: list[str] = []
        if len(sessions) < thresholds["minDistinctSessions"]:
            reasons.append("insufficient_distinct_sessions")
        if len(days) < thresholds["minDistinctDays"]:
            reasons.append("insufficient_distinct_days")
        if known < thresholds["minKnownOutcomes"]:
            reasons.append("insufficient_known_outcomes")
        if success_rate < thresholds["minSuccessRate"]:
            reasons.append("low_success_rate")
        if scored_coverage < thresholds["minScoredCoverage"]:
            reasons.append("low_scored_outcome_coverage")
        if len(merged_refs) < thresholds["minMergedPrs"] and mainline_count < thresholds["minMainlineEvidence"]:
            reasons.append("insufficient_mainline_corroboration")

        path_families = sorted(
            {
                str(path)
                for item in active
                for path in (item.get("pathFamilies") if isinstance(item.get("pathFamilies"), list) else [])
            }
        )
        subject_key = path_families[0] if path_families else str(active[0].get("kind") or "workflow")
        candidate_id = stable_hash({"fingerprint": fingerprint, "subjectKey": subject_key}, 20)
        candidate_version = stable_hash(
            {
                "candidateId": candidate_id,
                "evidenceKeys": sorted(str(item["evidenceKey"]) for item in active),
                "metrics": metrics,
            },
            20,
        )
        patterns.append(
            {
                "candidateId": candidate_id,
                "candidateVersion": candidate_version,
                "fingerprint": fingerprint,
                "subjectKey": subject_key,
                "kind": active[0].get("kind"),
                "signature": active[0].get("signature"),
                "commandTemplates": sorted(
                    {str(item.get("commandTemplate")) for item in active if item.get("commandTemplate")}
                )[:5],
                "pathFamilies": path_families,
                "branchCategories": sorted(branch_categories),
                "surfaces": sorted(surfaces),
                "prRefs": sorted(pr_refs),
                "mergedPrRefs": sorted(merged_refs),
                "firstObservedAt": min(str(item["completedAt"]) for item in active),
                "lastObservedAt": max(str(item["completedAt"]) for item in active),
                "metrics": metrics,
                "promotion": {
                    "eligible": not reasons,
                    "holdReasons": reasons,
                },
                "evidenceKeys": sorted(str(item["evidenceKey"]) for item in active),
            }
        )
    patterns.sort(
        key=lambda item: (
            not item["promotion"]["eligible"],
            -item["metrics"]["distinctSessionCount"],
            -item["metrics"]["successRate"],
            item["candidateId"],
        )
    )
    return patterns


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="input", required=True)
    parser.add_argument("--out", dest="output", required=True)
    parser.add_argument("--state-in")
    parser.add_argument("--state-out", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--next-cursor", required=True)
    parser.add_argument("--merged-prs")
    parser.add_argument("--active-days", type=int, default=90)
    parser.add_argument("--stale-days", type=int, default=180)
    parser.add_argument("--min-distinct-sessions", type=int, default=3)
    parser.add_argument("--min-distinct-days", type=int, default=2)
    parser.add_argument("--min-known-outcomes", type=int, default=3)
    parser.add_argument("--min-success-rate", type=float, default=0.7)
    parser.add_argument("--min-scored-coverage", type=float, default=0.5)
    parser.add_argument("--min-merged-prs", type=int, default=2)
    parser.add_argument("--min-mainline-evidence", type=int, default=2)
    args = parser.parse_args()

    document = read_json(args.input)
    state = read_json(args.state_in) if args.state_in else None
    merged_pr_values = read_json(args.merged_prs) if args.merged_prs else []
    if not isinstance(document, dict):
        raise SystemExit("input must be a sanitized primitive document")
    if state is not None and not isinstance(state, dict):
        raise SystemExit("state must be a JSON object")
    if not isinstance(merged_pr_values, list):
        raise SystemExit("merged PR input must be a JSON array")
    if document.get("coverage", {}).get("truncated"):
        raise SystemExit("refusing to aggregate truncated evidence")

    try:
        evidence, proposal_history = merge_evidence(state, document, args.repository)
        thresholds = {
            "minDistinctSessions": args.min_distinct_sessions,
            "minDistinctDays": args.min_distinct_days,
            "minKnownOutcomes": args.min_known_outcomes,
            "minSuccessRate": args.min_success_rate,
            "minScoredCoverage": args.min_scored_coverage,
            "minMergedPrs": args.min_merged_prs,
            "minMainlineEvidence": args.min_mainline_evidence,
        }
        patterns = aggregate(
            evidence,
            as_of=args.as_of,
            active_days=args.active_days,
            stale_days=args.stale_days,
            merged_prs={str(value) for value in merged_pr_values},
            thresholds=thresholds,
        )
    except (TypeError, ValueError) as error:
        raise SystemExit(str(error)) from error

    candidates = [pattern for pattern in patterns if pattern["promotion"]["eligible"]]
    result = {
        "schemaVersion": 1,
        "scope": {
            "kind": "repository",
            "repository": args.repository,
            "asOf": args.as_of,
            "activeDays": args.active_days,
            "staleDays": args.stale_days,
        },
        "thresholds": thresholds,
        "userDiversity": document.get("userDiversity"),
        "coverage": document.get("coverage"),
        "usagePatterns": patterns,
        "candidates": candidates,
    }
    stale_start = parse_timestamp(args.as_of) - timedelta(days=args.stale_days)
    retained_evidence = [
        item
        for item in evidence
        if isinstance(item.get("completedAt"), str)
        and parse_timestamp(str(item["completedAt"])) >= stale_start
    ]
    next_state = {
        "schemaVersion": 1,
        "scope": {"kind": "repository", "repository": args.repository},
        "cursor": args.next_cursor,
        "updatedAt": timestamp_text(parse_timestamp(args.as_of)),
        "evidenceLedger": sorted(retained_evidence, key=lambda item: str(item.get("evidenceKey"))),
        "proposalHistory": proposal_history,
    }
    write_json(args.output, result)
    write_json(args.state_out, next_state)


if __name__ == "__main__":
    main()
