#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. All rights reserved.

"""Atomically merge sanitized Forge batch primitives into a run ledger."""

from __future__ import annotations

import argparse
from typing import Any

from forge_common import read_json, write_json


def merge(
    documents: list[tuple[str, dict[str, Any]]],
    ledger: dict[str, Any] | None = None,
) -> dict[str, Any]:
    evidence: dict[str, dict[str, Any]] = {}
    coverage = {
        "batchCount": 0,
        "rowCount": 0,
        "sessionCount": 0,
        "toolCallCount": 0,
        "primitiveCount": 0,
        "extractionErrorCount": 0,
        "truncated": False,
    }
    scope: dict[str, Any] | None = None
    user_diversity: dict[str, Any] | None = None
    processed_batches: set[str] = set()
    if ledger:
        for primitive in ledger.get("primitives", []):
            if isinstance(primitive, dict) and primitive.get("evidenceKey"):
                evidence[str(primitive["evidenceKey"])] = primitive
        previous_coverage = ledger.get("coverage")
        if isinstance(previous_coverage, dict):
            for key in coverage:
                value = previous_coverage.get(key)
                if isinstance(value, (int, bool)):
                    coverage[key] = value
        processed_batches = {
            str(value) for value in ledger.get("processedBatchIds", []) if value
        }
        scope = ledger.get("scope") if isinstance(ledger.get("scope"), dict) else None
        user_diversity = (
            ledger.get("userDiversity")
            if isinstance(ledger.get("userDiversity"), dict)
            else None
        )
    for batch_id, document in documents:
        if batch_id in processed_batches:
            continue
        if document.get("sanitization", {}).get("rawSourceContentRetained") is not False:
            raise ValueError("input is not a sanitized primitive document")
        document_scope = document.get("scope")
        if not isinstance(document_scope, dict):
            raise ValueError("sanitized input is missing scope")
        if scope is None:
            scope = document_scope
        elif document_scope.get("repository") != scope.get("repository"):
            raise ValueError("sanitized batch repositories do not match")
        user_diversity = user_diversity or document.get("userDiversity")
        batch_coverage = document.get("coverage")
        if isinstance(batch_coverage, dict):
            for key in (
                "rowCount",
                "sessionCount",
                "toolCallCount",
                "primitiveCount",
                "extractionErrorCount",
            ):
                value = batch_coverage.get(key)
                if isinstance(value, int):
                    coverage[key] += value
            coverage["truncated"] = coverage["truncated"] or bool(
                batch_coverage.get("truncated")
            )
        coverage["batchCount"] += 1
        processed_batches.add(batch_id)
        primitives = document.get("primitives")
        if not isinstance(primitives, list):
            raise ValueError("sanitized input is missing primitives")
        for primitive in primitives:
            if not isinstance(primitive, dict) or not primitive.get("evidenceKey"):
                raise ValueError("sanitized primitive is missing evidenceKey")
            evidence[str(primitive["evidenceKey"])] = primitive
    coverage["primitiveCount"] = len(evidence)
    return {
        "schemaVersion": 1,
        "scope": scope,
        "coverage": coverage,
        "userDiversity": user_diversity,
        "sanitization": {
            "rawSourceContentRetained": False,
            "homePathsRedacted": True,
        },
        "processedBatchIds": sorted(processed_batches),
        "primitives": sorted(evidence.values(), key=lambda item: str(item["evidenceKey"])),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--batch",
        action="append",
        nargs=2,
        metavar=("BATCH_ID", "PATH"),
        required=True,
    )
    parser.add_argument("--ledger-in")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    ledger = None
    if args.ledger_in:
        ledger = read_json(args.ledger_in)
        if not isinstance(ledger, dict):
            raise SystemExit("ledger input must be a JSON object")
    documents = []
    for batch_id, path in args.batch:
        document = read_json(path)
        if not isinstance(document, dict):
            raise SystemExit("each input must be a JSON object")
        documents.append((batch_id, document))
    try:
        result = merge(documents, ledger)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    write_json(args.out, result)


if __name__ == "__main__":
    main()
