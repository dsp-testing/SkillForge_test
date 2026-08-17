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

from session_queries import build_discovery_query

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
        "max_concurrent_batches": 3,
        "max_query_retries": 1,
        "allow_partial": True,
        "enable_tool_event_fallback": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def write_rows(path: str, rows: list[dict[str, object]]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(rows), encoding="utf-8")


class ExtractionControllerTests(unittest.TestCase):
    def test_discovery_uses_ordered_keyset_query(self) -> None:
        query = build_discovery_query(
            repository="owner/repository",
            start="2026-08-01T00:00:00Z",
            end="2026-08-08T00:00:00Z",
            limit=100,
            cursor={
                "updatedAt": "2026-08-04T12:00:00Z",
                "sessionId": "session-100",
            },
        )

        self.assertTrue(query.startswith("SELECT id AS session_id, updated_at"))
        self.assertIn("ORDER BY updated_at, id", query)
        self.assertIn("id > 'session-100'", query)
        self.assertNotIn("agent_name, repository, branch", query)

    def test_discovery_timeout_splits_materialized_session_window(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = controller.initialize(arguments(run_dir))
            action = controller.next_action(state)
            assert action is not None

            controller.record_failure(state, action, "query timed out")
            next_action = controller.next_action(state)
            assert next_action is not None

            self.assertEqual(2, len(state["partitions"]))
            self.assertEqual("sessions", next_action["strategy"])
            self.assertEqual(
                "2026-08-04T12:00:00Z",
                state["partitions"][0]["end"],
            )
            self.assertFalse(
                any(item["kind"] == "retry_same_unit" for item in state["retryHistory"])
            )
            self.assertEqual("running", state["status"])

    def test_unicorn_discovery_failure_splits_materialized_session_window(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = controller.initialize(arguments(run_dir))
            primary = controller.next_action(state)
            assert primary is not None

            controller.record_failure(
                state,
                primary,
                "SQL Error: GitHub Unicorn HTML response",
                error_kind="other",
            )
            next_action = controller.next_action(state)

            self.assertEqual("running", state["status"])
            self.assertEqual("sessions", next_action["strategy"])
            self.assertEqual(2, len(state["partitions"]))
            self.assertFalse(state["blockers"])

    def test_explicit_other_is_not_reclassified_as_deterministic_kind(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = controller.initialize(arguments(run_dir))
            primary = controller.next_action(state)
            assert primary is not None

            controller.record_failure(
                state,
                primary,
                "unexpected column-shaped response",
                error_kind="other",
            )

            self.assertEqual("blocked", state["status"])
            self.assertEqual("other", state["blockers"][-1]["errorKind"])

    def test_discovery_overflow_pages_without_splitting(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = controller.initialize(arguments(run_dir))
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

            metadata = controller.next_action(state)

            self.assertEqual("metadata", metadata["kind"])
            self.assertEqual(1, len(state["partitions"]))
            self.assertEqual(100, len(state["partitions"][0]["sessions"]))
            self.assertEqual(
                {
                    "updatedAt": "2026-08-07T12:00:00Z",
                    "sessionId": "session-099",
                },
                state["partitions"][0]["discoveryCursor"],
            )

    def test_parallel_scheduler_returns_three_independent_batches(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = controller.initialize(
                arguments(
                    run_dir,
                    discovery_page_size=10,
                    session_batch_size=2,
                )
            )
            discovery = controller.next_action(state)
            assert discovery is not None
            write_rows(
                discovery["outputPath"],
                [
                    {
                        "session_id": f"session-{index}",
                        "updated_at": f"2026-08-07T12:0{index}:00Z",
                    }
                    for index in range(6)
                ],
            )
            controller.record_success(state, discovery, discovery["outputPath"])

            actions = controller.next_actions(state, 3)

            self.assertEqual(3, len(actions))
            self.assertTrue(all(action["kind"] == "metadata" for action in actions))
            self.assertEqual(3, len({action["batchId"] for action in actions}))

    def test_parallel_scheduler_keeps_discovery_sequential(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = controller.initialize(arguments(run_dir))

            actions = controller.next_actions(state, 3)

            self.assertEqual(1, len(actions))
            self.assertEqual("discovery", actions[0]["kind"])

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
            batch = state["partitions"][0]["batches"][0]
            self.assertEqual("refs", batch["status"])
            self.assertEqual("2026-08-07T12:01:00Z", batch["sourceEnd"])

    def test_metadata_timeout_omits_single_session_batch(self) -> None:
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

            controller.record_failure(state, metadata, "query timed out")
            done = controller.next_action(state)
            batch = state["partitions"][0]["batches"][0]

            self.assertIsNone(done)
            self.assertEqual("partial", state["status"])
            self.assertEqual("omitted", batch["status"])
            self.assertFalse(
                any(
                    item["kind"] == "retry_same_unit"
                    for item in state["retryHistory"]
                )
            )

    def test_refs_timeout_omits_batch_without_page_reduction(self) -> None:
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
            refs = controller.next_action(state)
            assert refs is not None

            controller.record_failure(state, refs, "query timed out")
            done = controller.next_action(state)
            batch = state["partitions"][0]["batches"][0]

            self.assertIsNone(done)
            self.assertEqual("partial", state["status"])
            self.assertEqual("omitted", batch["status"])
            self.assertEqual(500, batch["pageSize"])
            self.assertFalse(
                any(
                    item["kind"] == "reduce_evidence_page"
                    for item in state["retryHistory"]
                )
            )

    def test_refs_timeout_blocks_without_page_reduction_when_omission_fails(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = controller.initialize(
                arguments(run_dir, allow_partial=False)
            )
            discovery = controller.next_action(state)
            assert discovery is not None
            write_rows(
                discovery["outputPath"],
                [{"session_id": "session-1", "updated_at": "2026-08-07T12:00:00Z"}],
            )
            controller.record_success(state, discovery, discovery["outputPath"])
            batch = state["partitions"][0]["batches"][0]
            batch["status"] = "refs"
            batch["sourceStart"] = "2026-08-07T10:00:00Z"
            batch["sourceEnd"] = "2026-08-07T12:01:00Z"
            refs = controller.next_action(state)
            assert refs is not None

            controller.record_failure(state, refs, "query timed out")

            self.assertEqual("blocked", state["status"])
            self.assertEqual(500, batch["pageSize"])
            self.assertFalse(
                any(
                    item["kind"] == "reduce_evidence_page"
                    for item in state["retryHistory"]
                )
            )

    def test_tool_timeout_uses_event_fallback_then_omits(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = controller.initialize(
                arguments(run_dir, enable_tool_event_fallback=True)
            )
            discovery = controller.next_action(state)
            assert discovery is not None
            write_rows(
                discovery["outputPath"],
                [{"session_id": "session-1", "updated_at": "2026-08-07T12:00:00Z"}],
            )
            controller.record_success(state, discovery, discovery["outputPath"])
            batch = state["partitions"][0]["batches"][0]
            batch["status"] = "tools"
            batch["sourceStart"] = "2026-08-07T10:00:00Z"
            batch["sourceEnd"] = "2026-08-07T12:01:00Z"
            tools = controller.next_action(state)
            assert tools is not None

            controller.record_failure(state, tools, "query timed out")
            fallback = controller.next_action(state)
            assert fallback is not None

            self.assertEqual("events", fallback["strategy"])
            self.assertEqual(500, batch["pageSize"])

            controller.record_failure(state, fallback, "query timed out")
            done = controller.next_action(state)

            self.assertIsNone(done)
            self.assertEqual("partial", state["status"])
            self.assertEqual("omitted", batch["status"])

    def test_discovery_continues_after_four_materialized_window_omissions(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = controller.initialize(arguments(run_dir))
            primary = controller.next_action(state)
            assert primary is not None
            controller.record_failure(state, primary, "query timed out")
            while len(state["omittedUnits"]) < 4:
                action = controller.next_action(state)
                assert action is not None
                controller.record_failure(state, action, "query timed out")

            self.assertEqual("running", state["status"])
            self.assertEqual(4, len(state["omittedUnits"]))
            self.assertEqual(11, len(state["partitions"]))
            self.assertTrue(
                all(
                    item["kind"] == "split_time"
                    for item in state["retryHistory"]
                )
            )

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

    def test_irreducible_dense_discovery_uses_keyset_pagination(self) -> None:
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

            self.assertEqual("running", state["status"])
            self.assertFalse(state["omittedUnits"])
            self.assertEqual(100, len(state["partitions"][0]["sessions"]))
            self.assertFalse(state["partitions"][0]["discoveryComplete"])

    def test_later_discovery_page_timeout_retains_accepted_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = controller.initialize(
                arguments(
                    run_dir,
                    start="2026-08-01T00:00:00Z",
                    end="2026-08-01T00:20:00Z",
                    discovery_page_size=2,
                    min_window_minutes=15,
                )
            )
            first_page = controller.next_action(state)
            assert first_page is not None
            write_rows(
                first_page["outputPath"],
                [
                    {
                        "session_id": f"session-{index}",
                        "updated_at": f"2026-08-01T00:0{index}:00Z",
                    }
                    for index in range(3)
                ],
            )
            controller.record_success(state, first_page, first_page["outputPath"])
            state["partitions"][0]["batches"][0]["status"] = "complete"

            second_page = controller.next_action(state)
            assert second_page is not None
            self.assertEqual("discovery", second_page["kind"])
            self.assertIn("id > 'session-1'", second_page["sql"])

            controller.record_failure(state, second_page, "query timed out")
            controller.next_action(state)

            self.assertEqual("partial", state["status"])
            self.assertEqual(2, len(state["partitions"][0]["sessions"]))
            self.assertEqual(
                {
                    "updatedAt": "2026-08-01T00:01:00Z",
                    "sessionId": "session-1",
                },
                state["omittedUnits"][0]["cursor"],
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

    def test_discovery_cli_rejects_incomplete_session_cursor(self) -> None:
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
            "discovery cursor requires --after-session-id and --after-updated-at",
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
                "toolEventFallbackEnabled": False,
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
