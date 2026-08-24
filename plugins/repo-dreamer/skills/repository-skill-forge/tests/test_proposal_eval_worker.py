#!/usr/bin/env python3

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


def load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


controller = load_script("proposal_eval_controller_worker", "proposal-eval-controller.py")
worker = load_script("proposal_eval_worker", "run-proposal-evaluation.py")


VALID_SKILL = """---
name: repository-helper
description: Use when repository changes require the verified helper workflow and produce a checked result.
generated-by: forge-agent
---

# Repository Helper

**Abstraction level:** compositional

## Purpose

Provide a repeatable repository workflow with explicit validation and recovery.

## Conditions (C)

Use when a repository task matches the documented helper workflow and boundaries.

## Interface (R)

Accept repository inputs and produce a validated repository change with evidence.

## Policy (π)

Inspect the relevant repository guidance, apply the smallest compatible change,
run the documented validation, and revise only when the validation identifies a
specific failure.

## Termination (T)

Finish only when the repository outcome is verified; otherwise report the exact failure.

## Always do

Preserve repository behavior and validate the resulting change.

## Never do

Never invent repository commands, paths, or successful outcomes.

## Gotchas / edge cases

Treat missing dependencies and ambiguous repository guidance as explicit blockers.

## Assets and scripts

Use repository-provided validation commands and existing helper scripts.

## Scope boundaries

Do not expand beyond the selected repository workflow or modify unrelated files.
"""


def write_executable(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)


