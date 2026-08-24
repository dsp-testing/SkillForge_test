#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. All rights reserved.

"""Drive bounded baseline-versus-skill evaluation for a Forge proposal."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from forge_common import read_json, stable_hash, write_json

RUNNING_PHASES = {
    "development_baseline",
    "development_treatment",
    "revision",
    "heldout_baseline",
    "heldout_treatment",
}
TERMINAL_STATUSES = {"accepted", "rejected", "blocked"}
ARMS = {"baseline", "treatment"}
SPLITS = {"development", "heldout"}


def require_number(value: Any, name: str, minimum: float = 0.0) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{name} must be a number >= {minimum}")
    return float(value)


def validate_cases(document: Any) -> list[dict[str, Any]]:
    if not isinstance(document, dict) or not isinstance(document.get("cases"), list):
        raise ValueError("evaluation cases must contain a cases array")
    cases = document["cases"]
    if len(cases) < 3:
        raise ValueError("at least three evaluation cases are required")
    case_ids: set[str] = set()
    session_hashes: set[str] = set()
    validated: list[dict[str, Any]] = []
    for item in cases:
        if not isinstance(item, dict):
            raise ValueError("every evaluation case must be an object")
        case_id = item.get("caseId")
        session_hash = item.get("sessionHash")
        prompt = item.get("prompt")
        rubric = item.get("rubric")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("every evaluation case requires caseId")
        if case_id in case_ids:
            raise ValueError(f"duplicate evaluation caseId: {case_id}")
        if not isinstance(session_hash, str) or not session_hash:
            raise ValueError(f"evaluation case {case_id} requires sessionHash")
        if session_hash in session_hashes:
            raise ValueError(f"duplicate evaluation sessionHash: {session_hash}")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(f"evaluation case {case_id} requires prompt")
        if (
            not isinstance(rubric, list)
            or not rubric
            or any(not isinstance(entry, str) or not entry.strip() for entry in rubric)
        ):
            raise ValueError(f"evaluation case {case_id} requires a non-empty rubric")
        environment = item.get("environment", {})
        if not isinstance(environment, dict):
            raise ValueError(f"evaluation case {case_id} environment must be an object")
        case_ids.add(case_id)
        session_hashes.add(session_hash)
        validated.append(
            {
                "caseId": case_id,
                "sessionHash": session_hash,
                "prompt": prompt,
                "rubric": rubric,
                "environment": environment,
            }
        )
    return validated


def split_cases(
    cases: list[dict[str, Any]],
    proposal_key: str,
) -> dict[str, list[dict[str, Any]]]:
    ordered = sorted(
        cases,
        key=lambda item: stable_hash(
            [proposal_key, item["sessionHash"]],
            length=64,
        ),
    )
    total = len(ordered)
    heldout_count = max(1, round(total * 0.2))
    development_count = max(1, round(total * 0.2))
    if heldout_count + development_count >= total:
        heldout_count = 1
        development_count = 1
    return {
        "heldout": ordered[:heldout_count],
        "development": ordered[heldout_count : heldout_count + development_count],
        "authoring": ordered[heldout_count + development_count :],
    }


def vally_spec(
    proposal_key: str,
    split: str,
    cases: list[dict[str, Any]],
    runs: int,
) -> dict[str, Any]:
    stimuli: list[dict[str, Any]] = []
    for case in cases:
        stimulus: dict[str, Any] = {
            "name": case["caseId"],
            "prompt": case["prompt"],
            "tags": {"session_hash": case["sessionHash"], "split": split},
            "graders": [
                {
                    "type": "prompt",
                    "config": {
                        "rubric": case["rubric"],
                        "threshold": 1.0,
                    },
                }
            ],
        }
        if case["environment"]:
            stimulus["environment"] = case["environment"]
        stimuli.append(stimulus)
    return {
        "name": f"{proposal_key}-{split}",
        "type": "capability",
        "defaults": {"runs": runs},
        "stimuli": stimuli,
    }


def initialize(args: argparse.Namespace) -> dict[str, Any]:
    cases = validate_cases(read_json(args.cases))
    splits = split_cases(cases, args.proposal_key)
    run_dir = Path(args.run_dir).resolve()
    eval_dir = run_dir / "evaluation"
    eval_dir.mkdir(parents=True, exist_ok=True)
    spec_paths: dict[str, str] = {}
    for split in SPLITS:
        path = eval_dir / f"{args.proposal_key}-{split}.eval.yaml"
        write_json(
            path,
            vally_spec(args.proposal_key, split, splits[split], args.runs),
        )
        spec_paths[split] = str(path)
    return {
        "schemaVersion": 1,
        "proposal": {
            "proposalKey": args.proposal_key,
            "proposalVersion": args.proposal_version,
        },
        "runDir": str(run_dir),
        "limits": {
            "maxIterations": args.max_iterations,
            "runsPerCase": args.runs,
        },
        "thresholds": {
            "minTreatmentScore": args.min_treatment_score,
            "minScoreDelta": args.min_score_delta,
            "maxRegressions": args.max_regressions,
            "maxErrorRateIncrease": args.max_error_rate_increase,
            "maxScoreStdDev": args.max_score_stddev,
            "maxTokenIncreaseRatio": args.max_token_increase_ratio,
            "maxToolCallIncreaseRatio": args.max_tool_call_increase_ratio,
        },
        "splits": {
            name: [item["caseId"] for item in values]
            for name, values in splits.items()
        },
        "sessionHashes": {
            name: [item["sessionHash"] for item in values]
            for name, values in splits.items()
        },
        "evalSpecs": spec_paths,
        "iteration": 1,
        "revisions": [
            {
                "iteration": 1,
                "skillPath": str(Path(args.skill_path).resolve()),
                "proposalVersion": args.proposal_version,
            }
        ],
        "results": {
            "development": {"baseline": None, "treatments": []},
            "heldout": {"baseline": None, "treatment": None},
        },
        "phase": "development_baseline",
        "status": "running",
        "decision": None,
        "lastAssessment": None,
    }


def current_revision(state: dict[str, Any]) -> dict[str, Any]:
    return state["revisions"][-1]


def result_output_path(state: dict[str, Any], split: str, arm: str) -> str:
    suffix = "baseline" if arm == "baseline" else f"treatment-{state['iteration']}"
    return str(
        Path(state["runDir"])
        / "evaluation"
        / "results"
        / split
        / suffix
    )


def next_action(state: dict[str, Any]) -> dict[str, Any] | None:
    if state.get("status") != "running":
        return None
    phase = state.get("phase")
    mapping = {
        "development_baseline": ("development", "baseline"),
        "development_treatment": ("development", "treatment"),
        "heldout_baseline": ("heldout", "baseline"),
        "heldout_treatment": ("heldout", "treatment"),
    }
    if phase == "revision":
        assessment = state.get("lastAssessment") or {}
        return {
            "kind": "revise-proposal",
            "actionId": f"revise-{state['iteration'] + 1}",
            "iteration": state["iteration"] + 1,
            "currentSkillPath": current_revision(state)["skillPath"],
            "failedGates": assessment.get("failedGates", []),
            "regressedCaseIds": assessment.get("regressedCaseIds", []),
            "treatmentFailures": assessment.get("treatmentFailures", []),
            "instruction": (
                "Revise only the proposed skill using development failures. "
                "Do not edit evaluation cases or inspect held-out results."
            ),
        }
    if phase not in mapping:
        raise ValueError(f"unknown running evaluation phase: {phase}")
    split, arm = mapping[phase]
    action = {
        "kind": "run-vally",
        "actionId": f"{split}-{arm}-{state['iteration']}",
        "split": split,
        "arm": arm,
        "iteration": state["iteration"],
        "evalSpecPath": state["evalSpecs"][split],
        "caseIds": state["splits"][split],
        "runs": state["limits"]["runsPerCase"],
        "outputDir": result_output_path(state, split, arm),
        "normalizedResultPath": str(
            Path(state["runDir"])
            / "evaluation"
            / "normalized"
            / f"{split}-{arm}-{state['iteration']}.json"
        ),
    }
    if arm == "treatment":
        action["skillPath"] = current_revision(state)["skillPath"]
    return action


def validate_result(
    state: dict[str, Any],
    split: str,
    arm: str,
    document: Any,
) -> dict[str, Any]:
    if split not in SPLITS or arm not in ARMS:
        raise ValueError("result split or arm is invalid")
    if not isinstance(document, dict):
        raise ValueError("evaluation result must be an object")
    if document.get("engine") != "vally":
        raise ValueError("evaluation result engine must be vally")
    cases = document.get("cases")
    if not isinstance(cases, list):
        raise ValueError("evaluation result requires cases")
    expected = set(state["splits"][split])
    actual: set[str] = set()
    normalized_cases: list[dict[str, Any]] = []
    for item in cases:
        if not isinstance(item, dict):
            raise ValueError("evaluation result cases must be objects")
        case_id = item.get("caseId")
        if not isinstance(case_id, str) or case_id not in expected or case_id in actual:
            raise ValueError("evaluation result case IDs do not match the split")
        score = require_number(item.get("score"), f"{case_id} score")
        if score > 1:
            raise ValueError(f"{case_id} score must be <= 1")
        passed = item.get("passed")
        if not isinstance(passed, bool):
            raise ValueError(f"{case_id} passed must be boolean")
        evidence = item.get("evidence", [])
        if (
            not isinstance(evidence, list)
            or any(not isinstance(value, str) for value in evidence)
        ):
            raise ValueError(f"{case_id} evidence must be an array of strings")
        actual.add(case_id)
        normalized_cases.append(
            {
                "caseId": case_id,
                "score": score,
                "passed": passed,
                "evidence": evidence,
            }
        )
    if actual != expected:
        raise ValueError("evaluation result must include every case in the split")
    metrics = document.get("metrics")
    if not isinstance(metrics, dict):
        raise ValueError("evaluation result requires metrics")
    normalized_metrics = {
        "errorRate": require_number(metrics.get("errorRate"), "errorRate"),
        "scoreStdDev": require_number(metrics.get("scoreStdDev"), "scoreStdDev"),
        "meanTokens": require_number(metrics.get("meanTokens"), "meanTokens"),
        "meanToolCalls": require_number(metrics.get("meanToolCalls"), "meanToolCalls"),
        "meanWallTimeMs": require_number(metrics.get("meanWallTimeMs"), "meanWallTimeMs"),
    }
    if normalized_metrics["errorRate"] > 1:
        raise ValueError("errorRate must be <= 1")
    run_count = document.get("runCount")
    if not isinstance(run_count, int) or isinstance(run_count, bool) or run_count < 1:
        raise ValueError("runCount must be a positive integer")
    if run_count != state["limits"]["runsPerCase"]:
        raise ValueError("runCount must match the configured runsPerCase")
    return {
        "engine": "vally",
        "runCount": run_count,
        "meanScore": sum(item["score"] for item in normalized_cases)
        / len(normalized_cases),
        "cases": sorted(normalized_cases, key=lambda item: item["caseId"]),
        "metrics": normalized_metrics,
    }


def increase_ratio(baseline: float, treatment: float) -> float | None:
    if baseline == 0:
        return 0.0 if treatment == 0 else None
    return (treatment - baseline) / baseline


def assess_pair(
    state: dict[str, Any],
    baseline: dict[str, Any],
    treatment: dict[str, Any],
) -> dict[str, Any]:
    baseline_cases = {item["caseId"]: item for item in baseline["cases"]}
    treatment_cases = {item["caseId"]: item for item in treatment["cases"]}
    regressed = sorted(
        case_id
        for case_id, item in baseline_cases.items()
        if item["passed"] and not treatment_cases[case_id]["passed"]
    )
    failures = [
        {
            "caseId": item["caseId"],
            "score": item["score"],
            "evidence": item["evidence"],
        }
        for item in treatment["cases"]
        if not item["passed"]
    ]
    delta = treatment["meanScore"] - baseline["meanScore"]
    metrics = {
        "scoreDelta": delta,
        "errorRateIncrease": (
            treatment["metrics"]["errorRate"] - baseline["metrics"]["errorRate"]
        ),
        "tokenIncreaseRatio": increase_ratio(
            baseline["metrics"]["meanTokens"],
            treatment["metrics"]["meanTokens"],
        ),
        "toolCallIncreaseRatio": increase_ratio(
            baseline["metrics"]["meanToolCalls"],
            treatment["metrics"]["meanToolCalls"],
        ),
        "wallTimeIncreaseRatio": increase_ratio(
            baseline["metrics"]["meanWallTimeMs"],
            treatment["metrics"]["meanWallTimeMs"],
        ),
    }
    thresholds = state["thresholds"]
    gates = {
        "treatmentScore": treatment["meanScore"]
        >= thresholds["minTreatmentScore"],
        "scoreDelta": delta >= thresholds["minScoreDelta"],
        "regressions": len(regressed) <= thresholds["maxRegressions"],
        "errorRate": metrics["errorRateIncrease"]
        <= thresholds["maxErrorRateIncrease"],
        "stability": treatment["metrics"]["scoreStdDev"]
        <= thresholds["maxScoreStdDev"],
        "tokens": isinstance(metrics["tokenIncreaseRatio"], float)
        and metrics["tokenIncreaseRatio"] <= thresholds["maxTokenIncreaseRatio"],
        "toolCalls": isinstance(metrics["toolCallIncreaseRatio"], float)
        and metrics["toolCallIncreaseRatio"]
        <= thresholds["maxToolCallIncreaseRatio"],
    }
    return {
        "passed": all(gates.values()),
        "gates": gates,
        "failedGates": sorted(name for name, passed in gates.items() if not passed),
        "baselineScore": baseline["meanScore"],
        "treatmentScore": treatment["meanScore"],
        "metrics": metrics,
        "regressedCaseIds": regressed,
        "treatmentFailures": failures,
    }


def record_result(
    state: dict[str, Any],
    split: str,
    arm: str,
    document: Any,
) -> None:
    expected_phase = f"{split}_{arm}"
    if state.get("status") != "running" or state.get("phase") != expected_phase:
        raise ValueError(
            f"cannot record {split} {arm} result during phase {state.get('phase')}"
        )
    result = validate_result(state, split, arm, document)
    if split == "development":
        if arm == "baseline":
            state["results"]["development"]["baseline"] = result
            state["phase"] = "development_treatment"
            return
        result["iteration"] = state["iteration"]
        state["results"]["development"]["treatments"].append(result)
        assessment = assess_pair(
            state,
            state["results"]["development"]["baseline"],
            result,
        )
        assessment["split"] = "development"
        assessment["iteration"] = state["iteration"]
        state["lastAssessment"] = assessment
        if assessment["passed"]:
            state["phase"] = "heldout_baseline"
        elif state["iteration"] < state["limits"]["maxIterations"]:
            state["phase"] = "revision"
        else:
            state["phase"] = None
            state["status"] = "rejected"
            state["decision"] = "max_iterations_without_development_acceptance"
        return
    state["results"]["heldout"][arm] = result
    if arm == "baseline":
        state["phase"] = "heldout_treatment"
        return
    assessment = assess_pair(
        state,
        state["results"]["heldout"]["baseline"],
        result,
    )
    assessment["split"] = "heldout"
    assessment["iteration"] = state["iteration"]
    state["lastAssessment"] = assessment
    state["phase"] = None
    if assessment["passed"]:
        state["status"] = "accepted"
        state["decision"] = "heldout_acceptance_gates_passed"
    else:
        state["status"] = "rejected"
        state["decision"] = "heldout_acceptance_gates_failed"


def record_revision(
    state: dict[str, Any],
    skill_path: str,
    proposal_version: str,
) -> None:
    if state.get("status") != "running" or state.get("phase") != "revision":
        raise ValueError("a revision can only be recorded during the revision phase")
    if not proposal_version:
        raise ValueError("proposalVersion is required")
    next_iteration = state["iteration"] + 1
    state["iteration"] = next_iteration
    state["proposal"]["proposalVersion"] = proposal_version
    state["revisions"].append(
        {
            "iteration": next_iteration,
            "skillPath": str(Path(skill_path).resolve()),
            "proposalVersion": proposal_version,
        }
    )
    state["phase"] = "development_treatment"


def summary(state: dict[str, Any]) -> dict[str, Any]:
    assessment = state.get("lastAssessment")
    return {
        "schemaVersion": 1,
        "engine": "vally",
        "status": state.get("status"),
        "accepted": state.get("status") == "accepted",
        "decision": state.get("decision"),
        "iteration": state.get("iteration"),
        "maxIterations": state.get("limits", {}).get("maxIterations"),
        "proposalVersion": state.get("proposal", {}).get("proposalVersion"),
        "sessionSplit": {
            name: len(values)
            for name, values in state.get("splits", {}).items()
        },
        "thresholds": state.get("thresholds"),
        "assessment": assessment,
    }


def load_state(path: Path) -> dict[str, Any]:
    state = read_json(path)
    if not isinstance(state, dict) or state.get("schemaVersion") != 1:
        raise ValueError("evaluation state schemaVersion must be 1")
    return state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init")
    init.add_argument("--cases", required=True, type=Path)
    init.add_argument("--proposal-key", required=True)
    init.add_argument("--proposal-version", required=True)
    init.add_argument("--skill-path", required=True)
    init.add_argument("--run-dir", required=True)
    init.add_argument("--out", required=True, type=Path)
    init.add_argument("--max-iterations", type=int, default=3)
    init.add_argument("--runs", type=int, default=3)
    init.add_argument("--min-treatment-score", type=float, default=0.8)
    init.add_argument("--min-score-delta", type=float, default=0.05)
    init.add_argument("--max-regressions", type=int, default=0)
    init.add_argument("--max-error-rate-increase", type=float, default=0.0)
    init.add_argument("--max-score-stddev", type=float, default=0.15)
    init.add_argument("--max-token-increase-ratio", type=float, default=0.5)
    init.add_argument("--max-tool-call-increase-ratio", type=float, default=0.5)

    next_parser = subparsers.add_parser("next")
    next_parser.add_argument("--state", required=True, type=Path)
    next_parser.add_argument("--out", required=True, type=Path)

    record = subparsers.add_parser("record")
    record.add_argument("--state", required=True, type=Path)
    record.add_argument("--split", required=True, choices=sorted(SPLITS))
    record.add_argument("--arm", required=True, choices=sorted(ARMS))
    record.add_argument("--result", required=True, type=Path)

    revise = subparsers.add_parser("revise")
    revise.add_argument("--state", required=True, type=Path)
    revise.add_argument("--skill-path", required=True)
    revise.add_argument("--proposal-version", required=True)

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--state", required=True, type=Path)
    status_parser.add_argument("--out", type=Path)
    status_parser.add_argument("--assert-terminal", action="store_true")

    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        if args.command == "init":
            if args.max_iterations < 1 or args.runs < 1:
                raise ValueError("max-iterations and runs must be positive")
            state = initialize(args)
            write_json(args.out, state)
            return
        state = load_state(args.state)
        if args.command == "next":
            action = next_action(state)
            write_json(
                args.out,
                action
                if action is not None
                else {
                    "kind": "evaluation-terminal",
                    "status": state["status"],
                    "decision": state.get("decision"),
                },
            )
        elif args.command == "record":
            record_result(state, args.split, args.arm, read_json(args.result))
            write_json(args.state, state)
        elif args.command == "revise":
            record_revision(state, args.skill_path, args.proposal_version)
            write_json(args.state, state)
        elif args.command == "status":
            output = summary(state)
            if args.out:
                write_json(args.out, output)
            else:
                print(json.dumps(output, indent=2))
            if args.assert_terminal and state.get("status") not in TERMINAL_STATUSES:
                raise ValueError(f"evaluation status is {state.get('status')}")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"error: {error}") from error


if __name__ == "__main__":
    main()
