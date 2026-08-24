#!/usr/bin/env python3

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

SPEC = importlib.util.spec_from_file_location(
    "proposal_eval_controller",
    SCRIPTS_DIR / "proposal-eval-controller.py",
)
assert SPEC is not None and SPEC.loader is not None
controller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controller)


def cases_document(count: int = 5) -> dict[str, object]:
    return {
        "cases": [
            {
                "caseId": f"case-{index}",
                "sessionHash": f"session-{index}",
                "prompt": f"Complete repository task {index}.",
                "rubric": [f"Task {index} reaches the expected repository outcome."],
            }
            for index in range(count)
        ]
    }


def result(
    case_ids: list[str],
    *,
    score: float,
    passed: bool,
    error_rate: float = 0.0,
    tokens: float = 100.0,
    tool_calls: float = 10.0,
) -> dict[str, object]:
    return {
        "engine": "vally",
        "runCount": 3,
        "cases": [
            {
                "caseId": case_id,
                "score": score,
                "passed": passed,
                "evidence": [] if passed else ["expected outcome was not produced"],
            }
            for case_id in case_ids
        ],
        "metrics": {
            "errorRate": error_rate,
            "scoreStdDev": 0.0,
            "meanTokens": tokens,
            "meanToolCalls": tool_calls,
            "meanWallTimeMs": 1000.0,
        },
    }


