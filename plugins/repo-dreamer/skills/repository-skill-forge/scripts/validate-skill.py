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
    "## Always do",
    "## Never do",
    "## Gotchas / edge cases",
    "## Assets and scripts",
    "## Scope boundaries",
)
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ABSTRACTION_RE = re.compile(
    r"\*\*Abstraction level:\*\*\s*(primitive|compositional|strategic)\b",
    re.IGNORECASE,
)


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
    if frontmatter.get("generated-by") != "forge-agent":
        errors.append("generated-by must be forge-agent")
    if not re.search(r"^# .+", body, re.MULTILINE):
        errors.append("missing skill title")

    previous = -1
    section_indexes: list[tuple[str, int]] = []
    for section in REQUIRED_SECTIONS:
        index = body.find(section)
        if index == -1:
            errors.append(f"missing section: {section}")
            continue
        if index < previous:
            errors.append(f"section is out of order: {section}")
        previous = index
        section_indexes.append((section, index))
    for position, (section, start) in enumerate(section_indexes):
        end = section_indexes[position + 1][1] if position + 1 < len(section_indexes) else len(body)
        content = body[start + len(section) : end].strip()
        minimum_words = 12 if section == "## Policy (π)" else 4
        if len(content.split()) < minimum_words:
            errors.append(f"section is too thin to execute: {section}")
    if len(body.split()) > 5000:
        errors.append("skill body exceeds the 5000-token approximation")
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
