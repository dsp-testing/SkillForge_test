#!/usr/bin/env python3

"""Regression coverage for two security findings from an independent review
of the secret-shaped-evidence checkpoint fix (see
`test_checkpoint_secret_shaped_evidence.py` / `test_worker_secret_shaped_evidence.py`).

1. `private_key` previously matched only the PEM header. `redact()` therefore
   left the key body untouched, and removing the `blockingFindingCount` abort
   would have promoted the raw key body into the ledger. `private_key` (and
   every other concrete credential-format kind) must remain `blocking` and
   must still fail a batch closed: the policy change is that an *advisory*
   `assigned_secret` finding does not abort later batches, not that a
   `blocking` finding becomes promotable.

2. `command_signature` shlex-splits the raw command into individual tokens
   *before* sanitization. A whitespace-separated assignment such as
   `aws_secret_access_key = <value>` becomes three separate tokens, none of
   which alone matches `assigned_secret`, so sanitizing each token
   independently (as `sanitize_value` did) never touched the raw value even
   though `commandTemplate` (redacted from the whole command string) was
   already safe. The fix rebuilds `signature` from the fully redacted
   command before any tokenization happens.
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

AGGREGATE_SPEC = importlib.util.spec_from_file_location(
    "aggregate_primitives",
    SCRIPTS_DIR / "aggregate-primitives.py",
)
assert AGGREGATE_SPEC is not None and AGGREGATE_SPEC.loader is not None
aggregator = importlib.util.module_from_spec(AGGREGATE_SPEC)
AGGREGATE_SPEC.loader.exec_module(aggregator)

import test_extraction_worker as worker_tests  # noqa: E402

PEM_BODY = "MIIEREALPRIVATEKEYMATERIALTHATMUSTNEVERLEAK1234567890"
PEM_COMMAND = (
    "cat deploy.pem\n"
    "-----BEGIN RSA PRIVATE KEY-----\n"
    f"{PEM_BODY}\n"
    "-----END RSA PRIVATE KEY-----"
)
WHITESPACE_SECRET_VALUE = "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY"
WHITESPACE_SEPARATED_COMMAND = f"aws configure set aws_secret_access_key = {WHITESPACE_SECRET_VALUE}"
BENIGN_COMMAND = "go test ./..."


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def write_batch_fixture(run_dir: Path, batch_id: str, session_id: str, command: str) -> dict[str, object]:
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
    write_json(refs, [])
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


def build_state(run_dir: Path, batches: list[tuple[str, str, str]]) -> dict[str, object]:
    return {
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
                    write_batch_fixture(run_dir, batch_id, session_id, command)
                    for batch_id, session_id, command in batches
                ]
            }
        ],
    }


def all_text(run_dir: Path) -> str:
    chunks = []
    for path in run_dir.rglob("*"):
        if path.is_file():
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(chunks)


class PrivateKeyRemainsBlockingTests(unittest.TestCase):
    def test_pem_body_is_fully_redacted_by_the_sanitizer(self) -> None:
        import sys as _sys

        sanitizer_spec = importlib.util.spec_from_file_location(
            "sanitize_evidence_pk", SCRIPTS_DIR / "sanitize-evidence.py"
        )
        assert sanitizer_spec is not None and sanitizer_spec.loader is not None
        sanitizer = importlib.util.module_from_spec(sanitizer_spec)
        sanitizer_spec.loader.exec_module(sanitizer)

        findings = sanitizer.findings(PEM_COMMAND, "evidence-1", "command")
        self.assertEqual(["private_key"], [item["kind"] for item in findings])
        self.assertEqual(["blocking"], [item["severity"] for item in findings])

        redacted = sanitizer.redact(PEM_COMMAND)
        self.assertNotIn(PEM_BODY, redacted)
        self.assertNotIn("-----END RSA PRIVATE KEY-----", redacted)

    def test_batch_with_a_private_key_finding_still_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            state = build_state(
                run_dir,
                [
                    ("batch-1", "session-1", BENIGN_COMMAND),
                    ("batch-2", "session-2", PEM_COMMAND),
                    ("batch-3", "session-3", BENIGN_COMMAND),
                ],
            )
            ledger_path = run_dir / "primitives.sanitized.json"

            with self.assertRaises(ValueError) as raised:
                checkpoint.checkpoint(state, ledger_path=ledger_path, main_branches={"main"})
            self.assertIn("batch-2", str(raised.exception))
            self.assertIn("blocking leakage", str(raised.exception))

            # batch-1 (before the blocking batch) is safe to have promoted;
            # batch-2 and batch-3 (the blocking batch and everything after
            # it in this call) must never merge into the ledger, and no
            # promotable surface (ledger, per-batch sanitized output, or the
            # leakage report) may ever carry the raw key body. The batch's
            # own raw/normalized artifacts are deliberately left on disk for
            # investigation, same as any other checkpoint failure, and are
            # never merged, published, or exposed outside this run
            # directory.
            if ledger_path.is_file():
                ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
                self.assertEqual(["batch-1"], ledger.get("processedBatchIds", []))
                self.assertNotIn(PEM_BODY, json.dumps(ledger))
            batch2_sanitized = run_dir / "batches" / "batch-2" / "primitives.sanitized.json"
            self.assertNotIn(PEM_BODY, batch2_sanitized.read_text(encoding="utf-8"))
            batch2_report = run_dir / "batches" / "batch-2" / "leakage-report.json"
            self.assertNotIn(PEM_BODY, batch2_report.read_text(encoding="utf-8"))
            self.assertFalse((run_dir / "batches" / "batch-3").exists())

    def test_worker_run_ends_blocked_and_never_promotes_the_key(self) -> None:
        class PrivateKeyHarness(worker_tests.Harness):
            def rows_for(self, action: dict[str, object]) -> list[dict[str, object]]:
                rows = super().rows_for(action)
                if str(action["kind"]) == "tool-calls":
                    for row in rows:
                        if row["session_id"] == "session-1":
                            row["arguments_json"] = json.dumps({"command": PEM_COMMAND})
                return rows

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            harness = PrivateKeyHarness(root, ["session-1"])
            envelope = harness.start(session_batch_size="1")
            while envelope["kind"] == "wave":
                for action in envelope["wave"]["actions"]:
                    harness.respond(action)
                envelope = harness.advance()

            self.assertEqual("terminal", envelope["kind"])
            self.assertEqual("blocked", envelope["status"])
            self.assertEqual("artifact", envelope["blocker"]["errorKind"])
            self.assertNotIn(PEM_BODY, json.dumps(envelope))
            self.assertFalse((harness.run_dir / "primitives.sanitized.json").is_file())

            # A blocked run cannot reach terminal `complete`/`partial` status,
            # so nothing downstream (aggregation or publication) would ever
            # read this run's ledger.
            code, status, _ = harness.invoke(
                "status", "--run-dir", str(harness.run_dir), "--assert-terminal"
            )
            self.assertEqual(0, code)
            self.assertEqual("blocked", status["assertion"]["status"])


class WhitespaceSeparatedAssignedSecretTests(unittest.TestCase):
    def test_value_is_absent_from_the_sanitized_primitive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            state = build_state(run_dir, [("batch-1", "session-1", WHITESPACE_SEPARATED_COMMAND)])
            ledger_path = run_dir / "primitives.sanitized.json"

            result = checkpoint.checkpoint(state, ledger_path=ledger_path, main_branches={"main"})

            self.assertEqual(["batch-1"], result["checkpointedBatchIds"])
            diagnostics = result["findingDiagnostics"]
            self.assertEqual(0, diagnostics["blockingFindingCount"])
            self.assertEqual(1, diagnostics["advisoryFindingCount"])

            sanitized = json.loads(
                (run_dir / "batches" / "batch-1" / "primitives.sanitized.json").read_text(
                    encoding="utf-8"
                )
            )
            primitive_text = json.dumps(sanitized)
            self.assertNotIn(WHITESPACE_SECRET_VALUE, primitive_text)
            tokens = sanitized["primitives"][0]["signature"]["tokens"]
            self.assertNotIn(WHITESPACE_SECRET_VALUE, tokens)
            self.assertIn("<redacted-secret>", tokens)

    def test_value_is_absent_from_ledger_checkpoint_diagnostics_and_run_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            state = build_state(run_dir, [("batch-1", "session-1", WHITESPACE_SEPARATED_COMMAND)])
            ledger_path = run_dir / "primitives.sanitized.json"

            result = checkpoint.checkpoint(state, ledger_path=ledger_path, main_branches={"main"})
            self.assertNotIn(WHITESPACE_SECRET_VALUE, json.dumps(result))

            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            self.assertNotIn(WHITESPACE_SECRET_VALUE, json.dumps(ledger))

            report = json.loads(
                (run_dir / "batches" / "batch-1" / "leakage-report.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertNotIn(WHITESPACE_SECRET_VALUE, json.dumps(report))

            self.assertNotIn(WHITESPACE_SECRET_VALUE, all_text(run_dir))

    def test_value_is_absent_from_the_aggregated_candidate_surface(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            state = build_state(run_dir, [("batch-1", "session-1", WHITESPACE_SEPARATED_COMMAND)])
            ledger_path = run_dir / "primitives.sanitized.json"
            checkpoint.checkpoint(state, ledger_path=ledger_path, main_branches={"main"})
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))

            evidence = aggregator.merge_evidence(ledger, "owner/repository")
            patterns = aggregator.aggregate(
                evidence,
                as_of="2026-08-18T00:00:00Z",
                active_days=90,
                stale_days=180,
                merged_prs=set(),
                thresholds={
                    "minDistinctSessions": 1,
                    "minDistinctDays": 1,
                    "minKnownOutcomes": 0,
                    "minSuccessRate": 0.0,
                    "minScoredCoverage": 0.0,
                    "allowUnknownOutcomes": True,
                    "minMergedPrs": 0,
                    "minMainlineEvidence": 0,
                },
            )

            self.assertTrue(patterns)
            self.assertNotIn(WHITESPACE_SECRET_VALUE, json.dumps(patterns))
            self.assertIn(
                "<redacted-secret>",
                json.dumps(patterns[0]["commandTemplates"]),
            )

    def test_worker_run_records_the_finding_as_advisory_without_the_value(self) -> None:
        class WhitespaceSecretHarness(worker_tests.Harness):
            def rows_for(self, action: dict[str, object]) -> list[dict[str, object]]:
                rows = super().rows_for(action)
                if str(action["kind"]) == "tool-calls":
                    for row in rows:
                        if row["session_id"] == "session-1":
                            row["arguments_json"] = json.dumps(
                                {"command": WHITESPACE_SEPARATED_COMMAND}
                            )
                return rows

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            harness = WhitespaceSecretHarness(root, ["session-1"])
            envelope = harness.drive(harness.start(session_batch_size="1"))

            self.assertEqual("complete", envelope["status"])
            self.assertNotIn(WHITESPACE_SECRET_VALUE, json.dumps(envelope))

            state_path = harness.run_dir / "extraction-state.json"
            self.assertNotIn(WHITESPACE_SECRET_VALUE, state_path.read_text(encoding="utf-8"))

            completed = harness_invoke_diagnostics(harness, state_path)
            self.assertNotIn(WHITESPACE_SECRET_VALUE, completed)

            self.assertNotIn(WHITESPACE_SECRET_VALUE, all_text(harness.run_dir))


def harness_invoke_diagnostics(harness: "worker_tests.Harness", state_path: Path) -> str:
    import subprocess

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
    return completed.stdout


if __name__ == "__main__":
    unittest.main()
