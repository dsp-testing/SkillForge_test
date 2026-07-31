#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. All rights reserved.

"""Sanitize workflow primitives and fail closed on secret-shaped evidence."""

from __future__ import annotations

import argparse
import re
from typing import Any

from forge_common import read_json, stable_hash, write_json

HOME_PATH_RE = re.compile(r"/(?:Users|home)/[^/\s]+")
WINDOWS_HOME_RE = re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+", re.IGNORECASE)
TOKEN_PATTERNS = (
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}", re.IGNORECASE)),
    (
        "assigned_secret",
        re.compile(
            r"\b(?:password|passwd|token|secret|api[_-]?key)\s*[:=]\s*['\"]?(?!<|\$\{|\$[A-Z_]+)[^\s'\"]{12,}",
            re.IGNORECASE,
        ),
    ),
)


def redact(value: str) -> str:
    value = HOME_PATH_RE.sub("~", value)
    value = WINDOWS_HOME_RE.sub("~", value)
    for _kind, pattern in TOKEN_PATTERNS:
        value = pattern.sub("<redacted-secret>", value)
    return value


def sanitize_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: sanitize_value(item) for key, item in value.items()}
    return value


def findings(value: str, evidence_key: str, field: str) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for kind, pattern in TOKEN_PATTERNS:
        if pattern.search(value):
            result.append(
                {
                    "evidenceKey": evidence_key,
                    "field": field,
                    "kind": kind,
                    "severity": "blocking",
                }
            )
    return result


def sanitize(
    document: dict[str, Any],
    *,
    main_branches: set[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    primitives = document.get("primitives")
    if not isinstance(primitives, list):
        raise ValueError("derived document must contain primitives")
    sanitized: list[dict[str, Any]] = []
    leakage_findings: list[dict[str, str]] = []

    for primitive in primitives:
        if not isinstance(primitive, dict):
            continue
        evidence_key = str(primitive.get("evidenceKey") or "")
        raw = primitive.get("rawEvidence")
        raw = raw if isinstance(raw, dict) else {}
        command = str(raw.get("command") or "")
        script_content = str(raw.get("scriptContent") or "")
        branch = primitive.get("branch")
        branch_text = str(branch) if branch else None
        leakage_findings.extend(findings(command, evidence_key, "command"))
        leakage_findings.extend(findings(script_content, evidence_key, "scriptContent"))

        signature = primitive.get("signature")
        sanitized_signature = sanitize_value(signature) if isinstance(signature, dict) else {}
        path_families = primitive.get("pathFamilies")
        sanitized_path_families = (
            [redact(str(path)) for path in path_families]
            if isinstance(path_families, list)
            else []
        )
        sanitized.append(
            {
                key: value
                for key, value in primitive.items()
                if key not in {"rawEvidence", "signature", "branch", "pathFamilies"}
            }
            | {
                "signature": sanitized_signature,
                "commandTemplate": redact(command)[:500],
                "pathFamilies": sanitized_path_families,
                "branchId": stable_hash(branch_text, 16) if branch_text else None,
                "branchCategory": (
                    "default"
                    if branch_text in main_branches
                    else "other"
                    if branch_text
                    else "unknown"
                ),
                "sourceContentRetained": False,
            }
        )

    report = {
        "schemaVersion": 1,
        "findingCount": len(leakage_findings),
        "blockingFindingCount": sum(item["severity"] == "blocking" for item in leakage_findings),
        "findings": leakage_findings,
    }
    return (
        {
            "schemaVersion": 1,
            "scope": document.get("scope"),
            "coverage": document.get("coverage"),
            "userDiversity": document.get("userDiversity"),
            "sanitization": {
                "rawSourceContentRetained": False,
                "homePathsRedacted": True,
            },
            "primitives": sanitized,
        },
        report,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="input", required=True)
    parser.add_argument("--out", dest="output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--main-branch", action="append", default=["main", "master"])
    args = parser.parse_args()
    document = read_json(args.input)
    if not isinstance(document, dict):
        raise SystemExit("input must be a derived JSON object")
    try:
        sanitized, report = sanitize(document, main_branches=set(args.main_branch))
    except ValueError as error:
        raise SystemExit(str(error)) from error
    write_json(args.output, sanitized)
    write_json(args.report, report)
    if report["blockingFindingCount"]:
        raise SystemExit("blocking leakage findings detected")


if __name__ == "__main__":
    main()
