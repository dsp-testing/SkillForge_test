#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. All rights reserved.

"""Validate that semantic subject clusters exactly partition Forge candidates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from forge_common import read_json


def validate(candidates: dict[str, Any], clusters: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    candidate_rows = candidates.get("candidates")
    cluster_rows = clusters.get("clusters")
    if not isinstance(candidate_rows, list) or not isinstance(cluster_rows, list):
        return ["candidates and clusters must contain arrays"]
    expected = {
        str(item.get("candidateId"))
        for item in candidate_rows
        if isinstance(item, dict) and item.get("candidateId")
    }
    assigned: list[str] = []
    for cluster in cluster_rows:
        if not isinstance(cluster, dict):
            errors.append("cluster must be an object")
            continue
        if not cluster.get("label"):
            errors.append("cluster label is required")
        candidate_ids = cluster.get("candidateIds")
        if not isinstance(candidate_ids, list):
            errors.append("cluster candidateIds must be an array")
            continue
        assigned.extend(str(value) for value in candidate_ids)
    assigned_set = set(assigned)
    duplicates = sorted({value for value in assigned if assigned.count(value) > 1})
    missing = sorted(expected - assigned_set)
    unknown = sorted(assigned_set - expected)
    if duplicates:
        errors.append(f"candidate IDs assigned more than once: {duplicates}")
    if missing:
        errors.append(f"candidate IDs missing from clusters: {missing}")
    if unknown:
        errors.append(f"unknown candidate IDs in clusters: {unknown}")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", required=True, type=Path)
    parser.add_argument("--clusters", required=True, type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    candidate_document = read_json(args.candidates)
    cluster_document = read_json(args.clusters)
    errors = (
        validate(candidate_document, cluster_document)
        if isinstance(candidate_document, dict) and isinstance(cluster_document, dict)
        else ["candidate and cluster documents must be objects"]
    )
    result = {"valid": not errors, "errors": errors}
    if args.json:
        print(json.dumps(result, indent=2))
    elif errors:
        for error in errors:
            print(f"error: {error}")
    else:
        print(f"valid: {args.clusters}")
    raise SystemExit(0 if not errors else 1)


if __name__ == "__main__":
    main()
