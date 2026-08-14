#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from forge_common import stable_hash


def load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


aggregate = load_script("aggregate_primitives", "aggregate-primitives.py")
issue_state = load_script("issue_state", "issue-state.py")


def empty_state() -> dict[str, object]:
    return {
        "schemaVersion": 2,
        "stateVersion": 1,
        "scope": {"kind": "repository", "repository": "owner/repository"},
        "cursor": None,
        "updatedAt": "2026-08-14T00:00:00Z",
        "observations": [],
        "proposalQueue": [],
        "proposalHistory": {},
    }


def observation(fingerprint: str) -> dict[str, object]:
    return {
        "evidenceKey": "a" * 24,
        "fingerprint": fingerprint,
        "sessionHash": "b" * 24,
        "completedAt": "2026-08-13T00:00:00Z",
        "day": "2026-08-13",
        "outcome": "success",
        "surface": "cli",
        "kind": "command",
        "branchHash": None,
        "branchCategory": "unknown",
        "pathFamilies": [],
        "refs": [],
    }


class FingerprintCatalogTests(unittest.TestCase):
    def test_legacy_state_migrates_with_empty_catalog(self) -> None:
        body = (
            "repository-skill-forge-state:v2:begin\n"
            f"{json.dumps(empty_state())}\n"
            "repository-skill-forge-state:v2:end"
        )

        parsed = issue_state.parse_body(body, "owner/repository", 60_000)

        self.assertEqual({}, parsed["fingerprintCatalog"])

    def test_catalog_restores_historical_signature_for_aggregation(self) -> None:
        signature = {"tokens": ["git", "show", "<sha>"]}
        fingerprint = stable_hash({"kind": "command", "signature": signature})
        state = empty_state() | {
            "observations": [observation(fingerprint)],
            "fingerprintCatalog": {
                fingerprint: {
                    "kind": "command",
                    "signatureVersion": 1,
                    "signature": signature,
                    "lastSeenAt": "2026-08-13T00:00:00Z",
                }
            },
        }

        evidence, _history = aggregate.merge_evidence(
            state,
            {"primitives": []},
            "owner/repository",
        )
        patterns = aggregate.aggregate(
            evidence,
            as_of="2026-08-14T00:00:00Z",
            active_days=90,
            stale_days=180,
            merged_prs=set(),
            thresholds={
                "minDistinctSessions": 1,
                "minDistinctDays": 1,
                "minKnownOutcomes": 1,
                "minSuccessRate": 0,
                "minScoredCoverage": 0,
                "minMergedPrs": 0,
                "minMainlineEvidence": 0,
            },
        )

        self.assertEqual(signature, patterns[0]["signature"])

    def test_catalog_rejects_secret_shaped_signature_values(self) -> None:
        signature = {"tokens": ["deploy", "token=abcdefghijklmnop"]}
        fingerprint = stable_hash({"kind": "command", "signature": signature})
        state = empty_state() | {
            "fingerprintCatalog": {
                fingerprint: {
                    "kind": "command",
                    "signatureVersion": 1,
                    "signature": signature,
                    "lastSeenAt": "2026-08-13T00:00:00Z",
                }
            }
        }

        with self.assertRaisesRegex(ValueError, "secret-shaped"):
            issue_state.validate_state(state, "owner/repository")

    def test_catalog_rejects_signature_key_mismatch(self) -> None:
        signature = {"tokens": ["git", "status"]}
        state = empty_state() | {
            "fingerprintCatalog": {
                "c" * 16: {
                    "kind": "command",
                    "signatureVersion": 1,
                    "signature": signature,
                    "lastSeenAt": "2026-08-13T00:00:00Z",
                }
            }
        }

        with self.assertRaisesRegex(ValueError, "does not match"):
            issue_state.validate_state(state, "owner/repository")

    def test_catalog_builder_enforces_entry_and_byte_limits(self) -> None:
        evidence = []
        for index in range(100):
            signature = {"tokens": ["tool", f"operation-{index}"]}
            evidence.append(
                {
                    "fingerprint": stable_hash(
                        {"kind": "command", "signature": signature}
                    ),
                    "kind": "command",
                    "signature": signature,
                    "completedAt": f"2026-08-13T{index % 24:02d}:00:00Z",
                }
            )

        catalog = aggregate.build_fingerprint_catalog(evidence)

        self.assertLessEqual(
            len(catalog),
            aggregate.MAX_FINGERPRINT_CATALOG_ENTRIES,
        )
        self.assertLessEqual(
            len(json.dumps(catalog, separators=(",", ":")).encode()),
            aggregate.MAX_FINGERPRINT_CATALOG_BYTES,
        )


if __name__ == "__main__":
    unittest.main()