class ProposalEvalControllerTests(unittest.TestCase):
    def initialize(self, run_dir: str, case_count: int = 5) -> dict[str, object]:
        cases_path = Path(run_dir) / "cases.json"
        cases_path.write_text(json.dumps(cases_document(case_count)), encoding="utf-8")
        skill_path = Path(run_dir) / "proposal" / "SKILL.md"
        skill_path.parent.mkdir(parents=True)
        skill_path.write_text("# Skill\n", encoding="utf-8")
        return controller.initialize(
            argparse.Namespace(
                cases=cases_path,
                proposal_key="repository-helper",
                proposal_version="version-1",
                skill_path=str(skill_path),
                run_dir=run_dir,
                max_iterations=3,
                runs=3,
                min_treatment_score=0.8,
                min_score_delta=0.05,
                max_regressions=0,
                max_error_rate_increase=0.0,
                max_score_stddev=0.15,
                max_token_increase_ratio=0.5,
                max_tool_call_increase_ratio=0.5,
            )
        )

    def test_session_split_is_deterministic_and_disjoint(self) -> None:
        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            first = self.initialize(first_dir)
            second = self.initialize(second_dir)

            self.assertEqual(first["splits"], second["splits"])
            split_ids = [set(values) for values in first["splits"].values()]
            self.assertTrue(all(split_ids))
            self.assertEqual(5, len(set().union(*split_ids)))
            self.assertFalse(split_ids[0] & split_ids[1])
            self.assertFalse(split_ids[0] & split_ids[2])
            self.assertFalse(split_ids[1] & split_ids[2])

    def test_three_cases_leave_one_case_in_every_partition(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = self.initialize(run_dir, case_count=3)

            self.assertEqual(
                {"authoring": 1, "development": 1, "heldout": 1},
                {name: len(values) for name, values in state["splits"].items()},
            )

    def test_generated_vally_spec_uses_stimulus_rubric(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = self.initialize(run_dir)
            spec = json.loads(
                Path(state["evalSpecs"]["development"]).read_text(encoding="utf-8")
            )

            self.assertIn("rubric", spec["stimuli"][0])
            self.assertNotIn(
                "rubric",
                spec["stimuli"][0]["graders"][0]["config"],
            )
            self.assertEqual(0.8, spec["scoring"]["threshold"])

    def test_failed_development_result_requests_revision(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = self.initialize(run_dir)
            development = state["splits"]["development"]
            controller.record_result(
                state,
                "development",
                "baseline",
                result(development, score=0.5, passed=False),
            )
            controller.record_result(
                state,
                "development",
                "treatment",
                result(development, score=0.5, passed=False),
            )

            action = controller.next_action(state)
            self.assertEqual("revision", state["phase"])
            self.assertEqual("revise-proposal", action["kind"])
            self.assertIn("scoreDelta", action["failedGates"])
            self.assertEqual(development, [item["caseId"] for item in action["treatmentFailures"]])

    def test_revision_reuses_baseline_and_advances_iteration(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = self.initialize(run_dir)
            development = state["splits"]["development"]
            controller.record_result(
                state,
                "development",
                "baseline",
                result(development, score=0.6, passed=False),
            )
            controller.record_result(
                state,
                "development",
                "treatment",
                result(development, score=0.6, passed=False),
            )
            revised = Path(run_dir) / "revision-2" / "SKILL.md"
            revised.parent.mkdir()
            revised.write_text("# Revised\n", encoding="utf-8")

            controller.record_revision(state, str(revised), "version-2")

            action = controller.next_action(state)
            self.assertEqual(2, state["iteration"])
            self.assertEqual("development_treatment", state["phase"])
            self.assertEqual("treatment", action["arm"])
            self.assertEqual(str(revised.resolve()), action["skillPath"])
            self.assertIsNotNone(state["results"]["development"]["baseline"])

    def test_accepted_development_advances_to_heldout(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = self.initialize(run_dir)
            development = state["splits"]["development"]
            controller.record_result(
                state,
                "development",
                "baseline",
                result(development, score=0.5, passed=False),
            )
            controller.record_result(
                state,
                "development",
                "treatment",
                result(development, score=0.9, passed=True, tokens=120, tool_calls=12),
            )

            action = controller.next_action(state)
            self.assertEqual("heldout_baseline", state["phase"])
            self.assertEqual("heldout", action["split"])
            self.assertEqual("baseline", action["arm"])

    def test_heldout_failure_rejects_without_another_revision(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = self.initialize(run_dir)
            development = state["splits"]["development"]
            heldout = state["splits"]["heldout"]
            controller.record_result(
                state,
                "development",
                "baseline",
                result(development, score=0.5, passed=False),
            )
            controller.record_result(
                state,
                "development",
                "treatment",
                result(development, score=0.9, passed=True),
            )
            controller.record_result(
                state,
                "heldout",
                "baseline",
                result(heldout, score=0.5, passed=False),
            )
            controller.record_result(
                state,
                "heldout",
                "treatment",
                result(heldout, score=0.5, passed=False),
            )

            self.assertEqual("rejected", state["status"])
            self.assertEqual("heldout_acceptance_gates_failed", state["decision"])
            self.assertIsNone(controller.next_action(state))

    def test_score_gain_cannot_hide_case_regression(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = self.initialize(run_dir, case_count=6)
            development = state["splits"]["development"]
            self.assertGreaterEqual(len(development), 1)
            baseline = result(development, score=0.7, passed=True)
            treatment = result(development, score=0.9, passed=True)
            treatment["cases"][0]["passed"] = False

            assessment = controller.assess_pair(
                state,
                controller.validate_result(state, "development", "baseline", baseline),
                controller.validate_result(state, "development", "treatment", treatment),
            )

            self.assertFalse(assessment["passed"])
            self.assertEqual([development[0]], assessment["regressedCaseIds"])
            self.assertIn("regressions", assessment["failedGates"])

    def test_max_iterations_rejects_unimproved_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = self.initialize(run_dir)
            state["iteration"] = state["limits"]["maxIterations"]
            development = state["splits"]["development"]
            controller.record_result(
                state,
                "development",
                "baseline",
                result(development, score=0.6, passed=False),
            )
            controller.record_result(
                state,
                "development",
                "treatment",
                result(development, score=0.6, passed=False),
            )

            self.assertEqual("rejected", state["status"])
            self.assertEqual(
                "max_iterations_without_development_acceptance",
                state["decision"],
            )

    def test_rejects_result_with_too_few_runs(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = self.initialize(run_dir)
            document = result(
                state["splits"]["development"],
                score=0.9,
                passed=True,
            )
            document["runCount"] = 1

            with self.assertRaisesRegex(ValueError, "runsPerCase"):
                controller.validate_result(
                    state,
                    "development",
                    "baseline",
                    document,
                )

    def test_unstable_treatment_requests_revision(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = self.initialize(run_dir)
            development = state["splits"]["development"]
            baseline = result(development, score=0.5, passed=False)
            treatment = result(development, score=0.9, passed=True)
            treatment["metrics"]["scoreStdDev"] = 0.3
            controller.record_result(
                state,
                "development",
                "baseline",
                baseline,
            )
            controller.record_result(
                state,
                "development",
                "treatment",
                treatment,
            )

            self.assertEqual("revision", state["phase"])
            self.assertIn("stability", state["lastAssessment"]["failedGates"])


if __name__ == "__main__":
    unittest.main()
