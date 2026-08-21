#!/usr/bin/env python3

"""Regressions for interactions that only exist once the three branches combine.

The completion guard, the deterministic extraction worker, and the strict
trailing `_query_source` normalization are independently tested. These tests
cover the seams between them, which no single branch can exercise:

- worker harvesting materializes results that carry the runtime's trailing
  `_query_source` column;
- the worker's in-process checkpoint still feeds the guard's marker snapshot;
- the guard's `continuePrompt` steers the agent back into the worker loop
  rather than into raw controller commands the merged SKILL.md forbids.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import forge_marker  # noqa: E402
import test_extraction_worker as worker_tests  # noqa: E402


def load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


controller = load_script("integration_extraction_controller", "extraction-controller.py")
predicate = load_script("integration_completion_predicate", "completion-predicate.py")
worker = worker_tests.worker

NOW = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
BASE_RENDER = worker_tests.render_result
PIPED_COMMAND = 'go test ./... | tee "out | log"'


def render_with_query_source(kind: str, rows: list[dict[str, object]]) -> str:
    """Append the trailing provenance column the local session store supplies."""
    content = BASE_RENDER(kind, rows)
    if not rows:
        return content
    lines = content.splitlines()
    rendered = []
    for line in lines:
        if not (line.startswith("| ") and line.endswith(" |")):
            rendered.append(line)
            continue
        cell = "---" if set(line[2:-2].split(" | ")) == {"---"} else "local"
        cell = "_query_source" if line[2:-2].split(" | ")[0] == "session_id" else cell
        rendered.append(f"{line[:-2]} | {cell} |")
    return "\n".join(rendered)


class PipedHarness(worker_tests.Harness):
    """Harness whose tool-call rows embed a literal ` | ` inside a column value."""

    def rows_for(self, action: dict[str, object]) -> list[dict[str, object]]:
        rows = super().rows_for(action)
        if str(action["kind"]) == "tool-calls":
            for row in rows:
                row["arguments_json"] = json.dumps({"command": PIPED_COMMAND})
        return rows


class QuerySourceWorkerTests(unittest.TestCase):
    """PR #43 normalization applied on PR #42's harvesting path."""

    def test_worker_materializes_results_carrying_a_query_source_column(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            harness = PipedHarness(Path(root), ["session-1", "session-2"])
            with mock.patch.object(
                worker_tests, "render_result", render_with_query_source
            ):
                envelope = harness.start()
                self.assertEqual("wave", envelope["kind"])
                action = envelope["wave"]["actions"][0]
                harness.respond(action)

            issued = json.loads(harness.run_dir.joinpath("extraction-state.json").read_text())
            outcome = worker.probe_action(issued["issuedActions"][0], harness.events_root)

            self.assertEqual("success", outcome["outcome"])
            self.assertEqual(2, outcome["rowCount"])
            rows = json.loads(Path(outcome["resultPath"]).read_text(encoding="utf-8"))
            self.assertEqual(
                [
                    {"session_id": "session-1", "updated_at": "2026-08-17T00:00:00Z"},
                    {"session_id": "session-2", "updated_at": "2026-08-17T01:00:00Z"},
                ],
                rows,
            )
            for row in rows:
                self.assertNotIn("_query_source", row)

    def test_full_worker_run_completes_with_query_source_on_every_result(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            harness = PipedHarness(Path(root), ["session-1", "session-2"])
            with mock.patch.object(
                worker_tests, "render_result", render_with_query_source
            ):
                envelope = harness.drive(harness.start())

            self.assertEqual("terminal", envelope["kind"])
            self.assertEqual("complete", envelope["status"])
            self.assertEqual(0, envelope["recorded"]["counts"]["artifact"])
            self.assertEqual(0, envelope["recorded"]["counts"]["query-failure"])

            ledger = json.loads(
                harness.run_dir.joinpath("primitives.sanitized.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertNotIn("_query_source", json.dumps(ledger))

    def test_embedded_pipe_survives_query_source_stripping_through_the_worker(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            harness = PipedHarness(Path(root), ["session-1"])
            envelope = harness.start()
            while envelope["kind"] == "wave":
                actions = envelope["wave"]["actions"]
                tool_actions = [
                    action for action in actions if action["kind"] == "tool-calls"
                ]
                with mock.patch.object(
                    worker_tests, "render_result", render_with_query_source
                ):
                    for action in actions:
                        harness.respond(action)
                if tool_actions:
                    state = json.loads(
                        harness.run_dir.joinpath("extraction-state.json").read_text()
                    )
                    issued = {
                        str(action["actionId"]): action
                        for action in state["issuedActions"]
                    }
                    outcome = worker.probe_action(
                        issued[str(tool_actions[0]["actionId"])],
                        harness.events_root,
                    )
                    self.assertEqual("success", outcome["outcome"])
                    rows = json.loads(
                        Path(outcome["resultPath"]).read_text(encoding="utf-8")
                    )
                    self.assertTrue(rows)
                    for row in rows:
                        self.assertEqual(
                            PIPED_COMMAND,
                            json.loads(row["arguments_json"])["command"],
                        )
                        self.assertNotIn("_query_source", row)
                    return
                envelope = harness.advance()
            self.fail("worker never issued a tool-calls action")


class WorkerCheckpointMarkerTests(unittest.TestCase):
    """PR #42 checkpointing still feeding PR #41 marker diagnostics."""

    def test_worker_writes_the_checkpoint_summary_the_marker_reports(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            harness = worker_tests.Harness(Path(root), ["session-1", "session-2"])
            envelope = harness.drive(harness.start())
            self.assertEqual("terminal", envelope["kind"])

            summary_path = harness.run_dir / "checkpoint-summary.json"
            self.assertTrue(summary_path.is_file(), "worker did not publish a checkpoint summary")
            summary = json.loads(summary_path.read_text(encoding="utf-8"))

            self.assertFalse(summary["failed"])
            self.assertTrue(summary["terminalCoverageAttached"])
            self.assertGreaterEqual(summary["checkpointedBatchCount"], 0)

    def test_marker_snapshot_embeds_the_worker_checkpoint_summary(self) -> None:
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as marker_dir:
            harness = worker_tests.Harness(Path(root), ["session-1", "session-2"])
            harness.drive(harness.start())
            state_path = harness.run_dir / "extraction-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            summary_path = harness.run_dir / "checkpoint-summary.json"

            marker = forge_marker.build_marker(
                state=state,
                state_path=state_path,
                checkpoint_path=summary_path,
                ledger_path=harness.run_dir / "primitives.sanitized.json",
                skill_dir=SKILL_DIR,
                predicate_path=SCRIPTS_DIR / "completion-predicate.py",
                now=NOW,
            )
            marker["snapshot"] = controller.diagnostics(
                state,
                checkpoint=json.loads(summary_path.read_text(encoding="utf-8")),
            )
            forge_marker.write_marker(Path(marker_dir) / forge_marker.MARKER_FILENAME, marker)

            result = predicate.evaluate(
                controller=controller,
                marker_path=Path(marker_dir) / forge_marker.MARKER_FILENAME,
                max_age_seconds=forge_marker.DEFAULT_MAX_AGE_SECONDS,
                require_marker=True,
                max_pending_ids=20,
                now=NOW + timedelta(seconds=60),
            )

            self.assertEqual("complete", result["status"])
            self.assertEqual("extraction-terminal", result["verdict"])
            self.assertIsNotNone(result["snapshot"]["checkpoint"])
            self.assertFalse(result["snapshot"]["checkpoint"]["failed"])

    def test_unreadable_checkpoint_summary_never_changes_the_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as marker_dir:
            harness = worker_tests.Harness(Path(root), ["session-1", "session-2"])
            harness.drive(harness.start())
            state_path = harness.run_dir / "extraction-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            summary_path = harness.run_dir / "checkpoint-summary.json"
            summary_path.write_text("{ not json", encoding="utf-8")

            marker = forge_marker.build_marker(
                state=state,
                state_path=state_path,
                checkpoint_path=summary_path,
                skill_dir=SKILL_DIR,
                predicate_path=SCRIPTS_DIR / "completion-predicate.py",
                now=NOW,
            )
            forge_marker.write_marker(Path(marker_dir) / forge_marker.MARKER_FILENAME, marker)

            result = predicate.evaluate(
                controller=controller,
                marker_path=Path(marker_dir) / forge_marker.MARKER_FILENAME,
                max_age_seconds=forge_marker.DEFAULT_MAX_AGE_SECONDS,
                require_marker=True,
                max_pending_ids=20,
                now=NOW + timedelta(seconds=60),
            )

            self.assertEqual("complete", result["status"])
            self.assertEqual("extraction-terminal", result["verdict"])
            self.assertIsNone(result["snapshot"]["checkpoint"])

    def test_checkpoint_summary_cannot_make_a_running_run_look_complete(self) -> None:
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as marker_dir:
            harness = worker_tests.Harness(Path(root), ["session-1", "session-2"])
            envelope = harness.start()
            self.assertEqual("wave", envelope["kind"])
            state_path = harness.run_dir / "extraction-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            summary_path = harness.run_dir / "checkpoint-summary.json"
            summary_path.write_text(
                json.dumps({"failed": False, "terminalCoverageAttached": True}),
                encoding="utf-8",
            )

            marker = forge_marker.build_marker(
                state=state,
                state_path=state_path,
                checkpoint_path=summary_path,
                skill_dir=SKILL_DIR,
                predicate_path=SCRIPTS_DIR / "completion-predicate.py",
                now=NOW,
            )
            forge_marker.write_marker(Path(marker_dir) / forge_marker.MARKER_FILENAME, marker)

            result = predicate.evaluate(
                controller=controller,
                marker_path=Path(marker_dir) / forge_marker.MARKER_FILENAME,
                max_age_seconds=forge_marker.DEFAULT_MAX_AGE_SECONDS,
                require_marker=True,
                max_pending_ids=20,
                now=NOW + timedelta(seconds=60),
            )

            self.assertEqual("incomplete", result["status"])
            self.assertEqual("extraction-running", result["verdict"])


class GuardPromptsWorkerLoopTests(unittest.TestCase):
    """PR #41 continue prompts must not contradict PR #42's worker protocol."""

    def guarded(self, state: dict, run_dir: str, marker_dir: str) -> Path:
        state_path = Path(run_dir) / "extraction-state.json"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        marker_path = Path(marker_dir) / forge_marker.MARKER_FILENAME
        marker = forge_marker.build_marker(
            state=state,
            state_path=state_path,
            skill_dir=SKILL_DIR,
            predicate_path=SCRIPTS_DIR / "completion-predicate.py",
            now=NOW,
        )
        forge_marker.write_marker(marker_path, marker)
        return marker_path

    def evaluate(self, marker_path: Path) -> dict:
        return predicate.evaluate(
            controller=controller,
            marker_path=marker_path,
            max_age_seconds=forge_marker.DEFAULT_MAX_AGE_SECONDS,
            require_marker=False,
            max_pending_ids=20,
            now=NOW + timedelta(seconds=60),
        )

    def running_state(self, run_dir: str) -> dict:
        state = controller.initialize(
            worker_tests_arguments(run_dir),
        )
        action = controller.next_action(state)
        assert action is not None
        output = Path(action["outputPath"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                [
                    {"session_id": "session-1", "updated_at": "2026-08-01T00:00:00Z"},
                    {"session_id": "session-2", "updated_at": "2026-08-02T00:00:00Z"},
                ]
            ),
            encoding="utf-8",
        )
        controller.record_success(state, action, str(output))
        return state

    def test_running_prompt_drives_the_worker_loop(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir, tempfile.TemporaryDirectory() as marker_dir:
            state = self.running_state(run_dir)

            result = self.evaluate(self.guarded(state, run_dir, marker_dir))
            prompt = result["continuePrompt"]

            self.assertEqual("extraction-running", result["verdict"])
            self.assertIn("Do not finish", prompt)
            self.assertIn("extraction-worker.py advance", prompt)
            self.assertIn("status --run-dir", prompt)
            self.assertIn("--assert-terminal", prompt)
            self.assertIn("run-marker.py refresh", prompt)
            self.assertNotIn("extraction-controller.py next", prompt)
            self.assertNotIn("record-success", prompt)
            self.assertNotIn("checkpoint-completed-batches.py", prompt)

    def test_blocked_prompt_asserts_through_the_worker(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir, tempfile.TemporaryDirectory() as marker_dir:
            state = self.running_state(run_dir)
            controller.record_checkpoint_failure(
                state,
                "completed batch could not be normalized and sanitized",
            )

            result = self.evaluate(self.guarded(state, run_dir, marker_dir))
            prompt = result["continuePrompt"]

            self.assertEqual("extraction-blocked", result["verdict"])
            self.assertIn("Do not report success", prompt)
            self.assertIn("extraction-worker.py status", prompt)
            self.assertIn("--assert-terminal", prompt)
            self.assertNotIn("extraction-controller.py assert-terminal", prompt)

    def test_missing_marker_prompt_starts_the_worker(self) -> None:
        with tempfile.TemporaryDirectory() as marker_dir:
            marker_path = Path(marker_dir) / forge_marker.MARKER_FILENAME

            result = predicate.evaluate(
                controller=controller,
                marker_path=marker_path,
                max_age_seconds=forge_marker.DEFAULT_MAX_AGE_SECONDS,
                require_marker=True,
                max_pending_ids=20,
                now=NOW,
            )

            self.assertEqual("marker-missing", result["verdict"])
            self.assertIn("extraction-worker.py start", result["continuePrompt"])
            self.assertIn("run-marker.py init", result["continuePrompt"])
            self.assertIn(
                "--checkpoint $RUN_DIR/checkpoint-summary.json",
                result["continuePrompt"],
            )
            self.assertIn(
                "--ledger $RUN_DIR/primitives.sanitized.json",
                result["continuePrompt"],
            )

    def test_running_verdict_never_orders_the_pre_worker_controller_loop(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir, tempfile.TemporaryDirectory() as marker_dir:
            state = self.running_state(run_dir)

            result = self.evaluate(self.guarded(state, run_dir, marker_dir))
            surfaced = f"{result['reason']}\n{result['continuePrompt']}"

            self.assertEqual("extraction-running", result["verdict"])
            self.assertIn("controller reported:", result["reason"])
            self.assertNotIn("invoking next", surfaced)
            self.assertNotIn("extraction-controller.py next", surfaced)
            self.assertNotIn("record-success", surfaced)


class WorkerAssertionMessageTests(unittest.TestCase):
    """The worker's own terminal assertion must steer back into the worker loop."""

    def test_assert_terminal_failure_names_the_worker_advance_command(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            harness = worker_tests.Harness(Path(root), ["session-1"])
            envelope = harness.start()
            self.assertEqual("wave", envelope["kind"])

            code, _, stderr = harness.invoke(
                "status",
                "--run-dir",
                str(harness.run_dir),
                "--assert-terminal",
            )

            self.assertEqual(1, code)
            self.assertIn("extraction is not terminal", stderr)
            self.assertIn("extraction-worker.py advance", stderr)
            self.assertNotIn("invoking next", stderr)
            self.assertNotIn("record-success", stderr)


class MergedContractTests(unittest.TestCase):
    """The merged SKILL.md must document all three behaviors at once."""

    def test_skill_documents_the_worker_driven_marker_lifecycle(self) -> None:
        contract = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

        worker_start = contract.index('scripts/extraction-worker.py" start')
        marker_init = contract.index('scripts/run-marker.py" init')
        marker_refresh = contract.index('scripts/run-marker.py" refresh')
        assertion = contract.index("--run-dir \"$RUN_DIR\" --assert-terminal")
        marker_finish = contract.index('scripts/run-marker.py" finish')

        self.assertLess(worker_start, marker_init)
        self.assertLess(marker_init, marker_refresh)
        self.assertLess(assertion, marker_finish)
        self.assertIn("never call `session_store_sql` outside a", contract)

    def test_guard_reference_documents_the_worker_lifecycle(self) -> None:
        reference = (SKILL_DIR / "reference" / "cca-completion-guard.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("extraction-worker.py start", reference)
        self.assertIn("extraction-worker.py advance", reference)
        self.assertIn("extraction-worker.py status --assert-terminal", reference)
        self.assertNotIn("After `extraction-controller.py init`", reference)


def worker_tests_arguments(run_dir: str):
    import argparse

    return argparse.Namespace(
        repository="owner/repository",
        start="2026-08-01T00:00:00Z",
        end="2026-08-08T00:00:00Z",
        run_dir=run_dir,
        discovery_page_size=100,
        session_batch_size=100,
        tool_page_size=500,
        max_rows=1000,
        max_artifact_bytes=10_000_000,
        min_window_minutes=15,
        max_concurrent_batches=3,
        max_query_retries=1,
        allow_partial=True,
        enable_tool_event_fallback=False,
    )


if __name__ == "__main__":
    unittest.main()
