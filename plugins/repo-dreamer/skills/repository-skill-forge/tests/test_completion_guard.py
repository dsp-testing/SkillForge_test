#!/usr/bin/env python3

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import forge_marker


def load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


controller = load_script("extraction_controller", "extraction-controller.py")
predicate = load_script("completion_predicate", "completion-predicate.py")

NOW = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)


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


def discovered_state(run_dir: str, sessions: tuple[str, ...] = ("session-1", "session-2")):
    """Drive real discovery so diagnostics reflect genuine controller counters."""
    state = controller.initialize(arguments(run_dir))
    action = controller.next_action(state)
    assert action is not None
    output = Path(action["outputPath"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            [
                {
                    "session_id": session_id,
                    "updated_at": f"2026-08-0{index + 1}T00:00:00Z",
                }
                for index, session_id in enumerate(sessions)
            ]
        ),
        encoding="utf-8",
    )
    controller.record_success(state, action, str(output))
    return state


def complete_all_batches(state: dict) -> None:
    for partition in state["partitions"]:
        for batch in partition["batches"]:
            batch["status"] = "complete"
        partition["status"] = "complete"


def omit_first_batch(state: dict, reason: str = "post-discovery query timed out") -> None:
    partition = state["partitions"][0]
    batch = partition["batches"][0]
    controller.omit_batch(
        state,
        {"actionId": "tools-omitted-0", "kind": "tool-calls", "partitionId": partition["partitionId"]},
        partition,
        batch,
        reason,
    )


