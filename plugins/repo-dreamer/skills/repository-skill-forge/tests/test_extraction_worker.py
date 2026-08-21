#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

WORKER_SPEC = importlib.util.spec_from_file_location(
    "extraction_worker",
    SCRIPTS_DIR / "extraction-worker.py",
)
assert WORKER_SPEC is not None and WORKER_SPEC.loader is not None
worker = importlib.util.module_from_spec(WORKER_SPEC)
WORKER_SPEC.loader.exec_module(worker)

MATERIALIZER_SPEC = importlib.util.spec_from_file_location(
    "worker_materialize_session_query",
    SCRIPTS_DIR / "materialize-session-query.py",
)
assert MATERIALIZER_SPEC is not None and MATERIALIZER_SPEC.loader is not None
materializer = importlib.util.module_from_spec(MATERIALIZER_SPEC)
MATERIALIZER_SPEC.loader.exec_module(materializer)

RESULT_COLUMNS = {
    "discovery": ["session_id", "updated_at"],
    "metadata": [
        "session_id",
        "agent_name",
        "repository",
        "branch",
        "created_at",
        "updated_at",
    ],
    "refs": ["session_id", "ref_type", "ref_value", "turn_index"],
    "files": ["session_id", "file_path", "tool_name", "turn_index"],
    "tool-calls": [
        "session_id",
        "tool_call_id",
        "tool_name",
        "arguments_json",
        "exit_code",
        "completed_at",
    ],
}


