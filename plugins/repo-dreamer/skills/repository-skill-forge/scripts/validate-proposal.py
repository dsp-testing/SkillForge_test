#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. All rights reserved.

"""Validate a repository Forge proposal before GitHub MCP publication."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from forge_common import read_json

DECISIONS = {"create_skill", "improve_existing_skill", "merge_skills", "hold_as_pattern_only"}


def validate(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    decision = document.get("decision")
    if decision not in DECISIONS:
        errors.append("decision is invalid")
    if not document.get("candidateId"):
        errors.append("candidateId is required")
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
        if publication.get("newPrBudget", 0) > 1:
            errors.append("newPrBudget must be at most one")
        if publication.get("duplicate") is True:
            errors.append("duplicate proposals must not be published")
        if publication.get("cooldownSatisfied") is not True:
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