class DiagnosticsSnapshotTests(unittest.TestCase):
    def test_snapshot_reports_real_controller_counters(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = discovered_state(run_dir)

            snapshot = controller.diagnostics(state)

            self.assertEqual("extraction-diagnostics", snapshot["kind"])
            self.assertEqual("running", snapshot["status"])
            self.assertFalse(snapshot["terminal"])
            self.assertTrue(snapshot["consistent"])
            self.assertEqual("owner/repository", snapshot["repository"])
            self.assertEqual(1, snapshot["counters"]["queryAttempts"])
            self.assertEqual(1, snapshot["counters"]["successfulQueries"])
            self.assertEqual(0, snapshot["counters"]["failedQueries"])
            self.assertEqual(2, snapshot["counters"]["rows"])
            self.assertGreater(snapshot["counters"]["artifactBytes"], 0)
            self.assertEqual(2, snapshot["sessions"]["discovered"])
            self.assertEqual(0, snapshot["sessions"]["completed"])
            self.assertEqual(1, snapshot["batches"]["total"])
            self.assertEqual(1, snapshot["batches"]["pending"])
            self.assertEqual(1, snapshot["partitions"]["discoveryComplete"])
            self.assertEqual(0, snapshot["partitions"]["pendingDiscovery"])

    def test_snapshot_lists_issued_and_pending_action_ids(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = discovered_state(run_dir)
            actions = controller.next_actions(state, 3)
            expected = [action["actionId"] for action in actions]

            snapshot = controller.diagnostics(state)

            self.assertEqual(expected, snapshot["actions"]["issuedActionIds"])
            self.assertEqual(expected, snapshot["actions"]["pendingActionIds"])
            self.assertEqual(len(expected), snapshot["actions"]["pendingActionCount"])

    def test_snapshot_reports_completed_sessions_and_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = discovered_state(run_dir)
            complete_all_batches(state)
            controller.record_checkpoint_failure(state, "sanitization failed")

            snapshot = controller.diagnostics(state)

            self.assertEqual("blocked", snapshot["status"])
            self.assertTrue(snapshot["terminal"])
            self.assertEqual(2, snapshot["sessions"]["completed"])
            self.assertEqual(1, snapshot["batches"]["complete"])
            self.assertEqual("sanitization failed", snapshot["blocker"]["reason"])
            self.assertEqual(1, snapshot["blockerCount"])

    def test_snapshot_does_not_mutate_state(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = discovered_state(run_dir)
            del state["workCounters"]
            before = json.dumps(state, sort_keys=True)

            controller.diagnostics(state)

            self.assertEqual(before, json.dumps(state, sort_keys=True))

    def test_snapshot_flags_inconsistent_state(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = discovered_state(run_dir)
            state["status"] = "blocked"

            snapshot = controller.diagnostics(state)

            self.assertFalse(snapshot["consistent"])
            self.assertIn("terminal blocker", snapshot["invariantError"])

    def test_snapshot_attaches_checkpoint_summary(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = discovered_state(run_dir)

            snapshot = controller.diagnostics(
                state,
                checkpoint={"checkpointedBatchIds": ["batch-1"], "ledgerBytes": 42},
            )

            self.assertEqual(["batch-1"], snapshot["checkpoint"]["checkpointedBatchIds"])
            self.assertEqual(42, snapshot["checkpoint"]["ledgerBytes"])

    def test_corrupt_counters_never_produce_a_negative_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = discovered_state(run_dir)
            state["workCounters"]["rows"] = -5
            state["workCounters"]["toolCalls"] = "12"

            counters = controller.diagnostics(state)["counters"]

            self.assertEqual(0, counters["rows"])
            self.assertEqual(0, counters["toolCalls"])
            self.assertTrue(all(count >= 0 for count in counters.values()))


class RunMarkerTests(unittest.TestCase):
    def marker_for(self, run_dir: str, marker_dir: str, state: dict) -> tuple[Path, dict]:
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
        return marker_path, marker

    def test_run_id_is_stable_and_derived_from_state(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = discovered_state(run_dir)

            self.assertEqual(
                forge_marker.run_id_for(state),
                forge_marker.run_id_for(json.loads(json.dumps(state))),
            )

    def test_marker_update_is_atomic_and_leaves_no_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir, tempfile.TemporaryDirectory() as marker_dir:
            state = discovered_state(run_dir)
            marker_path, marker = self.marker_for(run_dir, marker_dir, state)
            original = marker_path.read_bytes()

            broken = dict(marker)
            broken["phase"] = "unsupported"
            with self.assertRaises(forge_marker.MarkerError):
                forge_marker.write_marker(marker_path, broken)

            self.assertEqual(original, marker_path.read_bytes())
            self.assertEqual(
                [forge_marker.MARKER_FILENAME],
                sorted(item.name for item in Path(marker_dir).iterdir()),
            )

    def test_advance_marker_increments_revision_without_mutating_original(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir, tempfile.TemporaryDirectory() as marker_dir:
            state = discovered_state(run_dir)
            marker_path, marker = self.marker_for(run_dir, marker_dir, state)

            updated = forge_marker.advance_marker(marker, snapshot={"status": "running"}, now=NOW)
            forge_marker.write_marker(marker_path, updated)

            self.assertEqual(0, marker["revision"])
            self.assertEqual(1, updated["revision"])
            self.assertEqual(1, forge_marker.read_marker(marker_path)["revision"])

    def test_malformed_marker_raises_marker_error(self) -> None:
        with tempfile.TemporaryDirectory() as marker_dir:
            marker_path = Path(marker_dir) / forge_marker.MARKER_FILENAME
            marker_path.write_text("{not json", encoding="utf-8")

            with self.assertRaisesRegex(forge_marker.MarkerError, "unreadable"):
                forge_marker.read_marker(marker_path)

    def test_missing_marker_raises_file_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as marker_dir:
            with self.assertRaises(FileNotFoundError):
                forge_marker.read_marker(Path(marker_dir) / forge_marker.MARKER_FILENAME)

    def test_staleness_uses_last_refresh_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir, tempfile.TemporaryDirectory() as marker_dir:
            state = discovered_state(run_dir)
            _, marker = self.marker_for(run_dir, marker_dir, state)

            fresh = NOW + timedelta(seconds=60)
            stale = NOW + timedelta(seconds=forge_marker.DEFAULT_MAX_AGE_SECONDS + 60)

            self.assertFalse(forge_marker.marker_is_stale(marker, now=fresh))
            self.assertTrue(forge_marker.marker_is_stale(marker, now=stale))
            self.assertFalse(
                forge_marker.marker_is_stale(marker, max_age_seconds=0, now=stale)
            )

    def test_marker_path_resolution_precedence(self) -> None:
        environ = {
            forge_marker.MARKER_PATH_ENV: "/tmp/env-marker.json",
            forge_marker.MARKER_DIR_ENV: "/tmp/env-dir",
        }

        self.assertEqual(
            Path("/tmp/explicit.json"),
            forge_marker.resolve_marker_path("/tmp/explicit.json", None, environ),
        )
        self.assertEqual(
            Path("/tmp/flag-dir") / forge_marker.MARKER_FILENAME,
            forge_marker.resolve_marker_path(None, "/tmp/flag-dir", environ),
        )
        self.assertEqual(
            Path("/tmp/env-marker.json"),
            forge_marker.resolve_marker_path(None, None, environ),
        )
        self.assertEqual(
            Path(forge_marker.DEFAULT_MARKER_DIR) / forge_marker.MARKER_FILENAME,
            forge_marker.resolve_marker_path(None, None, {}),
        )

    def test_launcher_quotes_paths_and_defaults_the_marker(self) -> None:
        launcher = forge_marker.render_launcher(
            marker_path="/tmp/space dir/run-marker.json",
            predicate_path="/tmp/skill/completion-predicate.py",
            python_executable="/usr/bin/python3",
        )

        self.assertIn("FORGE_RUN_MARKER=${FORGE_RUN_MARKER:-'/tmp/space dir/run-marker.json'}", launcher)
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", launcher)
        self.assertIn("exec /usr/bin/python3 /tmp/skill/completion-predicate.py \"$@\"", launcher)

    def test_group_writable_marker_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as marker_dir:
            Path(marker_dir).chmod(0o770)

            with self.assertRaisesRegex(forge_marker.MarkerError, "group or world writable"):
                forge_marker.assert_private_directory(Path(marker_dir))

    def test_rejection_names_the_override(self) -> None:
        with tempfile.TemporaryDirectory() as marker_dir:
            Path(marker_dir).chmod(0o777)

            with self.assertRaisesRegex(forge_marker.MarkerError, "--marker-dir"):
                forge_marker.assert_private_directory(Path(marker_dir))

    def test_symlinked_marker_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            target = Path(base) / "real"
            target.mkdir(mode=0o700)
            link = Path(base) / "link"
            link.symlink_to(target, target_is_directory=True)

            with self.assertRaisesRegex(forge_marker.MarkerError, "not a real directory"):
                forge_marker.ensure_private_directory(link)

    def test_absent_marker_directory_is_rejected_without_creating_it(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            missing = Path(base) / "absent"

            with self.assertRaisesRegex(forge_marker.MarkerError, "unusable"):
                forge_marker.assert_private_directory(missing)
            self.assertFalse(missing.exists())

    def test_dangling_symlink_directory_raises_marker_error(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            link = Path(base) / "link"
            link.symlink_to(Path(base) / "missing", target_is_directory=True)

            with self.assertRaises(forge_marker.MarkerError):
                forge_marker.ensure_private_directory(link)
            self.assertFalse((Path(base) / "missing").exists())

    def test_symlink_to_a_file_raises_marker_error(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            target = Path(base) / "file"
            target.write_text("x", encoding="utf-8")
            link = Path(base) / "link"
            link.symlink_to(target)

            with self.assertRaises(forge_marker.MarkerError):
                forge_marker.ensure_private_directory(link)

    def test_resolved_marker_paths_are_always_absolute(self) -> None:
        relative_dir = forge_marker.resolve_marker_path(None, "relative-dir", {})
        relative_marker = forge_marker.resolve_marker_path("relative.json", None, {})
        from_environment = forge_marker.resolve_marker_path(
            None,
            None,
            {forge_marker.MARKER_PATH_ENV: "relative-env.json"},
        )

        for candidate in (relative_dir, relative_marker, from_environment):
            self.assertTrue(candidate.is_absolute(), candidate)
        self.assertEqual(
            Path.cwd() / "relative-dir" / forge_marker.MARKER_FILENAME,
            relative_dir,
        )

    def test_absolute_normalizes_without_resolving_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            real = Path(base) / "real"
            real.mkdir()
            link = Path(base) / "link"
            link.symlink_to(real, target_is_directory=True)

            resolved = forge_marker.absolute(link / "run-marker.json")

            self.assertEqual(link / "run-marker.json", resolved)


class CompletionPredicateTests(unittest.TestCase):
    def evaluate(self, marker_path: Path, **overrides) -> dict:
        options = {
            "controller": controller,
            "marker_path": marker_path,
            "max_age_seconds": forge_marker.DEFAULT_MAX_AGE_SECONDS,
            "require_marker": False,
            "max_pending_ids": 20,
            "now": NOW + timedelta(seconds=60),
        }
        options.update(overrides)
        return predicate.evaluate(**options)

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

    def test_complete_extraction_is_complete(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir, tempfile.TemporaryDirectory() as marker_dir:
            state = discovered_state(run_dir)
            complete_all_batches(state)
            controller.finalize_extraction(state)
            self.assertEqual("complete", state["status"])

            result = self.evaluate(self.guarded(state, run_dir, marker_dir))

            self.assertEqual("complete", result["status"])
            self.assertEqual("extraction-terminal", result["verdict"])

    def test_partial_extraction_is_complete(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir, tempfile.TemporaryDirectory() as marker_dir:
            state = discovered_state(run_dir)
            omit_first_batch(state)
            controller.finalize_extraction(state)
            self.assertEqual("partial", state["status"])

            result = self.evaluate(self.guarded(state, run_dir, marker_dir))

            self.assertEqual("complete", result["status"])
            self.assertEqual("extraction-terminal", result["verdict"])

    def test_running_extraction_is_incomplete_with_pending_action_ids(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir, tempfile.TemporaryDirectory() as marker_dir:
            state = discovered_state(run_dir)
            actions = controller.next_actions(state, 3)
            pending = [action["actionId"] for action in actions]
            self.assertTrue(pending)

            result = self.evaluate(self.guarded(state, run_dir, marker_dir))

            self.assertEqual("incomplete", result["status"])
            self.assertEqual("extraction-running", result["verdict"])
            for action_id in pending:
                self.assertIn(action_id, result["reason"])
                self.assertIn(action_id, result["continuePrompt"])
            self.assertIn("Do not finish", result["continuePrompt"])

    def test_running_extraction_without_pending_actions_is_still_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir, tempfile.TemporaryDirectory() as marker_dir:
            state = discovered_state(run_dir)

            result = self.evaluate(self.guarded(state, run_dir, marker_dir))

            self.assertEqual("incomplete", result["status"])
            self.assertEqual("extraction-running", result["verdict"])
            self.assertIn("pending actions: none", result["reason"])

    def test_blocked_extraction_is_incomplete_and_preserves_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir, tempfile.TemporaryDirectory() as marker_dir:
            state = discovered_state(run_dir)
            controller.record_checkpoint_failure(
                state,
                "completed batch could not be normalized and sanitized",
            )

            result = self.evaluate(self.guarded(state, run_dir, marker_dir))

            self.assertEqual("incomplete", result["status"])
            self.assertEqual("extraction-blocked", result["verdict"])
            self.assertIn(
                "completed batch could not be normalized and sanitized",
                result["reason"],
            )
            self.assertIn("Do not report success", result["continuePrompt"])

    def test_missing_marker_passes_by_default_and_fails_when_required(self) -> None:
        with tempfile.TemporaryDirectory() as marker_dir:
            marker_path = Path(marker_dir) / forge_marker.MARKER_FILENAME

            permissive = self.evaluate(marker_path)
            strict = self.evaluate(marker_path, require_marker=True)

            self.assertEqual("complete", permissive["status"])
            self.assertEqual("no-active-run", permissive["verdict"])
            self.assertEqual("incomplete", strict["status"])
            self.assertEqual("marker-missing", strict["verdict"])

    def test_unreadable_marker_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as marker_dir:
            marker_path = Path(marker_dir) / forge_marker.MARKER_FILENAME
            marker_path.write_text("{", encoding="utf-8")

            result = self.evaluate(marker_path)

            self.assertEqual("incomplete", result["status"])
            self.assertEqual("marker-unreadable", result["verdict"])

    def test_stale_marker_with_missing_state_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir, tempfile.TemporaryDirectory() as marker_dir:
            state = discovered_state(run_dir)
            marker_path = self.guarded(state, run_dir, marker_dir)
            Path(run_dir, "extraction-state.json").unlink()

            fresh = self.evaluate(marker_path)
            stale = self.evaluate(
                marker_path,
                now=NOW + timedelta(seconds=forge_marker.DEFAULT_MAX_AGE_SECONDS + 60),
            )

            self.assertEqual("incomplete", fresh["status"])
            self.assertEqual("state-missing", fresh["verdict"])
            self.assertEqual("incomplete", stale["status"])
            self.assertEqual("marker-stale", stale["verdict"])
            self.assertIn("stale", stale["reason"])

    def test_stale_marker_on_running_extraction_reports_staleness(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir, tempfile.TemporaryDirectory() as marker_dir:
            state = discovered_state(run_dir)
            marker_path = self.guarded(state, run_dir, marker_dir)

            result = self.evaluate(
                marker_path,
                now=NOW + timedelta(seconds=forge_marker.DEFAULT_MAX_AGE_SECONDS + 60),
            )

            self.assertEqual("incomplete", result["status"])
            self.assertEqual("extraction-running", result["verdict"])
            self.assertIn("stale", result["reason"])
            self.assertTrue(result["marker"]["stale"])

    def test_malformed_state_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir, tempfile.TemporaryDirectory() as marker_dir:
            state = discovered_state(run_dir)
            marker_path = self.guarded(state, run_dir, marker_dir)
            Path(run_dir, "extraction-state.json").write_text("[]", encoding="utf-8")

            result = self.evaluate(marker_path)

            self.assertEqual("incomplete", result["status"])
            self.assertEqual("state-unreadable", result["verdict"])

    def test_truncated_state_json_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir, tempfile.TemporaryDirectory() as marker_dir:
            state = discovered_state(run_dir)
            marker_path = self.guarded(state, run_dir, marker_dir)
            Path(run_dir, "extraction-state.json").write_text(
                '{"status": "comp',
                encoding="utf-8",
            )

            result = self.evaluate(marker_path)

            self.assertEqual("incomplete", result["status"])
            self.assertEqual("state-unreadable", result["verdict"])

    def test_state_missing_required_keys_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir, tempfile.TemporaryDirectory() as marker_dir:
            state = discovered_state(run_dir)
            marker_path = self.guarded(state, run_dir, marker_dir)
            Path(run_dir, "extraction-state.json").write_text(
                json.dumps({"status": "complete"}),
                encoding="utf-8",
            )

            result = self.evaluate(marker_path)

            self.assertEqual("incomplete", result["status"])
            self.assertEqual("state-inconsistent", result["verdict"])

    def test_unsupported_status_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir, tempfile.TemporaryDirectory() as marker_dir:
            state = discovered_state(run_dir)
            state["status"] = "finished"
            marker_path = self.guarded(state, run_dir, marker_dir)

            result = self.evaluate(marker_path)

            self.assertEqual("incomplete", result["status"])
            self.assertEqual("state-unsupported", result["verdict"])

    def test_terminal_status_with_violated_invariant_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir, tempfile.TemporaryDirectory() as marker_dir:
            state = discovered_state(run_dir)
            state["status"] = "partial"
            controller.finalize_extraction(state)
            state["status"] = "partial"
            state["omittedUnits"] = []
            marker_path = self.guarded(state, run_dir, marker_dir)

            result = self.evaluate(marker_path)

            self.assertEqual("incomplete", result["status"])
            self.assertEqual("state-inconsistent", result["verdict"])

    def test_untrusted_marker_directory_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir, tempfile.TemporaryDirectory() as marker_dir:
            state = discovered_state(run_dir)
            complete_all_batches(state)
            controller.finalize_extraction(state)
            marker_path = self.guarded(state, run_dir, marker_dir)
            Path(marker_dir).chmod(0o777)

            result = self.evaluate(marker_path)

            self.assertEqual("incomplete", result["status"])
            self.assertEqual("marker-untrusted", result["verdict"])

    def test_untrusted_directory_without_a_marker_still_passes(self) -> None:
        with tempfile.TemporaryDirectory() as marker_dir:
            Path(marker_dir).chmod(0o777)

            result = self.evaluate(Path(marker_dir) / forge_marker.MARKER_FILENAME)

            self.assertEqual("complete", result["status"])
            self.assertEqual("no-active-run", result["verdict"])

    def test_evaluation_does_not_write_to_the_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir, tempfile.TemporaryDirectory() as marker_dir:
            state = discovered_state(run_dir)
            marker_path = self.guarded(state, run_dir, marker_dir)
            before = tree_snapshot(Path(run_dir)), tree_snapshot(Path(marker_dir))

            self.evaluate(marker_path)

            self.assertEqual(
                before,
                (tree_snapshot(Path(run_dir)), tree_snapshot(Path(marker_dir))),
            )

    def test_bounded_reason_preserves_the_prefix(self) -> None:
        self.assertEqual("abc", predicate.bounded("abc", 10))
        bounded = predicate.bounded("x" * 200, 40)
        self.assertEqual(40, len(bounded))
        self.assertTrue(bounded.endswith(predicate.TRUNCATION_SUFFIX))

    def test_contract_line_only_carries_guard_fields(self) -> None:
        complete = predicate.contract_line(
            {"status": "complete", "reason": "r", "continuePrompt": "p"},
            1200,
            2400,
        )
        incomplete = predicate.contract_line(
            {"status": "incomplete", "reason": "r", "continuePrompt": "p"},
            1200,
            2400,
        )

        self.assertEqual({"status": "complete"}, complete)
        self.assertEqual(
            {"status": "incomplete", "reason": "r", "continuePrompt": "p"},
            incomplete,
        )


def tree_snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {
        str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


JSON_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "null": type(None),
}


def matches_type(value, name: str) -> bool:
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "boolean":
        return isinstance(value, bool)
    expected = JSON_TYPES.get(name)
    if expected is None:
        return True
    if expected in (dict, list, str) and isinstance(value, bool):
        return False
    return isinstance(value, expected)


def schema_errors(value, schema: dict, root: dict, path: str = "$") -> list[str]:
    """Validate the JSON Schema keywords actually used by assets/schemas.json."""
    if "$ref" in schema:
        pointer = schema["$ref"]
        if not pointer.startswith("#/"):
            return [f"{path}: unsupported $ref {pointer}"]
        target = root
        for part in pointer[2:].split("/"):
            target = target[part]
        return schema_errors(value, target, root, path)

    errors: list[str] = []

    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}, got {value!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not one of {schema['enum']}")

    declared = schema.get("type")
    if declared is not None:
        names = declared if isinstance(declared, list) else [declared]
        if not any(matches_type(value, name) for name in names):
            errors.append(f"{path}: expected type {declared}, got {type(value).__name__}")
            return errors

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} is below minimum {schema['minimum']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: shorter than minLength {schema['minLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: does not match pattern {schema['pattern']}")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: fewer than minItems {schema['minItems']}")
        if schema.get("uniqueItems") and len(value) != len({json.dumps(i, sort_keys=True) for i in value}):
            errors.append(f"{path}: items are not unique")
        if "items" in schema:
            for index, item in enumerate(value):
                errors.extend(schema_errors(item, schema["items"], root, f"{path}[{index}]"))

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required property {key!r}")
        additional = schema.get("additionalProperties")
        for key, item in value.items():
            if key in properties:
                errors.extend(schema_errors(item, properties[key], root, f"{path}.{key}"))
            elif additional is False:
                errors.append(f"{path}: undeclared property {key!r}")
            elif isinstance(additional, dict):
                errors.extend(schema_errors(item, additional, root, f"{path}.{key}"))

    if "oneOf" in schema:
        matched = sum(
            1 for option in schema["oneOf"] if not schema_errors(value, option, root, path)
        )
        if matched != 1:
            errors.append(f"{path}: matched {matched} oneOf branches, expected exactly 1")
    for option in schema.get("allOf", []):
        if "if" in option:
            if not schema_errors(value, option["if"], root, path):
                errors.extend(schema_errors(value, option.get("then", {}), root, path))
        else:
            errors.extend(schema_errors(value, option, root, path))

    return errors


class CompletionGuardCommandTests(unittest.TestCase):
    def environment(self) -> dict[str, str]:
        values = dict(os.environ)
        values.pop(forge_marker.MARKER_PATH_ENV, None)
        values.pop(forge_marker.MARKER_DIR_ENV, None)
        values.pop("PYTHONPYCACHEPREFIX", None)
        values.pop("PYTHONDONTWRITEBYTECODE", None)
        return values

    def run_marker(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "run-marker.py"), *args],
            capture_output=True,
            text=True,
            env=self.environment(),
            check=False,
        )

    def initialized_run(self, run_dir: str, marker_dir: str) -> Path:
        state_path = Path(run_dir) / "extraction-state.json"
        state_path.write_text(json.dumps(discovered_state(run_dir)), encoding="utf-8")
        result = self.run_marker(
            "init",
            "--state",
            str(state_path),
            "--marker-dir",
            marker_dir,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return state_path

    def test_lifecycle_produces_a_stable_executable_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir, tempfile.TemporaryDirectory() as marker_dir:
            state_path = self.initialized_run(run_dir, marker_dir)
            launcher = Path(marker_dir) / forge_marker.LAUNCHER_FILENAME
            self.assertTrue(os.access(launcher, os.X_OK))

            running = subprocess.run(
                [str(launcher)],
                capture_output=True,
                text=True,
                env=self.environment(),
                check=False,
            )
            self.assertEqual(1, running.returncode)
            self.assertEqual(
                "incomplete",
                json.loads(running.stdout.strip().splitlines()[-1])["status"],
            )

            state = json.loads(state_path.read_text(encoding="utf-8"))
            complete_all_batches(state)
            controller.finalize_extraction(state)
            state_path.write_text(json.dumps(state), encoding="utf-8")

            finished = subprocess.run(
                [str(launcher)],
                capture_output=True,
                text=True,
                env=self.environment(),
                check=False,
            )
            self.assertEqual(0, finished.returncode)
            self.assertEqual(
                {"status": "complete"},
                json.loads(finished.stdout.strip().splitlines()[-1]),
            )

    def test_refresh_and_finish_advance_the_marker(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir, tempfile.TemporaryDirectory() as marker_dir:
            state_path = self.initialized_run(run_dir, marker_dir)
            marker_path = Path(marker_dir) / forge_marker.MARKER_FILENAME

            self.assertEqual(0, self.run_marker("refresh", "--marker-dir", marker_dir).returncode)
            refreshed = forge_marker.read_marker(marker_path)
            self.assertEqual(1, refreshed["revision"])
            self.assertEqual("active", refreshed["phase"])
            self.assertEqual("running", refreshed["snapshot"]["status"])

            blocked = self.run_marker("finish", "--marker-dir", marker_dir)
            self.assertNotEqual(0, blocked.returncode)
            self.assertIn("not terminal", blocked.stderr)

            state = json.loads(state_path.read_text(encoding="utf-8"))
            complete_all_batches(state)
            controller.finalize_extraction(state)
            state_path.write_text(json.dumps(state), encoding="utf-8")

            self.assertEqual(0, self.run_marker("finish", "--marker-dir", marker_dir).returncode)
            finished = forge_marker.read_marker(marker_path)
            self.assertEqual("terminal", finished["phase"])
            self.assertEqual("complete", finished["terminalSummary"]["status"])

    def test_clear_requires_terminal_phase_and_keeps_the_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir, tempfile.TemporaryDirectory() as marker_dir:
            state_path = self.initialized_run(run_dir, marker_dir)
            marker_path = Path(marker_dir) / forge_marker.MARKER_FILENAME
            launcher = Path(marker_dir) / forge_marker.LAUNCHER_FILENAME

            refused = self.run_marker("clear", "--marker-dir", marker_dir)
            self.assertNotEqual(0, refused.returncode)
            self.assertIn("refusing to clear an active run marker", refused.stderr)
            self.assertTrue(marker_path.is_file())

            state = json.loads(state_path.read_text(encoding="utf-8"))
            complete_all_batches(state)
            controller.finalize_extraction(state)
            state_path.write_text(json.dumps(state), encoding="utf-8")
            self.assertEqual(0, self.run_marker("finish", "--marker-dir", marker_dir).returncode)
            self.assertEqual(0, self.run_marker("clear", "--marker-dir", marker_dir).returncode)

            self.assertFalse(marker_path.exists())
            self.assertTrue(launcher.is_file())

            cleared = subprocess.run(
                [str(launcher)],
                capture_output=True,
                text=True,
                env=self.environment(),
                check=False,
            )
            self.assertEqual(0, cleared.returncode)
            self.assertEqual(
                {"status": "complete"},
                json.loads(cleared.stdout.strip().splitlines()[-1]),
            )

    def test_guard_lifecycle_never_writes_to_the_repository(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir, tempfile.TemporaryDirectory() as marker_dir:
            for cache in SKILL_DIR.rglob("__pycache__"):
                shutil.rmtree(cache, ignore_errors=True)
            before = tree_snapshot(SKILL_DIR)
            state_path = self.initialized_run(run_dir, marker_dir)
            self.run_marker("refresh", "--marker-dir", marker_dir)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            complete_all_batches(state)
            controller.finalize_extraction(state)
            state_path.write_text(json.dumps(state), encoding="utf-8")
            self.run_marker("finish", "--marker-dir", marker_dir)
            subprocess.run(
                [str(Path(marker_dir) / forge_marker.LAUNCHER_FILENAME)],
                capture_output=True,
                text=True,
                env=self.environment(),
                check=False,
            )

            self.assertEqual(before, tree_snapshot(SKILL_DIR))
            self.assertEqual([], list(SKILL_DIR.rglob("__pycache__")))

    def test_clear_has_no_force_escape_hatch(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir, tempfile.TemporaryDirectory() as marker_dir:
            self.initialized_run(run_dir, marker_dir)

            forced = self.run_marker("clear", "--force", "--marker-dir", marker_dir)

            self.assertNotEqual(0, forced.returncode)
            self.assertIn("unrecognized arguments", forced.stderr)
            self.assertTrue((Path(marker_dir) / forge_marker.MARKER_FILENAME).is_file())

    def test_init_refuses_to_displace_another_active_run(self) -> None:
        with tempfile.TemporaryDirectory() as marker_dir, \
                tempfile.TemporaryDirectory() as first_run, \
                tempfile.TemporaryDirectory() as second_run:
            self.initialized_run(first_run, marker_dir)
            marker_path = Path(marker_dir) / forge_marker.MARKER_FILENAME
            first = forge_marker.read_marker(marker_path)

            second_state = Path(second_run) / "extraction-state.json"
            second_state.write_text(
                json.dumps(discovered_state(second_run)),
                encoding="utf-8",
            )
            clash = self.run_marker(
                "init",
                "--state",
                str(second_state),
                "--marker-dir",
                marker_dir,
            )

            self.assertNotEqual(0, clash.returncode)
            self.assertIn("already belongs to active run", clash.stderr)
            self.assertEqual(first, forge_marker.read_marker(marker_path))

    def test_reinitializing_the_same_run_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir, tempfile.TemporaryDirectory() as marker_dir:
            state_path = self.initialized_run(run_dir, marker_dir)

            again = self.run_marker(
                "init",
                "--state",
                str(state_path),
                "--marker-dir",
                marker_dir,
            )

            self.assertEqual(0, again.returncode, again.stderr)

    def test_refresh_rejects_an_untrusted_marker_directory(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir, tempfile.TemporaryDirectory() as marker_dir:
            self.initialized_run(run_dir, marker_dir)
            Path(marker_dir).chmod(0o777)

            refused = self.run_marker("refresh", "--marker-dir", marker_dir)

            self.assertNotEqual(0, refused.returncode)
            self.assertIn("group or world writable", refused.stderr)

    def test_purge_cannot_remove_the_launcher_before_terminal_assertion(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir, tempfile.TemporaryDirectory() as marker_dir:
            self.initialized_run(run_dir, marker_dir)
            launcher = Path(marker_dir) / forge_marker.LAUNCHER_FILENAME

            refused = self.run_marker("clear", "--purge", "--marker-dir", marker_dir)

            self.assertNotEqual(0, refused.returncode)
            self.assertIn("refusing to clear an active run marker", refused.stderr)
            self.assertTrue(launcher.is_file())
            self.assertTrue((Path(marker_dir) / forge_marker.MARKER_FILENAME).is_file())

            still_guarded = subprocess.run(
                [str(launcher)],
                capture_output=True,
                text=True,
                env=self.environment(),
                check=False,
            )
            self.assertEqual(1, still_guarded.returncode)
            self.assertEqual(
                "incomplete",
                json.loads(still_guarded.stdout.strip().splitlines()[-1])["status"],
            )

    def test_controller_diagnostics_command_emits_a_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state_path = Path(run_dir) / "extraction-state.json"
            state_path.write_text(json.dumps(discovered_state(run_dir)), encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "extraction-controller.py"),
                    "diagnostics",
                    "--state",
                    str(state_path),
                ],
                capture_output=True,
                text=True,
                env=self.environment(),
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            snapshot = json.loads(result.stdout)
            self.assertEqual("extraction-diagnostics", snapshot["kind"])
            self.assertEqual("running", snapshot["status"])
            self.assertEqual(2, snapshot["sessions"]["discovered"])


class GuardContractDocumentationTests(unittest.TestCase):
    def schemas(self) -> dict:
        return json.loads(
            (SKILL_DIR / "assets" / "schemas.json").read_text(encoding="utf-8")
        )

    def schema(self, name: str) -> dict:
        return self.schemas()["$defs"][name]

    def assert_conforms(self, document, schema: dict, name: str = "") -> None:
        errors = schema_errors(document, schema, self.schemas())
        self.assertEqual([], errors, f"{name or 'document'} violates its schema")

    def test_diagnostics_snapshot_matches_its_schema(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            snapshot = controller.diagnostics(discovered_state(run_dir))

            self.assert_conforms(snapshot, self.schema("extractionDiagnostics"))

    def test_run_marker_matches_its_schema(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = discovered_state(run_dir)
            marker = forge_marker.build_marker(
                state=state,
                state_path=Path(run_dir) / "extraction-state.json",
                now=NOW,
            )
            marker["snapshot"] = controller.diagnostics(state)

            self.assert_conforms(marker, self.schema("runMarker"))

    def test_completion_verdict_matches_its_schema(self) -> None:
        schema = self.schema("completionVerdict")

        self.assert_conforms(
            predicate.contract_line({"status": "complete"}, 1200, 2400),
            schema,
        )
        self.assert_conforms(
            predicate.contract_line(
                {"status": "incomplete", "reason": "r", "continuePrompt": "p"},
                1200,
                2400,
            ),
            schema,
        )

    def test_validator_rejects_contract_violations(self) -> None:
        """The schema tests must fail on bad documents, not pass vacuously."""
        diagnostics_schema = self.schema("extractionDiagnostics")
        verdict_schema = self.schema("completionVerdict")
        with tempfile.TemporaryDirectory() as run_dir:
            snapshot = controller.diagnostics(discovered_state(run_dir))

        self.assertEqual([], schema_errors(snapshot, diagnostics_schema, self.schemas()))

        bad_status = copy.deepcopy(snapshot)
        bad_status["status"] = "finished"
        self.assertNotEqual([], schema_errors(bad_status, diagnostics_schema, self.schemas()))

        negative_counter = copy.deepcopy(snapshot)
        negative_counter["counters"]["rows"] = -1
        self.assertNotEqual(
            [], schema_errors(negative_counter, diagnostics_schema, self.schemas())
        )

        string_counter = copy.deepcopy(snapshot)
        string_counter["counters"]["rows"] = "12"
        self.assertNotEqual(
            [], schema_errors(string_counter, diagnostics_schema, self.schemas())
        )

        bad_kind = copy.deepcopy(snapshot)
        bad_kind["kind"] = "something-else"
        self.assertNotEqual([], schema_errors(bad_kind, diagnostics_schema, self.schemas()))

        bad_ids = copy.deepcopy(snapshot)
        bad_ids["actions"]["pendingActionIds"] = [1, 2]
        self.assertNotEqual([], schema_errors(bad_ids, diagnostics_schema, self.schemas()))

        self.assertNotEqual(
            [],
            schema_errors({"status": "incomplete"}, verdict_schema, self.schemas()),
        )

    def test_marker_schema_traverses_the_snapshot_reference(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            state = discovered_state(run_dir)
            marker = forge_marker.build_marker(
                state=state,
                state_path=Path(run_dir) / "extraction-state.json",
                now=NOW,
            )
            marker["snapshot"] = controller.diagnostics(state)
            marker["snapshot"]["status"] = "finished"

            self.assertNotEqual(
                [],
                schema_errors(marker, self.schema("runMarker"), self.schemas()),
            )

    def test_skill_documents_the_marker_lifecycle(self) -> None:
        contract = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn('scripts/run-marker.py" init', contract)
        self.assertIn('scripts/run-marker.py" refresh', contract)
        self.assertIn('scripts/run-marker.py" finish', contract)
        self.assertIn("run-marker.py clear", contract)
        self.assertIn("reference/cca-completion-guard.md", contract)

    def test_skill_still_requires_terminal_assertion_before_finishing(self) -> None:
        contract = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("assert-terminal", contract)
        self.assertIn(
            "If this command fails because status is `running`, a final response and cleanup\nare forbidden.",
            contract,
        )
        self.assertIn("Only once that command succeeds", contract)

    def test_guard_reference_documents_the_command_contract(self) -> None:
        reference = (SKILL_DIR / "reference" / "cca-completion-guard.md").read_text(
            encoding="utf-8"
        )

        self.assertIn(forge_marker.DEFAULT_MARKER_DIR, reference)
        self.assertIn(forge_marker.LAUNCHER_FILENAME, reference)
        self.assertIn(forge_marker.MARKER_PATH_ENV, reference)
        self.assertIn(forge_marker.MARKER_DIR_ENV, reference)
        self.assertIn('{"status":"complete"}', reference)
        self.assertIn('"status":"incomplete"', reference)


if __name__ == "__main__":
    unittest.main()
