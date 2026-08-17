#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. All rights reserved.

"""Validate a repository Forge proposal before GitHub MCP publication."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from forge_common import read_json

DECISIONS = {"create_skill", "improve_existing_skill", "merge_skills", "hold_as_pattern_only"}
PUBLICATION_ACTIONS = {"create", "update", "skip", "hold"}
PROPOSAL_KEY_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def validate_extraction(document: dict[str, Any], errors: list[str]) -> None:
    extraction = document.get("extraction")
    if not isinstance(extraction, dict):
        errors.append("extraction coverage is required")
        return
    status = extraction.get("status")
    if status not in {"complete", "partial"}:
        errors.append("extraction status is invalid")
        return
    if status == "complete":
        return
    discovered = extraction.get("discoveredSessionCount")
    completed = extraction.get("completedSessionCount")
    coverage = extraction.get("sessionCoverage")
    discovery_complete = extraction.get("discoveryComplete")
    coverage_status = extraction.get("sessionCoverageStatus")
    omissions = extraction.get("omittedUnitCount")
    kinds = extraction.get("omittedUnitKinds")
    fallback_enabled = extraction.get("toolEventFallbackEnabled")
    if not isinstance(discovery_complete, bool):
        errors.append("partial extraction requires discoveryComplete")
    if not isinstance(discovered, int) or discovered < 0:
        errors.append("partial extraction requires a non-negative discoveredSessionCount")
    if not isinstance(completed, int) or completed < 0:
        errors.append("partial extraction requires a non-negative completedSessionCount")
    if (
        discovery_complete is True
        and isinstance(discovered, int)
        and isinstance(completed, int)
        and completed >= discovered
    ):
        errors.append("partial extraction completedSessionCount must be below discoveredSessionCount")
    if discovery_complete is False:
        if coverage is not None or coverage_status != "unknown":
            errors.append("partial discovery requires unknown sessionCoverage")
    else:
        if coverage_status != "known":
            errors.append("complete discovery requires known sessionCoverage")
        if not isinstance(coverage, (int, float)) or not 0 <= coverage < 1:
            errors.append("partial extraction requires sessionCoverage below 1")
        elif (
            isinstance(discovered, int)
            and discovered > 0
            and isinstance(completed, int)
            and abs(coverage - completed / discovered) > 1e-9
        ):
            errors.append("partial extraction sessionCoverage does not match its counts")
    if not isinstance(omissions, int) or omissions < 1:
        errors.append("partial extraction requires a positive omittedUnitCount")
    if (
        not isinstance(kinds, list)
        or not kinds
        or any(not isinstance(kind, str) or not kind for kind in kinds)
    ):
        errors.append("partial extraction requires omittedUnitKinds")
    if not isinstance(fallback_enabled, bool):
        errors.append("partial extraction requires toolEventFallbackEnabled")


def validate(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    decision = document.get("decision")
    if decision not in DECISIONS:
        errors.append("decision is invalid")
    proposal_key = document.get("proposalKey")
    if not isinstance(proposal_key, str) or not PROPOSAL_KEY_PATTERN.fullmatch(proposal_key):
        errors.append("proposalKey must be lowercase letters, numbers, and single hyphens")
    candidate_ids = document.get("candidateIds")
    if (
        not isinstance(candidate_ids, list)
        or not candidate_ids
        or any(not isinstance(candidate_id, str) or not candidate_id for candidate_id in candidate_ids)
        or len(set(candidate_ids)) != len(candidate_ids)
    ):
        errors.append("candidateIds must be a non-empty array of unique strings")
    if not document.get("proposalVersion"):
        errors.append("proposalVersion is required")
    validate_extraction(document, errors)
    review = document.get("review")
    if not isinstance(review, dict):
        errors.append("review is required")
    else:
        if review.get("leakageFindingCount") != 0:
            errors.append("proposal has leakage findings")
        if review.get("unresolvedConflictCount") != 0:
            errors.append("proposal has unresolved conflicts")
        if review.get("executable") is not True:
            errors.append("proposal is not executable")
        if review.get("branchSpecific") is True:
            errors.append("proposal is branch-specific")
    publication = document.get("publication")
    if not isinstance(publication, dict):
        errors.append("publication policy is required")
    else:
        if publication.get("duplicate") is True:
            errors.append("duplicate proposals must not be published")
        if publication.get("reconciled") is not True:
            errors.append("matching PR status was not reconciled")
        action = publication.get("action")
        if action not in PUBLICATION_ACTIONS:
            errors.append("publication action is invalid")
        if action == "create" and publication.get("cooldownSatisfied") is not True:
            errors.append("publication cooldown is not satisfied")
    if decision == "hold_as_pattern_only" and document.get("skillPath"):
        errors.append("hold decisions must not include a skillPath")
    if decision != "hold_as_pattern_only" and not document.get("skillPath"):
        errors.append("promoted decisions require a skillPath")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("proposal", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    document = read_json(args.proposal)
    errors = validate(document) if isinstance(document, dict) else ["proposal must be a JSON object"]
    result = {"valid": not errors, "path": str(args.proposal.resolve()), "errors": errors}
    if args.json:
        print(json.dumps(result, indent=2))
    elif errors:
        for error in errors:
            print(f"error: {error}")
    else:
        print(f"valid: {args.proposal}")
    raise SystemExit(0 if not errors else 1)


if __name__ == "__main__":
    main()