def render_result(kind: str, rows: list[dict[str, object]]) -> str:
    columns = RESULT_COLUMNS[kind]
    if not rows:
        return "Query returned 0 rows."
    lines = [
        f"{len(rows)} row(s) returned:",
        "",
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    lines.extend(
        "| " + " | ".join(str(row[column]) for column in columns) + " |"
        for row in rows
    )
    return "\n".join(lines)


def metadata_row(session: str) -> dict[str, object]:
    return {
        "session_id": session,
        "agent_name": "Copilot CLI",
        "repository": "owner/repository",
        "branch": "main",
        "created_at": "2026-08-17T00:00:00Z",
        "updated_at": "2026-08-17T01:00:00Z",
    }


def matches_type(value: object, name: str) -> bool:
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    raise AssertionError(f"unsupported schema type: {name}")


def conformance_errors(
    value: object,
    schema: dict[str, object],
    definitions: dict[str, object],
    path: str = "$",
) -> list[str]:
    """Validate the JSON Schema keywords the worker protocol contract uses.

    Supports $ref, type, const, enum, required, properties, items, minItems,
    minimum, maxLength, boolean and schema-valued additionalProperties, allOf,
    if/then/else, and not. Any other keyword fails loudly rather than silently
    passing, so the contract cannot outgrow this checker unnoticed.
    """
    supported = {
        "$ref",
        "description",
        "type",
        "const",
        "enum",
        "required",
        "properties",
        "additionalProperties",
        "items",
        "minItems",
        "minimum",
        "maxLength",
        "allOf",
        "if",
        "then",
        "else",
        "not",
    }
    unsupported = set(schema) - supported
    assert not unsupported, f"{path}: unsupported schema keywords {sorted(unsupported)}"

    if "$ref" in schema:
        reference = str(schema["$ref"]).rsplit("/", 1)[-1]
        return conformance_errors(value, definitions[reference], definitions, path)
    errors: list[str] = []
    if "type" in schema:
        names = schema["type"]
        allowed = [names] if isinstance(names, str) else list(names)
        if not any(matches_type(value, str(name)) for name in allowed):
            errors.append(f"{path}: {type(value).__name__} is not {allowed}")
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: {value!r} is not {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not one of {schema['enum']}")
    if "maxLength" in schema and isinstance(value, str):
        if len(value) > int(schema["maxLength"]):
            errors.append(f"{path}: longer than {schema['maxLength']}")
    if "minimum" in schema and isinstance(value, (int, float)):
        if not isinstance(value, bool) and value < schema["minimum"]:
            errors.append(f"{path}: {value} is below {schema['minimum']}")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties")
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required key {key}")
        for key, item in value.items():
            if key in properties:
                errors.extend(
                    conformance_errors(
                        item,
                        properties[key],
                        definitions,
                        f"{path}.{key}",
                    )
                )
            elif additional is False:
                errors.append(f"{path}: undeclared key {key}")
            elif isinstance(additional, dict):
                errors.extend(
                    conformance_errors(
                        item,
                        additional,
                        definitions,
                        f"{path}.{key}",
                    )
                )
    if isinstance(value, list):
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        if "items" in schema:
            for index, item in enumerate(value):
                errors.extend(
                    conformance_errors(
                        item,
                        schema["items"],
                        definitions,
                        f"{path}[{index}]",
                    )
                )
    for index, subschema in enumerate(schema.get("allOf", [])):
        errors.extend(
            conformance_errors(value, subschema, definitions, f"{path}/allOf[{index}]")
        )
    if "if" in schema:
        branch = (
            schema.get("then")
            if not conformance_errors(value, schema["if"], definitions, path)
            else schema.get("else")
        )
        if isinstance(branch, dict):
            errors.extend(conformance_errors(value, branch, definitions, path))
    if "not" in schema:
        if not conformance_errors(value, schema["not"], definitions, path):
            errors.append(f"{path}: must not match the forbidden subschema")
    return errors


class Harness:
    """Deterministic `session_store_sql` transport backed by a synthetic log."""

    def __init__(
        self,
        root: Path,
        sessions: list[str],
        page_size: int = 500,
        run_name: str = "run",
    ) -> None:
        self.root = root
        self.run_dir = root / run_name
        self.events_root = root / "session-state"
        self.log = self.events_root / "current" / "events.jsonl"
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self.log.write_text("", encoding="utf-8")
        self.sessions = sessions
        self.page_size = page_size
        self.spill_dir = root / "spill"
        self.spill_dir.mkdir(exist_ok=True)
        self.discovery_actions: list[str] = []
        self.call_count = 0
        self.spill_paths: dict[str, Path] = {}

    def batch_sessions(self, query: str) -> list[str]:
        return [session for session in self.sessions if f"'{session}'" in query]

    def rows_for(self, action: dict[str, object]) -> list[dict[str, object]]:
        kind = str(action["kind"])
        query = str(action["query"])
        if kind == "discovery":
            page = int(str(action["actionId"]).rsplit("-", 1)[-1])
            window = self.sessions[page * self.page_size :]
            return [
                {"session_id": session, "updated_at": f"2026-08-17T{index:02d}:00:00Z"}
                for index, session in enumerate(window[: self.page_size + 1])
            ]
        members = self.batch_sessions(query)
        if kind == "metadata":
            return [metadata_row(session) for session in members]
        if kind == "refs":
            return [
                {
                    "session_id": session,
                    "ref_type": "pr",
                    "ref_value": str(index + 1),
                    "turn_index": 1,
                }
                for index, session in enumerate(members)
            ]
        if kind == "files":
            return [
                {
                    "session_id": session,
                    "file_path": "scripts/release.sh",
                    "tool_name": "edit",
                    "turn_index": 2,
                }
                for session in members
            ]
        return [
            {
                "session_id": session,
                "tool_call_id": f"call-{session}",
                "tool_name": "bash",
                "arguments_json": json.dumps({"command": "go test ./..."}),
                "exit_code": 0,
                "completed_at": "2026-08-17T00:30:00Z",
            }
            for session in members
        ]

    def respond(
        self,
        action: dict[str, object],
        *,
        rows: list[dict[str, object]] | None = None,
        query: str | None = None,
        success: bool = True,
        error: str = "",
        spill: bool = False,
    ) -> None:
        action_id = str(action["actionId"])
        if str(action["kind"]) == "discovery":
            self.discovery_actions.append(action_id)
        self.call_count += 1
        call_id = f"call-{self.call_count}-{action_id}"
        submitted = query if query is not None else str(action["query"])
        payload = (
            render_result(
                str(action["kind"]),
                self.rows_for(action) if rows is None else rows,
            )
            if success
            else error
        )
        if success and spill:
            spill_path = self.spill_dir / f"{call_id}.txt"
            spill_path.write_text(payload, encoding="utf-8")
            self.spill_paths[action_id] = spill_path
            payload = f"Output too large to read at once. Saved to: {spill_path}"
        with self.log.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "tool.execution_start",
                        "data": {
                            "toolName": "session_store_sql",
                            "toolCallId": call_id,
                            "arguments": {
                                "description": action_id,
                                "query": submitted,
                            },
                        },
                    }
                )
                + "\n"
            )
            handle.write(
                json.dumps(
                    {
                        "type": "tool.execution_complete",
                        "data": {
                            "toolCallId": call_id,
                            "success": success,
                            "result": {"content": payload},
                        },
                    }
                )
                + "\n"
            )

    def invoke(self, *argv: str) -> tuple[int, dict[str, object], str]:
        completed = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "extraction-worker.py"), *argv],
            check=False,
            capture_output=True,
            text=True,
        )
        payload = json.loads(completed.stdout) if completed.stdout.strip() else {}
        return completed.returncode, payload, completed.stderr

    def start(self, **options: str) -> dict[str, object]:
        arguments = [
            "start",
            "--run-dir",
            str(self.run_dir),
            "--repository",
            "owner/repository",
            "--window-end",
            "2026-08-18T00:00:00Z",
            "--window-hours",
            "96",
            "--events-root",
            str(self.events_root),
            "--discovery-page-size",
            str(self.page_size),
        ]
        for name, value in options.items():
            arguments.extend([f"--{name.replace('_', '-')}", value])
        code, envelope, stderr = self.invoke(*arguments)
        assert code == 0, stderr
        return envelope

    def advance(self) -> dict[str, object]:
        code, envelope, stderr = self.invoke(
            "advance",
            "--run-dir",
            str(self.run_dir),
            "--events-root",
            str(self.events_root),
        )
        assert code == 0, stderr
        return envelope

    def drive(self, envelope: dict[str, object], limit: int = 60) -> dict[str, object]:
        cycles = 0
        while envelope["kind"] == "wave":
            cycles += 1
            assert cycles < limit, "worker did not reach a terminal state"
            for action in envelope["wave"]["actions"]:
                self.respond(action)
            envelope = self.advance()
        return envelope


class ExtractionWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_start_emits_one_versioned_discovery_wave(self) -> None:
        harness = Harness(self.root, ["session-1"])

        envelope = harness.start()

        self.assertEqual(worker.PROTOCOL_VERSION, envelope["protocolVersion"])
        self.assertEqual("wave", envelope["kind"])
        self.assertFalse(envelope["terminal"])
        self.assertEqual(1, envelope["wave"]["actionCount"])
        self.assertEqual("discovery", envelope["wave"]["actions"][0]["kind"])
        self.assertEqual(
            envelope["wave"]["actions"][0]["actionId"],
            envelope["wave"]["actions"][0]["description"],
        )
        self.assertEqual({"success": 0, "query-failure": 0, "handoff": 0,
                          "artifact": 0, "pending": 0},
                         envelope["recorded"]["counts"])
        self.assertEqual("session_store_sql", envelope["next"]["tool"])
        manifest = json.loads(
            Path(envelope["wave"]["manifestPath"]).read_text(encoding="utf-8")
        )
        self.assertEqual("action-batch", manifest["kind"])

    def test_multi_page_discovery_and_parallel_batches_reach_complete(self) -> None:
        harness = Harness(
            self.root,
            [f"session-{index}" for index in range(5)],
            page_size=2,
        )

        envelope = harness.drive(
            harness.start(session_batch_size="1", max_concurrent_batches="3")
        )

        self.assertEqual("terminal", envelope["kind"])
        self.assertEqual("complete", envelope["status"])
        self.assertGreaterEqual(len(harness.discovery_actions), 3)
        self.assertEqual(
            len(harness.discovery_actions),
            len(set(harness.discovery_actions)),
        )
        self.assertEqual(5, envelope["coverage"]["discoveredSessionCount"])
        self.assertEqual(5, envelope["coverage"]["completedSessionCount"])
        self.assertEqual(1.0, envelope["coverage"]["sessionCoverage"])
        ledger = json.loads(
            (harness.run_dir / "primitives.sanitized.json").read_text(encoding="utf-8")
        )
        self.assertEqual(5, len(ledger["primitives"]))
        self.assertFalse(ledger["coverage"]["partial"])

    def test_wave_never_exceeds_configured_batch_concurrency(self) -> None:
        harness = Harness(self.root, [f"session-{index}" for index in range(6)])
        envelope = harness.start(session_batch_size="1", max_concurrent_batches="2")
        widths = []

        while envelope["kind"] == "wave":
            widths.append(envelope["wave"]["actionCount"])
            manifest = json.loads(
                Path(envelope["wave"]["manifestPath"]).read_text(encoding="utf-8")
            )
            batch_ids = [
                action["batchId"]
                for action in manifest["actions"]
                if action["kind"] != "discovery"
            ]
            self.assertEqual(len(batch_ids), len(set(batch_ids)))
            for action in envelope["wave"]["actions"]:
                harness.respond(action)
            envelope = harness.advance()

        self.assertEqual("complete", envelope["status"])
        self.assertLessEqual(max(widths), 2)

    def test_spilled_results_are_materialized_without_transcript_rows(self) -> None:
        harness = Harness(self.root, ["session-1", "session-2"])
        envelope = harness.start(session_batch_size="2")

        while envelope["kind"] == "wave":
            for action in envelope["wave"]["actions"]:
                harness.respond(action, spill=True)
            envelope = harness.advance()

        self.assertEqual("complete", envelope["status"])
        self.assertEqual(2, envelope["coverage"]["completedSessionCount"])

    def test_handoff_mismatch_splits_the_batch_without_blocking(self) -> None:
        harness = Harness(self.root, ["session-1", "session-2"])
        envelope = harness.start(session_batch_size="2")
        harness.respond(envelope["wave"]["actions"][0])
        envelope = harness.advance()
        metadata = envelope["wave"]["actions"][0]
        self.assertEqual("metadata", metadata["kind"])

        harness.respond(metadata, query="SELECT 1", rows=[])
        envelope = harness.advance()

        self.assertEqual("wave", envelope["kind"])
        self.assertEqual(1, envelope["recorded"]["counts"]["handoff"])
        self.assertEqual("running", envelope["status"])
        self.assertEqual(2, envelope["progress"]["batchCount"])
        self.assertEqual(0, envelope["progress"]["blockerCount"])
        self.assertEqual(0, envelope["progress"]["omittedUnitCount"])

        envelope = harness.drive(envelope)
        self.assertEqual("complete", envelope["status"])
        self.assertEqual(2, envelope["coverage"]["completedSessionCount"])

    def test_timeout_omits_the_unit_and_reports_disclosed_partial(self) -> None:
        harness = Harness(self.root, ["session-1", "session-2"])
        envelope = harness.start(session_batch_size="1")
        timed_out = False

        while envelope["kind"] == "wave":
            for action in envelope["wave"]["actions"]:
                if action["kind"] == "tool-calls" and not timed_out:
                    timed_out = True
                    harness.respond(
                        action,
                        success=False,
                        error="query timed out after the maximum allowed runtime",
                    )
                else:
                    harness.respond(action)
            envelope = harness.advance()

        self.assertEqual("partial", envelope["status"])
        self.assertEqual(["tool-calls"], envelope["omissionSummary"]["kinds"])
        self.assertEqual(1, envelope["omissionSummary"]["count"])
        self.assertEqual(2, envelope["coverage"]["discoveredSessionCount"])
        self.assertEqual(1, envelope["coverage"]["completedSessionCount"])
        self.assertEqual(0.5, envelope["coverage"]["sessionCoverage"])
        ledger = json.loads(
            (harness.run_dir / "primitives.sanitized.json").read_text(encoding="utf-8")
        )
        self.assertTrue(ledger["coverage"]["partial"])

    def test_identical_timed_out_query_is_never_reissued(self) -> None:
        harness = Harness(self.root, ["session-1"])
        envelope = harness.start(session_batch_size="1")
        harness.respond(envelope["wave"]["actions"][0])
        envelope = harness.advance()
        metadata = envelope["wave"]["actions"][0]
        harness.respond(
            metadata,
            success=False,
            error="query timed out after the maximum allowed runtime",
        )

        envelope = harness.advance()
        issued = [
            action["actionId"]
            for action in (
                envelope["wave"]["actions"] if envelope["kind"] == "wave" else []
            )
        ]

        self.assertNotIn(metadata["actionId"], issued)
        self.assertEqual("terminal", envelope["kind"])
        self.assertEqual("partial", envelope["status"])

    def test_checkpoint_failure_blocks_with_terminal_status(self) -> None:
        harness = Harness(self.root, ["session-1"])
        envelope = harness.start(session_batch_size="1")
        harness.run_dir.mkdir(parents=True, exist_ok=True)
        (harness.run_dir / "primitives.sanitized.json").write_text(
            json.dumps({"scope": {"repository": "other/repository"}}),
            encoding="utf-8",
        )

        harness.respond(envelope["wave"]["actions"][0])
        envelope = harness.advance()

        self.assertEqual("terminal", envelope["kind"])
        self.assertEqual("blocked", envelope["status"])
        self.assertTrue(envelope["terminal"])
        self.assertEqual("batch-checkpoint", envelope["blocker"]["kind"])
        self.assertEqual("artifact", envelope["blocker"]["errorKind"])
        self.assertEqual(1, envelope["progress"]["blockerCount"])

        code, status, _ = harness.invoke(
            "status",
            "--run-dir",
            str(harness.run_dir),
            "--assert-terminal",
        )
        self.assertEqual(0, code)
        self.assertTrue(status["terminal"])
        self.assertEqual("blocked", status["assertion"]["status"])

    def test_artifact_failure_blocks_after_recording_wave_successes(self) -> None:
        harness = Harness(self.root, ["session-1", "session-2"])
        envelope = harness.start(session_batch_size="1", max_concurrent_batches="2")
        harness.respond(envelope["wave"]["actions"][0])
        envelope = harness.advance()
        first, second = envelope["wave"]["actions"]

        harness.respond(second)
        harness.respond(first, rows=[metadata_row("session-9")])
        envelope = harness.advance()

        self.assertEqual("terminal", envelope["kind"])
        self.assertEqual("blocked", envelope["status"])
        counts = envelope["recorded"]["counts"]
        self.assertEqual(1, counts["success"])
        self.assertEqual(1, counts["artifact"])
        self.assertEqual(1, envelope["progress"]["blockerCount"])
        self.assertEqual("artifact", envelope["blocker"]["errorKind"])
        self.assertIn(
            "does not exactly cover the session batch",
            envelope["blocker"]["reason"],
        )
        state = json.loads(
            (harness.run_dir / "extraction-state.json").read_text(encoding="utf-8")
        )
        statuses = sorted(
            batch["status"]
            for partition in state["partitions"]
            for batch in partition["batches"]
        )
        self.assertEqual(["metadata", "refs"], statuses)

    def test_missing_results_stay_pending_and_reissue_the_same_wave(self) -> None:
        harness = Harness(self.root, ["session-1"])
        first = harness.start(session_batch_size="1")

        second = harness.advance()

        self.assertEqual("wave", second["kind"])
        self.assertEqual(1, second["recorded"]["counts"]["pending"])
        self.assertEqual(0, second["progress"]["failedQueries"])
        self.assertEqual(0, second["progress"]["queryAttempts"])
        self.assertEqual(
            [action["actionId"] for action in first["wave"]["actions"]],
            [action["actionId"] for action in second["wave"]["actions"]],
        )
        self.assertEqual(
            [action["query"] for action in first["wave"]["actions"]],
            [action["query"] for action in second["wave"]["actions"]],
        )

    def test_run_resumes_from_run_local_files_after_interruption(self) -> None:
        harness = Harness(self.root, ["session-1", "session-2"])
        envelope = harness.start(session_batch_size="2")
        harness.respond(envelope["wave"]["actions"][0])
        envelope = harness.advance()
        metadata_id = envelope["wave"]["actions"][0]["actionId"]
        harness.respond(envelope["wave"]["actions"][0])

        resumed = Harness.__new__(Harness)
        resumed.__dict__.update(harness.__dict__)
        envelope = resumed.advance()

        self.assertEqual(3, envelope["cycle"])
        self.assertEqual(1, envelope["recorded"]["counts"]["success"])
        self.assertEqual(metadata_id, envelope["recorded"]["actions"][0]["actionId"])
        self.assertEqual("complete", resumed.drive(envelope)["status"])

    def test_start_refuses_to_overwrite_an_existing_run(self) -> None:
        harness = Harness(self.root, ["session-1"])
        harness.start()

        code, _, stderr = harness.invoke(
            "start",
            "--run-dir",
            str(harness.run_dir),
            "--repository",
            "owner/repository",
            "--window-end",
            "2026-08-18T00:00:00Z",
            "--events-root",
            str(harness.events_root),
        )

        self.assertEqual(1, code)
        self.assertIn("already exists", stderr)

    def test_status_assert_terminal_fails_while_extraction_is_running(self) -> None:
        harness = Harness(self.root, ["session-1"])
        harness.start()

        code, envelope, stderr = harness.invoke(
            "status",
            "--run-dir",
            str(harness.run_dir),
            "--assert-terminal",
        )

        self.assertEqual(1, code)
        self.assertIn("status is running", stderr)
        self.assertFalse(envelope["terminal"])
        self.assertEqual(1, len(envelope["pendingActionIds"]))

    def test_envelope_excludes_result_rows_and_state_internals(self) -> None:
        harness = Harness(self.root, ["session-1", "session-2"])
        envelope = harness.start(session_batch_size="1")
        harness.respond(envelope["wave"]["actions"][0])
        envelope = harness.advance()

        without_wave = json.dumps(
            {key: value for key, value in envelope.items() if key != "wave"}
        )
        for leaked in (
            "session-1",
            "session-2",
            "row(s) returned",
            "arguments_json",
            "go test ./...",
            "SELECT",
            "processedBatchIds",
            "sessionHashes",
            "handledActionIds",
            "issuedActions",
        ):
            self.assertNotIn(leaked, without_wave)
        self.assertLess(len(without_wave), 4000)
        self.assertNotIn("session-1", json.dumps(envelope["progress"]))

    def test_terminal_envelope_keeps_omission_detail_in_run_local_files(self) -> None:
        harness = Harness(self.root, ["session-1", "session-2"])
        envelope = harness.start(session_batch_size="1")
        timed_out = False

        while envelope["kind"] == "wave":
            for action in envelope["wave"]["actions"]:
                if action["kind"] == "tool-calls" and not timed_out:
                    timed_out = True
                    harness.respond(action, success=False, error="query timed out")
                else:
                    harness.respond(action)
            envelope = harness.advance()

        self.assertNotIn("omittedUnits", envelope["coverage"])
        self.assertNotIn("fallbacks", envelope["coverage"])
        coverage = json.loads(
            Path(envelope["coveragePath"]).read_text(encoding="utf-8")
        )
        self.assertEqual(1, len(coverage["omittedUnits"]))
        self.assertIn("sessionHashes", coverage["omittedUnits"][0])

    def test_raw_query_artifacts_are_deleted_after_promotion(self) -> None:
        harness = Harness(self.root, ["session-1"])
        envelope = harness.drive(harness.start(session_batch_size="1"))

        extraction = harness.run_dir / "extraction"

        self.assertEqual("complete", envelope["status"])
        self.assertEqual([], sorted(extraction.glob("metadata-*.json")))
        self.assertEqual([], sorted(extraction.glob("*.accepted.json")))
        self.assertTrue((harness.run_dir / "primitives.sanitized.json").is_file())

    def test_harvest_waits_for_in_flight_results_without_wall_clock(self) -> None:
        harness = Harness(self.root, ["session-1"])
        envelope = harness.start()
        action = json.loads(
            (harness.run_dir / "worker" / "wave.json").read_text(encoding="utf-8")
        )["actions"][0]
        clock = iter([0.0, 0.0, 1.0, 2.0, 3.0])
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            harness.respond(envelope["wave"]["actions"][0])

        outcomes = worker.harvest(
            [action],
            harness.events_root,
            wait_seconds=10.0,
            poll_interval=0.5,
            sleep=sleep,
            monotonic=lambda: next(clock),
        )

        self.assertEqual([0.5], sleeps)
        self.assertEqual("success", outcomes[0]["outcome"])

    def test_harvest_stops_at_the_deadline_and_reports_pending(self) -> None:
        harness = Harness(self.root, ["session-1"])
        harness.start()
        action = json.loads(
            (harness.run_dir / "worker" / "wave.json").read_text(encoding="utf-8")
        )["actions"][0]
        clock = iter([0.0, 5.0, 20.0])
        sleeps: list[float] = []

        outcomes = worker.harvest(
            [action],
            harness.events_root,
            wait_seconds=10.0,
            poll_interval=0.25,
            sleep=sleeps.append,
            monotonic=lambda: next(clock),
        )

        self.assertEqual([0.25], sleeps)
        self.assertEqual("pending", outcomes[0]["outcome"])

    def test_omitted_discovery_is_never_reported_as_complete(self) -> None:
        harness = Harness(self.root, ["session-1"])
        envelope = harness.start(min_window_minutes="3000")
        harness.respond(
            envelope["wave"]["actions"][0],
            success=False,
            error="query timed out after the maximum allowed runtime",
        )

        envelope = harness.advance()

        self.assertEqual("terminal", envelope["kind"])
        self.assertEqual("partial", envelope["status"])
        self.assertFalse(envelope["progress"]["discoveryComplete"])
        self.assertEqual(
            envelope["coverage"]["discoveryComplete"],
            envelope["progress"]["discoveryComplete"],
        )
        self.assertEqual(["discovery"], envelope["progress"]["omittedUnitKinds"])
        self.assertIsNone(envelope["coverage"]["sessionCoverage"])
        self.assertEqual("unknown", envelope["coverage"]["sessionCoverageStatus"])

    def test_worker_only_writes_inside_the_run_directory(self) -> None:
        harness = Harness(self.root, ["session-1"])
        harness.drive(harness.start(session_batch_size="1"))

        entries = sorted(path.name for path in self.root.iterdir())

        self.assertEqual(["run", "session-state", "spill"], entries)

    def test_every_envelope_conforms_to_the_published_schema(self) -> None:
        schemas = json.loads(
            (SKILL_DIR / "assets" / "schemas.json").read_text(encoding="utf-8")
        )
        definitions = schemas["$defs"]
        harness = Harness(self.root, ["session-1", "session-2"])
        envelope = harness.start(session_batch_size="1")
        envelopes = [envelope]

        while envelope["kind"] == "wave":
            for action in envelope["wave"]["actions"]:
                harness.respond(action)
            envelope = harness.advance()
            envelopes.append(envelope)
        _, status_envelope, _ = harness.invoke(
            "status",
            "--run-dir",
            str(harness.run_dir),
        )
        envelopes.append(status_envelope)

        self.assertEqual("terminal", envelopes[-2]["kind"])
        for observed in envelopes:
            self.assertEqual(
                [],
                conformance_errors(
                    observed,
                    definitions["workerEnvelope"],
                    definitions,
                ),
                observed["kind"],
            )

    def test_blocked_envelope_conforms_to_the_published_schema(self) -> None:
        schemas = json.loads(
            (SKILL_DIR / "assets" / "schemas.json").read_text(encoding="utf-8")
        )
        definitions = schemas["$defs"]
        harness = Harness(self.root, ["session-1"])
        envelope = harness.start(session_batch_size="1")
        harness.run_dir.mkdir(parents=True, exist_ok=True)
        (harness.run_dir / "primitives.sanitized.json").write_text(
            json.dumps({"scope": {"repository": "other/repository"}}),
            encoding="utf-8",
        )
        harness.respond(envelope["wave"]["actions"][0])

        envelope = harness.advance()

        self.assertEqual("blocked", envelope["status"])
        self.assertEqual(
            [],
            conformance_errors(
                envelope,
                definitions["workerEnvelope"],
                definitions,
            ),
        )


