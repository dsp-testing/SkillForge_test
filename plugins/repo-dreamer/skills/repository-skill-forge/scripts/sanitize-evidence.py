#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. All rights reserved.

"""Sanitize workflow primitives and fail closed on secret-shaped evidence."""

from __future__ import annotations

import argparse
import importlib.util
import re
from pathlib import Path
from typing import Any

from forge_common import read_json, stable_hash, write_json

HOME_PATH_RE = re.compile(r"/(?:Users|home)/[^/\s]+")
WINDOWS_HOME_RE = re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+", re.IGNORECASE)
TOKEN_PATTERNS = (
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "private_key",
        re.compile(
            # Match the full PEM block for *any* private-key label, not just
            # a fixed list of algorithm prefixes, and not just the header:
            # capture the label after BEGIN (e.g. "PRIVATE KEY",
            # "RSA PRIVATE KEY", "ENCRYPTED PRIVATE KEY",
            # "PGP PRIVATE KEY BLOCK") and require the matching END label so
            # this never matches a public key or certificate block, which
            # end in "PUBLIC KEY" / "CERTIFICATE" rather than "PRIVATE KEY".
            # If no matching END label is found (a truncated command), fail
            # closed by consuming the rest of the string so no key-body
            # material can survive redaction.
            r"-----BEGIN (?P<pem_label>(?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?)-----"
            r"[\s\S]*?(?:-----END (?P=pem_label)-----|\Z)"
        ),
    ),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}", re.IGNORECASE)),
    (
        "assigned_secret",
        re.compile(
            r"""
            \b(?:password|passwd|token|secret|api[_-]?key|aws_secret_access_key)\s*[:=]\s*
            (?:
                (?P<quote>['"])(?!<|\$\{|\$[A-Z_]+)[^'"\r\n]{12,}(?P=quote)
                |
                (?!<|\$\{|\$[A-Z_]+)
                [A-Za-z0-9._~+/=-]{12,}(?![A-Za-z0-9._~+/=\-\[(])
            )
            """,
            re.IGNORECASE | re.VERBOSE,
        ),
    ),
)


# `assigned_secret` matches an assignment shape (`token = <long value>`), not a
# recognized credential format. It is the heuristic responsible for
# false positives such as computed references, environment lookups, and
# dotted identifiers. Every other kind matches a concrete, near-deterministic
# secret shape (a GitHub token prefix, an AWS access key ID, a PEM header, or
# a bearer token). Classify `assigned_secret` as advisory and everything else
# as blocking so operators can distinguish heuristic noise from confirmed
# shapes in diagnostics. This label is informational only: `redact()` always
# substitutes every matched pattern regardless of severity, so no finding
# kind changes what gets written into the sanitized document.
ADVISORY_FINDING_KINDS = {"assigned_secret"}


def finding_severity(kind: str) -> str:
    return "advisory" if kind in ADVISORY_FINDING_KINDS else "blocking"


def _load_derive_primitives() -> Any:
    """Load derive-primitives.py's signature builders so signatures are
    rebuilt from already-redacted content instead of sanitized independently
    per token (see `_signature_for` below)."""
    path = Path(__file__).resolve().with_name("derive-primitives.py")
    spec = importlib.util.spec_from_file_location("forge_derive_primitives_for_sanitizer", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load derive-primitives.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_DERIVER = _load_derive_primitives()


def _signature_for(
    kind: Any,
    signature: Any,
    redacted_command: str,
    redacted_script_content: str,
) -> dict[str, Any]:
    """Rebuild the signature from fully redacted content rather than
    sanitizing the raw signature's already-split fields independently.

    `command_signature` shlex-splits the command into individual tokens.
    A whitespace-separated assignment such as
    `aws_secret_access_key = <value>` becomes three separate tokens, none of
    which alone matches an `assigned_secret`-shaped pattern, so redacting
    each token in isolation (the previous approach) never touches the raw
    value. Redacting the full command first collapses the whole
    `key = value` span into `<redacted-secret>` before it is ever split, so
    no assignment value can survive in `signature.tokens` regardless of
    whitespace.
    """
    if kind == "command" and redacted_command:
        return _DERIVER.command_signature(redacted_command)
    if kind == "script" and redacted_script_content:
        return _DERIVER.script_signature(redacted_script_content)
    return sanitize_value(signature) if isinstance(signature, dict) else {}


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


def token_pattern_matches(value: Any) -> list[str]:
    """Recursively scan a sanitized value for any surviving TOKEN_PATTERNS.

    This is a defensive backstop, not the primary control: it catches a
    class of bug where some field was sanitized independently of the rest of
    the document (as `signature.tokens` once was) rather than derived from
    already-redacted content. It must never be relied on alone to contain a
    multi-line body such as a PEM private key; `redact()` itself must
    consume the entire sensitive span.
    """
    matches: list[str] = []
    if isinstance(value, str):
        for kind, pattern in TOKEN_PATTERNS:
            if pattern.search(value):
                matches.append(kind)
    elif isinstance(value, list):
        for item in value:
            matches.extend(token_pattern_matches(item))
    elif isinstance(value, dict):
        for item in value.values():
            matches.extend(token_pattern_matches(item))
    return matches


def findings(value: str, evidence_key: str, field: str) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for kind, pattern in TOKEN_PATTERNS:
        if pattern.search(value):
            result.append(
                {
                    "evidenceKey": evidence_key,
                    "field": field,
                    "kind": kind,
                    "severity": finding_severity(kind),
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

        redacted_command = redact(command)
        redacted_script_content = redact(script_content)
        signature = primitive.get("signature")
        sanitized_signature = _signature_for(
            primitive.get("kind"),
            signature,
            redacted_command,
            redacted_script_content,
        )
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
                "commandTemplate": redacted_command[:500],
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

    findings_by_kind: dict[str, int] = {}
    for item in leakage_findings:
        findings_by_kind[item["kind"]] = findings_by_kind.get(item["kind"], 0) + 1
    report = {
        "schemaVersion": 1,
        "findingCount": len(leakage_findings),
        "blockingFindingCount": sum(item["severity"] == "blocking" for item in leakage_findings),
        "advisoryFindingCount": sum(item["severity"] == "advisory" for item in leakage_findings),
        "findingsByKind": findings_by_kind,
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
