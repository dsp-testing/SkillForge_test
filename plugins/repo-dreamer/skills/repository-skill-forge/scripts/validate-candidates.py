#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. All rights reserved.

"""Validate repository Forge candidate invariants."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from forge_common import read_json


def validate(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    scope = document.get("scope")
    if not isinstance(scope, dict) or scope.get("kind") != "repository" or not scope.get("repository"):
        errors.append("scope must identify one repository")
    if document.get("coverage", {}).get("truncated"):
        errors.append("coverage must not be truncated")
    if document.get("userDiversity", {}).get("status") != "unknown":
        errors.append("user diversity must remain unknown until trusted actor identity exists")
    patterns = document.get("usagePatterns")
    candidates = document.get("candidates")
    if not isinstance(patterns, list) or not isinstance(candidates, list):
        errors.append("usagePatterns and candidates must be arrays")
        return errors
    pattern_ids = {item.get("candidateId") for item in patterns if isinstance(item, dict)}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            errors.append("candidate must be an object")
            continue
        if candidate.get("candidateId") not in pattern_ids:
            errors.append("candidate must also exist in usagePatterns")
        if not candidate.get("promotion", {}).get("eligible"):
            errors.append(f"candidate {candidate.get('candidateId')} is not eligible")
        metrics = candidate.get("metrics")
        if not isinstance(metrics, dict):
            errors.append(f"candidate {candidate.get('candidateId')} has no metrics")
            continue
        known = metrics.get("knownOutcomeCount")
        successes = metrics.get("successCount")
        failures = metrics.get("failureCount")
        if not all(isinstance(value, int) for value in (known, successes, failures)):
            errors.append(f"candidate {candidate.get('candidateId')} has invalid outcome counts")
        elif known != successes + failures:
            errors.append(f"candidate {candidate.get('candidateId')} outcome denominator is inconsistent")
        if "rawEvidence" in json.dumps(candidate):
            errors.append(f"candidate {candidate.get('candidateId')} contains raw evidence")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidates", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    document = read_json(args.candidates)
    errors = validate(document) if isinstance(document, dict) else ["candidate document must be an object"]
    result = {"valid": not errors, "path": str(args.candidates.resolve()), "errors": errors}
    if args.json:
        print(json.dumps(result, indent=2))
    elif errors:
        for error in errors:
            print(f"error: {error}")
    else:
        print(f"valid: {args.candidates}")
    raise SystemExit(0 if not errors else 1)


if __name__ == "__main__":
    main()