class ProposalEvalWorkerTests(unittest.TestCase):
    def initialize_state(self, root: Path) -> tuple[Path, Path]:
        cases = {
            "cases": [
                {
                    "caseId": f"case-{index}",
                    "sessionHash": f"session-{index}",
                    "prompt": f"Complete task {index}.",
                    "rubric": [f"Task {index} is correct."],
                }
                for index in range(5)
            ]
        }
        cases_path = root / "cases.json"
        cases_path.write_text(json.dumps(cases), encoding="utf-8")
        initial_skill = root / "initial" / "repository-helper" / "SKILL.md"
        initial_skill.parent.mkdir(parents=True)
        initial_skill.write_text(VALID_SKILL, encoding="utf-8")
        state = controller.initialize(
            argparse.Namespace(
                cases=cases_path,
                proposal_key="repository-helper",
                proposal_version="version-1",
                skill_path=str(initial_skill),
                run_dir=str(root / "proposal-run"),
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
        state_path = root / "evaluation-state.json"
        controller.write_json(state_path, state)
        proposal_path = root / "proposal.json"
        proposal_path.write_text(
            json.dumps(
                {
                    "proposalKey": "repository-helper",
                    "proposalVersion": "version-1",
                    "candidateIds": ["candidate-1"],
                    "decision": "create_skill",
                    "skillPath": str(initial_skill),
                }
            ),
            encoding="utf-8",
        )
        return state_path, proposal_path

    def fake_vally(self, root: Path) -> Path:
        path = root / "fake-vally"
        write_executable(
            path,
            r"""#!/usr/bin/env python3
import json
import sys
from pathlib import Path

if "--version" in sys.argv:
    print("0.14.0")
    raise SystemExit(0)

def value(flag):
    return sys.argv[sys.argv.index(flag) + 1]

spec = json.loads(Path(value("--eval-spec")).read_text(encoding="utf-8"))
output = Path(value("--output-dir")) / "2026-08-24T000000Z"
output.mkdir(parents=True)
runs = int(value("--runs"))
skill_dir = Path(value("--skill-dir")) if "--skill-dir" in sys.argv else None
is_baseline = skill_dir is None
is_revision = skill_dir is not None and "iteration-2" in str(skill_dir)
score = 0.4 if is_baseline else 0.9 if is_revision else 0.6
passed = score >= 0.8
tokens = 100 if is_baseline else 120
with (output / "results.jsonl").open("w", encoding="utf-8") as handle:
    for stimulus in spec["stimuli"]:
        for index in range(runs):
            record = {
                "type": "trial-result",
                "itemId": f"{stimulus['name']}-{index}",
                "evalName": spec["name"],
                "evalFilePath": value("--eval-spec"),
                "variant": "main",
                "stimulus": stimulus["name"],
                "trialIndex": index,
                "totalTrials": runs,
                "status": "success",
                "durationMs": 1000,
                "gradeResult": {
                    "name": "prompt",
                    "kind": "llm",
                    "passed": passed,
                    "score": score,
                    "evidence": "fixture result",
                    "stimulusName": stimulus["name"],
                    "trajectoryId": f"trajectory-{index}",
                    "timestamp": "2026-08-24T00:00:00Z"
                },
                "trajectory": {
                    "metrics": {
                        "tokenUsage": {"totalTokens": tokens},
                        "toolCallCount": 10,
                        "wallTimeMs": 900,
                        "errorCount": 0
                    }
                }
            }
            handle.write(json.dumps(record) + "\n")
raise SystemExit(1 if not passed else 0)
""",
        )
        return path

    def fake_reviser(self, root: Path) -> Path:
        path = root / "fake-reviser.py"
        write_executable(
            path,
            f"""#!/usr/bin/env python3
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--action")
parser.add_argument("--output")
parser.add_argument("--revision-dir")
args = parser.parse_args()
skill = Path(args.revision_dir) / "repository-helper" / "SKILL.md"
skill.parent.mkdir(parents=True)
skill.write_text({VALID_SKILL!r}, encoding="utf-8")
Path(args.output).write_text(json.dumps({{"skillPath": str(skill)}}), encoding="utf-8")
""",
        )
        return path

    def test_runs_baseline_revision_and_heldout_to_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir()
            state_path, proposal_path = self.initialize_state(root)
            vally = self.fake_vally(root)
            reviser = self.fake_reviser(root)
            summary_path = root / "summary.json"
            args = argparse.Namespace(
                state=state_path,
                repository=repository,
                workspace_root=root / "isolated-evaluation",
                summary=summary_path,
                proposal=proposal_path,
                vally_cli=str(vally),
                vally_version="0.14.0",
                model=None,
                judge_model=None,
                revision_command=(
                    f"{sys.executable} {reviser} --action {{action}} "
                    "--output {output} --revision-dir {revision_dir}"
                ),
                copilot_home=None,
                allow_host_execution=True,
                allow_env=[],
                timeout_seconds=30,
                max_actions=16,
            )

            result = worker.run_loop(args)

            self.assertEqual("accepted", result["status"])
            self.assertEqual(2, result["iteration"])
            self.assertEqual(6, result["actionsRun"])
            proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
            self.assertTrue(proposal["evaluation"]["accepted"])
            self.assertTrue(proposal["proposalVersion"].startswith("eval-"))
            self.assertIn("iteration-2", proposal["skillPath"])

    def test_blocks_when_workspace_is_inside_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir()
            state_path, _ = self.initialize_state(root)
            args = argparse.Namespace(
                state=state_path,
                repository=repository,
                workspace_root=repository / "evaluation",
                summary=root / "summary.json",
                proposal=None,
                vally_cli=str(self.fake_vally(root)),
                vally_version="0.14.0",
                model=None,
                judge_model=None,
                revision_command=None,
                copilot_home=None,
                allow_host_execution=True,
                allow_env=[],
                timeout_seconds=30,
                max_actions=16,
            )

            result = worker.run_loop(args)

            self.assertEqual("blocked", result["status"])
            self.assertIn("outside the repository", result["decision"])

    def test_blocks_without_explicit_host_execution_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir()
            state_path, _ = self.initialize_state(root)
            args = argparse.Namespace(
                state=state_path,
                repository=repository,
                workspace_root=root / "evaluation",
                summary=root / "summary.json",
                proposal=None,
                vally_cli=str(self.fake_vally(root)),
                vally_version="0.14.0",
                model=None,
                judge_model=None,
                revision_command=None,
                copilot_home=None,
                allow_host_execution=False,
                allow_env=[],
                timeout_seconds=30,
                max_actions=16,
            )

            result = worker.run_loop(args)

            self.assertEqual("blocked", result["status"])
            self.assertIn("allow-host-execution", result["decision"])

    def test_copilot_auth_home_copies_only_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host_home = root / "host-copilot"
            host_home.mkdir()
            (host_home / "config.json").write_text(
                '{"lastLoggedInUser":"example"}',
                encoding="utf-8",
            )
            (host_home / "mcp-config.json").write_text(
                '{"servers":{"unsafe":{}}}',
                encoding="utf-8",
            )
            action_root = root / "action"
            action_root.mkdir()

            environment = worker.action_environment(
                {"PATH": os.environ.get("PATH", "")},
                action_root,
                host_home,
            )

            isolated = Path(environment["COPILOT_HOME"])
            self.assertEqual(["config.json"], sorted(path.name for path in isolated.iterdir()))
            self.assertEqual("1", environment["EVALUATE_USE_HOST_COPILOT_HOME"])


if __name__ == "__main__":
    unittest.main()
