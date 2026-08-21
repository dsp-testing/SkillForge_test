#!/usr/bin/env python3

"""Measure worker orchestration cost on the historical 286-session run shape."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from test_extraction_worker import SCRIPTS_DIR, Harness

HISTORICAL_SESSION_COUNT = 286
HISTORICAL_BATCH_SIZE = 25
MAX_CONCURRENT_BATCHES = 3


def legacy_wave_commands(
    harness: Harness,
    actions: list[dict[str, object]],
) -> list[str]:
    """The exact shell commands SKILL.md required per wave before the worker.

    One `extraction-controller.py next`, then per action one
    `materialize-session-query.py`, one `record-success`, and one `mv` of the
    controller's `--out` state, then one `checkpoint-completed-batches.py`.
    """
    run_dir = harness.run_dir
    state = run_dir / "extraction-state.json"
    commands = [
        f'python3 "{SCRIPTS_DIR}/extraction-controller.py" next'
        f' --state "{state}" --parallel --out "{run_dir}/actions.json"'
    ]
    for action in actions:
        commands.extend(
            [
                'RESULT_PATH="$(python3'
                f' "{SCRIPTS_DIR}/materialize-session-query.py"'
                f' --actions "{run_dir}/actions.json"'
                f' --action "{action["actionId"]}")"',
                f'python3 "{SCRIPTS_DIR}/extraction-controller.py" record-success'
                f' --state "{state}" --action "{action["actionId"]}"'
                f' --result "$RESULT_PATH" --out "{state}.next"',
                f'mv "{state}.next" "{state}"',
            ]
        )
    commands.append(
        f'python3 "{SCRIPTS_DIR}/checkpoint-completed-batches.py"'
        f' --state "{state}" --ledger "{run_dir}/primitives.sanitized.json"'
        f' --main-branch main --out "{run_dir}/checkpoint-summary.json"'
    )
    return commands


def legacy_wave_bytes(harness: Harness, actions: list[dict[str, object]]) -> int:
    """Command text plus every output the model had to read, minus tool results."""
    manifest = json.dumps(
        {
            "kind": "action-batch",
            "status": "running",
            "terminal": False,
            "actions": actions,
        },
        indent=2,
    )
    return (
        sum(len(command) + 1 for command in legacy_wave_commands(harness, actions))
        + len(manifest)
        + sum(len(str(action["outputPath"])) + 1 for action in actions)
    )


def simulate(session_count: int = HISTORICAL_SESSION_COUNT) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as temporary:
        harness = Harness(
            Path(temporary),
            [f"session-{index:04d}" for index in range(session_count)],
        )
        envelope = harness.start(
            session_batch_size=str(HISTORICAL_BATCH_SIZE),
            max_concurrent_batches=str(MAX_CONCURRENT_BATCHES),
        )
        advance_command = (
            f'python3 "{SCRIPTS_DIR}/extraction-worker.py"'
            f' advance --run-dir "{harness.run_dir}"'
        )
        waves = 0
        actions = 0
        worker_bytes = len(json.dumps(envelope, indent=2))
        legacy_bytes = 0
        legacy_commands = 3

        while envelope["kind"] == "wave":
            waves += 1
            assert waves < 200, "simulation did not converge"
            manifest = json.loads(
                Path(envelope["wave"]["manifestPath"]).read_text(encoding="utf-8")
            )
            actions += envelope["wave"]["actionCount"]
            legacy_commands += 2 + 3 * envelope["wave"]["actionCount"]
            legacy_bytes += legacy_wave_bytes(harness, manifest["actions"])
            for action in envelope["wave"]["actions"]:
                harness.respond(action)
            envelope = harness.advance()
            worker_bytes += len(advance_command) + 1 + len(
                json.dumps(envelope, indent=2)
            )

        return {
            "sessionCount": session_count,
            "batchCount": envelope["progress"]["batchCount"],
            "status": envelope["status"],
            "completedSessionCount": envelope["coverage"]["completedSessionCount"],
            "toolWaves": waves,
            "toolCalls": actions,
            "workerCommands": waves + 2,
            "workerModelTurns": 2 * waves + 1,
            "workerFusedModelTurns": waves + 1,
            "workerOrchestrationBytes": worker_bytes,
            "legacyCommands": legacy_commands,
            "legacyModelTurns": 3 * waves + 2,
            "legacyOrchestrationBytes": legacy_bytes,
        }


class HistoricalShapeSimulationTests(unittest.TestCase):
    def test_worker_cuts_orchestration_commands_and_turns(self) -> None:
        report = simulate()

        self.assertEqual("complete", report["status"])
        self.assertEqual(HISTORICAL_SESSION_COUNT, report["completedSessionCount"])
        self.assertEqual(12, report["batchCount"])
        self.assertEqual(17, report["toolWaves"])
        self.assertEqual(49, report["toolCalls"])
        self.assertEqual(19, report["workerCommands"])
        self.assertEqual(184, report["legacyCommands"])
        self.assertEqual(35, report["workerModelTurns"])
        self.assertEqual(53, report["legacyModelTurns"])
        self.assertEqual(18, report["workerFusedModelTurns"])
        self.assertLess(report["workerCommands"] * 9, report["legacyCommands"])
        self.assertLess(
            report["workerOrchestrationBytes"] * 1.3,
            report["legacyOrchestrationBytes"],
        )

    def test_simulated_run_leaves_no_raw_evidence_behind(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = Harness(
                Path(temporary),
                [f"session-{index:04d}" for index in range(60)],
            )
            envelope = harness.drive(
                harness.start(
                    session_batch_size=str(HISTORICAL_BATCH_SIZE),
                    max_concurrent_batches=str(MAX_CONCURRENT_BATCHES),
                )
            )

            extraction = harness.run_dir / "extraction"
            remaining = sorted(extraction.glob("*.json"))

            self.assertEqual("complete", envelope["status"])
            self.assertEqual([], sorted(extraction.glob("*.accepted.json")))
            self.assertEqual([], sorted(extraction.glob("metadata-*.json")))
            for artifact in remaining:
                self.assertEqual(
                    "discovery",
                    "discovery" if artifact.name.startswith("discover-") else "evidence",
                    artifact.name,
                )
            self.assertEqual(
                60,
                len(
                    json.loads(
                        (harness.run_dir / "primitives.sanitized.json").read_text(
                            encoding="utf-8"
                        )
                    )["primitives"]
                ),
            )


if __name__ == "__main__":
    unittest.main()
