#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

CHECKPOINT_SPEC = importlib.util.spec_from_file_location(
    "checkpoint_completed_batches",
    SCRIPTS_DIR / "checkpoint-completed-batches.py",
)
assert CHECKPOINT_SPEC is not None and CHECKPOINT_SPEC.loader is not None
checkpoint = importlib.util.module_from_spec(CHECKPOINT_SPEC)
CHECKPOINT_SPEC.loader.exec_module(checkpoint)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class ProgressiveCheckpointTests(unittest.TestCase):
    def test_terminal_empty_run_writes_covered_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            state = {
                "scope": {
                    "kind": "repository",
                    "repository": "owner/repository",
                    "windowStart": "2026-08-11T00:00:00Z",
                    "windowEnd": "2026-08-18T00:00:00Z",
                },
                "runDir": str(run_dir),
                "status": "complete",
                "coverage": {
                    "discoveredSessionCount": 0,
                    "completedSessionCount": 0,
                    "discoveryComplete": True,
                },
                "partitions": [],
            }
            ledger_path = run_dir / "primitives.sanitized.json"

            result = checkpoint.checkpoint(
                state,
                ledger_path=ledger_path,
                main_branches={"main"},
            )
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))

            self.assertTrue(result["terminalCoverageAttached"])
            self.assertEqual("owner/repository", ledger["scope"]["repository"])
            self.assertEqual([], ledger["primitives"])
            self.assertEqual(0, ledger["coverage"]["completedSessionCount"])

    def test_completed_batch_is_sanitized_merged_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            extraction = run_dir / "extraction"
            metadata = extraction / "metadata-batch.json"
            refs = extraction / "refs-batch-0-p1000-r0.accepted.json"
            files = extraction / "files-batch-0-p1000-r0.accepted.json"
            tools = extraction / "tools-batch-0-p1000-r0.accepted.json"
            for accepted in (refs, files, tools):
                write_json(
                    accepted.with_name(
                        accepted.name.removesuffix(".accepted.json") + ".json"
                    ),
                    [],
                )
            write_json(
                metadata,
                [
                    {
                        "session_id": "session-1",
                        "agent_name": "Copilot CLI",
                        "repository": "owner/repository",
                        "branch": "main",
                        "created_at": "2026-08-17T00:00:00Z",
                        "updated_at": "2026-08-17T01:00:00Z",
                    }
                ],
            )
            write_json(
                refs,
                [
                    {
                        "session_id": "session-1",
                        "ref_type": "pr",
                        "ref_value": "1",
                        "turn_index": 1,
                    }
                ],
            )
            write_json(
                files,
                [
                    {
                        "session_id": "session-1",
                        "file_path": "scripts/test.sh",
                        "tool_name": "edit",
                        "turn_index": 2,
                    }
                ],
            )
            write_json(
                tools,
                [
                    {
                        "session_id": "session-1",
                        "tool_call_id": "call-1",
                        "tool_name": "bash",
                        "arguments_json": json.dumps({"command": "go test ./..."}),
                        "exit_code": None,
                        "completed_at": None,
                    }
                ],
            )
            state = {
                "scope": {
                    "kind": "repository",
                    "repository": "owner/repository",
                    "windowStart": "2026-08-11T00:00:00Z",
                    "windowEnd": "2026-08-18T00:00:00Z",
                },
                "runDir": str(run_dir),
                "status": "running",
                "partitions": [
                    {
                        "batches": [
                            {
                                "batchId": "batch-1",
                                "sessionIds": ["session-1"],
                                "status": "complete",
                                "metadataArtifact": str(metadata),
                                "refsArtifacts": [str(refs)],
                                "filesArtifacts": [str(files)],
                                "toolArtifacts": [str(tools)],
                            }
                        ]
                    }
                ],
            }
            ledger_path = run_dir / "primitives.sanitized.json"

            first = checkpoint.checkpoint(
                state,
                ledger_path=ledger_path,
                main_branches={"main"},
            )
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))

            self.assertEqual(["batch-1"], first["checkpointedBatchIds"])
            self.assertEqual(["batch-1"], ledger["processedBatchIds"])
            self.assertEqual("go test ./...", ledger["primitives"][0]["commandTemplate"])
            self.assertFalse(metadata.exists())
            self.assertFalse(refs.exists())
            self.assertFalse(files.exists())
            self.assertFalse(tools.exists())
            self.assertTrue(
                (run_dir / "batches/batch-1/primitives.sanitized.json").is_file()
            )

            before = ledger_path.read_bytes()
            second = checkpoint.checkpoint(
                state,
                ledger_path=ledger_path,
                main_branches={"main"},
            )

            self.assertEqual([], second["checkpointedBatchIds"])
            self.assertEqual(before, ledger_path.read_bytes())

            state["status"] = "complete"
            state["coverage"] = {
                "discoveredSessionCount": 1,
                "completedSessionCount": 1,
                "discoveryComplete": True,
            }
            terminal = checkpoint.checkpoint(
                state,
                ledger_path=ledger_path,
                main_branches={"main"},
            )
            finalized = json.loads(ledger_path.read_text(encoding="utf-8"))

            self.assertTrue(terminal["terminalCoverageAttached"])
            self.assertFalse(finalized["coverage"]["partial"])
            self.assertEqual(1, finalized["coverage"]["completedSessionCount"])


if __name__ == "__main__":
    unittest.main()
