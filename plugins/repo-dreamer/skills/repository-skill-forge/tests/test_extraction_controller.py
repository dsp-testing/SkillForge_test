#!/usr/bin/env python3

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from session_queries import build_discovery_query, build_shutdown_discovery_query

CONTROLLER_SPEC = importlib.util.spec_from_file_location(
    "extraction_controller",
    SCRIPTS_DIR / "extraction-controller.py",
)
assert CONTROLLER_SPEC is not None and CONTROLLER_SPEC.loader is not None
controller = importlib.util.module_from_spec(CONTROLLER_SPEC)
CONTROLLER_SPEC.loader.exec_module(controller)

VALIDATOR_SPEC = importlib.util.spec_from_file_location(
    "validate_proposal",
    SCRIPTS_DIR / "validate-proposal.py",
)
assert VALIDATOR_SPEC is not None and VALIDATOR_SPEC.loader is not None
validator = importlib.util.module_from_spec(VALIDATOR_SPEC)
VALIDATOR_SPEC.loader.exec_module(validator)


def arguments(run_dir: str, **overrides: object) -> argparse.Namespace:
    values = {
        "repository": "owner/repository",
        "start": "2026-08-01T00:00:00Z",
        "end": "2026-08-08T00:00:00Z",
        "run_dir": run_dir,
        "discovery_page_size": 100,
        "session_batch_size": 100,
        "tool_page_size": 500,
        "max_rows": 1000,
        "max_artifact_bytes": 10_000_000,
        "min_window_minutes": 15,
        "max_query_retries": 1,
        "allow_partial": True,
        "enable_targeted_fallback": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def write_rows(path: str, rows: list[dict[str, object]]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(rows), encoding="utf-8")


class ExtractionControllerTests(unittest.TestCase):
    def test_discovery_uses_narrow_unordered_query(self) -> None:
        query = build_discovery_query(
            repository="owner/repository",
            start="2026-08-01T00:00:00Z",
            end="2026-08-08T00:00:00Z",
            limit=100,
        )

        self.assertTrue(query.startswith("SELECT id AS session_id, updated_at"))
        self.assertNotIn("ORDER BY", query)
        self.assertNotIn("agent_name, repository, branch", query)

    def test_discovery_timeout_switches_to_daily_shutdown_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = controller.initialize(
                arguments(run_dir, enable_targeted_fallback=True)
            )
            action = controller.next_action(state)
            assert action is not None

            controller.record_failure(state, action, "query timed out")
            fallback = controller.next_action(state)
            assert fallback is not None

            self.assertEqual(7, len(state["partitions"]))
            self.assertEqual("shutdown_events", fallback["strategy"])
            self.assertIn("type = 'session.shutdown'", fallback["sql"])
            self.assertFalse(
                any(item["kind"] == "retry_same_unit" for item in state["retryHistory"])
            )
            self.assertEqual("running", state["status"])

    def test_shutdown_discovery_query_is_ordered_and_cursor_bounded(self) -> None:
        query = build_shutdown_discovery_query(
            repository="owner/repository",
            start="2026-08-01T00:00:00Z",
            end="2026-08-02T00:00:00Z",
            limit=100,
            after_completed_at="2026-08-01T12:00:00Z",
            after_session_id="session-1",
            after_shutdown_event_id="event-1",
        )

        self.assertIn("type = 'session.shutdown'", query)
        self.assertIn("repository = 'owner/repository'", query)
        self.assertIn("ORDER BY timestamp, session_id, id", query)
        self.assertIn("(timestamp, session_id, id) >", query)

    def test_fallback_success_extracts_before_next_discovery_window(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = controller.initialize(
                arguments(run_dir, enable_targeted_fallback=True)
            )
            primary = controller.next_action(state)
            assert primary is not None
            controller.record_failure(state, primary, "query timed out")
            fallback = controller.next_action(state)
            assert fallback is not None
            write_rows(
                fallback["outputPath"],
                [
                    {
                        "session_id": "session-1",
                        "shutdown_event_id": "event-1",
                        "completed_at": "2026-08-01T12:00:00Z",
                        "shutdown_type": "completed",
                    }
                ],
            )
            controller.record_success(state, fallback, fallback["outputPath"])

            next_action = controller.next_action(state)

            self.assertEqual("metadata", next_action["kind"])
            self.assertEqual(
                ["session-1"],
                state["partitions"][0]["batches"][0]["sessionIds"],
            )
            self.assertFalse(state["partitions"][1]["discoveryComplete"])

    def test_fallback_timeout_omits_window_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = controller.initialize(
                arguments(run_dir, enable_targeted_fallback=True)
            )
            primary = controller.next_action(state)
            assert primary is not None
            controller.record_failure(state, primary, "query timed out")
            fallback = controller.next_action(state)
            assert fallback is not None

            controller.record_failure(state, fallback, "query timed out")
            next_action = controller.next_action(state)

            self.assertEqual("omitted", state["partitions"][0]["status"])
            self.assertEqual("discovery", state["omittedUnits"][0]["kind"])
            self.assertEqual("shutdown_events", next_action["strategy"])
            self.assertEqual(
                state["partitions"][1]["partitionId"],
                next_action["partitionId"],
            )

    def test_discovery_overflow_splits_without_accepting_rows(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = controller.initialize(
                arguments(run_dir, enable_targeted_fallback=True)
            )
            discovery = controller.next_action(state)
            assert discovery is not None
            rows = [
                {
                    "session_id": f"session-{index:03d}",
                    "updated_at": "2026-08-07T12:00:00Z",
                }
                for index in range(101)
            ]
            write_rows(discovery["outputPath"], rows)
            controller.record_success(state, discovery, discovery["outputPath"])

            next_partition = controller.next_action(state)

            self.assertEqual("discovery", next_partition["kind"])
            self.assertEqual(2, len(state["partitions"]))
            self.assertTrue(
                all(not partition["sessions"] for partition in state["partitions"])
            )

    def test_transient_metadata_failure_recovers_on_single_retry(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = controller.initialize(arguments(run_dir))
            discovery = controller.next_action(state)
            assert discovery is not None
            write_rows(
                discovery["outputPath"],
                [{"session_id": "session-1", "updated_at": "2026-08-07T12:00:00Z"}],
            )
            controller.record_success(state, discovery, discovery["outputPath"])
            metadata = controller.next_action(state)
            assert metadata is not None

            controller.record_failure(state, metadata, "temporary network connection failure")
            retry = controller.next_action(state)
            self.assertEqual(metadata["actionId"], retry["actionId"])

            write_rows(
                metadata["outputPath"],
                [
                    {
                        "session_id": "session-1",
                        "agent_name": "Copilot CLI",
                        "repository": "owner/repository",
                        "branch": "main",
                        "created_at": "2026-08-07T10:00:00Z",
                        "updated_at": "2026-08-07T12:00:00Z",
                    }
                ],
            )
            controller.record_success(state, metadata, metadata["outputPath"])

            retries = [
                item
                for item in state["retryHistory"]
                if item["kind"] == "retry_same_unit"
            ]
            self.assertEqual(1, len(retries))
            self.assertEqual("refs", state["partitions"][0]["batches"][0]["status"])

    def test_discovery_continues_after_four_fallback_omissions(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = controller.initialize(
                arguments(run_dir, enable_targeted_fallback=True)
            )
            primary = controller.next_action(state)
            assert primary is not None
            controller.record_failure(state, primary, "query timed out")
            for _ in range(4):
                action = controller.next_action(state)
                assert action is not None
                controller.record_failure(state, action, "query timed out")

            self.assertEqual("running", state["status"])
            self.assertEqual(5, state["workCounters"]["failedQueries"])
            self.assertEqual(4, len(state["omittedUnits"]))
            self.assertEqual(7, len(state["partitions"]))

    def test_irreducible_discovery_timeout_becomes_partial_omission(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = controller.initialize(
                arguments(
                    run_dir,
                    start="2026-08-01T00:00:00Z",
                    end="2026-08-01T00:20:00Z",
                    min_window_minutes=15,
                )
            )
            action = controller.next_action(state)
            assert action is not None

            controller.record_failure(state, action, "query timed out")
            done = controller.next_action(state)

            self.assertIsNone(done)
            self.assertEqual("partial", state["status"])
            self.assertFalse(state["coverage"]["discoveryComplete"])
            self.assertIsNone(state["coverage"]["sessionCoverage"])
            self.assertEqual("unknown", state["coverage"]["sessionCoverageStatus"])
            self.assertEqual("discovery", state["omittedUnits"][0]["kind"])
            self.assertEqual(
                "2026-08-01T00:00:00Z",
                state["omittedUnits"][0]["windowStart"],
            )

    def test_irreducible_dense_discovery_becomes_partial_omission(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = controller.initialize(
                arguments(
                    run_dir,
                    start="2026-08-01T00:00:00Z",
                    end="2026-08-01T00:20:00Z",
                    min_window_minutes=15,
                )
            )
            action = controller.next_action(state)
            assert action is not None
            write_rows(
                action["outputPath"],
                [
                    {
                        "session_id": f"session-{index:03d}",
                        "updated_at": "2026-08-01T00:10:00Z",
                    }
                    for index in range(101)
                ],
            )

            controller.record_success(state, action, action["outputPath"])
            controller.next_action(state)

            self.assertEqual("partial", state["status"])
            self.assertEqual(
                "discovery_partition_too_dense",
                state["omittedUnits"][0]["reason"],
            )

    def test_nonrecoverable_discovery_error_blocks_without_splitting(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = controller.initialize(arguments(run_dir))
            action = controller.next_action(state)
            assert action is not None

            controller.record_failure(
                state,
                action,
                "access denied for sessions",
            )

            self.assertEqual("blocked", state["status"])
            self.assertEqual(1, len(state["partitions"]))
            self.assertEqual("authorization", state["blockers"][-1]["errorKind"])
            self.assertFalse(
                any(item["kind"] == "split_time" for item in state["retryHistory"])
            )

    def test_irreducible_transient_discovery_error_blocks_without_batch_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = controller.initialize(
                arguments(
                    run_dir,
                    start="2026-08-01T00:00:00Z",
                    end="2026-08-01T00:20:00Z",
                    min_window_minutes=15,
                )
            )
            action = controller.next_action(state)
            assert action is not None

            controller.record_failure(
                state,
                action,
                "server error 503",
                error_kind="server",
            )
            controller.record_failure(
                state,
                action,
                "server error 503",
                error_kind="server",
            )

            self.assertEqual("blocked", state["status"])
            self.assertEqual("server error 503", state["blockers"][-1]["reason"])

    def test_action_id_can_replace_missing_action_file(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = controller.initialize(arguments(run_dir))
            action = controller.next_action(state)
            assert action is not None

            loaded = controller.load_action(action["actionId"], state)

            self.assertEqual(action, loaded)

    def test_discovery_cli_rejects_session_cursor(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS_DIR / "build-session-query.py"),
                "--kind",
                "discovery",
                "--repository",
                "owner/repository",
                "--start",
                "2026-08-01T00:00:00Z",
                "--end",
                "2026-08-08T00:00:00Z",
                "--after-session-id",
                "session-10",
            ],
            capture_output=True,
            check=False,
            text=True,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn(
            "discovery does not support --after-session-id",
            result.stderr,
        )

    def test_proposal_validator_accepts_unknown_partial_discovery_coverage(self) -> None:
        document = {
            "decision": "hold_as_pattern_only",
            "proposalKey": "partial-discovery",
            "candidateIds": ["candidate-1"],
            "proposalVersion": "version-1",
            "extraction": {
                "status": "partial",
                "discoveryComplete": False,
                "discoveredSessionCount": 2,
                "completedSessionCount": 2,
                "sessionCoverage": None,
                "sessionCoverageStatus": "unknown",
                "omittedUnitCount": 1,
                "omittedUnitKinds": ["discovery"],
                "targetedFallbackEnabled": False,
            },
            "review": {
                "leakageFindingCount": 0,
                "unresolvedConflictCount": 0,
                "executable": True,
                "branchSpecific": False,
            },
            "publication": {
                "duplicate": False,
                "reconciled": True,
                "action": "hold",
            },
        }

        self.assertEqual([], validator.validate(document))

if __name__ == "__main__":
    unittest.main()
