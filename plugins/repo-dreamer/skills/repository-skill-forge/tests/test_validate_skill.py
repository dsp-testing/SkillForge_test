#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. All rights reserved.

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]


def load_validator(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VALIDATOR = load_validator(
    "validate_skill",
    SKILL_DIR / "scripts" / "validate-skill.py",
)
USER_VALIDATOR = load_validator(
    "user_validate_skill",
    SKILL_DIR.parents[2]
    / "user-dreamer"
    / "skills"
    / "user-skill-forge"
    / "scripts"
    / "validate-skill.py",
)


def skill_text(body: str) -> str:
    return f"""---
name: example-skill
description: Validate the example workflow when relevant.
generated-by: forge-agent
---

# Example skill

{body}

**Abstraction level:** primitive
"""


def minimal_body() -> str:
    return """## Purpose

Standardize validation for the example workflow.

## Conditions (C)

Use this for relevant example changes.

## Interface (R)

Accept changes and return validation results.

## Policy (π)

Run the repository-supported checks in order and report a concrete failure before attempting recovery.

## Termination (T)

Finish when all required checks pass.

## Assets and scripts

Use the repository validation script.

## Scope boundaries

Only validate the example workflow."""


class ValidateSkillConcisenessTests(unittest.TestCase):
    def validate_body(self, body: str) -> list[str]:
        with tempfile.TemporaryDirectory() as temporary:
            skill_dir = Path(temporary) / "example-skill"
            skill_dir.mkdir()
            path = skill_dir / "SKILL.md"
            path.write_text(skill_text(body), encoding="utf-8")
            return VALIDATOR.validate(path)

    def test_accepts_required_sections_without_optional_sections(self) -> None:
        self.assertEqual([], self.validate_body(minimal_body()))

    def test_accepts_optional_sections_when_they_add_guidance(self) -> None:
        body = minimal_body().replace(
            "## Assets and scripts",
            """## Gotchas / edge cases

Missing dependencies can make checks unavailable.

## Assets and scripts""",
        )

        self.assertEqual([], self.validate_body(body))

    def test_rejects_repeated_guidance_across_sections(self) -> None:
        repeated = "Run the repository checks once and report the concrete result."
        body = f"""## Purpose

{repeated}

## Conditions (C)

Use this for relevant example changes.

## Interface (R)

Accept changes and return validation results.

## Policy (π)

{repeated}
Stop immediately when a required check fails.

## Termination (T)

Finish when all required checks pass.

## Assets and scripts

Use the repository validation script.

## Scope boundaries

Only validate the example workflow."""

        errors = self.validate_body(body)

        self.assertTrue(
            any(error.startswith("guidance is repeated in ") for error in errors),
            errors,
        )

    def test_rejects_oversized_sections(self) -> None:
        body = minimal_body().replace(
            "Run the repository-supported checks in order and report a concrete failure before attempting recovery.",
            " ".join(["policy"] * 401),
        )

        self.assertIn(
            "section exceeds 400 words: ## Policy (π)",
            self.validate_body(body),
        )

    def test_rejects_oversized_skill_body(self) -> None:
        sections = (
            ("## Purpose", "purpose", 80),
            ("## Conditions (C)", "condition", 160),
            ("## Interface (R)", "interface", 160),
            ("## Policy (π)", "policy", 400),
            ("## Termination (T)", "termination", 120),
            ("## Always do", "always", 120),
            ("## Never do", "never", 120),
            ("## Gotchas / edge cases", "gotcha", 160),
            ("## Assets and scripts", "asset", 120),
            ("## Scope boundaries", "boundary", 120),
        )
        body = "\n\n".join(
            f"{heading}\n\n{' '.join([word] * count)}"
            for heading, word, count in sections
        )

        self.assertIn(
            "skill body exceeds 1200 words",
            self.validate_body(body),
        )

    def test_repository_and_user_forge_share_conciseness_contract(self) -> None:
        self.assertEqual(VALIDATOR.REQUIRED_SECTIONS, USER_VALIDATOR.REQUIRED_SECTIONS)
        self.assertEqual(VALIDATOR.OPTIONAL_SECTIONS, USER_VALIDATOR.OPTIONAL_SECTIONS)
        self.assertEqual(VALIDATOR.SECTION_MAX_WORDS, USER_VALIDATOR.SECTION_MAX_WORDS)
        self.assertEqual(VALIDATOR.MAX_BODY_WORDS, USER_VALIDATOR.MAX_BODY_WORDS)


if __name__ == "__main__":
    unittest.main()
