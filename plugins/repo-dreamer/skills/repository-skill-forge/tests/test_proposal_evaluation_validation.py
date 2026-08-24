#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

SPEC = importlib.util.spec_from_file_location(
    "validate_proposal",
    SCRIPTS_DIR / "validate-proposal.py",
)
assert SPEC is not None and SPEC.loader is not None
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


def proposal() -> dict[str, object]:
    return {
        "decision": "create_skill",
        "proposalKey": "repository-helper",
        "candidateIds": ["candidate-1"],
        "proposalVersion": "version-2",
        "rank": 0,
        "skillPath": "repository-helper/SKILL.md",
        "extraction": {"status": "complete"},
        "review": {
            "leakageFindingCount": 0,
            "unresolvedConflictCount": 0,
            "executable": True,
            "branchSpecific": False,
            "descriptionHasActivationCriteria": True,
            "descriptionStatesOutcome": True,
            "descriptionCoversNecessaryTriggers": True,
            "concise": True,
            "nonRedundant": True,
        },
        "publication": {
            "duplicate": False,
            "reconciled": True,
            "action": "create",
            "markerValidated": True,
        },
        "evaluation": {
            "schemaVersion": 1,
            "engine": "vally",
            "status": "accepted",
            "accepted": True,
            "decision": "heldout_acceptance_gates_passed",
            "iteration": 2,
            "maxIterations": 3,
            "proposalVersion": "version-2",
            "sessionSplit": {
                "authoring": 3,
                "development": 1,
                "heldout": 1,
            },
            "thresholds": {
                "minTreatmentScore": 0.8,
                "minScoreDelta": 0.05,
            },
            "assessment": {
                "split": "heldout",
                "iteration": 2,
                "passed": True,
                "gates": {
                    "treatmentScore": True,
                    "scoreDelta": True,
                    "regressions": True,
                    "errorRate": True,
                    "tokens": True,
                    "toolCalls": True,
                },
                "failedGates": [],
                "baselineScore": 0.5,
                "treatmentScore": 0.9,
                "metrics": {
                    "scoreDelta": 0.4,
                    "errorRateIncrease": 0.0,
                    "tokenIncreaseRatio": 0.2,
                    "toolCallIncreaseRatio": 0.1,
                    "wallTimeIncreaseRatio": 0.1,
                },
                "regressedCaseIds": [],
                "treatmentFailures": [],
            },
        },
    }


class ProposalEvaluationValidationTests(unittest.TestCase):
    def test_accepts_promoted_proposal_with_heldout_evaluation(self) -> None:
        self.assertEqual([], validator.validate(proposal()))

    def test_rejects_promoted_proposal_without_evaluation(self) -> None:
        document = proposal()
        document.pop("evaluation")

        self.assertIn(
            "promoted proposals require evaluation",
            validator.validate(document),
        )

    def test_rejects_development_only_acceptance(self) -> None:
        document = proposal()
        document["evaluation"]["assessment"]["split"] = "development"

        self.assertIn(
            "proposal evaluation requires a passing heldout assessment",
            validator.validate(document),
        )

    def test_rejects_heldout_regression(self) -> None:
        document = proposal()
        document["evaluation"]["assessment"]["regressedCaseIds"] = ["case-1"]

        self.assertIn(
            "proposal evaluation has heldout regressions",
            validator.validate(document),
        )

    def test_hold_decision_does_not_require_evaluation(self) -> None:
        document = proposal()
        document["decision"] = "hold_as_pattern_only"
        document.pop("skillPath")
        document.pop("evaluation")
        document["publication"]["action"] = "hold"

        self.assertEqual([], validator.validate(document))


if __name__ == "__main__":
    unittest.main()
