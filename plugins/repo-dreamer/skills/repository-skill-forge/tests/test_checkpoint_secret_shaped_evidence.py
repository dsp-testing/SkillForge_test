#!/usr/bin/env python3

"""Regression coverage for github/agents#1861.

The seven-day post-merge run (copilot-dreams Actions run 32510078508)
discovered 272 sessions across eleven batches, then stopped after batch 3
because `checkpoint-completed-batches.py` treated a secret-shaped
`assigned_secret` finding as batch-fatal. The sanitizer had already stripped
`rawEvidence` and redacted the emitted `commandTemplate`, so the abort
discarded already-safe output and left batches 4 through 11 unprocessed.

These tests reproduce that eleven-batch shape directly against the
checkpoint script: a secret-shaped finding in one batch must not prevent any
other batch from checkpointing, the matched value must never reach the
ledger or the per-batch diagnostics, and rerunning the checkpoint must stay
idempotent.
"""

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

BATCH_COUNT = 11
SECRET_SHAPED_BATCH = "batch-3"
# A synthetic value shaped like the `assigned_secret` heuristic
# (`token = <long literal>`) so it is never mistaken for a real credential.
SECRET_LITERAL = "abcdefghijklmnop1234567"
SECRET_COMMAND = f'token="{SECRET_LITERAL}"'
BENIGN_COMMAND = "go test ./..."


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def write_batch_fixture(run_dir: Path, batch_id: str, session_id: str, command: str) -> dict[str, object]:
    """Write the four artifact files one completed batch reads, mirroring an
    exactly-one-session batch from the real eleven-batch run."""
    extraction = run_dir / "extraction"
    metadata = extraction / f"metadata-{batch_id}.json"
    refs = extraction / f"refs-{batch_id}.accepted.json"
    files = extraction / f"files-{batch_id}.accepted.json"
    tools = extraction / f"tools-{batch_id}.accepted.json"
    for accepted in (refs, files, tools):
        write_json(
            accepted.with_name(accepted.name.removesuffix(".accepted.json") + ".json"),
            [],
        )
    write_json(
        metadata,
        [
            {
                "session_id": session_id,
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
                "session_id": session_id,
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
                "session_id": session_id,
                "file_path": "scripts/release.sh",
                "tool_name": "edit",
                "turn_index": 2,
            }
        ],
    )
    write_json(
        tools,
        [
            {
                "session_id": session_id,
                "tool_call_id": f"call-{session_id}",
                "tool_name": "bash",
                "arguments_json": json.dumps({"command": command}),
                "exit_code": 0,
                "completed_at": "2026-08-17T00:30:00Z",
            }
        ],
    )
    return {
        "batchId": batch_id,
        "sessionIds": [session_id],
        "status": "complete",
        "metadataArtifact": str(metadata),
        "refsArtifacts": [str(refs)],
        "filesArtifacts": [str(files)],
        "toolArtifacts": [str(tools)],
    }


def build_state(run_dir: Path) -> dict[str, object]:
    batches = []
    for index in range(1, BATCH_COUNT + 1):
        batch_id = f"batch-{index}"
        session_id = f"session-{index}"
        command = SECRET_COMMAND if batch_id == SECRET_SHAPED_BATCH else BENIGN_COMMAND
        batches.append(write_batch_fixture(run_dir, batch_id, session_id, command))
    return {
        "scope": {
            "kind": "repository",
            "repository": "owner/repository",
            "windowStart": "2026-08-11T00:00:00Z",
            "windowEnd": "2026-08-18T00:00:00Z",
        },
        "runDir": str(run_dir),
        "status": "running",
        "partitions": [{"batches": batches}],
    }


def all_text(run_dir: Path) -> str:
    """Concatenate every file still on disk under run_dir for leakage scans."""
    chunks = []
    for path in run_dir.rglob("*"):
        if path.is_file():
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(chunks)


class SecretShapedBatchRegressionTests(unittest.TestCase):
    def test_secret_shaped_batch_does_not_abort_later_batches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            state = build_state(run_dir)
            ledger_path = run_dir / "primitives.sanitized.json"

            result = checkpoint.checkpoint(
                state,
                ledger_path=ledger_path,
                main_branches={"main"},
            )

            expected_batch_ids = [f"batch-{index}" for index in range(1, BATCH_COUNT + 1)]
            self.assertEqual(expected_batch_ids, result["checkpointedBatchIds"])
            self.assertEqual(BATCH_COUNT, result["checkpointedBatchCount"])

            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            self.assertEqual(sorted(expected_batch_ids), ledger["processedBatchIds"])
            self.assertEqual(BATCH_COUNT, len(ledger["primitives"]))

    def test_secret_shaped_finding_is_advisory_and_recorded_without_the_value(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            state = build_state(run_dir)
            ledger_path = run_dir / "primitives.sanitized.json"

            result = checkpoint.checkpoint(
                state,
                ledger_path=ledger_path,
                main_branches={"main"},
            )

            diagnostics = result["findingDiagnostics"]
            self.assertEqual(1, diagnostics["findingCount"])
            self.assertEqual(0, diagnostics["blockingFindingCount"])
            self.assertEqual(1, diagnostics["advisoryFindingCount"])
            self.assertEqual({"assigned_secret": 1}, diagnostics["findingsByKind"])
            self.assertEqual([SECRET_SHAPED_BATCH], diagnostics["batchesWithFindings"])

            report = json.loads(
                (run_dir / "batches" / SECRET_SHAPED_BATCH / "leakage-report.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(1, report["findingCount"])
            self.assertEqual(0, report["blockingFindingCount"])
            self.assertEqual(1, report["advisoryFindingCount"])
            self.assertEqual(["assigned_secret"], [item["kind"] for item in report["findings"]])
            self.assertEqual(["advisory"], [item["severity"] for item in report["findings"]])

    def test_promoted_command_template_is_redacted_not_raw(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            state = build_state(run_dir)
            ledger_path = run_dir / "primitives.sanitized.json"

            checkpoint.checkpoint(
                state,
                ledger_path=ledger_path,
                main_branches={"main"},
            )

            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            templates = [primitive["commandTemplate"] for primitive in ledger["primitives"]]
            self.assertIn("<redacted-secret>", templates)
            self.assertIn(BENIGN_COMMAND, templates)

    def test_no_matched_value_survives_anywhere_under_run_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            state = build_state(run_dir)
            ledger_path = run_dir / "primitives.sanitized.json"

            checkpoint.checkpoint(
                state,
                ledger_path=ledger_path,
                main_branches={"main"},
            )

            self.assertNotIn(SECRET_LITERAL, all_text(run_dir))

    def test_rerunning_checkpoint_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            state = build_state(run_dir)
            ledger_path = run_dir / "primitives.sanitized.json"

            checkpoint.checkpoint(state, ledger_path=ledger_path, main_branches={"main"})
            before = ledger_path.read_bytes()

            second = checkpoint.checkpoint(state, ledger_path=ledger_path, main_branches={"main"})

            self.assertEqual([], second["checkpointedBatchIds"])
            self.assertEqual(0, second["findingDiagnostics"]["findingCount"])
            self.assertEqual(before, ledger_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
