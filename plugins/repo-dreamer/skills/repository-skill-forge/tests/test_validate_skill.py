#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. All rights reserved.

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "validate_skill",
    SKILL_DIR / "scripts" / "validate-skill.py",
)
assert SPEC is not None and SPEC.loader is not None
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)


def skill_text(description: str) -> str:
    return f"""---
name: example-skill
description: {description}
generated-by: forge-agent
---

# Example skill

## Purpose

Validate the example workflow.

## Conditions (C)

Use this for example changes.

## Interface (R)

Accept changes and return results.

## Policy (π)

Run the repository-supported example checks and report any concrete failures before completion.

## Termination (T)

Finish when all checks pass.

## Always do

- Report the validation result.

## Never do

- Never invent successful output.

## Gotchas / edge cases

Missing dependencies can block checks.

## Assets and scripts

Use the repository validation script.

## Scope boundaries

Only validate the example workflow.

**Abstraction level:** primitive
"""


class ValidateSkillDescriptionTests(unittest.TestCase):
    def validate_description(self, description: str) -> list[str]:
        with tempfile.TemporaryDirectory() as temporary:
            skill_dir = Path(temporary) / "example-skill"
            skill_dir.mkdir()
            path = skill_dir / "SKILL.md"
            path.write_text(skill_text(description), encoding="utf-8")
            return VALIDATOR.validate(path)

    def test_accepts_activation_trigger_and_outcome(self) -> None:
        errors = self.validate_description(
            "Use when changes touch `go/` to format, test, and lint the Go module."
        )

        self.assertEqual([], errors)

    def test_rejects_description_without_activation_trigger(self) -> None:
        errors = self.validate_description(
            "Format, test, and lint changes to the Go module."
        )

        self.assertIn(
            "description must use 'Use when <activation trigger> to <outcome>.'",
            errors,
        )

    def test_rejects_description_without_outcome(self) -> None:
        errors = self.validate_description("Use when changes touch `go/`.")

        self.assertIn(
            "description must use 'Use when <activation trigger> to <outcome>.'",
            errors,
        )


if __name__ == "__main__":
    unittest.main()
