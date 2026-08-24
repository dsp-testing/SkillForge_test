#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. All rights reserved.

"""Validate the structural contract for a Forge-generated repository skill."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

REQUIRED_FRONTMATTER = ("name", "description", "generated-by")
REQUIRED_SECTIONS = (
    "## Conditions (C)",
    "## Interface (R)",
    "## Policy (π)",
    "## Termination (T)",
)
OPTIONAL_SECTIONS = (
    "## Purpose",
    "## Always do",
    "## Never do",
    "## Gotchas / edge cases",
    "## Assets and scripts",
    "## Scope boundaries",
)
SECTION_ORDER = (
    "## Purpose",
    *REQUIRED_SECTIONS,
    *OPTIONAL_SECTIONS[1:],
)
SECTION_MAX_WORDS = {
    "## Purpose": 80,
    "## Conditions (C)": 160,
    "## Interface (R)": 160,
    "## Policy (π)": 400,
    "## Termination (T)": 120,
    "## Always do": 120,
    "## Never do": 120,
    "## Gotchas / edge cases": 160,
    "## Assets and scripts": 120,
    "## Scope boundaries": 120,
}
MAX_BODY_WORDS = 1200
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ABSTRACTION_RE = re.compile(
    r"\*\*Abstraction level:\*\*\s*(primitive|compositional|strategic)\b",
    re.IGNORECASE,
)


def normalized_guidance(line: str) -> str | None:
    value = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", line.strip())
    if not value or value.startswith(("#", "```", "**Abstraction level:**")):
        return None
    words = re.findall(r"[a-z0-9]+", value.lower())
    return " ".join(words) if len(words) >= 6 else None


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        raise ValueError("SKILL.md must start with YAML frontmatter")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise ValueError("SKILL.md frontmatter is not closed")
    frontmatter: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"invalid frontmatter line: {line}")
        key, value = line.split(":", 1)
        frontmatter[key.strip()] = value.strip().strip("\"'")
    return frontmatter, text[end + 5 :].lstrip()


def validate(path: Path) -> list[str]:
    errors: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        return [f"unable to read skill: {error}"]

    try:
        frontmatter, body = parse_frontmatter(text)
    except ValueError as error:
        return [str(error)]

    for key in REQUIRED_FRONTMATTER:
        if not frontmatter.get(key):
            errors.append(f"missing frontmatter field: {key}")

    name = frontmatter.get("name", "")
    if name and (len(name) > 64 or not NAME_RE.fullmatch(name)):
        errors.append("name must be kebab-case and at most 64 characters")
    if name and path.parent.name != name:
        errors.append(f"name must match parent directory: expected {path.parent.name}")
    description = frontmatter.get("description", "")
    if description and not 1 <= len(description) <= 1024:
        errors.append("description must contain 1-1024 characters")
    if frontmatter.get("generated-by") != "forge-agent":
        errors.append("generated-by must be forge-agent")

    if not re.search(r"^# .+", body, re.MULTILINE):
        errors.append("missing skill title")

    section_indexes: list[tuple[str, int]] = []
    for section in SECTION_ORDER:
        index = body.find(section)
        if index == -1 and section in REQUIRED_SECTIONS:
            errors.append(f"missing section: {section}")
        elif index != -1:
            section_indexes.append((section, index))
    section_indexes.sort(key=lambda item: item[1])
    expected_positions = {section: position for position, section in enumerate(SECTION_ORDER)}
    for previous, current in zip(section_indexes, section_indexes[1:]):
        if expected_positions[current[0]] < expected_positions[previous[0]]:
            errors.append(f"section is out of order: {current[0]}")

    repeated_guidance: dict[str, str] = {}
    for position, (section, start) in enumerate(section_indexes):
        end = section_indexes[position + 1][1] if position + 1 < len(section_indexes) else len(body)
        content = body[start + len(section) : end].strip()
        minimum_words = 12 if section == "## Policy (π)" else 4
        word_count = len(content.split())
        if word_count < minimum_words:
            errors.append(f"section is too thin to execute: {section}")
        if word_count > SECTION_MAX_WORDS[section]:
            errors.append(
                f"section exceeds {SECTION_MAX_WORDS[section]} words: {section}"
            )
        for line in content.splitlines():
            normalized = normalized_guidance(line)
            if not normalized:
                continue
            first_section = repeated_guidance.get(normalized)
            if first_section and first_section != section:
                errors.append(
                    f"guidance is repeated in {first_section} and {section}: {line.strip()}"
                )
            else:
                repeated_guidance[normalized] = section

    if len(body.split()) > MAX_BODY_WORDS:
        errors.append(f"skill body exceeds {MAX_BODY_WORDS} words")
    if not ABSTRACTION_RE.search(body):
        errors.append("missing abstraction level: primitive, compositional, or strategic")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("skill", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    errors = validate(args.skill)
    result = {"valid": not errors, "path": str(args.skill.resolve()), "errors": errors}
    if args.json:
        print(json.dumps(result, indent=2))
    elif errors:
        for error in errors:
            print(f"error: {error}")
    else:
        print(f"valid: {args.skill}")
    raise SystemExit(0 if not errors else 1)


if __name__ == "__main__":
    main()
