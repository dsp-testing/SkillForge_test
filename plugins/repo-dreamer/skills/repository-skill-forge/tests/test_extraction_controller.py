#!/usr/bin/env python3

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from forge_common import timestamp_text
from session_queries import build_discovery_query

CONTROLLER_SPEC = importlib.util.spec_from_file_location(
    "extraction_controller",
    SCRIPTS_DIR / "extraction-controller.py",
)
assert CONTROLLER_SPEC is not None and CONTROLLER_SPEC.loader is not None
controller = importlib.util.module_from_spec(CONTROLLER_SPEC)
CONTROLLER_SPEC.loader.exec_module(controller)


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
        "max_discovery_failures": 8,
        "max_discovery_minutes": 10,
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
    def test_discovery_uses_narrow_id_keyset_query(self) -> None:
        query = build_discovery_query(
            repository="owner/repository",
            start="2026-08-01T00:00:00Z",
            end="2026-08-08T00:00:00Z",
            limit=100,
            after_session_id="session-10",
        )

        self.assertTrue(query.startswith("SELECT id AS session_id, updated_at"))
        self.assertIn("AND id > 'session-10'", query)
        self.assertIn("ORDER BY id", query)
        self.assertNotIn("ORDER BY updated_at", query)
        self.assertNotIn("agent_name, repository, branch", query)

    def test_discovery_timeout_splits_without_identical_retry(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = controller.initialize(arguments(run_dir))
            action = controller.next_action(state)
            assert action is not None

            controller.record_failure(state, action, "query timed out")

            self.assertEqual(2, len(state["partitions"]))
            self.assertFalse(
                any(item["kind"] == "retry_same_unit" for item in state["retryHistory"])
            )
            self.assertEqual("running", state["status"])

    def test_discovery_finishes_before_metadata_extraction(self) -> None:
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

            next_page = controller.next_action(state)

            self.assertEqual("discovery", next_page["kind"])
            self.assertIn("AND id > 'session-099'", next_page["sql"])

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

    def test_discovery_failure_budget_returns_explicit_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = controller.initialize(
                arguments(run_dir, max_discovery_failures=1)
            )
            action = controller.next_action(state)
            assert action is not None

            controller.record_failure(state, action, "query timed out")

            self.assertEqual("blocked", state["status"])
            self.assertEqual(
                "discovery_budget_exhausted",
                state["blockers"][-1]["reason"],
            )

    def test_discovery_time_budget_blocks_before_another_query(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = controller.initialize(arguments(run_dir))
            state["discoveryStartedAt"] = timestamp_text(
                datetime.now(timezone.utc) - timedelta(minutes=11)
            )

            action = controller.next_action(state)

            self.assertIsNone(action)
            self.assertEqual("blocked", state["status"])
            self.assertEqual(
                "discovery_budget_exhausted",
                state["blockers"][-1]["reason"],
            )


if __name__ == "__main__":
    unittest.main()
