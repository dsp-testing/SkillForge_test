#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


def load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


aggregate = load_script("aggregate_primitives", "aggregate-primitives.py")
ledger = load_script("proposal_ledger", "proposal-ledger.py")


def proposal(
    *,
    key: str = "test-helper",
    version: str = "version-1",
    decision: str = "create_skill",
    rank: int = 0,
) -> dict[str, object]:
    return {
        "proposalKey": key,
        "proposalVersion": version,
        "candidateIds": ["candidate-1"],
        "decision": decision,
        "rank": rank,
    }


def catalog_entry(
    *,
    key: str = "test-helper",
    version: str = "version-1",
    status: str = "closed",
    number: int = 1,
) -> dict[str, object]:
    return {
        **proposal(key=key, version=version),
        "number": number,
        "url": f"https://example.test/pull/{number}",
        "status": status,
        "draft": False,
        "updatedAt": "2026-08-15T00:00:00Z",
    }


class StatelessAggregationTests(unittest.TestCase):
    def evidence(self, outcome: str) -> list[dict[str, object]]:
        return [
            {
                "evidenceKey": f"evidence-{index}",
                "fingerprint": "fingerprint-1",
                "sessionHash": f"session-{index}",
                "completedAt": f"2026-08-{13 + index % 2:02d}T00:00:00Z",
                "day": f"2026-08-{13 + index % 2:02d}",
                "outcome": outcome,
                "surface": "cli",
                "kind": "command",
                "branchHash": "main",
                "branchCategory": "default",
                "pathFamilies": ["scripts"],
                "refs": [],
                "signature": {"tokens": ["test"]},
            }
            for index in range(3)
        ]

    def thresholds(self) -> dict[str, float | int | bool]:
        return {
            "minDistinctSessions": 3,
            "minDistinctDays": 2,
            "minKnownOutcomes": 3,
            "minSuccessRate": 0.7,
            "minScoredCoverage": 0.5,
            "allowUnknownOutcomes": True,
            "minMergedPrs": 2,
            "minMainlineEvidence": 2,
        }

    def test_all_unknown_outcomes_can_use_corroboration_gates(self) -> None:
        patterns = aggregate.aggregate(
            self.evidence("unknown"),
            as_of="2026-08-15T00:00:00Z",
            active_days=90,
            stale_days=180,
            merged_prs=set(),
            thresholds=self.thresholds(),
        )

        self.assertTrue(patterns[0]["promotion"]["eligible"])

    def test_known_failures_still_apply_outcome_gates(self) -> None:
        patterns = aggregate.aggregate(
            self.evidence("failure"),
            as_of="2026-08-15T00:00:00Z",
            active_days=90,
            stale_days=180,
            merged_prs=set(),
            thresholds=self.thresholds(),
        )

        self.assertFalse(patterns[0]["promotion"]["eligible"])

    def test_merge_evidence_uses_only_current_document(self) -> None:
        document = {
            "primitives": [
                {
                    "evidenceKey": "current",
                    "fingerprint": "fingerprint-1",
                    "sessionId": "session-1",
                    "completedAt": "2026-08-14T00:00:00Z",
                    "outcome": "unknown",
                    "surface": "cli",
                    "kind": "command",
                    "branchCategory": "unknown",
                    "signature": {"tokens": ["test"]},
                }
            ]
        }

        evidence = aggregate.merge_evidence(document, "owner/repository")

        self.assertEqual(["current"], [item["evidenceKey"] for item in evidence])


class ProposalMarkerTests(unittest.TestCase):
    def test_marker_round_trips(self) -> None:
        original = proposal()

        parsed = ledger.parse_marker(ledger.render_marker(original))

        self.assertEqual(
            {
                "proposalKey": "test-helper",
                "proposalVersion": "version-1",
                "candidateIds": ["candidate-1"],
                "decision": "create_skill",
            },
            parsed,
        )

    def test_catalog_includes_open_closed_and_merged_prs(self) -> None:
        body = ledger.render_marker(proposal())
        catalog = ledger.build_catalog(
            [
                {"number": 1, "state": "OPEN", "body": body},
                {"number": 2, "state": "CLOSED", "body": body},
                {
                    "number": 3,
                    "state": "CLOSED",
                    "mergedAt": "2026-08-15T00:00:00Z",
                    "body": body,
                },
            ]
        )

        self.assertEqual(["open", "closed", "merged"], [item["status"] for item in catalog])

    def test_catalog_accepts_merged_state_without_timestamp(self) -> None:
        body = ledger.render_marker(proposal())

        catalog = ledger.build_catalog(
            [{"number": 1, "state": "MERGED", "body": body}]
        )

        self.assertEqual("merged", catalog[0]["status"])

    def test_duplicate_markers_are_rejected(self) -> None:
        marker = ledger.render_marker(proposal())

        with self.assertRaisesRegex(ValueError, "duplicate"):
            ledger.parse_marker(f"{marker}\n{marker}")

    def test_same_version_is_skipped_in_any_pr_state(self) -> None:
        for status in ("open", "closed", "merged"):
            with self.subTest(status=status):
                result = ledger.reconcile(proposal(), [catalog_entry(status=status)])
                self.assertFalse(result["allowed"])
                self.assertEqual("skip", result["action"])

    def test_new_version_updates_open_pr(self) -> None:
        result = ledger.reconcile(
            proposal(version="version-2"),
            [catalog_entry(version="version-1", status="open")],
        )

        self.assertTrue(result["allowed"])
        self.assertEqual("update", result["action"])

    def test_new_version_can_replace_closed_unmerged_pr(self) -> None:
        result = ledger.reconcile(
            proposal(version="version-2"),
            [catalog_entry(version="version-1", status="closed")],
        )

        self.assertTrue(result["allowed"])
        self.assertEqual("create", result["action"])

    def test_merged_skill_requires_improvement_decision(self) -> None:
        blocked = ledger.reconcile(
            proposal(version="version-2"),
            [catalog_entry(version="version-1", status="merged")],
        )
        allowed = ledger.reconcile(
            proposal(version="version-2", decision="improve_existing_skill"),
            [catalog_entry(version="version-1", status="merged")],
        )

        self.assertFalse(blocked["allowed"])
        self.assertTrue(allowed["allowed"])

    def test_merged_skill_gate_precedes_open_update(self) -> None:
        result = ledger.reconcile(
            proposal(version="version-3"),
            [
                catalog_entry(version="version-1", status="merged", number=1),
                catalog_entry(version="version-2", status="open", number=2),
            ],
        )

        self.assertFalse(result["allowed"])
        self.assertEqual("merged_proposal_requires_improvement_decision", result["reason"])

    def test_selection_allows_at_most_one_mutation(self) -> None:
        result = ledger.select(
            [
                proposal(key="second", rank=2),
                proposal(key="first", rank=1),
            ],
            [],
        )

        self.assertEqual(1, result["mutationCount"])
        self.assertEqual("first", result["selection"]["proposal"]["proposalKey"])

    def test_selection_rejects_invalid_rank(self) -> None:
        invalid = proposal()
        invalid["rank"] = "first"

        with self.assertRaisesRegex(ValueError, "rank"):
            ledger.select([invalid], [])


if __name__ == "__main__":
    unittest.main()
