#!/usr/bin/env python3

"""Full worker/controller regression coverage for github/agents#1861.

Mirrors the eleven-batch shape of the post-merge run
(copilot-dreams Actions run 32510078508) through the real
`extraction-worker.py` protocol: eleven single-session batches, one of which
(the third) carries a secret-shaped tool-call command. Before this fix,
`checkpoint-completed-batches.py` treated that finding as batch-fatal and the
whole run ended `blocked` with `errorKind: "artifact"`, discarding the eight
already-safe later batches. This confirms the run instead reaches `complete`,
and that no matched value leaks into the ledger, the checkpoint summary, the
persisted controller state, or the `diagnostics` snapshot.
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

# Reuse the deterministic session_store_sql Harness the worker tests already
# validate against the real subprocess-driven protocol.
import subprocess  # noqa: E402

import test_extraction_worker as worker_tests  # noqa: E402

BATCH_SESSION_COUNT = 11
SECRET_SESSION = "session-3"
SECRET_LITERAL = "abcdefghijklmnop1234567"
SECRET_COMMAND = f'token="{SECRET_LITERAL}"'


class SecretShapedHarness(worker_tests.Harness):
    """Harness whose batch 3 tool-call row embeds a secret-shaped assignment."""

    def rows_for(self, action: dict[str, object]) -> list[dict[str, object]]:
        rows = super().rows_for(action)
        if str(action["kind"]) == "tool-calls":
            for row in rows:
                if row["session_id"] == SECRET_SESSION:
                    row["arguments_json"] = json.dumps({"command": SECRET_COMMAND})
        return rows


def all_text(run_dir: Path) -> str:
    chunks = []
    for path in run_dir.rglob("*"):
        if path.is_file():
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(chunks)


class WorkerSecretShapedEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_eleven_batch_run_completes_despite_one_secret_shaped_batch(self) -> None:
        sessions = [f"session-{index}" for index in range(1, BATCH_SESSION_COUNT + 1)]
        harness = SecretShapedHarness(self.root, sessions)

        envelope = harness.drive(harness.start(session_batch_size="1"))

        self.assertEqual("terminal", envelope["kind"])
        self.assertEqual("complete", envelope["status"])
        self.assertEqual(BATCH_SESSION_COUNT, envelope["coverage"]["completedSessionCount"])
        self.assertEqual(0, envelope["progress"]["blockerCount"])

    def test_no_matched_value_reaches_ledger_checkpoint_controller_or_diagnostics(self) -> None:
        sessions = [f"session-{index}" for index in range(1, BATCH_SESSION_COUNT + 1)]
        harness = SecretShapedHarness(self.root, sessions)

        envelope = harness.start(session_batch_size="1")
        finding_summaries = []
        while envelope["kind"] == "wave":
            for action in envelope["wave"]["actions"]:
                harness.respond(action)
            envelope = harness.advance()
            checkpoint_summary = envelope.get("checkpoint") or {}
            diagnostics = checkpoint_summary.get("findingDiagnostics") or {}
            if diagnostics.get("findingCount"):
                finding_summaries.append(diagnostics)
            # The raw value must never appear in any advance envelope, live or terminal.
            self.assertNotIn(SECRET_LITERAL, json.dumps(envelope))

        self.assertEqual("terminal", envelope["kind"])
        self.assertEqual("complete", envelope["status"])

        # The batch containing the secret-shaped command must have surfaced
        # its diagnostics at the moment it was checkpointed.
        self.assertEqual(1, len(finding_summaries))
        diagnostics = finding_summaries[0]
        self.assertEqual(1, diagnostics["findingCount"])
        self.assertEqual(0, diagnostics["blockingFindingCount"])
        self.assertEqual(1, diagnostics["advisoryFindingCount"])
        self.assertEqual({"assigned_secret": 1}, diagnostics["findingsByKind"])

        ledger = json.loads(
            (harness.run_dir / "primitives.sanitized.json").read_text(encoding="utf-8")
        )
        ledger_text = json.dumps(ledger)
        self.assertNotIn(SECRET_LITERAL, ledger_text)
        self.assertIn(
            "<redacted-secret>",
            [primitive["commandTemplate"] for primitive in ledger["primitives"]],
        )

        # The per-batch leakage report is never deleted; it must record the
        # finding by kind and count without ever retaining the matched text.
        reports = list(harness.run_dir.glob("batches/*/leakage-report.json"))
        reports_with_findings = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in reports
            if json.loads(path.read_text(encoding="utf-8"))["findingCount"]
        ]
        self.assertEqual(1, len(reports_with_findings))
        self.assertEqual(["assigned_secret"], [item["kind"] for item in reports_with_findings[0]["findings"]])
        self.assertNotIn(SECRET_LITERAL, json.dumps(reports_with_findings[0]))

        state_path = harness.run_dir / "extraction-state.json"
        self.assertNotIn(SECRET_LITERAL, state_path.read_text(encoding="utf-8"))

        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS_DIR / "extraction-controller.py"),
                "diagnostics",
                "--state",
                str(state_path),
                "--checkpoint",
                str(harness.run_dir / "checkpoint-summary.json"),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        controller_diagnostics = json.loads(completed.stdout)
        self.assertNotIn(SECRET_LITERAL, completed.stdout)
        self.assertEqual("complete", controller_diagnostics["status"])
        self.assertEqual(0, controller_diagnostics["blockerCount"])

        self.assertNotIn(SECRET_LITERAL, all_text(harness.run_dir))


if __name__ == "__main__":
    unittest.main()
