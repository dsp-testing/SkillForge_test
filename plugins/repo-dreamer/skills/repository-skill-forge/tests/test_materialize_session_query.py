#!/usr/bin/env python3

from __future__ import annotations

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

from session_queries import build_tool_calls_query

MATERIALIZER_SPEC = importlib.util.spec_from_file_location(
    "materialize_session_query",
    SCRIPTS_DIR / "materialize-session-query.py",
)
assert MATERIALIZER_SPEC is not None and MATERIALIZER_SPEC.loader is not None
materializer = importlib.util.module_from_spec(MATERIALIZER_SPEC)
MATERIALIZER_SPEC.loader.exec_module(materializer)

NORMALIZER_SPEC = importlib.util.spec_from_file_location(
    "normalize_sessions",
    SCRIPTS_DIR / "normalize-sessions.py",
)
assert NORMALIZER_SPEC is not None and NORMALIZER_SPEC.loader is not None
normalizer = importlib.util.module_from_spec(NORMALIZER_SPEC)
NORMALIZER_SPEC.loader.exec_module(normalizer)

DERIVER_SPEC = importlib.util.spec_from_file_location(
    "derive_primitives",
    SCRIPTS_DIR / "derive-primitives.py",
)
assert DERIVER_SPEC is not None and DERIVER_SPEC.loader is not None
deriver = importlib.util.module_from_spec(DERIVER_SPEC)
DERIVER_SPEC.loader.exec_module(deriver)


