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
    "## Purpose",
    "## Conditions (C)",
    "## Interface (R)",
    "## Policy (π)",
    "## Termination (T)",
    "## Assets and scripts",
    "## Scope boundaries",
)
OPTIONAL_SECTIONS = (
    "## Always do",
    "## Never do",
    "## Gotchas / edge cases",
)
SECTION_ORDER = (
    "## Purpose",
    "## Conditions (C)",
    "## Interface (R)",
    "## Policy (π)",
    "## Termination (T)",
    *OPTIONAL_SECTIONS,
    "## Assets and scripts",
    "## Scope boundaries",
)
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
    return " ".join(words) if words else None


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

    heading_matches = list(re.finditer(r"^## [^\n]+$", body, re.MULTILINE))
    section_matches = [
        (match.group(0), match.start(), match.end(), position)
        for position, match in enumerate(heading_matches)
        if match.group(0) in SECTION_ORDER
    ]
    section_counts = {
        section: sum(match[0] == section for match in section_matches)
        for section in SECTION_ORDER
    }
    for section in REQUIRED_SECTIONS:
        if section_counts[section] == 0:
            errors.append(f"missing section: {section}")
    for section, count in section_counts.items():
        if count > 1:
            errors.append(f"duplicate section: {section}")

    expected_positions = {section: position for position, section in enumerate(SECTION_ORDER)}
    for previous, current in zip(section_matches, section_matches[1:]):
        if expected_positions[current[0]] < expected_positions[previous[0]]:
            errors.append(f"section is out of order: {current[0]}")

    repeated_guidance: dict[str, str] = {}
    for section, _start, content_start, heading_position in section_matches:
        end = (
            heading_matches[heading_position + 1].start()
            if heading_position + 1 < len(heading_matches)
            else len(body)
        )
        content = body[content_start:end].strip()
        if not content:
            errors.append(f"section is empty: {section}")
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