class ReviewRegressionTests(unittest.TestCase):
    """Regressions for the findings raised on pull request 42."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def metadata_wave(self, harness: Harness, **options: str) -> dict[str, object]:
        envelope = harness.start(**options)
        harness.respond(envelope["wave"]["actions"][0])
        envelope = harness.advance()
        self.assertEqual("metadata", envelope["wave"]["actions"][0]["kind"])
        return envelope

    def test_retriable_failure_never_harvests_its_own_previous_attempt(self) -> None:
        harness = Harness(self.root, ["session-1"])
        envelope = self.metadata_wave(harness, session_batch_size="1")
        metadata = envelope["wave"]["actions"][0]

        harness.respond(metadata, success=False, error="connection reset by peer")
        envelope = harness.advance()

        self.assertEqual(1, envelope["recorded"]["counts"]["query-failure"])
        self.assertEqual("wave", envelope["kind"])
        reissued = envelope["wave"]["actions"][0]
        self.assertEqual(metadata["actionId"], reissued["actionId"])
        self.assertEqual(metadata["query"], reissued["query"])

        envelope = harness.advance()

        self.assertEqual("wave", envelope["kind"])
        self.assertEqual(1, envelope["recorded"]["counts"]["pending"])
        self.assertEqual(0, envelope["recorded"]["counts"]["query-failure"])
        self.assertEqual(1, envelope["progress"]["failedQueries"])
        self.assertEqual(1, envelope["progress"]["batchCount"])
        self.assertEqual(metadata["actionId"], envelope["wave"]["actions"][0]["actionId"])

        harness.respond(envelope["wave"]["actions"][0])
        envelope = harness.advance()

        self.assertEqual(1, envelope["recorded"]["counts"]["success"])
        self.assertEqual("complete", harness.drive(envelope)["status"])

    def test_wave_boundary_is_persisted_per_issued_action(self) -> None:
        harness = Harness(self.root, ["session-1"])
        envelope = self.metadata_wave(harness, session_batch_size="1")
        metadata = envelope["wave"]["actions"][0]
        harness.respond(metadata, success=False, error="connection reset by peer")

        envelope = harness.advance()
        worker_state = json.loads(
            (harness.run_dir / "worker" / "worker-state.json").read_text(
                encoding="utf-8"
            )
        )

        boundary = worker_state["waveBoundary"]
        self.assertEqual([metadata["actionId"]], list(boundary))
        self.assertEqual(1, len(boundary[metadata["actionId"]]))

        terminal = harness.drive(envelope)
        final = json.loads(
            (harness.run_dir / "worker" / "worker-state.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual("complete", terminal["status"])
        self.assertEqual({}, final["waveBoundary"])

    def test_unreadable_spill_file_blocks_instead_of_crashing(self) -> None:
        harness = Harness(self.root, ["session-1"])
        envelope = self.metadata_wave(harness, session_batch_size="1")
        metadata = envelope["wave"]["actions"][0]
        harness.respond(metadata, spill=True)
        harness.spill_paths[metadata["actionId"]].write_bytes(b"\xff\xfe not utf-8")

        code, envelope, stderr = harness.invoke(
            "advance",
            "--run-dir",
            str(harness.run_dir),
            "--events-root",
            str(harness.events_root),
        )

        self.assertEqual(0, code, stderr)
        self.assertEqual("terminal", envelope["kind"])
        self.assertEqual("blocked", envelope["status"])
        self.assertTrue(envelope["terminal"])
        self.assertEqual(1, envelope["recorded"]["counts"]["artifact"])
        self.assertEqual("artifact", envelope["blocker"]["errorKind"])
        self.assertIn("could not be read", envelope["blocker"]["reason"])

    def test_probe_result_never_raises_on_an_unreadable_spill_file(self) -> None:
        spill = self.root / "spill.txt"
        spill.write_bytes(b"\xff\xfe")
        events = self.root / "session-state" / "current"
        events.mkdir(parents=True)
        (events / "events.jsonl").write_text(
            json.dumps(
                {
                    "type": "tool.execution_complete",
                    "data": {
                        "toolCallId": "call-1",
                        "success": True,
                        "result": {
                            "content": f"Output too large to read at once. Saved to: {spill}",
                            "detailedContent": "SQL (session_store): SELECT 1",
                        },
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        probe = materializer.probe_result(
            self.root / "session-state",
            "SELECT 1",
            "action-1",
        )

        self.assertEqual("error", probe["state"])
        self.assertIn("could not be read", probe["message"])
        with self.assertRaisesRegex(ValueError, "could not be read"):
            materializer.result_content(
                self.root / "session-state",
                "SELECT 1",
                "action-1",
            )

    def test_then_command_is_shell_safe_for_paths_containing_spaces(self) -> None:
        harness = Harness(self.root, ["session-1"], run_name="forge run dir")

        envelope = harness.start(session_batch_size="1")

        command = envelope["next"]["thenCommand"]
        self.assertEqual(
            [
                "python3",
                str(SCRIPTS_DIR / "extraction-worker.py"),
                "advance",
                "--run-dir",
                str(harness.run_dir.resolve()),
            ],
            shlex.split(command),
        )
        self.assertIn("forge run dir", shlex.split(command)[-1])
        self.assertEqual("complete", harness.drive(envelope)["status"])

    def test_conformance_checker_rejects_what_the_contract_forbids(self) -> None:
        schemas = json.loads(
            (SKILL_DIR / "assets" / "schemas.json").read_text(encoding="utf-8")
        )
        definitions = schemas["$defs"]
        harness = Harness(self.root, ["session-1"])
        valid = harness.start(session_batch_size="1")

        def errors(envelope: dict[str, object]) -> list[str]:
            return conformance_errors(
                envelope,
                definitions["workerEnvelope"],
                definitions,
            )

        self.assertEqual([], errors(valid))

        string_cycle = {**valid, "cycle": "2"}
        zero_actions = {
            **valid,
            "wave": {**valid["wave"], "actionCount": 0, "actions": []},
        }
        negative_count = {
            **valid,
            "recorded": {
                **valid["recorded"],
                "counts": {**valid["recorded"]["counts"], "success": -1},
            },
        }
        terminal_with_wave = {
            **valid,
            "kind": "terminal",
            "terminal": True,
            "ledgerPath": "/tmp/ledger.json",
        }
        wrong_enum = {**valid, "status": "finished"}
        undeclared = {**valid, "surprise": 1}

        self.assertTrue(any("cycle" in error for error in errors(string_cycle)))
        self.assertTrue(any("items" in error for error in errors(zero_actions)))
        self.assertTrue(any("below" in error for error in errors(negative_count)))
        self.assertTrue(
            any("forbidden" in error for error in errors(terminal_with_wave))
        )
        self.assertTrue(any("status" in error for error in errors(wrong_enum)))
        self.assertTrue(any("surprise" in error for error in errors(undeclared)))

    def test_conformance_checker_rejects_unsupported_schema_keywords(self) -> None:
        with self.assertRaisesRegex(AssertionError, "unsupported schema keywords"):
            conformance_errors({"a": 1}, {"patternProperties": {}}, {})


class WorkerContractTests(unittest.TestCase):
    def test_skill_documents_the_worker_protocol(self) -> None:
        contract = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("scripts/extraction-worker.py", contract)
        self.assertIn("advance --run-dir", contract)
        self.assertIn("status \\\n  --run-dir \"$RUN_DIR\" --assert-terminal", contract)
        self.assertIn("`workerEnvelope`", contract)
        self.assertIn("reference/extraction-worker.md", contract)
        self.assertNotIn("record-success \\", contract)
        self.assertNotIn("extraction-state.next.json", contract)
        self.assertNotIn("--parallel", contract)

    def test_reference_documents_the_runtime_boundary_and_fallback(self) -> None:
        reference = (
            SKILL_DIR / "reference" / "extraction-worker.md"
        ).read_text(encoding="utf-8")

        self.assertIn("Remaining runtime boundary", reference)
        self.assertIn("Minimal cross-repo follow-up contract", reference)
        self.assertIn("## Fallback", reference)
        self.assertIn("record-checkpoint-failure", reference)


if __name__ == "__main__":
    unittest.main()
