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


def load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ledger = load_script("proposal_ledger_publication", "proposal-ledger.py")
validator = load_script("validate_publication", "validate-publication.py")


def proposal(*, partial: bool = False) -> dict[str, object]:
    extraction: dict[str, object] = {"status": "complete"}
    if partial:
        extraction = {
            "status": "partial",
            "discoveryComplete": False,
            "discoveredSessionCount": 12,
            "completedSessionCount": 10,
            "sessionCoverage": None,
            "sessionCoverageStatus": "unknown",
            "omittedUnitCount": 2,
            "omittedUnitKinds": ["discovery", "tools"],
            "toolEventFallbackEnabled": False,
        }
    return {
        "proposalKey": "stacked-pr-workflow",
        "proposalVersion": "version-1",
        "candidateIds": ["candidate-1", "candidate-2"],
        "decision": "create_skill",
        "extraction": extraction,
    }


def selection_document(*, partial: bool = False) -> dict[str, object]:
    selected = proposal(partial=partial)
    return {
        "selection": {
            "proposal": selected,
            "marker": ledger.render_marker(selected),
        }
    }


def valid_body(*, partial: bool = False) -> str:
    selection = selection_document(partial=partial)
    selected = selection["selection"]["proposal"]
    marker = selection["selection"]["marker"]
    extraction = selected["extraction"]
    coverage = ""
    if partial:
        coverage = """
- Discovery complete: no
- Sessions: 10 of 12 completed
- Coverage: unknown
- Omissions: 2 (`discovery`, `tools`)
- Tool-event fallback: disabled
"""
    return f"""## What this adds

A short instruction file that teaches Copilot to map and verify stacked pull requests safely.

## Why it matters

This repository uses dependent pull requests whose base relationships must stay explicit during review.

## What changes in practice

**Example request**

> Review these related pull requests and recommend a safe merge order.

**Without these instructions**

> Copilot may inspect each pull request independently and miss the dependency between their bases.

**With these instructions**

> Copilot first maps every head and base, checks ancestry and scope, and then recommends an order.

## How to verify

Open the proposed `SKILL.md` and compare its stack-mapping steps with the repository's pull request guidance.

<details>
<summary>Forge details</summary>

- Proposal key: `stacked-pr-workflow`
- Proposal version: `version-1`
- Decision: `create_skill`
- Candidate IDs: `candidate-1`, `candidate-2`
- Confidence: high
- Evidence window: `2026-08-15T00:00:00Z` to `2026-08-19T00:00:00Z`
- Repository sources: `.github/copilot-instructions.md`
- Target SHA: `0123456789012345678901234567890123456789`
- Extraction: {extraction["status"]}
{coverage}- Validation: structural checks passed
- Review findings: none
- Trusted-user diversity: unknown

</details>

{marker}"""


class PublicationBodyValidationTests(unittest.TestCase):
    def test_accepts_plain_language_body(self) -> None:
        errors = validator.validate_body(
            selection_document(),
            valid_body(),
            target_sha="0123456789012345678901234567890123456789",
        )

        self.assertEqual([], errors)

    def test_rejects_template_and_temporary_validation_path(self) -> None:
        body = valid_body().replace(
            "Open the proposed `SKILL.md`",
            "Run `/tmp/copilot-plugins/build/scripts/validate-skill.py` and then open the proposed `SKILL.md`",
        ).replace(
            "<details>",
            "## Deployment\n\n- [ ] Deploy to canary\n\n<details>",
        )

        errors = validator.validate_body(
            selection_document(),
            body,
            target_sha="0123456789012345678901234567890123456789",
        )

        self.assertTrue(any("temporary path" in error for error in errors))
        self.assertTrue(any("host template" in error for error in errors))

    def test_rejects_additional_user_facing_section(self) -> None:
        body = valid_body().replace(
            "<details>",
            "## Evidence this helps\n\nAI credits improved in an evaluation.\n\n<details>",
        )

        errors = validator.validate_body(
            selection_document(),
            body,
            target_sha="0123456789012345678901234567890123456789",
        )

        self.assertIn(
            "PR body must contain only the four required user-facing sections",
            errors,
        )

    def test_rejects_jargon_heavy_opening_and_missing_example(self) -> None:
        body = valid_body().replace(
            "A short instruction file that teaches Copilot to map and verify stacked pull requests safely.",
            "Adds a forge-generated skill at `.github/skills/stacked-pr-workflow/SKILL.md`.",
        ).replace("**Without these instructions**", "**Before**")

        errors = validator.validate_body(
            selection_document(),
            body,
            target_sha="0123456789012345678901234567890123456789",
        )

        self.assertTrue(any("jargon" in error for error in errors))
        self.assertTrue(any("file path" in error for error in errors))
        self.assertTrue(any("Without these instructions" in error for error in errors))

    def test_rejects_result_shaped_fabrication(self) -> None:
        body = valid_body().replace(
            "Copilot first maps every head and base, checks ancestry and scope, and then recommends an order.",
            "Copilot runs `git diff` → ✅ passed and regenerated 4 files.",
        )

        errors = validator.validate_body(
            selection_document(),
            body,
            target_sha="0123456789012345678901234567890123456789",
        )

        self.assertTrue(any("fabricated output" in error for error in errors))

    def test_requires_partial_coverage_details(self) -> None:
        body = valid_body(partial=True).replace("- Coverage: unknown\n", "")

        errors = validator.validate_body(
            selection_document(partial=True),
            body,
            target_sha="0123456789012345678901234567890123456789",
        )

        self.assertIn("partial Forge details are missing Coverage:", errors)

    def test_requires_exact_marker_at_end(self) -> None:
        body = valid_body() + "\n\nExtra text"

        errors = validator.validate_body(
            selection_document(),
            body,
            target_sha="0123456789012345678901234567890123456789",
        )

        self.assertIn("selected Forge marker must be the final PR body content", errors)


class PublicationCheckoutValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.source = self.root / "proposal"
        self.repository.mkdir()
        self.source.mkdir()
        subprocess.run(["git", "-C", str(self.repository), "init", "-q"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repository), "config", "user.email", "forge@example.test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repository), "config", "user.name", "Forge Test"],
            check=True,
        )
        (self.repository / "README.md").write_text("baseline\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repository), "add", "README.md"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repository), "commit", "-qm", "baseline"],
            check=True,
        )
        (self.source / "SKILL.md").write_text(
            "---\nname: example\ndescription: Verify an example workflow.\n---\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def copy_source(self) -> None:
        destination = self.repository / "skills" / "example"
        destination.mkdir(parents=True)
        for path in self.source.rglob("*"):
            if path.is_file():
                relative = path.relative_to(self.source)
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())

    def test_accepts_exact_selected_source_tree(self) -> None:
        self.copy_source()

        errors, expected, pending = validator.validate_checkout(
            self.repository,
            self.source,
            "skills/example",
        )

        self.assertEqual([], errors)
        self.assertEqual({"skills/example/SKILL.md"}, expected)
        self.assertEqual(expected, pending)

    def test_clean_checkout_rejects_untracked_content(self) -> None:
        self.assertEqual([], validator.validate_clean_checkout(self.repository))

        (self.repository / "$OUT").write_text("[]\n", encoding="utf-8")

        errors = validator.validate_clean_checkout(self.repository)

        self.assertTrue(any("$OUT" in error for error in errors))

    def test_rejects_unrelated_checkout_artifact(self) -> None:
        self.copy_source()
        (self.repository / "$OUT").write_text("[]\n", encoding="utf-8")

        errors, _expected, _pending = validator.validate_checkout(
            self.repository,
            self.source,
            "skills/example",
        )

        self.assertTrue(any("$OUT" in error for error in errors))

    def test_rejects_raw_session_metadata_in_selected_tree(self) -> None:
        assets = self.source / "assets"
        assets.mkdir()
        (assets / "sessions.json").write_text(
            json.dumps(
                [
                    {
                        "session_id": "session-1",
                        "agent_name": "Copilot CLI",
                        "repository": "owner/repository",
                        "branch": "main",
                        "created_at": "2026-08-19T00:00:00Z",
                        "updated_at": "2026-08-19T00:01:00Z",
                    }
                ]
            ),
            encoding="utf-8",
        )
        self.copy_source()

        errors, _expected, _pending = validator.validate_checkout(
            self.repository,
            self.source,
            "skills/example",
        )

        self.assertTrue(any("raw session metadata" in error for error in errors))

    def test_rejects_secret_shaped_selected_content(self) -> None:
        (self.source / "SKILL.md").write_text(
            "token=abcDEF1234567890\n",
            encoding="utf-8",
        )
        self.copy_source()

        errors, _expected, _pending = validator.validate_checkout(
            self.repository,
            self.source,
            "skills/example",
        )

        self.assertTrue(any("sensitive content" in error for error in errors))

    def test_rejects_binary_selected_content(self) -> None:
        assets = self.source / "assets"
        assets.mkdir()
        (assets / "artifact.bin").write_bytes(b"\xff\xfe\x00\x01")
        self.copy_source()

        errors, _expected, _pending = validator.validate_checkout(
            self.repository,
            self.source,
            "skills/example",
        )

        self.assertTrue(any("not UTF-8 text" in error for error in errors))

    def test_rejects_destination_content_mismatch(self) -> None:
        self.copy_source()
        (self.repository / "skills" / "example" / "SKILL.md").write_text(
            "different\n",
            encoding="utf-8",
        )

        errors, _expected, _pending = validator.validate_checkout(
            self.repository,
            self.source,
            "skills/example",
        )

        self.assertTrue(any("does not match selected source" in error for error in errors))

    def test_rejects_destination_symlink(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "example").mkdir()
        (outside / "example" / "SKILL.md").write_bytes(
            (self.source / "SKILL.md").read_bytes()
        )
        (self.repository / "skills").symlink_to(outside, target_is_directory=True)

        errors, _expected, _pending = validator.validate_checkout(
            self.repository,
            self.source,
            "skills/example",
        )

        self.assertTrue(any("contains a symlink" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
