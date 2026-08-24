#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. All rights reserved.

"""Normalize Vally 0.14 trial-result JSONL into the Forge evaluation contract."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from forge_common import read_json, write_json


def number(value: Any, name: str, minimum: float = 0.0) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{name} must be a number >= {minimum}")
    return float(value)


def load_trial_records(path: str | Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"invalid JSONL at line {line_number}: {error.msg}"
                ) from error
            if isinstance(value, dict) and value.get("type") == "trial-result":
                records.append(value)
    if not records:
        raise ValueError("Vally output contains no trial-result records")
    return records


def bounded_evidence(values: list[str], limit: int = 8) -> list[str]:
    unique: list[str] = []
    for value in values:
        normalized = value.strip()
        if normalized and normalized not in unique:
            unique.append(normalized[:2000])
        if len(unique) >= limit:
            break
    return unique


def trial_identity(record: dict[str, Any], ordinal: int) -> int:
    index = record.get("trialIndex")
    if index is None:
        return ordinal
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        raise ValueError("Vally trialIndex must be a non-negative integer")
    return index


def normalize(
    records: list[dict[str, Any]],
    expected_case_ids: list[str],
    expected_runs: int,
) -> dict[str, Any]:
    if expected_runs < 1:
        raise ValueError("expected runs must be positive")
    if (
        not expected_case_ids
        or any(not isinstance(value, str) or not value for value in expected_case_ids)
        or len(expected_case_ids) != len(set(expected_case_ids))
    ):
        raise ValueError("expected case IDs must be unique non-empty strings")
    expected = set(expected_case_ids)
    grouped: dict[str, list[dict[str, Any]]] = {
        case_id: [] for case_id in expected_case_ids
    }
    unexpected: set[str] = set()
    seen_trials: set[tuple[str, int]] = set()
    for record in records:
        stimulus = record.get("stimulus")
        if not isinstance(stimulus, str) or not stimulus:
            raise ValueError("Vally trial-result requires stimulus")
        if stimulus not in expected:
            unexpected.add(stimulus)
            continue
        identity = trial_identity(record, len(grouped[stimulus]))
        key = (stimulus, identity)
        if key in seen_trials:
            raise ValueError(f"duplicate Vally trial result: {stimulus} trial {identity}")
        seen_trials.add(key)
        grouped[stimulus].append(record)
    if unexpected:
        raise ValueError(
            "Vally output contains unexpected stimuli: " + ", ".join(sorted(unexpected))
        )
    missing = [
        case_id
        for case_id, values in grouped.items()
        if len(values) != expected_runs
    ]
    if missing:
        counts = ", ".join(
            f"{case_id}={len(grouped[case_id])}/{expected_runs}"
            for case_id in sorted(missing)
        )
        raise ValueError(f"Vally trial counts do not match configured runs: {counts}")

    normalized_cases: list[dict[str, Any]] = []
    all_scores: list[float] = []
    total_tokens: list[float] = []
    total_tool_calls: list[float] = []
    total_wall_time: list[float] = []
    error_trials = 0
    for case_id in expected_case_ids:
        trial_scores: list[float] = []
        trial_passes: list[bool] = []
        evidence: list[str] = []
        for record in grouped[case_id]:
            status = record.get("status")
            if status not in {"success", "error", "skipped"}:
                raise ValueError(f"Vally trial status is invalid for {case_id}")
            grade = record.get("gradeResult")
            trajectory = record.get("trajectory")
            execution_error = status != "success"
            if status == "success":
                if not isinstance(grade, dict):
                    raise ValueError(
                        f"successful Vally trial for {case_id} has no gradeResult"
                    )
                score = number(grade.get("score"), f"{case_id} grade score")
                if score > 1:
                    raise ValueError(f"{case_id} grade score must be <= 1")
                passed = grade.get("passed")
                if not isinstance(passed, bool):
                    raise ValueError(f"{case_id} grade passed must be boolean")
                grade_status = grade.get("status")
                if grade_status not in {None, "success"}:
                    execution_error = True
                    passed = False
                grade_evidence = grade.get("evidence")
                if isinstance(grade_evidence, str):
                    evidence.append(grade_evidence)
            else:
                score = 0.0
                passed = False
                message = record.get("error") or record.get("skipReason") or status
                evidence.append(str(message))
            metrics: dict[str, Any] = {}
            if isinstance(trajectory, dict):
                value = trajectory.get("metrics")
                if isinstance(value, dict):
                    metrics = value
            token_usage = metrics.get("tokenUsage")
            tokens = (
                number(token_usage.get("totalTokens"), "trajectory totalTokens")
                if isinstance(token_usage, dict)
                else 0.0
            )
            tool_calls = number(
                metrics.get("toolCallCount", 0),
                "trajectory toolCallCount",
            )
            wall_time = number(
                metrics.get("wallTimeMs", record.get("durationMs", 0)),
                "trajectory wallTimeMs",
            )
            error_count = number(
                metrics.get("errorCount", 0),
                "trajectory errorCount",
            )
            if error_count > 0:
                execution_error = True
            if execution_error:
                error_trials += 1
            trial_scores.append(score)
            trial_passes.append(passed and not execution_error)
            all_scores.append(score)
            total_tokens.append(tokens)
            total_tool_calls.append(tool_calls)
            total_wall_time.append(wall_time)
        normalized_cases.append(
            {
                "caseId": case_id,
                "score": sum(trial_scores) / len(trial_scores),
                "passed": all(trial_passes),
                "evidence": bounded_evidence(evidence),
            }
        )

    mean_score = sum(all_scores) / len(all_scores)
    variance = sum((score - mean_score) ** 2 for score in all_scores) / len(all_scores)
    trial_count = len(all_scores)
    return {
        "engine": "vally",
        "runCount": expected_runs,
        "cases": normalized_cases,
        "metrics": {
            "errorRate": error_trials / trial_count,
            "scoreStdDev": math.sqrt(variance),
            "meanTokens": sum(total_tokens) / trial_count,
            "meanToolCalls": sum(total_tool_calls) / trial_count,
            "meanWallTimeMs": sum(total_wall_time) / trial_count,
        },
    }


def parse_case_ids(path: str | Path) -> list[str]:
    value = read_json(path)
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and isinstance(value.get("caseIds"), list):
        return value["caseIds"]
    raise ValueError("case ID input must be an array or action object with caseIds")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--case-ids", required=True, type=Path)
    parser.add_argument("--runs", required=True, type=int)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = normalize(
            load_trial_records(args.results),
            parse_case_ids(args.case_ids),
            args.runs,
        )
        write_json(args.out, result)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"error: {error}") from error


if __name__ == "__main__":
    main()
