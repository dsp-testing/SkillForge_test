#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

SANITIZER_SPEC = importlib.util.spec_from_file_location(
    "sanitize_evidence",
    SCRIPTS_DIR / "sanitize-evidence.py",
)
assert SANITIZER_SPEC is not None and SANITIZER_SPEC.loader is not None
sanitizer = importlib.util.module_from_spec(SANITIZER_SPEC)
SANITIZER_SPEC.loader.exec_module(sanitizer)


class SanitizeEvidenceTests(unittest.TestCase):
    def test_computed_token_assignment_is_not_a_secret(self) -> None:
        value = "token=json.loads(raw)[0]"

        self.assertEqual([], sanitizer.findings(value, "evidence-1", "command"))
        self.assertEqual(value, sanitizer.redact(value))

    def test_literal_secret_assignments_remain_blocking(self) -> None:
        values = [
            'token="abcdefghijklmnop"',
            "api_key=abcDEF1234567890",
            "aws_secret_access_key=abcDEF1234567890",
        ]

        for value in values:
            with self.subTest(value=value):
                findings = sanitizer.findings(value, "evidence-1", "command")
                self.assertEqual(["assigned_secret"], [item["kind"] for item in findings])
                self.assertEqual("<redacted-secret>", sanitizer.redact(value))

    def test_quoted_placeholders_are_not_secrets(self) -> None:
        values = [
            'token="$GITHUB_TOKEN"',
            'token="${GITHUB_TOKEN}"',
            'token="<placeholder>"',
        ]

        for value in values:
            with self.subTest(value=value):
                self.assertEqual([], sanitizer.findings(value, "evidence-1", "command"))
                self.assertEqual(value, sanitizer.redact(value))


if __name__ == "__main__":
    unittest.main()