class MaterializeSessionQueryTests(unittest.TestCase):
    def test_primary_tool_query_uses_only_minimal_completion_events(self) -> None:
        query = build_tool_calls_query(
            session_ids=["session-1"],
            start="2026-08-01T00:00:00Z",
            end="2026-08-02T00:00:00Z",
            limit=500,
        )

        self.assertEqual(1, query.count("FROM events"))
        self.assertNotIn("tool.execution_start", query)
        self.assertNotIn("tool_complete_success", query)
        self.assertNotIn(" AS result_content", query)
        self.assertIn("AS exit_code", query)
        self.assertIn("FROM tool_requests tr", query)

    def test_materializes_spilled_tool_rows_with_pipes_and_newlines(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            events_root = root / "session-state"
            event_dir = events_root / "session-1"
            event_dir.mkdir(parents=True)
            output = root / "tools.json"
            spill = root / "tool-output.txt"
            sql = "SELECT session_id, tool_call_id FROM tool_requests"
            spill.write_text(
                "\n".join(
                    [
                        "2 row(s) returned:",
                        "",
                        "| session_id | tool_call_id | tool_name | arguments_json | exit_code | completed_at |",
                        "| --- | --- | --- | --- | --- | --- |",
                        '| session-1 | call-1 | bash | {"command":"printf \\"a | b\\\\n\\""} | 0 | 2026-08-01T00:00:01Z |',
                        "| session-1 | call-2 | edit | {} | NULL | NULL |",
                    ]
                ),
                encoding="utf-8",
            )
            (event_dir / "events.jsonl").write_text(
                json.dumps(
                    {
                        "type": "tool.execution_complete",
                        "data": {
                            "success": True,
                            "result": {
                                "content": f"Output too large to read at once. Saved to: {spill}",
                                "detailedContent": f"SQL (session_store/repo/owner/repository): {sql}",
                            },
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            actions = root / "actions.json"
            actions.write_text(
                json.dumps(
                    {
                        "kind": "action-batch",
                        "actions": [
                            {
                                "actionId": "tools-1",
                                "kind": "tools",
                                "sql": sql,
                                "outputPath": str(output),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "materialize-session-query.py"),
                    "--actions",
                    str(actions),
                    "--action",
                    "tools-1",
                    "--events-root",
                    str(events_root),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual("", result.stderr)
            self.assertEqual(0, result.returncode)
            rows = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(2, len(rows))
            self.assertEqual(0, rows[0]["exit_code"])
            self.assertEqual(
                {"command": 'printf "a | b\\n"'},
                json.loads(rows[0]["arguments_json"]),
            )
            self.assertIsNone(rows[1]["exit_code"])
            self.assertIsNone(rows[1]["completed_at"])

    def test_uses_latest_exact_sql_result_after_an_earlier_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            events_root = Path(temporary)
            event_dir = events_root / "session-1"
            event_dir.mkdir()
            sql = "SELECT id AS session_id, updated_at FROM sessions"
            events = [
                {
                    "type": "tool.execution_complete",
                    "data": {
                        "success": False,
                        "result": {
                            "content": "timeout",
                            "detailedContent": f"SQL (session_store/repo/owner/repository): {sql}",
                        },
                    },
                },
                {
                    "type": "tool.execution_complete",
                    "data": {
                        "success": True,
                        "result": {
                            "content": "\n".join(
                                [
                                    "1 row(s) returned:",
                                    "",
                                    "| session_id | updated_at |",
                                    "| --- | --- |",
                                    "| session-1 | 2026-08-01T00:00:00Z |",
                                ]
                            ),
                            "detailedContent": f"SQL (session_store/repo/owner/repository): {sql}",
                        },
                    },
                },
            ]
            (event_dir / "events.jsonl").write_text(
                "".join(json.dumps(event) + "\n" for event in events),
                encoding="utf-8",
            )

            content = materializer.result_content(events_root, sql)
            header, rows = materializer.table_rows(content)

            self.assertEqual(["session_id", "updated_at"], header)
            self.assertEqual(
                [
                    {
                        "session_id": "session-1",
                        "updated_at": "2026-08-01T00:00:00Z",
                    }
                ],
                materializer.parse_rows("discovery", header, rows),
            )

    def test_matches_detailed_content_with_sql_and_rendered_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            events_root = Path(temporary)
            event_dir = events_root / "session-1"
            event_dir.mkdir()
            sql = "SELECT id AS session_id, updated_at FROM sessions"
            content = "\n".join(
                [
                    "1 row(s) returned:",
                    "",
                    "| session_id | updated_at |",
                    "| --- | --- |",
                    "| session-1 | 2026-08-01T00:00:00Z |",
                ]
            )
            event = {
                "type": "tool.execution_complete",
                "data": {
                    "success": True,
                    "result": {
                        "content": content,
                        "detailedContent": (
                            "SQL (session_store/repo/owner/repository): "
                            f"{sql}\n\n{content}"
                        ),
                    },
                },
            }
            (event_dir / "events.jsonl").write_text(
                json.dumps(event) + "\n",
                encoding="utf-8",
            )

            self.assertEqual(content, materializer.result_content(events_root, sql))

    def test_matches_completion_by_exact_start_event_sql(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            events_root = Path(temporary)
            event_dir = events_root / "session-1"
            event_dir.mkdir()
            sql = "SELECT id AS session_id, updated_at FROM sessions"
            content = "Query returned 0 rows."
            events = [
                {
                    "type": "tool.execution_start",
                    "data": {
                        "toolCallId": "call-1",
                        "toolName": "session_store_sql",
                        "arguments": {
                            "description": "Discover repository sessions",
                            "query": sql,
                        },
                    },
                },
                {
                    "type": "tool.execution_complete",
                    "data": {
                        "toolCallId": "call-1",
                        "success": True,
                        "result": {
                            "content": content,
                            "detailedContent": "SQL omitted from detailed output",
                        },
                    },
                },
            ]
            (event_dir / "events.jsonl").write_text(
                "".join(json.dumps(event) + "\n" for event in events),
                encoding="utf-8",
            )

            self.assertEqual(content, materializer.result_content(events_root, sql))

    def test_skips_malformed_event_log_lines_before_a_match(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            events_root = Path(temporary)
            event_dir = events_root / "session-1"
            event_dir.mkdir()
            sql = "SELECT id AS session_id, updated_at FROM sessions"
            event = {
                "type": "tool.execution_complete",
                "data": {
                    "success": True,
                    "result": {
                        "content": "Query returned 0 rows.",
                        "detailedContent": f"SQL (session_store/repo/owner/repository): {sql}",
                    },
                },
            }
            (event_dir / "events.jsonl").write_text(
                "\n{partially-written\n" + json.dumps(event) + "\n",
                encoding="utf-8",
            )

            self.assertEqual(
                "Query returned 0 rows.",
                materializer.result_content(events_root, sql),
            )

    def test_rejects_match_without_explicit_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            events_root = Path(temporary)
            event_dir = events_root / "session-1"
            event_dir.mkdir()
            sql = "SELECT id AS session_id, updated_at FROM sessions"
            event = {
                "type": "tool.execution_complete",
                "data": {
                    "result": {
                        "content": "Query returned 0 rows.",
                        "detailedContent": f"SQL (session_store/repo/owner/repository): {sql}",
                    },
                },
            }
            (event_dir / "events.jsonl").write_text(
                json.dumps(event) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "not explicitly successful"):
                materializer.result_content(events_root, sql)

    def test_zero_rows_and_row_count_mismatch_are_explicit(self) -> None:
        self.assertEqual((None, []), materializer.table_rows("Query returned 0 rows."))
        with self.assertRaisesRegex(ValueError, "row count mismatch"):
            materializer.table_rows(
                "\n".join(
                    [
                        "2 row(s) returned:",
                        "",
                        "| session_id | updated_at |",
                        "| --- | --- |",
                        "| session-1 | 2026-08-01T00:00:00Z |",
                    ]
                )
            )

    def test_explicit_exit_code_drives_primitive_outcome(self) -> None:
        normalized = normalizer.normalize_batched_rows(
            [
                {
                    "session_id": "session-1",
                    "agent_name": "Copilot CLI",
                    "repository": "owner/repository",
                    "branch": "main",
                    "created_at": "2026-08-01T00:00:00Z",
                    "updated_at": "2026-08-01T00:01:00Z",
                }
            ],
            [],
            [],
            [
                {
                    "session_id": "session-1",
                    "tool_call_id": "call-1",
                    "tool_name": "bash",
                    "arguments_json": '{"command":"false"}',
                    "exit_code": 1,
                    "completed_at": "2026-08-01T00:00:30Z",
                }
            ],
            repository="owner/repository",
            window_start="2026-08-01T00:00:00Z",
            window_end="2026-08-02T00:00:00Z",
            limit_sessions=500,
        )

        primitives = deriver.derive(normalized)["primitives"]

        self.assertEqual(1, primitives[0]["exitCode"])
        self.assertEqual("failure", primitives[0]["outcome"])


if __name__ == "__main__":
    unittest.main()
