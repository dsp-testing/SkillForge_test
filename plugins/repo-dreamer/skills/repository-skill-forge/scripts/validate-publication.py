#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. All rights reserved.

"""Validate a Forge PR body and the exact checkout files selected for publication."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from forge_common import read_json

REQUIRED_HEADINGS = (
    "## What this adds",
    "## Why it matters",
    "## What changes in practice",
    "## How to verify",
)
EXAMPLE_LABELS = (
    "**Example request**",
    "**Without these instructions**",
    "**With these instructions**",
)
DETAIL_LABELS = (
    "Proposal key",
    "Proposal version",
    "Decision",
    "Candidate IDs",
    "Confidence",
    "Evidence window",
    "Repository sources",
    "Target SHA",
    "Extraction",
    "Validation",
    "Review findings",
    "Trusted-user diversity",
)
PARTIAL_DETAIL_LABELS = (
    "Discovery complete",
    "Sessions",
    "Coverage",
    "Omissions",
    "Tool-event fallback",
)
DETAIL_BULLET_RE = re.compile(r"^-\s+(?P<label>[^:\n]+):\s*(?P<value>.*)$")
OPENING_JARGON_RE = re.compile(
    r"\b(?:forge(?:-generated)?|skills?|guardrails?|proposals?|candidates?)\b",
    re.IGNORECASE,
)
OPENING_PATH_RE = re.compile(
    r"(?:\.github/|SKILL\.md|`[^`\n]*/[^`\n]*`)",
    re.IGNORECASE,
)
TEMPLATE_RE = re.compile(
    r"(?mi)^##\s+(?:Overview|Checklist|Deployment)\s*$"
    r"|^###\s+(?:What|Why|How to verify)\s*$"
    r"|^-\s+\[\s\]\s+"
    r"|production_rollout"
    r"|staff-wus2-01"
    r"|Need a reviewer, or stuck on something\?",
)
MACHINE_PATH_RE = re.compile(
    r"(?:/tmp(?:/|\b)|/Users/[^/\s]+|/home/[^/\s]+|"
    r"[A-Za-z]:\\Users\\[^\\\s]+|copilot-plugins/)",
    re.IGNORECASE,
)
FABRICATED_OUTPUT_PATTERNS = (
    re.compile(r"(?:→|=>)\s*(?:✅|❌|✓|✗|pass(?:ed)?|fail(?:ed)?)", re.IGNORECASE),
    re.compile(r"\bregenerated\s+\d+\s+files?\b", re.IGNORECASE),
    re.compile(r"\b(?:completed in|took)\s+\d", re.IGNORECASE),
    re.compile(r"\bI(?:'ve| have)\s+committed\b", re.IGNORECASE),
)
RAW_SESSION_KEYS = {
    "sessionid",
    "agentname",
    "repository",
    "branch",
    "createdat",
    "updatedat",
}
RAW_SESSION_KEY_RE = re.compile(
    r"""(?ix)
    ["']?
    (?P<key>
        session[_-]?id
        |agent[_-]?name
        |repository
        |branch
        |created[_-]?at
        |updated[_-]?at
    )
    ["']?
    \s*:
    """
)


def load_sanitizer() -> Any:
    path = Path(__file__).with_name("sanitize-evidence.py")
    spec = importlib.util.spec_from_file_location("forge_sanitize_evidence", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load sanitize-evidence.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SANITIZER = load_sanitizer()


def selection_parts(document: dict[str, Any]) -> tuple[dict[str, Any], str]:
    selection = document.get("selection")
    if not isinstance(selection, dict):
        raise ValueError("selection document does not contain a selected proposal")
    proposal = selection.get("proposal")
    marker = selection.get("marker")
    if not isinstance(proposal, dict):
        raise ValueError("selection proposal must be an object")
    if not isinstance(marker, str) or not marker:
        raise ValueError("selection marker must be a non-empty string")
    return proposal, marker


def section(body: str, heading: str, next_heading: str | None) -> str:
    start = body.find(heading)
    if start < 0:
        return ""
    start += len(heading)
    end = body.find(next_heading, start) if next_heading else len(body)
    if end < 0:
        end = len(body)
    return body[start:end].strip()


def first_paragraph(value: str) -> str:
    for paragraph in re.split(r"\n\s*\n", value):
        stripped = paragraph.strip()
        if stripped:
            return stripped
    return ""


def sensitive_findings(value: str) -> list[str]:
    findings: list[str] = []
    if SANITIZER.HOME_PATH_RE.search(value) or SANITIZER.WINDOWS_HOME_RE.search(value):
        findings.append("home_path")
    for kind, pattern in SANITIZER.TOKEN_PATTERNS:
        if pattern.search(value):
            findings.append(kind)
    return findings


def canonical_key(value: Any) -> str:
    return re.sub(r"[_-]", "", str(value)).lower()


def contains_raw_session_rows(value: Any) -> bool:
    if isinstance(value, dict):
        keys = {canonical_key(key) for key in value}
        if "sessionid" in keys and len(keys & RAW_SESSION_KEYS) >= 4:
            return True
        return any(contains_raw_session_rows(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_raw_session_rows(item) for item in value)
    return False


def contains_raw_session_content(content: str) -> bool:
    decoder = json.JSONDecoder()
    for match in re.finditer(r"[\[{]", content):
        try:
            value, _end = decoder.raw_decode(content[match.start() :])
        except json.JSONDecodeError:
            continue
        if contains_raw_session_rows(value):
            return True
    keys = {
        canonical_key(match.group("key"))
        for match in RAW_SESSION_KEY_RE.finditer(content)
    }
    return "sessionid" in keys and len(keys & RAW_SESSION_KEYS) >= 4


def parse_detail_bullets(details: str) -> tuple[dict[str, str], list[str]]:
    values: dict[str, str] = {}
    errors: list[str] = []
    for line in details.splitlines():
        match = DETAIL_BULLET_RE.fullmatch(line.strip())
        if match is None:
            continue
        label = match.group("label").strip()
        value = match.group("value").strip()
        if label in values:
            errors.append(f"Forge details contain duplicate {label!r} bullets")
            continue
        values[label] = value
    return values, errors


def expect_detail(
    values: dict[str, str],
    label: str,
    expected: str,
    errors: list[str],
) -> None:
    actual = values.get(label)
    if actual is None:
        errors.append(f"Forge details are missing {label}:")
    elif actual != expected:
        errors.append(f"Forge details {label}: must equal {expected!r}")


def coverage_text(extraction: dict[str, Any]) -> str | None:
    status = extraction.get("sessionCoverageStatus")
    coverage = extraction.get("sessionCoverage")
    if status == "unknown" and coverage is None:
        return "unknown"
    if (
        status == "known"
        and isinstance(coverage, (int, float))
        and not isinstance(coverage, bool)
        and 0 <= coverage <= 1
    ):
        return f"{coverage * 100:.1f}%"
    return None


def validate_body(
    selection_document: dict[str, Any],
    body: str,
    *,
    target_sha: str,
) -> list[str]:
    errors: list[str] = []
    try:
        proposal, marker = selection_parts(selection_document)
    except ValueError as error:
        return [str(error)]

    positions: list[int] = []
    for heading in REQUIRED_HEADINGS:
        count = body.count(heading)
        if count != 1:
            errors.append(f"PR body must contain exactly one {heading!r} heading")
        positions.append(body.find(heading))
    if all(position >= 0 for position in positions) and positions != sorted(positions):
        errors.append("PR body user-facing headings are out of order")

    sections = [
        section(
            body,
            heading,
            REQUIRED_HEADINGS[index + 1]
            if index + 1 < len(REQUIRED_HEADINGS)
            else "<details>",
        )
        for index, heading in enumerate(REQUIRED_HEADINGS)
    ]
    for heading, content in zip(REQUIRED_HEADINGS, sections, strict=True):
        if not content:
            errors.append(f"{heading} must not be empty")

    opening = first_paragraph(sections[0])
    if OPENING_JARGON_RE.search(opening):
        errors.append("What this adds opens with Forge or skill jargon")
    if OPENING_PATH_RE.search(opening):
        errors.append("What this adds opens with a file path")
    if len(re.findall(r"\b[\w'-]+\b", opening)) > 80:
        errors.append("What this adds opening exceeds 80 words")

    example = sections[2]
    example_positions: list[int] = []
    for label in EXAMPLE_LABELS:
        count = example.count(label)
        if count != 1:
            errors.append(f"What changes in practice must contain exactly one {label}")
        example_positions.append(example.find(label))
    if all(position >= 0 for position in example_positions):
        if example_positions != sorted(example_positions):
            errors.append("What changes in practice labels are out of order")
        for index, label in enumerate(EXAMPLE_LABELS):
            start = example_positions[index] + len(label)
            end = (
                example_positions[index + 1]
                if index + 1 < len(EXAMPLE_LABELS)
                else len(example)
            )
            if not example[start:end].strip():
                errors.append(f"{label} must be followed by example content")
    for pattern in FABRICATED_OUTPUT_PATTERNS:
        if pattern.search(example):
            errors.append("What changes in practice contains result-shaped fabricated output")
            break

    if MACHINE_PATH_RE.search(body):
        errors.append("PR body contains a machine-specific or temporary path")
    if TEMPLATE_RE.search(body):
        errors.append("PR body contains an inapplicable host template section")
    if sensitive_findings(body):
        errors.append("PR body contains secret-shaped or home-path content")

    details_start = body.find("<details>")
    details_end = body.find("</details>")
    if details_start < 0 or details_end < details_start:
        errors.append("PR body must contain a Forge details block")
        details = ""
    else:
        details = body[details_start : details_end + len("</details>")]
        if "<summary>Forge details</summary>" not in details:
            errors.append("PR body Forge details summary is missing")
        if positions[-1] >= 0 and details_start < positions[-1]:
            errors.append("Forge details must follow all user-facing sections")

    visible = body[:details_start] if details_start >= 0 else body
    visible_headings = tuple(re.findall(r"(?m)^##\s+.+$", visible))
    if visible_headings != REQUIRED_HEADINGS:
        errors.append("PR body must contain only the four required user-facing sections")
    if len(re.findall(r"\b[\w'-]+\b", visible)) > 500:
        errors.append("PR body user-facing content exceeds 500 words")

    detail_values, detail_errors = parse_detail_bullets(details)
    errors.extend(detail_errors)
    for label in DETAIL_LABELS:
        if label not in detail_values:
            errors.append(f"Forge details are missing {label}:")
        elif not detail_values[label]:
            errors.append(f"Forge details {label}: must not be empty")

    identity_fields = (
        ("Proposal key", "proposalKey"),
        ("Proposal version", "proposalVersion"),
        ("Decision", "decision"),
    )
    for label, field in identity_fields:
        value = proposal.get(field)
        if not isinstance(value, str) or not value:
            errors.append(f"selected proposal {field} is invalid")
        else:
            expect_detail(detail_values, label, f"`{value}`", errors)

    candidate_ids = proposal.get("candidateIds")
    if (
        not isinstance(candidate_ids, list)
        or not candidate_ids
        or any(not isinstance(candidate_id, str) or not candidate_id for candidate_id in candidate_ids)
    ):
        errors.append("selected proposal candidateIds are invalid")
    else:
        expected_ids = ", ".join(f"`{candidate_id}`" for candidate_id in sorted(candidate_ids))
        expect_detail(detail_values, "Candidate IDs", expected_ids, errors)
    expect_detail(detail_values, "Target SHA", f"`{target_sha}`", errors)
    expect_detail(
        detail_values,
        "Trusted-user diversity",
        "unknown",
        errors,
    )

    extraction = proposal.get("extraction")
    if not isinstance(extraction, dict):
        errors.append("selected proposal extraction coverage is missing")
    else:
        status_value = extraction.get("status")
        if status_value not in {"complete", "partial"}:
            errors.append("selected proposal extraction status is invalid")
        else:
            expect_detail(detail_values, "Extraction", status_value, errors)
        if status_value == "partial":
            discovery_complete = extraction.get("discoveryComplete")
            if not isinstance(discovery_complete, bool):
                errors.append("selected proposal discoveryComplete is invalid")
            else:
                expect_detail(
                    detail_values,
                    "Discovery complete",
                    "yes" if discovery_complete else "no",
                    errors,
                )
            discovered = extraction.get("discoveredSessionCount")
            completed = extraction.get("completedSessionCount")
            if (
                not isinstance(discovered, int)
                or isinstance(discovered, bool)
                or not isinstance(completed, int)
                or isinstance(completed, bool)
            ):
                errors.append("selected proposal partial session counts are invalid")
            else:
                expect_detail(
                    detail_values,
                    "Sessions",
                    f"{completed} of {discovered} completed",
                    errors,
                )
            formatted_coverage = coverage_text(extraction)
            if formatted_coverage is None:
                errors.append("selected proposal partial coverage is invalid")
            else:
                expect_detail(
                    detail_values,
                    "Coverage",
                    formatted_coverage,
                    errors,
                )
            omission_count = extraction.get("omittedUnitCount")
            omission_kinds = extraction.get("omittedUnitKinds")
            if (
                not isinstance(omission_count, int)
                or isinstance(omission_count, bool)
                or not isinstance(omission_kinds, list)
                or any(not isinstance(kind, str) or not kind for kind in omission_kinds)
            ):
                errors.append("selected proposal partial omissions are invalid")
            else:
                kinds = ", ".join(f"`{kind}`" for kind in sorted(omission_kinds))
                expect_detail(
                    detail_values,
                    "Omissions",
                    f"{omission_count} ({kinds})",
                    errors,
                )
            fallback_enabled = extraction.get("toolEventFallbackEnabled")
            if not isinstance(fallback_enabled, bool):
                errors.append("selected proposal toolEventFallbackEnabled is invalid")
            else:
                expect_detail(
                    detail_values,
                    "Tool-event fallback",
                    "enabled" if fallback_enabled else "disabled",
                    errors,
                )
        elif status_value == "complete":
            unexpected = sorted(set(PARTIAL_DETAIL_LABELS) & set(detail_values))
            if unexpected:
                errors.append(
                    "complete Forge details contain partial-only fields: "
                    + ", ".join(unexpected)
                )

    marker_signature = "repository-skill-forge-proposal:v1"
    if body.count(marker) != 1 or body.count(marker_signature) != 1:
        errors.append("PR body must contain exactly the selected Forge marker")
    if details_end >= 0:
        suffix = body[details_end + len("</details>") :].strip()
        if suffix != marker:
            errors.append(
                "selected Forge marker must immediately follow the Forge details block"
            )
    return errors


def git_output(repository: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise ValueError(message or f"git {' '.join(arguments)} failed")
    return result.stdout


def split_paths(value: bytes) -> set[str]:
    return {
        item.decode("utf-8", "surrogateescape")
        for item in value.split(b"\0")
        if item
    }


def changed_paths(repository: Path) -> set[str]:
    tracked = split_paths(
        git_output(repository, "diff", "--name-only", "--no-renames", "-z", "HEAD", "--")
    )
    untracked = split_paths(
        git_output(repository, "ls-files", "--others", "--exclude-standard", "-z", "--")
    )
    return tracked | untracked


def validate_clean_checkout(repository: Path) -> list[str]:
    try:
        pending = changed_paths(repository)
    except ValueError as error:
        return [str(error)]
    return (
        [f"checkout is not clean: {', '.join(sorted(pending))}"]
        if pending
        else []
    )


def validate_destination(value: str) -> PurePosixPath:
    if not value or "\\" in value:
        raise ValueError("destination must be a non-empty POSIX repository path")
    destination = PurePosixPath(value)
    if destination.is_absolute() or any(part in {"", ".", ".."} for part in destination.parts):
        raise ValueError("destination must be a safe repository-relative path")
    if len(destination.parts) < 2 or destination.parts[0] != "skills":
        raise ValueError("destination must be a non-root path beneath skills/")
    if ".git" in destination.parts:
        raise ValueError("destination must not enter repository metadata")
    if any("$" in part for part in destination.parts):
        raise ValueError("destination must not contain shell placeholders")
    return destination


def source_files(source: Path) -> tuple[list[Path], list[str]]:
    errors: list[str] = []
    files: list[Path] = []
    try:
        source_stat = source.lstat()
    except OSError as error:
        return [], [
            "failed to inspect selected proposal source: "
            + (error.strerror or error.__class__.__name__)
        ]
    if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISDIR(source_stat.st_mode):
        return [], ["selected proposal source must be a real directory"]

    for root, directories, filenames in os.walk(source, followlinks=False):
        root_path = Path(root)
        for directory in list(directories):
            path = root_path / directory
            relative = path.relative_to(source)
            if ".git" in relative.parts:
                errors.append(
                    f"selected proposal path enters repository metadata: {relative}"
                )
                directories.remove(directory)
                continue
            if path.is_symlink():
                errors.append(
                    f"selected proposal contains symlink directory: {relative}"
                )
                directories.remove(directory)
        for filename in filenames:
            path = root_path / filename
            relative = path.relative_to(source)
            if ".git" in relative.parts:
                errors.append(f"selected proposal path enters repository metadata: {relative}")
            if any("$" in part for part in relative.parts):
                errors.append(f"selected proposal path contains a shell placeholder: {relative}")
            try:
                mode = path.lstat().st_mode
            except OSError as error:
                errors.append(
                    f"failed to inspect selected proposal entry {relative}: "
                    + (error.strerror or error.__class__.__name__)
                )
                continue
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                errors.append(f"selected proposal entry is not a regular file: {relative}")
                continue
            files.append(path)
    if not (source / "SKILL.md").is_file():
        errors.append("selected proposal source must contain SKILL.md")
    if not files:
        errors.append("selected proposal source contains no files")
    return sorted(files), errors


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_leakage_errors(path: Path, label: str) -> list[str]:
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return [f"{label} is not UTF-8 text"]
    except OSError as error:
        return [
            f"failed to read {label}: "
            + (error.strerror or error.__class__.__name__)
        ]
    errors: list[str] = []
    findings = sensitive_findings(content)
    if findings:
        errors.append(f"{label} contains sensitive content: {', '.join(sorted(set(findings)))}")
    if contains_raw_session_content(content):
        errors.append(f"{label} contains raw session metadata")
    return errors


def validate_checkout(
    repository: Path,
    source: Path,
    destination_value: str,
) -> tuple[list[str], set[str], set[str]]:
    errors: list[str] = []
    try:
        destination = validate_destination(destination_value)
    except ValueError as error:
        return [str(error)], set(), set()
    files, source_errors = source_files(source)
    errors.extend(source_errors)
    if source.name != destination.name:
        errors.append(
            "selected proposal directory name must match the destination skill name"
        )
    expected = {
        (destination / PurePosixPath(path.relative_to(source).as_posix())).as_posix()
        for path in files
    }
    try:
        pending = changed_paths(repository)
    except ValueError as error:
        errors.append(str(error))
        pending = set()
    if pending != expected:
        extra = sorted(pending - expected)
        missing = sorted(expected - pending)
        if extra:
            errors.append(f"checkout contains unexpected changed paths: {', '.join(extra)}")
        if missing:
            errors.append(f"checkout is missing selected changed paths: {', '.join(missing)}")

    for source_path in files:
        relative = PurePosixPath(source_path.relative_to(source).as_posix())
        expected_path = destination / relative
        checkout_path = repository.joinpath(*expected_path.parts)
        current = repository
        unsafe_destination = False
        for part in expected_path.parts:
            current /= part
            if current.is_symlink():
                errors.append(f"checkout destination contains a symlink: {expected_path}")
                unsafe_destination = True
                break
        if unsafe_destination:
            continue
        try:
            mode = checkout_path.lstat().st_mode
        except OSError as error:
            errors.append(
                f"failed to inspect checkout destination {expected_path}: "
                + (error.strerror or error.__class__.__name__)
            )
            continue
        if not stat.S_ISREG(mode):
            errors.append(f"checkout destination is not a regular file: {expected_path}")
            continue
        if sha256(source_path) != sha256(checkout_path):
            errors.append(f"checkout file does not match selected source: {expected_path}")
        errors.extend(file_leakage_errors(source_path, relative.as_posix()))
    return errors, expected, pending


def print_result(
    errors: list[str],
    *,
    json_output: bool,
    extra: dict[str, Any] | None = None,
) -> None:
    result = {"valid": not errors, "errors": errors, **(extra or {})}
    if json_output:
        print(json.dumps(result, indent=2, sort_keys=False))
    elif errors:
        for error in errors:
            print(f"error: {error}")
    else:
        print("valid")
    raise SystemExit(0 if not errors else 1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    body_command = commands.add_parser("body")
    body_command.add_argument("--selection", required=True, type=Path)
    body_command.add_argument("--body", required=True, type=Path)
    body_command.add_argument("--target-sha", required=True)
    body_command.add_argument("--json", action="store_true")

    clean_command = commands.add_parser("clean")
    clean_command.add_argument("--repository", required=True, type=Path)
    clean_command.add_argument("--json", action="store_true")

    checkout_command = commands.add_parser("checkout")
    checkout_command.add_argument("--repository", required=True, type=Path)
    checkout_command.add_argument("--source", required=True, type=Path)
    checkout_command.add_argument("--destination", required=True)
    checkout_command.add_argument("--json", action="store_true")

    args = parser.parse_args()
    try:
        if args.command == "body":
            selection_document = read_json(args.selection)
            if not isinstance(selection_document, dict):
                raise ValueError("selection must be a JSON object")
            body = args.body.read_text(encoding="utf-8")
            errors = validate_body(
                selection_document,
                body,
                target_sha=args.target_sha,
            )
            print_result(errors, json_output=args.json)
        repository = args.repository.resolve()
        if args.command == "clean":
            print_result(
                validate_clean_checkout(repository),
                json_output=args.json,
            )
        errors, expected, pending = validate_checkout(
            repository,
            args.source.absolute(),
            args.destination,
        )
        print_result(
            errors,
            json_output=args.json,
            extra={
                "expectedPaths": sorted(expected),
                "changedPaths": sorted(pending),
            },
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print_result([str(error)], json_output=getattr(args, "json", False))


if __name__ == "__main__":
    main()
