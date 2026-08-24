#!/usr/bin/env python3

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import vally_results


def trial(
    stimulus: str,
    index: int,
    *,
    score: float,
    passed: bool,
    status: str = "success",
) -> dict[str, object]:
    return {
        "type": "trial-result",
        "itemId": f"{stimulus}-{index}",
        "evalName": "repository-helper-development",
        "evalFilePath": "development.eval.yaml",
        "variant": "main",
        "stimulus": stimulus,
        "trialIndex": index,
        "totalTrials": 3,
        "status": status,
        "durationMs": 1000,
        "gradeResult": (
            {
                "name": "prompt",
                "kind": "llm",
                "passed": passed,
                "score": score,
                "evidence": "repository outcome verified",
                "stimulusName": stimulus,
                "trajectoryId": f"trajectory-{index}",
                "timestamp": "2026-08-24T00:00:00Z",
            }
            if status == "success"
            else None
        ),
        "trajectory": {
            "metrics": {
                "tokenUsage": {"totalTokens": 100},
                "toolCallCount": 10,
                "wallTimeMs": 900,
                "errorCount": 0,
            }
        },
    }


class VallyResultTests(unittest.TestCase):
    def test_normalizes_trials_and_metrics(self) -> None:
        records = [
            trial(case_id, index, score=0.9, passed=True)
            for case_id in ("case-1", "case-2")
            for index in range(3)
        ]

        result = vally_results.normalize(records, ["case-1", "case-2"], 3)

        self.assertEqual(3, result["runCount"])
        self.assertEqual(0.9, result["cases"][0]["score"])
        self.assertTrue(all(item["passed"] for item in result["cases"]))
        self.assertAlmostEqual(0.0, result["metrics"]["scoreStdDev"])
        self.assertEqual(100.0, result["metrics"]["meanTokens"])
        self.assertEqual(10.0, result["metrics"]["meanToolCalls"])

    def test_error_trial_is_failed_and_counted(self) -> None:
        records = [
            trial("case-1", 0, score=0.9, passed=True),
            trial("case-1", 1, score=0.9, passed=True),
            {
                **trial("case-1", 2, score=0.0, passed=False, status="error"),
                "error": "executor failed",
                "trajectory": None,
            },
        ]

        result = vally_results.normalize(records, ["case-1"], 3)

        self.assertFalse(result["cases"][0]["passed"])
        self.assertAlmostEqual(0.6, result["cases"][0]["score"])
        self.assertAlmostEqual(1 / 3, result["metrics"]["errorRate"])
        self.assertIn("executor failed", result["cases"][0]["evidence"])

    def test_rejects_missing_trials(self) -> None:
        records = [
            trial("case-1", index, score=0.9, passed=True)
            for index in range(2)
        ]

        with self.assertRaisesRegex(ValueError, "trial counts"):
            vally_results.normalize(records, ["case-1"], 3)

    def test_rejects_unexpected_stimulus(self) -> None:
        records = [
            trial("other-case", index, score=0.9, passed=True)
            for index in range(3)
        ]

        with self.assertRaisesRegex(ValueError, "unexpected stimuli"):
            vally_results.normalize(records, ["case-1"], 3)

    def test_rejects_duplicate_trial_index(self) -> None:
        records = [
            trial("case-1", 0, score=0.9, passed=True),
            trial("case-1", 0, score=0.9, passed=True),
            trial("case-1", 2, score=0.9, passed=True),
        ]

        with self.assertRaisesRegex(ValueError, "duplicate"):
            vally_results.normalize(records, ["case-1"], 3)


if __name__ == "__main__":
    unittest.main()
